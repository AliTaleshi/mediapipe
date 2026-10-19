"""Document processing: text extraction, page count and metadata for PDF, DOCX and TXT."""

import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import docx
import pypdf
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf.errors import PyPdfError

from app.core.logging import get_logger
from app.processors.config import DocumentPresetConfig
from app.processors.errors import PermanentError
from app.processors.types import ProcessingResult, ProcessorInput, ProducedFile, ProgressCallback

log = get_logger(__name__)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PREVIEW_WIDTH = 800


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value else ""
    return text or None


def extract_pdf(
    path: Path, on_progress: ProgressCallback, max_chars: int
) -> tuple[str, dict[str, Any]]:
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise PermanentError("The PDF is password protected.")
        page_count = len(reader.pages)
        info = reader.metadata
    except PermanentError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        raise PermanentError(f"Corrupt or unsupported PDF: {exc}") from exc

    chunks: list[str] = []
    total = 0
    unreadable = 0
    for i in range(page_count):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:  # pypdf raises many types on malformed pages
            unreadable += 1
            text = ""
        chunks.append(text)
        total += len(text)
        on_progress(round((i + 1) / page_count * 100))
        if total > max_chars:
            break

    metadata: dict[str, Any] = {
        "page_count": page_count,
        "title": _clean(info.title) if info else None,
        "author": _clean(info.author) if info else None,
        "subject": _clean(info.subject) if info else None,
        "creator": _clean(info.creator) if info else None,
        "created": _iso(info.creation_date) if info else None,
        "modified": _iso(info.modification_date) if info else None,
    }
    if unreadable:
        metadata["unreadable_pages"] = unreadable
    return "\n\n".join(chunks), metadata


def _docx_page_count(path: Path) -> int | None:
    """Word records its last computed page count in docProps/app.xml (absent in many files)."""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("docProps/app.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    match = re.search(r"<Pages>(\d+)</Pages>", xml)
    return int(match.group(1)) if match else None


def extract_docx(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        document = docx.Document(str(path))
        parts: list[str] = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                parts.append(block.text)
            elif isinstance(block, Table):
                parts.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
        props = document.core_properties
    except Exception as exc:  # python-docx surfaces zip/xml/key errors of many types
        raise PermanentError(f"Corrupt or unsupported DOCX: {exc}") from exc
    metadata: dict[str, Any] = {
        "page_count": _docx_page_count(path),
        "title": _clean(props.title),
        "author": _clean(props.author),
        "subject": _clean(props.subject),
        "created": _iso(props.created),
        "modified": _iso(props.modified),
        "last_modified_by": _clean(props.last_modified_by),
    }
    return "\n".join(parts), metadata


def extract_txt(path: Path) -> tuple[str, dict[str, Any]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    return text, {"page_count": 1, "line_count": text.count("\n") + (1 if text else 0)}


def render_pdf_preview(path: Path, dest: Path) -> tuple[int, int]:
    """Renders page 1 as WebP. Raises on any failure; callers treat the preview as optional."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[0]
        scale = min(PREVIEW_WIDTH / page.get_width(), 4.0)
        image = page.render(scale=scale).to_pil().convert("RGB")
        image.save(dest, "WEBP", quality=80)
        return int(image.width), int(image.height)
    finally:
        pdf.close()


def process_document(
    inp: ProcessorInput, config: DocumentPresetConfig, on_progress: ProgressCallback
) -> ProcessingResult:
    kind = inp.content_type
    if kind == "application/pdf":
        text, metadata = extract_pdf(
            inp.path, lambda p: on_progress(round(p * 0.8)), config.max_text_chars
        )
    elif kind == DOCX_TYPE:
        text, metadata = extract_docx(inp.path)
    elif kind == "text/plain":
        text, metadata = extract_txt(inp.path)
    else:
        raise PermanentError(f"Unsupported document type: {kind}")
    on_progress(80)

    truncated = len(text) > config.max_text_chars
    if truncated:
        text = text[: config.max_text_chars]
    metadata.update(
        char_count=len(text),
        word_count=len(text.split()),
        has_text=bool(text.strip()),
        text_truncated=truncated,
    )

    result = ProcessingResult(metadata=metadata)
    if config.extract_text:
        dest = inp.workdir / "text.txt"
        dest.write_text(text, encoding="utf-8")
        result.outputs.append(ProducedFile("text.txt", dest, "text/plain; charset=utf-8"))

    if config.preview and kind == "application/pdf":
        preview = inp.workdir / "preview.webp"
        try:
            width, height = render_pdf_preview(inp.path, preview)
            result.outputs.append(
                ProducedFile("preview.webp", preview, "image/webp", width=width, height=height)
            )
        except Exception as exc:  # the preview is a nice-to-have; never fail the job over it
            log.warning("PDF preview failed: %s", exc)
            metadata["preview_error"] = str(exc)[:200]
    on_progress(100)
    return result
