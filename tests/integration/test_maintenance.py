import uuid
from datetime import timedelta
from typing import Any

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import File, FileStatus
from app.db.session import sync_session
from app.services.maintenance import cleanup_abandoned_uploads
from app.services.storage import StorageError, get_storage
from app.workers.celery_app import celery_app
from tests.conftest import BUCKET, Tenant
from tests.helpers import put_to_url


def make_pending(tenant: Tenant, age_hours: float, s3: Any, *, uploaded: bool = True) -> uuid.UUID:
    resp = tenant.post(
        "/v1/uploads", json={"filename": "a.jpg", "content_type": "image/jpeg", "size": 10}
    )
    info = resp.json()
    if uploaded:
        put_to_url(info["upload"]["url"], b"0123456789", info["upload"]["headers"])
    file_id = uuid.UUID(info["file_id"])
    with sync_session() as s:
        file = s.get(File, file_id)
        assert file is not None
        file.created_at = utcnow() - timedelta(hours=age_hours)
    return file_id


def exists(file_id: uuid.UUID) -> bool:
    with sync_session() as s:
        return s.get(File, file_id) is not None


def test_removes_only_stale_pending_uploads(tenant: Tenant, s3: Any) -> None:
    old = make_pending(tenant, 25, s3)
    old_never_uploaded = make_pending(tenant, 48, s3, uploaded=False)
    fresh = make_pending(tenant, 23, s3)

    with sync_session() as s:
        done = File(
            project_id=uuid.UUID(tenant.project_id),
            original_name="done.jpg",
            content_type="image/jpeg",
            media_type="image",
            size=1,
            storage_key="projects/x/done.jpg",
            status=FileStatus.PROCESSED,
            created_at=utcnow() - timedelta(days=30),
        )
        s.add(done)
        s.flush()
        done_id = done.id

    removed = cleanup_abandoned_uploads(get_storage(), get_settings())

    assert removed == 2
    assert not exists(old) and not exists(old_never_uploaded)
    assert exists(fresh) and exists(done_id)
    remaining = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    assert len(remaining) == 1 and str(fresh) in remaining[0]


def test_aborts_stale_multipart_uploads(tenant: Tenant, s3: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("MULTIPART_THRESHOLD_BYTES", "5")
    get_settings.cache_clear()
    file_id = make_pending(tenant, 30, s3, uploaded=False)
    assert len(s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])) == 1
    assert cleanup_abandoned_uploads(get_storage(), get_settings()) == 1
    assert s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
    assert not exists(file_id)


def test_threshold_is_configurable(tenant: Tenant, s3: Any, monkeypatch: Any) -> None:
    file_id = make_pending(tenant, 2, s3)
    assert cleanup_abandoned_uploads(get_storage(), get_settings()) == 0
    monkeypatch.setenv("UPLOAD_CLEANUP_AFTER_HOURS", "1")
    get_settings.cache_clear()
    assert cleanup_abandoned_uploads(get_storage(), get_settings()) == 1
    assert not exists(file_id)


def test_storage_failure_keeps_row_for_next_run(tenant: Tenant, s3: Any, monkeypatch: Any) -> None:
    file_id = make_pending(tenant, 25, s3)
    storage = get_storage()

    def boom(keys: list[str]) -> None:
        raise StorageError("storage down")

    monkeypatch.setattr(storage, "delete", boom)
    assert cleanup_abandoned_uploads(storage, get_settings()) == 0
    assert exists(file_id)
    monkeypatch.undo()
    assert cleanup_abandoned_uploads(get_storage(), get_settings()) == 1


def test_beat_schedule_points_at_a_registered_task() -> None:
    celery_app.loader.import_default_modules()  # what a worker does on startup
    entry = celery_app.conf.beat_schedule["cleanup-abandoned-uploads"]
    assert entry["task"] in celery_app.tasks
    task = celery_app.tasks[entry["task"]]
    assert task() == 0  # runs eagerly against the empty test DB
