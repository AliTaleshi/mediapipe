import io
from pathlib import Path

import pypdf
import pytest
from PIL import Image

from app.processors.config import DocumentPresetConfig
from app.processors.document import DOCX_TYPE, process_document
from app.processors.errors import PermanentError
from app.processors.types import ProcessingResult, ProcessorInput, noop_progress
from tests.helpers import docx_bytes, pdf_bytes


def run(
    tmp_path: Path,
    data: bytes,
    content_type: str,
    config: DocumentPresetConfig | None = None,
    progress: list[int] | None = None,
) -> ProcessingResult:
    src = tmp_path / "src"
    src.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    cb = progress.append if progress is not None else noop_progress
    return process_document(
        ProcessorInput(src, content_type, out), config or DocumentPresetConfig(), cb
    )


def text_of(result: ProcessingResult) -> str:
    out = next(o for o in result.outputs if o.name == "text.txt")
    return out.path.read_text(encoding="utf-8")


def test_pdf_text_pages_metadata_and_preview(tmp_path: Path) -> None:
    progress: list[int] = []
    data = pdf_bytes(["First page text", "Second page text", "Third"], title="My Report")
    result = run(tmp_path, data, "application/pdf", progress=progress)
    assert "First page text" in text_of(result) and "Third" in text_of(result)
    md = result.metadata
    assert md["page_count"] == 3
    assert md["title"] == "My Report" and md["author"] == "Test Author"
    assert md["word_count"] == 7 and md["has_text"] is True and md["text_truncated"] is False
    assert progress == sorted(progress) and progress[-1] == 100

    preview = next(o for o in result.outputs if o.name == "preview.webp")
    assert preview.content_type == "image/webp" and preview.width == 800
    with Image.open(preview.path) as img:
        assert img.size == (preview.width, preview.height)
    text_out = next(o for o in result.outputs if o.name == "text.txt")
    assert text_out.content_type.startswith("text/plain")


def test_pdf_preview_can_be_disabled(tmp_path: Path) -> None:
    result = run(tmp_path, pdf_bytes(), "application/pdf", DocumentPresetConfig(preview=False))
    assert [o.name for o in result.outputs] == ["text.txt"]


def test_pdf_preview_failure_does_not_fail_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object) -> tuple[int, int]:
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("app.processors.document.render_pdf_preview", boom)
    result = run(tmp_path, pdf_bytes(), "application/pdf")
    assert [o.name for o in result.outputs] == ["text.txt"]
    assert "renderer exploded" in result.metadata["preview_error"]


def test_pdf_without_text_layer(tmp_path: Path) -> None:
    result = run(tmp_path, pdf_bytes([""]), "application/pdf")
    assert result.metadata["has_text"] is False and text_of(result).strip() == ""


def test_pdf_text_truncation(tmp_path: Path) -> None:
    config = DocumentPresetConfig(max_text_chars=1000)
    result = run(tmp_path, ("word " * 2000).encode(), "text/plain", config)
    assert result.metadata["text_truncated"] is True and len(text_of(result)) == 1000


def test_encrypted_pdf_is_permanent_error(tmp_path: Path) -> None:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(PermanentError, match="password"):
        run(tmp_path, buf.getvalue(), "application/pdf")


@pytest.mark.parametrize("data", [b"%PDF-1.4 garbage", b"%PDF-", pdf_bytes()[:150]])
def test_corrupt_pdf_is_permanent_error(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(PermanentError):
        run(tmp_path, data, "application/pdf", DocumentPresetConfig(preview=False))


def test_docx_text_and_metadata(tmp_path: Path) -> None:
    result = run(tmp_path, docx_bytes(["Alpha beta", "Gamma"], title="Spec"), DOCX_TYPE)
    assert text_of(result) == "Alpha beta\nGamma"
    md = result.metadata
    assert md["title"] == "Spec" and md["author"] == "Test Author"
    assert md["word_count"] == 3 and md["created"] is not None
    assert [o.name for o in result.outputs] == ["text.txt"]


def test_docx_tables_are_included_in_order(tmp_path: Path) -> None:
    import docx

    doc = docx.Document()
    doc.add_paragraph("Before")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "A1"
    table.rows[0].cells[1].text = "B1"
    doc.add_paragraph("After")
    buf = io.BytesIO()
    doc.save(buf)
    assert text_of(run(tmp_path, buf.getvalue(), DOCX_TYPE)) == "Before\nA1\tB1\nAfter"


def test_corrupt_docx_is_permanent_error(tmp_path: Path) -> None:
    with pytest.raises(PermanentError):
        run(tmp_path, b"PK\x03\x04 definitely not a docx", DOCX_TYPE)


def test_plain_text_passthrough(tmp_path: Path) -> None:
    result = run(tmp_path, "héllo\nwörld\n".encode(), "text/plain")
    assert text_of(result) == "héllo\nwörld\n"
    assert result.metadata["page_count"] == 1 and result.metadata["line_count"] == 3


def test_plain_text_with_bom_and_bad_bytes(tmp_path: Path) -> None:
    result = run(tmp_path, b"\xef\xbb\xbfabc\xff def", "text/plain")
    assert text_of(result) == "abc� def"


def test_extract_text_can_be_disabled(tmp_path: Path) -> None:
    result = run(tmp_path, b"hello", "text/plain", DocumentPresetConfig(extract_text=False))
    assert result.outputs == [] and result.metadata["word_count"] == 1


def test_unsupported_type(tmp_path: Path) -> None:
    with pytest.raises(PermanentError, match="Unsupported"):
        run(tmp_path, b"x", "application/zip")
