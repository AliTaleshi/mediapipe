from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import AppError, ConflictError, register_exception_handlers


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    class Body(BaseModel):
        n: int

    @app.post("/validate")
    def validate(body: Body) -> None: ...

    @app.get("/app-error")
    def app_error() -> None:
        raise ConflictError("Already there.", code="already_exists", details={"id": "1"})

    @app.get("/plain")
    def plain() -> None:
        raise AppError("Nope.", headers={"X-Test": "1"})

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    return app


def test_app_error_format() -> None:
    resp = TestClient(_app()).get("/app-error")
    assert resp.status_code == 409
    assert resp.json() == {
        "error": {"code": "already_exists", "message": "Already there.", "details": {"id": "1"}}
    }


def test_app_error_defaults_and_headers() -> None:
    resp = TestClient(_app()).get("/plain")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    assert resp.headers["X-Test"] == "1"


def test_validation_error_format() -> None:
    resp = TestClient(_app()).post("/validate", json={"n": "x"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"]["errors"][0]["loc"] == ["body", "n"]


def test_unhandled_error_hides_internals() -> None:
    resp = TestClient(_app(), raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "secret" not in resp.text
