from fastapi.testclient import TestClient

PUBLIC = {"/health", "/ready"}


def test_every_operation_is_documented(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    missing = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if not op.get("summary") or not (op.get("description") or op.get("tags")):
                missing.append(f"{method.upper()} {path}")
            needs_auth = path not in PUBLIC and not path.startswith("/v1/admin")
            if needs_auth and "401" not in op["responses"]:
                missing.append(f"{method.upper()} {path} (no 401 documented)")
    assert missing == []


def test_error_schema_and_security_are_published(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert "ErrorResponse" in spec["components"]["schemas"]
    assert "HTTPBearer" in spec["components"]["securitySchemes"]


def test_request_examples_exist_for_main_bodies(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("UploadCreate", "WebhookCreate", "PresetCreate"):
        props = schemas[name]["properties"]
        assert any("examples" in p or "default" in p for p in props.values()), name
    assert client.get("/docs").status_code == 200
