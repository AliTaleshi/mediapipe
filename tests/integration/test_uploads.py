import uuid
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import File, FileStatus, Job
from app.db.session import sync_session
from tests.conftest import BUCKET, Tenant
from tests.helpers import image_bytes, pdf_bytes, put_to_url, upload_file


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[uuid.UUID, str]]]:
    """These tests stop at "job queued"; processing is covered elsewhere."""
    calls: list[tuple[uuid.UUID, str]] = []
    monkeypatch.setattr("app.api.uploads.enqueue_job", lambda j, m: calls.append((j, m)))
    yield calls


def _create(tenant: Tenant, **overrides: object) -> dict:  # type: ignore[type-arg]
    body = {"filename": "a.jpg", "content_type": "image/jpeg", "size": 100, **overrides}
    resp = tenant.post("/v1/uploads", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def test_create_single_upload_gives_working_presigned_url(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    data = image_bytes()
    info = _create(tenant, size=len(data))
    assert info["mode"] == "single" and info["multipart"] is None
    assert info["upload"]["headers"] == {"Content-Type": "image/jpeg"}
    put_to_url(info["upload"]["url"], data, info["upload"]["headers"])

    with sync_session() as s:
        file = s.get(File, uuid.UUID(info["file_id"]))
        assert file is not None
        assert file.status == FileStatus.PENDING_UPLOAD
        assert file.project_id == uuid.UUID(tenant.project_id)
        assert s3.head_object(Bucket=BUCKET, Key=file.storage_key)["ContentLength"] == len(data)


def test_presigned_put_signs_the_content_type(tenant: Tenant) -> None:
    # moto does not enforce signatures, so check what we asked S3 to sign instead.
    info = _create(tenant)
    query = parse_qs(urlparse(info["upload"]["url"]).query)
    assert "content-type" in query["X-Amz-SignedHeaders"][0].split(";")
    assert query["X-Amz-Expires"] == ["900"]


def test_rejects_unsupported_type(tenant: Tenant) -> None:
    resp = tenant.post(
        "/v1/uploads",
        json={"filename": "a.exe", "content_type": "application/x-msdownload", "size": 5},
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "unsupported_type" and "image/jpeg" in err["details"]["allowed"]


@pytest.mark.parametrize(
    ("content_type", "limit_env", "size"),
    [
        ("image/png", "MAX_IMAGE_BYTES", 1001),
        ("application/pdf", "MAX_DOCUMENT_BYTES", 2001),
        ("video/mp4", "MAX_VIDEO_BYTES", 3001),
    ],
)
def test_size_limits_per_type(
    tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    limit_env: str,
    size: int,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv(limit_env, str(size - 1))
    get_settings.cache_clear()
    resp = tenant.post(
        "/v1/uploads", json={"filename": "f", "content_type": content_type, "size": size}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"
    assert resp.json()["error"]["details"]["max_bytes"] == size - 1


def test_default_limits_match_spec(tenant: Tenant) -> None:
    mb = 1024 * 1024
    for ct, ok, too_big in [
        ("image/jpeg", 25 * mb, 25 * mb + 1),
        ("application/pdf", 50 * mb, 50 * mb + 1),
        ("video/mp4", 2048 * mb, 2048 * mb + 1),
    ]:
        base = {"filename": "f", "content_type": ct}
        assert tenant.post("/v1/uploads", json={**base, "size": ok}).status_code == 201
        assert tenant.post("/v1/uploads", json={**base, "size": too_big}).status_code == 413


def test_validation_and_unknown_preset(tenant: Tenant) -> None:
    bad = tenant.post("/v1/uploads", json={"filename": "", "content_type": "image/png", "size": 0})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
    resp = tenant.post(
        "/v1/uploads",
        json={"filename": "a.png", "content_type": "image/png", "size": 5, "preset": "nope"},
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unknown_preset"


def test_requires_auth(client: TestClient) -> None:
    assert client.post("/v1/uploads", json={}).status_code == 401


def test_complete_happy_path(tenant: Tenant, enqueued: list) -> None:  # type: ignore[type-arg]
    data = image_bytes("PNG")
    file_id = upload_file(tenant, "pic.png", "image/png", data)
    assert len(enqueued) == 1 and enqueued[0][1] == "image"
    with sync_session() as s:
        file = s.get(File, uuid.UUID(file_id))
        assert file is not None
        assert file.status == FileStatus.UPLOADED and file.detected_type == "image/png"
        job = s.scalars(select(Job).where(Job.file_id == file.id)).one()
        assert job.status == "queued" and job.type == "image"
        assert [v["name"] for v in job.config["variants"]] == ["thumb", "medium", "large"]


def test_complete_is_idempotent(tenant: Tenant, enqueued: list) -> None:  # type: ignore[type-arg]
    file_id = upload_file(tenant, "doc.pdf", "application/pdf", pdf_bytes())
    again = tenant.post(f"/v1/uploads/{file_id}/complete")
    assert again.status_code == 200
    assert len(again.json()["jobs"]) == 1
    assert len(enqueued) == 1


def test_complete_before_upload_is_409(tenant: Tenant) -> None:
    info = _create(tenant)
    resp = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "upload_not_found"


def test_size_mismatch_rejected_and_object_deleted(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    data = image_bytes()
    info = _create(tenant, size=len(data) + 10)
    put_to_url(info["upload"]["url"], data, info["upload"]["headers"])
    resp = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "size_mismatch"
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    with sync_session() as s:
        file = s.get(File, uuid.UUID(info["file_id"]))
        assert file is not None and file.status == FileStatus.REJECTED
    assert tenant.post(f"/v1/uploads/{info['file_id']}/complete").status_code == 409


@pytest.mark.parametrize(
    ("declared", "data", "detected"),
    [
        ("image/jpeg", image_bytes("PNG"), "image/png"),
        ("image/png", b"MZ\x90\x00\x03\x00\x00\x00 fake exe", None),
        ("application/pdf", image_bytes("JPEG"), "image/jpeg"),
        ("video/mp4", b"just some text, not a video", "text/plain"),
        ("text/plain", b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png"),
    ],
)
def test_type_sniffing_rejects_mismatch(
    tenant: Tenant, declared: str, data: bytes, detected: str | None
) -> None:
    info = _create(tenant, content_type=declared, size=len(data))
    put_to_url(info["upload"]["url"], data, info["upload"]["headers"])
    resp = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "type_mismatch"
    assert err["details"] == {"declared": declared, "detected": detected}


def test_plain_zip_is_not_accepted_as_docx(tenant: Tenant) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.txt", "hi")
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    info = _create(tenant, content_type=docx_type, size=len(buf.getvalue()))
    put_to_url(info["upload"]["url"], buf.getvalue(), info["upload"]["headers"])
    resp = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["detected"] == "application/zip"


def test_multipart_upload(tenant: Tenant, monkeypatch: pytest.MonkeyPatch, enqueued: list) -> None:  # type: ignore[type-arg]
    from app.core.config import get_settings

    mb = 1024 * 1024
    monkeypatch.setenv("MULTIPART_THRESHOLD_BYTES", str(mb))
    monkeypatch.setenv("MULTIPART_PART_SIZE_BYTES", str(5 * mb))
    get_settings.cache_clear()

    data = image_bytes("JPEG") + b"\0" * (6 * mb)  # valid JPEG header, 2 parts
    info = _create(tenant, size=len(data))
    assert info["mode"] == "multipart" and info["upload"] is None
    mp = info["multipart"]
    assert mp["part_size"] == 5 * mb and len(mp["parts"]) == 2

    etags = []
    for part in mp["parts"]:
        chunk = data[(part["part_number"] - 1) * mp["part_size"] :][: mp["part_size"]]
        resp = requests.put(part["url"], data=chunk, timeout=10)
        assert resp.status_code == 200
        etags.append({"part_number": part["part_number"], "etag": resp.headers["ETag"]})

    missing = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "parts_required"

    done = tenant.post(f"/v1/uploads/{info['file_id']}/complete", json={"parts": etags})
    assert done.status_code == 200, done.text
    assert done.json()["file"]["detected_type"] == "image/jpeg"
    assert len(enqueued) == 1


def test_tenant_isolation_on_uploads(tenant: Tenant, other_tenant: Tenant) -> None:
    info = _create(tenant)
    resp = other_tenant.post(f"/v1/uploads/{info['file_id']}/complete")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "file_not_found"
    assert other_tenant.post(f"/v1/uploads/{uuid.uuid4()}/complete").status_code == 404
