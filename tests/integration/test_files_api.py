import uuid
from typing import Any

import requests

from tests.conftest import BUCKET, Tenant
from tests.helpers import docx_bytes, image_bytes, pdf_bytes, upload_file


def keys_in_bucket(s3: Any, prefix: str = "") -> list[str]:
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"] for o in resp.get("Contents", [])]


def test_variant_urls_are_downloadable_presigned_gets(tenant: Tenant) -> None:
    data = image_bytes("JPEG", (900, 600))
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", data)
    body = tenant.get(f"/v1/files/{file_id}").json()
    medium = next(v for v in body["variants"] if v["name"] == "medium.jpg")
    assert "X-Amz-Expires=3600" in medium["url"]
    resp = requests.get(medium["url"], timeout=10)
    assert resp.status_code == 200 and len(resp.content) == medium["size"]
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert "inline" in resp.headers["Content-Disposition"]
    # the URL is a plain presigned URL: no API key is needed (or sent) to use it
    assert "Authorization" not in resp.request.headers


def test_file_details_include_everything(tenant: Tenant) -> None:
    file_id = upload_file(tenant, "My Photo.jpg", "image/jpeg", image_bytes("JPEG"), preset=None)
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["original_name"] == "My Photo.jpg"
    assert body["content_type"] == body["detected_type"] == "image/jpeg"
    assert body["media_type"] == "image" and body["preset"] == "default"
    assert body["jobs"][0]["file_id"] == file_id and len(body["variants"]) == 6


def test_list_files_filters_and_pagination(tenant: Tenant, other_tenant: Tenant) -> None:
    ids = [
        upload_file(tenant, f"{i}.jpg", "image/jpeg", image_bytes("JPEG", (50 + i, 50)))
        for i in range(3)
    ]
    pdf_id = upload_file(tenant, "a.pdf", "application/pdf", pdf_bytes())
    failed_id = upload_file(tenant, "bad.pdf", "application/pdf", b"%PDF-1.4 bad")
    pending = tenant.post(
        "/v1/uploads", json={"filename": "p.png", "content_type": "image/png", "size": 10}
    ).json()["file_id"]
    upload_file(other_tenant, "theirs.jpg", "image/jpeg", image_bytes("JPEG"))

    everything = tenant.get("/v1/files").json()
    assert everything["total"] == 6 and everything["limit"] == 25 and everything["offset"] == 0
    assert [f["id"] for f in everything["items"]][0] == pending  # newest first
    assert "variants" not in everything["items"][0]

    def ids_for(**params: Any) -> list[str]:
        return [f["id"] for f in tenant.get("/v1/files", params=params).json()["items"]]

    assert sorted(ids_for(type="image", status="processed")) == sorted(ids)
    assert ids_for(type="document", status="failed") == [failed_id]
    assert ids_for(status="pending_upload") == [pending]
    assert pdf_id in ids_for(type="document")

    page1 = tenant.get("/v1/files", params={"limit": 4}).json()
    page2 = tenant.get("/v1/files", params={"limit": 4, "offset": 4}).json()
    assert len(page1["items"]) == 4 and len(page2["items"]) == 2 and page2["total"] == 6
    assert not {f["id"] for f in page1["items"]} & {f["id"] for f in page2["items"]}

    assert other_tenant.get("/v1/files").json()["total"] == 1


def test_list_files_validation(tenant: Tenant) -> None:
    for params in (
        {"status": "bogus"},
        {"type": "audio"},
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
    ):
        resp = tenant.get("/v1/files", params=params)
        assert resp.status_code == 422, params
        assert resp.json()["error"]["code"] == "validation_error"


def test_delete_removes_rows_and_all_objects(tenant: Tenant, s3: Any) -> None:
    keep = upload_file(tenant, "keep.jpg", "image/jpeg", image_bytes("JPEG"))
    gone = upload_file(tenant, "gone.pdf", "application/pdf", pdf_bytes())
    before = len(keys_in_bucket(s3))
    assert tenant.delete(f"/v1/files/{gone}").status_code == 204
    assert tenant.get(f"/v1/files/{gone}").status_code == 404
    assert keys_in_bucket(s3, f"projects/{tenant.project_id}/files/{gone}/") == []
    assert len(keys_in_bucket(s3)) == before - 3  # original + text.txt + preview.webp
    assert tenant.get(f"/v1/files/{keep}").status_code == 200
    assert tenant.delete(f"/v1/files/{gone}").status_code == 404

    from sqlalchemy import func, select

    from app.db.models import Job, Variant
    from app.db.session import sync_session

    with sync_session() as s:
        for model in (Variant, Job):
            stmt = select(func.count()).select_from(model).where(model.file_id == uuid.UUID(gone))
            assert s.scalar(stmt) == 0


def test_delete_pending_multipart_upload_aborts_it(
    tenant: Tenant, s3: Any, monkeypatch: Any
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("MULTIPART_THRESHOLD_BYTES", "1000")
    get_settings.cache_clear()
    info = tenant.post(
        "/v1/uploads", json={"filename": "v.mp4", "content_type": "video/mp4", "size": 10_000}
    ).json()
    assert info["mode"] == "multipart"
    assert len(s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])) == 1
    assert tenant.delete(f"/v1/files/{info['file_id']}").status_code == 204
    assert s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


def test_tenant_isolation_on_files(tenant: Tenant, other_tenant: Tenant, s3: Any) -> None:
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    objects = keys_in_bucket(s3)
    assert other_tenant.get(f"/v1/files/{file_id}").status_code == 404
    assert other_tenant.delete(f"/v1/files/{file_id}").status_code == 404
    assert keys_in_bucket(s3) == objects
    assert tenant.get(f"/v1/files/{file_id}").status_code == 200


def test_docx_listing_type_is_document(tenant: Tenant) -> None:
    upload_file(
        tenant,
        "a.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        docx_bytes(),
    )
    assert tenant.get("/v1/files", params={"type": "document"}).json()["total"] == 1
