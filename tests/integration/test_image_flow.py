import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import File, FileStatus, Job, JobStatus, Variant
from app.db.session import sync_session
from app.processors.types import ProcessingResult
from app.services.job_runner import run_job
from app.services.storage import TransientStorageError, get_storage
from tests.conftest import BUCKET, Tenant
from tests.helpers import image_bytes, upload_file


def job_of(file_id: str) -> Job:
    with sync_session() as s:
        return s.scalars(select(Job).where(Job.file_id == uuid.UUID(file_id))).one()


def variant_count(file_id: str) -> int:
    with sync_session() as s:
        return (
            s.scalar(
                select(func.count())
                .select_from(Variant)
                .where(Variant.file_id == uuid.UUID(file_id))
            )
            or 0
        )


def test_image_upload_is_processed_end_to_end(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    file_id = upload_file(tenant, "photo.jpg", "image/jpeg", image_bytes("JPEG", (1200, 900)))

    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "processed"
    assert body["metadata"]["width"] == 1200
    names = sorted(v["name"] for v in body["variants"])
    assert names == [
        "large.jpg",
        "large.webp",
        "medium.jpg",
        "medium.webp",
        "thumb.jpg",
        "thumb.webp",
    ]
    thumb = next(v for v in body["variants"] if v["name"] == "thumb.webp")
    assert (thumb["width"], thumb["height"], thumb["content_type"]) == (200, 200, "image/webp")
    assert "X-Amz-Expires=3600" in thumb["url"]

    for v in body["variants"]:
        key = f"projects/{tenant.project_id}/files/{file_id}/variants/{v['name']}"
        assert s3.head_object(Bucket=BUCKET, Key=key)["ContentLength"] == v["size"]

    job = tenant.get(f"/v1/jobs/{body['jobs'][0]['id']}").json()
    assert job["status"] == "succeeded" and job["progress"] == 100 and job["attempts"] == 1
    assert job["finished_at"] is not None and job["error"] is None


def test_corrupt_image_fails_permanently_without_retry(tenant: Tenant) -> None:
    # Valid JPEG signature so it passes type sniffing, but the body is garbage.
    file_id = upload_file(tenant, "bad.jpg", "image/jpeg", b"\xff\xd8\xff" + b"garbage" * 50)
    job = job_of(file_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1  # permanent errors are not retried
    assert job.error and "Corrupt or unsupported image" in job.error
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "failed" and body["variants"] == []
    assert body["error"] == job.error


class FlakyStorage:
    """Wraps real storage; ``download`` fails transiently the first ``failures`` times."""

    def __init__(self, failures: int) -> None:
        self.real = get_storage()
        self.failures = failures
        self.calls = 0

    def download(self, *a: object, **k: object) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientStorageError("connection reset")
        self.real.download(*a, **k)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)


def test_transient_errors_are_retried_then_succeed(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = FlakyStorage(failures=2)
    monkeypatch.setattr("app.workers.tasks.get_storage", lambda: flaky)
    file_id = upload_file(tenant, "p.png", "image/png", image_bytes("PNG"))
    job = job_of(file_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempts == 3
    assert flaky.calls == 3


def test_gives_up_after_max_attempts(tenant: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = FlakyStorage(failures=99)
    monkeypatch.setattr("app.workers.tasks.get_storage", lambda: flaky)
    file_id = upload_file(tenant, "p.png", "image/png", image_bytes("PNG"))
    job = job_of(file_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 3 and flaky.calls == 3
    assert job.error and "Gave up after 3 attempts" in job.error
    with sync_session() as s:
        assert s.get(File, uuid.UUID(file_id)).status == FileStatus.FAILED  # type: ignore[union-attr]


def test_running_a_job_twice_creates_no_duplicates(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    job = job_of(file_id)
    assert variant_count(file_id) == 6
    with sync_session() as s:
        ids_before = {v.name: v.id for v in s.scalars(select(Variant))}

    # duplicate delivery of a finished job is a no-op
    from app.core.config import get_settings

    assert run_job(job.id, get_storage(), get_settings()) is None
    assert job_of(file_id).attempts == 1

    # forced re-run (e.g. worker crashed after uploading but before acking)
    with sync_session() as s:
        s.get(Job, job.id).status = JobStatus.PROCESSING  # type: ignore[union-attr]
    run_job(job.id, get_storage(), get_settings())
    assert variant_count(file_id) == 6
    with sync_session() as s:
        assert {v.name: v.id for v in s.scalars(select(Variant))} == ids_before
    keys = s3.list_objects_v2(
        Bucket=BUCKET, Prefix=f"projects/{tenant.project_id}/files/{file_id}/variants/"
    )
    assert keys["KeyCount"] == 6


def test_rerun_with_changed_config_removes_stale_variants(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    job = job_of(file_id)
    with sync_session() as s:
        j = s.get(Job, job.id)
        assert j is not None
        j.status = JobStatus.QUEUED
        j.config = {"variants": [{"name": "tiny", "width": 32}], "formats": ["webp"]}
    from app.core.config import get_settings

    run_job(job.id, get_storage(), get_settings())
    with sync_session() as s:
        assert [v.name for v in s.scalars(select(Variant))] == ["tiny.webp"]
    assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 2  # original + tiny.webp


def test_manual_retry_of_failed_job(tenant: Tenant, s3, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    file_id = upload_file(tenant, "bad.jpg", "image/jpeg", b"\xff\xd8\xff" + b"x" * 100)
    job = job_of(file_id)
    assert job.status == JobStatus.FAILED

    # Fix the underlying problem (replace the stored object), then retry.
    key = f"projects/{tenant.project_id}/files/{file_id}/original.jpg"
    good = image_bytes("JPEG")
    s3.put_object(Bucket=BUCKET, Key=key, Body=good)
    resp = tenant.post(f"/v1/jobs/{job.id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded" and resp.json()["attempts"] == 1
    assert tenant.get(f"/v1/files/{file_id}").json()["status"] == "processed"

    again = tenant.post(f"/v1/jobs/{job.id}/retry")
    assert again.status_code == 409 and again.json()["error"]["code"] == "job_not_failed"


def test_tenant_isolation_for_files_and_jobs(tenant: Tenant, other_tenant: Tenant) -> None:
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    job_id = job_of(file_id).id
    assert other_tenant.get(f"/v1/files/{file_id}").status_code == 404
    assert other_tenant.get(f"/v1/jobs/{job_id}").status_code == 404
    assert other_tenant.post(f"/v1/jobs/{job_id}/retry").status_code == 404
    assert tenant.get(f"/v1/jobs/{job_id}").status_code == 200


def test_missing_job_is_skipped_quietly() -> None:
    from app.core.config import get_settings

    assert run_job(uuid.uuid4(), get_storage(), get_settings()) is None


def test_file_deleted_mid_flight_is_handled(
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    s3: Any,
) -> None:
    from app.core.config import get_settings
    from app.services import job_runner

    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    job = job_of(file_id)
    with sync_session() as s:
        s.get(Job, job.id).status = JobStatus.QUEUED  # type: ignore[union-attr]
    real = job_runner.PROCESSORS["image"]

    def delete_then_process(*args: Any, **kwargs: Any) -> ProcessingResult:
        with sync_session() as s:
            s.delete(s.get(File, uuid.UUID(file_id)))
        return real(*args, **kwargs)

    monkeypatch.setitem(job_runner.PROCESSORS, "image", delete_then_process)
    assert run_job(job.id, get_storage(), get_settings()) is None
    with sync_session() as s:
        assert s.get(Job, job.id) is None  # cascaded with the file
        assert s.scalar(select(func.count()).select_from(Variant)) == 0


def test_missing_original_fails_permanently_with_clear_error(tenant: Tenant, s3: Any) -> None:
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"), complete=False)
    key = f"projects/{tenant.project_id}/files/{file_id}/original.jpg"
    assert tenant.post(f"/v1/uploads/{file_id}/complete").status_code == 200
    job = job_of(file_id)
    with sync_session() as s:
        j = s.get(Job, job.id)
        assert j is not None
        j.status = JobStatus.QUEUED
    s3.delete_object(Bucket=BUCKET, Key=key)
    from app.core.config import get_settings

    run_job(job.id, get_storage(), get_settings())
    after = job_of(file_id)
    assert after.status == JobStatus.FAILED and after.attempts == 2  # no retry loop
    assert after.error and after.error.startswith("Storage error: Object not found")
