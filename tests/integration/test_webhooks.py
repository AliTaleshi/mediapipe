import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.webhooks import verify_signature
from tests.conftest import Tenant
from tests.helpers import image_bytes, upload_file


class Receiver:
    """Fake webhook receiver. ``script`` decides each response (status code or exception)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[int | Exception] = []
        self.default = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.script.pop(0) if self.script else self.default
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    rx = Receiver()
    monkeypatch.setattr(
        "app.services.webhooks.build_http_client",
        lambda settings: httpx.Client(transport=httpx.MockTransport(rx.handle)),
    )
    return rx


def register(
    tenant: Tenant, url: str = "https://hooks.example.com/mp", events: list[str] | None = None
) -> dict:  # type: ignore[type-arg]
    body: dict[str, object] = {"url": url}
    if events is not None:
        body["events"] = events
    resp = tenant.post("/v1/webhooks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def deliveries(tenant: Tenant, **params: object) -> list[dict]:  # type: ignore[type-arg]
    resp = tenant.get("/v1/webhooks/deliveries", params=params)
    assert resp.status_code == 200
    return resp.json()["items"]  # type: ignore[no-any-return]


def good_upload(tenant: Tenant) -> str:
    return upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))


def bad_upload(tenant: Tenant) -> str:
    return upload_file(tenant, "bad.jpg", "image/jpeg", b"\xff\xd8\xff" + b"junk" * 40)


def test_signed_webhook_delivered_when_file_is_processed(
    tenant: Tenant, receiver: Receiver
) -> None:
    hook = register(tenant)
    assert hook["secret"].startswith("whsec_") and hook["events"] == [
        "file.failed",
        "file.processed",
    ]
    file_id = good_upload(tenant)

    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    assert req.url == "https://hooks.example.com/mp"
    assert verify_signature(hook["secret"], req.headers["X-MediaPipe-Signature"], req.content)
    assert not verify_signature("whsec_wrong", req.headers["X-MediaPipe-Signature"], req.content)
    assert req.headers["X-MediaPipe-Event"] == "file.processed"
    assert req.headers["Content-Type"] == "application/json"

    payload = json.loads(req.content)
    assert payload["event"] == "file.processed" and payload["id"].startswith("evt_")
    file = payload["data"]["file"]
    assert file["id"] == file_id and file["status"] == "processed"
    assert sorted(v["name"] for v in file["variants"])[0] == "large.jpg"
    assert "url" not in file["variants"][0]  # presigned URLs expire; clients fetch fresh ones

    log = deliveries(tenant)
    assert len(log) == 1
    assert log[0]["success"] and log[0]["status_code"] == 200 and log[0]["attempt"] == 1
    assert log[0]["event"] == "file.processed" and log[0]["delivered_at"] is not None
    assert log[0]["id"] == req.headers["X-MediaPipe-Delivery"]


def test_failed_file_sends_file_failed(tenant: Tenant, receiver: Receiver) -> None:
    register(tenant)
    bad_upload(tenant)
    assert [json.loads(r.content)["event"] for r in receiver.requests] == ["file.failed"]
    assert json.loads(receiver.requests[0].content)["data"]["file"]["error"]


def test_event_subscriptions_are_respected(tenant: Tenant, receiver: Receiver) -> None:
    register(tenant, "https://a.example.com/", ["file.failed"])
    register(tenant, "https://b.example.com/", ["file.processed"])
    good_upload(tenant)
    bad_upload(tenant)
    seen = sorted((str(r.url), json.loads(r.content)["event"]) for r in receiver.requests)
    assert seen == [
        ("https://a.example.com/", "file.failed"),
        ("https://b.example.com/", "file.processed"),
    ]


def test_retries_with_backoff_until_success(tenant: Tenant, receiver: Receiver) -> None:
    register(tenant)
    receiver.script = [500, httpx.ConnectError("boom"), 200]
    good_upload(tenant)
    log = sorted(deliveries(tenant), key=lambda d: d["attempt"])
    assert [(d["attempt"], d["success"], d["status_code"]) for d in log] == [
        (1, False, 500),
        (2, False, None),
        (3, True, 200),
    ]
    assert "ConnectError" in log[1]["error"] and "HTTP 500" in log[0]["error"]
    assert len({d["payload"]["id"] for d in log}) == 1  # same event id on every attempt


def test_gives_up_after_five_attempts(tenant: Tenant, receiver: Receiver) -> None:
    register(tenant)
    receiver.default = 503
    good_upload(tenant)
    log = deliveries(tenant)
    assert len(log) == 5 and len(receiver.requests) == 5
    assert not any(d["success"] for d in log)
    assert sorted(d["attempt"] for d in log) == [1, 2, 3, 4, 5]


def test_delivery_log_filters_and_pagination(tenant: Tenant, receiver: Receiver) -> None:
    hook = register(tenant)
    receiver.script = [500, 200]
    good_upload(tenant)
    assert len(deliveries(tenant, success="false")) == 1
    assert len(deliveries(tenant, success="true")) == 1
    assert len(deliveries(tenant, endpoint_id=hook["id"])) == 2
    assert deliveries(tenant, endpoint_id=str(uuid.uuid4())) == []
    page = tenant.get("/v1/webhooks/deliveries", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1


def test_resend_delivers_same_payload_with_fresh_signature(
    tenant: Tenant, receiver: Receiver
) -> None:
    hook = register(tenant)
    good_upload(tenant)
    original = deliveries(tenant)[0]
    first = receiver.requests[0]

    resp = tenant.post(f"/v1/webhooks/deliveries/{original['id']}/resend")
    assert resp.status_code == 202 and resp.json()["event"] == "file.processed"
    assert len(receiver.requests) == 2
    again = receiver.requests[1]
    assert json.loads(again.content) == json.loads(first.content)
    assert verify_signature(hook["secret"], again.headers["X-MediaPipe-Signature"], again.content)
    assert again.headers["X-MediaPipe-Delivery"] != first.headers["X-MediaPipe-Delivery"]
    assert len(deliveries(tenant)) == 2


def test_resend_unknown_or_other_tenants_delivery(
    tenant: Tenant, other_tenant: Tenant, receiver: Receiver
) -> None:
    register(tenant)
    good_upload(tenant)
    delivery_id = deliveries(tenant)[0]["id"]
    assert other_tenant.post(f"/v1/webhooks/deliveries/{delivery_id}/resend").status_code == 404
    assert tenant.post(f"/v1/webhooks/deliveries/{uuid.uuid4()}/resend").status_code == 404
    assert len(receiver.requests) == 1


def test_tenant_isolation(tenant: Tenant, other_tenant: Tenant, receiver: Receiver) -> None:
    hook = register(tenant)
    good_upload(tenant)
    # Another project's uploads never reach this project's endpoint ...
    good_upload(other_tenant)
    assert len(receiver.requests) == 1
    # ... and it can neither see nor delete it.
    assert other_tenant.get("/v1/webhooks").json() == []
    assert deliveries(other_tenant) == []
    assert other_tenant.delete(f"/v1/webhooks/{hook['id']}").status_code == 404
    assert len(tenant.get("/v1/webhooks").json()) == 1


def test_delete_endpoint_stops_deliveries(tenant: Tenant, receiver: Receiver) -> None:
    hook = register(tenant)
    assert "secret" not in tenant.get("/v1/webhooks").json()[0]
    assert tenant.delete(f"/v1/webhooks/{hook['id']}").status_code == 204
    good_upload(tenant)
    assert receiver.requests == []
    assert tenant.delete(f"/v1/webhooks/{hook['id']}").status_code == 404


def test_validation(tenant: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        tenant.post("/v1/webhooks", json={"url": "https://x.io", "events": ["nope"]}).status_code
        == 422
    )
    assert (
        tenant.post("/v1/webhooks", json={"url": "https://x.io", "events": []}).status_code == 422
    )

    from app.core.config import get_settings

    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "false")
    get_settings.cache_clear()
    resp = tenant.post("/v1/webhooks", json={"url": "http://127.0.0.1/hook"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_webhook_url"


def test_endpoint_limit(tenant: Tenant) -> None:
    for i in range(10):
        register(tenant, f"https://h{i}.example.com/")
    resp = tenant.post("/v1/webhooks", json={"url": "https://one-too-many.example.com/"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "endpoint_limit"


def test_private_address_blocked_at_delivery_time(
    tenant: Tenant, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    register(tenant, "https://rebinding.example.com/hook")  # looked public when registered
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "false")
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.webhooks.resolves_to_private", lambda host: True)
    good_upload(tenant)
    assert receiver.requests == []
    log = deliveries(tenant)
    assert len(log) == 1 and "private address" in log[0]["error"]  # and not retried


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/webhooks").status_code == 401
    assert client.get("/v1/webhooks/deliveries").status_code == 401
