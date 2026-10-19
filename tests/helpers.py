"""Builders for small sample files, generated in code to keep the repo small."""

import io
import shutil
import subprocess
from pathlib import Path

import pytest
import requests
from docx import Document
from PIL import Image

from tests.conftest import Tenant

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed; video tests skipped"
)

CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "mp4": "video/mp4",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (640, 480), color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def pdf_bytes(pages: list[str] | None = None, title: str | None = None) -> bytes:
    """A minimal valid PDF with one line of text per page."""
    pages = pages if pages is not None else ["Hello MediaPipe"]
    objs: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info_id = None
    if title:
        objs.append(f"<< /Title ({title}) /Author (Test Author) >>".encode())
        info_id = len(objs)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (n, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R"
    if info_id:
        trailer += f" /Info {info_id} 0 R"
    out += f"{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def docx_bytes(paragraphs: list[str] | None = None, title: str = "Sample") -> bytes:
    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = "Test Author"
    for text in paragraphs or ["First paragraph.", "Second paragraph."]:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_video(path: Path, seconds: float = 2.0, size: str = "320x240", audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i"]
    cmd += [f"testsrc=duration={seconds}:size={size}:rate=15"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def put_to_url(url: str, data: bytes, headers: dict[str, str]) -> requests.Response:
    resp = requests.put(url, data=data, headers=headers, timeout=10)
    assert resp.status_code == 200, resp.text
    return resp


def upload_file(
    tenant: Tenant,
    filename: str,
    content_type: str,
    data: bytes,
    preset: str | None = None,
    complete: bool = True,
) -> str:
    """Runs the whole client flow against the API (storage is mocked). Returns the file id."""
    body: dict[str, object] = {
        "filename": filename,
        "content_type": content_type,
        "size": len(data),
    }
    if preset:
        body["preset"] = preset
    created = tenant.post("/v1/uploads", json=body)
    assert created.status_code == 201, created.text
    info = created.json()
    put_to_url(info["upload"]["url"], data, info["upload"]["headers"])
    if complete:
        done = tenant.post(f"/v1/uploads/{info['file_id']}/complete")
        assert done.status_code == 200, done.text
    return str(info["file_id"])
