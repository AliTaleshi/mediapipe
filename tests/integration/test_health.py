from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_all_ok(client: TestClient) -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"database": "ok", "redis": "ok", "storage": "ok"}


def test_ready_reports_storage_down(client: TestClient, s3) -> None:  # type: ignore[no-untyped-def]
    s3.delete_bucket(Bucket="test-bucket")
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"] == "unavailable"
    assert resp.json()["checks"]["database"] == "ok"


def test_unknown_route_uses_error_format(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Not Found", "details": {}}}
