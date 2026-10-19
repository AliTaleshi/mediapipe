"""Shared fixtures. Everything runs without Docker: SQLite, moto (S3), fakeredis, eager Celery."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

BUCKET = "test-bucket"
ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Per-test settings, fresh SQLite database and cleared caches."""
    env = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/test.db",
        "REDIS_URL": "redis://localhost:6399/0",
        "CELERY_TASK_ALWAYS_EAGER": "true",
        "S3_BUCKET": BUCKET,
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        "ADMIN_TOKEN": ADMIN_TOKEN,
        "WEBHOOK_ALLOW_PRIVATE_URLS": "true",
        "SSE_POLL_INTERVAL_SECONDS": "0.05",
        "JOB_RETRY_BASE_SECONDS": "0",
        "WEBHOOK_RETRY_BASE_SECONDS": "0",
        "RATE_LIMIT_PER_MINUTE": "1000",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_caches()

    from app.db.base import Base
    from app.db.session import get_sync_engine

    Base.metadata.create_all(get_sync_engine())
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _celery_eager() -> None:
    from app.workers.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.db.session import reset_db_caches
    from app.services.redis import get_redis
    from app.services.storage import get_storage

    for fn in (get_settings, get_storage, get_redis):
        fn.cache_clear()
    reset_db_caches()


@pytest.fixture
def s3() -> Iterator[object]:
    """Mocked S3 with the test bucket created; yields a boto3 client for direct access."""
    import boto3

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        from app.services.storage import get_storage

        get_storage.cache_clear()
        yield client


@pytest.fixture
def fake_redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def client(s3: object, fake_redis: fakeredis.FakeAsyncRedis) -> Iterator[TestClient]:
    from app.main import create_app
    from app.services.redis import get_redis

    app = create_app()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


class Tenant:
    """A project with its API key, for driving the API in tests."""

    def __init__(self, client: TestClient, project_id: str, api_key: str) -> None:
        self.client = client
        self.project_id = project_id
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp: httpx.Response = self.client.request(method, url, headers=self.headers, **kwargs)
        return resp

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)

    def delete(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("DELETE", url, **kw)


def _new_tenant(client: TestClient, name: str) -> Tenant:
    resp = client.post(
        "/v1/admin/projects", json={"name": name}, headers={"X-Admin-Token": ADMIN_TOKEN}
    )
    assert resp.status_code == 201, resp.text
    return Tenant(client, resp.json()["id"], resp.json()["api_key"])


@pytest.fixture
def tenant(client: TestClient) -> Tenant:
    return _new_tenant(client, "Project A")


@pytest.fixture
def other_tenant(client: TestClient) -> Tenant:
    return _new_tenant(client, "Project B")
