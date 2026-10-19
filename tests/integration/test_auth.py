import fakeredis
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import ApiKey
from app.db.session import sync_session
from tests.conftest import Tenant


def test_missing_and_malformed_keys_are_rejected(client: TestClient) -> None:
    for headers in [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer mp_abcd_x"}]:
        resp = client.get("/v1/api-keys", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"


def test_valid_key_works_and_is_stored_hashed(tenant: Tenant) -> None:
    assert tenant.get("/v1/api-keys").status_code == 200
    with sync_session() as s:
        key = s.scalars(select(ApiKey)).one()
        assert tenant.api_key not in (key.key_hash, key.prefix)
        assert len(key.key_hash) == 64
        assert key.last_used_at is not None


def test_admin_endpoints_need_token(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert client.post("/v1/admin/projects", json={"name": "x"}).status_code == 401
    bad = client.post("/v1/admin/projects", json={"name": "x"}, headers={"X-Admin-Token": "no"})
    assert bad.status_code == 401
    ok = client.post("/v1/admin/projects", json={"name": "x"}, headers=admin_headers)
    assert ok.status_code == 201 and ok.json()["api_key"].startswith("mp_")


def test_admin_disabled_without_token(
    client: TestClient, monkeypatch: object, admin_headers: dict[str, str]
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("ADMIN_TOKEN", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    resp = client.post("/v1/admin/projects", json={"name": "x"}, headers=admin_headers)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "admin_disabled"


def test_create_list_and_revoke_keys(tenant: Tenant, client: TestClient) -> None:
    created = tenant.post("/v1/api-keys", json={"name": "second"})
    assert created.status_code == 201
    new_key = created.json()
    assert new_key["api_key"].startswith("mp_")

    listing = tenant.get("/v1/api-keys").json()
    assert len(listing) == 2 and all("api_key" not in k for k in listing)

    assert tenant.delete(f"/v1/api-keys/{new_key['id']}").status_code == 204
    revoked = client.get("/v1/api-keys", headers={"Authorization": f"Bearer {new_key['api_key']}"})
    assert revoked.status_code == 401


def test_cannot_revoke_last_key(tenant: Tenant) -> None:
    key_id = tenant.get("/v1/api-keys").json()[0]["id"]
    resp = tenant.delete(f"/v1/api-keys/{key_id}")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "last_api_key"


def test_tenant_isolation_for_api_keys(tenant: Tenant, other_tenant: Tenant) -> None:
    a_key_id = tenant.get("/v1/api-keys").json()[0]["id"]
    b_listing = other_tenant.get("/v1/api-keys").json()
    assert a_key_id not in [k["id"] for k in b_listing]
    # B cannot revoke A's key, and gets the same answer as for a key that doesn't exist.
    other_tenant.post("/v1/api-keys", json={})
    resp = other_tenant.delete(f"/v1/api-keys/{a_key_id}")
    assert resp.status_code == 404
    assert tenant.get("/v1/api-keys").status_code == 200


def test_rate_limit_returns_429_with_headers(
    tenant: Tenant, monkeypatch: object, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    from app.core.config import get_settings

    get_settings.cache_clear()
    first = tenant.get("/v1/api-keys")
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert tenant.get("/v1/api-keys").status_code == 200
    limited = tenant.get("/v1/api-keys")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in limited.headers
