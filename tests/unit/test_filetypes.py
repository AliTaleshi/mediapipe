from pathlib import Path

import pytest

from app.services.filetypes import (
    DOCX,
    is_docx,
    normalize_content_type,
    sniff_bytes,
)
from app.services.uploads import clean_filename, plan_parts
from tests.helpers import docx_bytes, image_bytes, pdf_bytes


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (image_bytes("JPEG"), "image/jpeg"),
        (image_bytes("PNG"), "image/png"),
        (image_bytes("WEBP"), "image/webp"),
        (image_bytes("GIF"), "image/gif"),
        (pdf_bytes(), "application/pdf"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00", "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        (b"\x1a\x45\xdf\xa3\x93B\x82\x84webm", "video/webm"),
        (b"\x1a\x45\xdf\xa3\x93B\x82\x88matroska", None),
        (docx_bytes()[:64], "application/zip"),
        ("héllo wörld\n".encode(), "text/plain"),
        ("héllo".encode()[:-1], "text/plain"),  # multibyte char cut off by the sample size
        (b"MZ\x90\x00\x03\x00\x00\x00", None),  # Windows executable
        (b"\x00\x01\x02\x03", None),
        (b"", None),
    ],
)
def test_sniff_bytes(data: bytes, expected: str | None) -> None:
    assert sniff_bytes(data) == expected


def test_normalize_content_type() -> None:
    assert normalize_content_type("Text/Plain; charset=UTF-8") == "text/plain"


def test_is_docx(tmp_path: Path) -> None:
    good = tmp_path / "a.docx"
    good.write_bytes(docx_bytes())
    assert is_docx(good)
    assert DOCX.endswith("wordprocessingml.document")

    import zipfile

    other = tmp_path / "b.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("hello.txt", "hi")
    assert not is_docx(other)

    broken = tmp_path / "c.docx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    assert not is_docx(broken)


def test_plan_parts_respects_s3_limit() -> None:
    mb = 1024 * 1024
    assert plan_parts(200 * mb, 64 * mb) == (64 * mb, 4)
    size = 2 * 1024 * mb
    part_size, count = plan_parts(size, 64 * mb)
    assert count == 32 and part_size == 64 * mb
    huge = 5 * 1024 * 1024 * mb  # 5 TB
    part_size, count = plan_parts(huge, 64 * mb)
    assert count <= 10_000 and part_size * count >= huge


def test_clean_filename() -> None:
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("C:\\Users\\me\\photo.jpg") == "photo.jpg"
    assert clean_filename("a\x00b\nc.png") == "abc.png"
    assert clean_filename("   ") == "file"
