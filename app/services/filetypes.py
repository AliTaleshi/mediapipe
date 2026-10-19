"""Allow-list of file types and content sniffing (never trust the client's content type)."""

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.db.models import MediaType

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_UNCOMPRESSED_DOCX_BYTES = 300 * 1024 * 1024
SNIFF_BYTES = 4096


@dataclass(frozen=True)
class FileType:
    content_type: str
    media_type: MediaType
    extension: str


ALLOWED_TYPES: dict[str, FileType] = {
    t.content_type: t
    for t in [
        FileType("image/jpeg", MediaType.IMAGE, "jpg"),
        FileType("image/png", MediaType.IMAGE, "png"),
        FileType("image/webp", MediaType.IMAGE, "webp"),
        FileType("image/gif", MediaType.IMAGE, "gif"),
        FileType("video/mp4", MediaType.VIDEO, "mp4"),
        FileType("video/quicktime", MediaType.VIDEO, "mov"),
        FileType("video/webm", MediaType.VIDEO, "webm"),
        FileType("application/pdf", MediaType.DOCUMENT, "pdf"),
        FileType(DOCX, MediaType.DOCUMENT, "docx"),
        FileType("text/plain", MediaType.DOCUMENT, "txt"),
    ]
}


def normalize_content_type(value: str) -> str:
    """``Text/Plain; charset=utf-8`` -> ``text/plain``."""
    return value.split(";", 1)[0].strip().lower()


def sniff_bytes(head: bytes) -> str | None:
    """Detects the real type from the first bytes.

    Returns an allowed content type, ``application/zip`` for a ZIP container (the caller must
    look inside to tell DOCX apart), or ``None`` when nothing matches.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:12] == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in head[:64] else None  # plain Matroska is not allowed
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if _looks_like_text(head):
        return "text/plain"
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        # final=False tolerates a multi-byte character cut off at the end of the sample
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def is_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            total = sum(i.file_size for i in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return False
    return (
        "[Content_Types].xml" in names
        and "word/document.xml" in names
        and total <= MAX_UNCOMPRESSED_DOCX_BYTES
    )
