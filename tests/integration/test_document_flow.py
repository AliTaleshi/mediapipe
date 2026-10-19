import io

import pypdf
from sqlalchemy import select

from app.db.models import Job, JobStatus
from app.db.session import sync_session
from tests.conftest import BUCKET, Tenant
from tests.helpers import CONTENT_TYPES, docx_bytes, pdf_bytes, upload_file


def test_pdf_is_processed_end_to_end(tenant: Tenant, s3) -> None:  # type: ignore[no-untyped-def]
    data = pdf_bytes(["Invoice 2026", "Total due"], title="Invoice")
    file_id = upload_file(tenant, "invoice.pdf", CONTENT_TYPES["pdf"], data)
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "processed"
    assert body["metadata"]["page_count"] == 2 and body["metadata"]["title"] == "Invoice"
    assert sorted(v["name"] for v in body["variants"]) == ["preview.webp", "text.txt"]

    text_variant = next(v for v in body["variants"] if v["name"] == "text.txt")
    key = f"projects/{tenant.project_id}/files/{file_id}/variants/text.txt"
    stored = s3.get_object(Bucket=BUCKET, Key=key)
    assert "Invoice 2026" in stored["Body"].read().decode()
    assert stored["ContentType"].startswith("text/plain")
    assert text_variant["size"] > 0


def test_docx_and_txt_flow(tenant: Tenant) -> None:
    docx_id = upload_file(tenant, "memo.docx", CONTENT_TYPES["docx"], docx_bytes(["Hello docx"]))
    txt_id = upload_file(tenant, "notes.txt", "text/plain; charset=utf-8", b"plain words here")
    for file_id, words in [(docx_id, 2), (txt_id, 3)]:
        body = tenant.get(f"/v1/files/{file_id}").json()
        assert body["status"] == "processed" and body["metadata"]["word_count"] == words
        assert [v["name"] for v in body["variants"]] == ["text.txt"]


def test_corrupt_pdf_fails_with_clear_error(tenant: Tenant) -> None:
    file_id = upload_file(tenant, "broken.pdf", CONTENT_TYPES["pdf"], b"%PDF-1.4\nnot really")
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "failed" and "PDF" in body["error"]
    with sync_session() as s:
        job = s.scalars(select(Job)).one()
        assert job.status == JobStatus.FAILED and job.attempts == 1


def test_encrypted_pdf_fails_with_clear_error(tenant: Tenant) -> None:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("pw")
    buf = io.BytesIO()
    writer.write(buf)
    file_id = upload_file(tenant, "locked.pdf", CONTENT_TYPES["pdf"], buf.getvalue())
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "failed" and "password" in body["error"]


def test_custom_document_preset(tenant: Tenant) -> None:
    # Presets can be created once the presets API exists; until then write one directly.
    from app.db.models import Preset

    with sync_session() as s:
        import uuid

        s.add(
            Preset(
                project_id=uuid.UUID(tenant.project_id),
                name="text-only",
                media_type="document",
                config={"preview": False},
            )
        )
    file_id = upload_file(tenant, "a.pdf", CONTENT_TYPES["pdf"], pdf_bytes(), preset="text-only")
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert [v["name"] for v in body["variants"]] == ["text.txt"]
