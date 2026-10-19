"""The definition of done, as a test: image + PDF + video uploaded, processed, progress
visible, signed webhook delivered for each."""

import json
from pathlib import Path

import httpx
import pytest

from app.services.webhooks import verify_signature
from tests.conftest import Tenant
from tests.helpers import HAS_FFMPEG, image_bytes, make_video, pdf_bytes, upload_file


@pytest.fixture
def received(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(
        "app.services.webhooks.build_http_client",
        lambda settings: httpx.Client(transport=httpx.MockTransport(handle)),
    )
    return requests


def test_image_pdf_and_video_end_to_end(
    tenant: Tenant, received: list[httpx.Request], tmp_path: Path
) -> None:
    secret = tenant.post("/v1/webhooks", json={"url": "https://app.example.com/hook"}).json()[
        "secret"
    ]
    samples = [
        ("photo.jpg", "image/jpeg", image_bytes("JPEG", (1600, 1200))),
        ("report.pdf", "application/pdf", pdf_bytes(["Quarterly report"], title="Q3")),
    ]
    if HAS_FFMPEG:
        video = make_video(tmp_path / "clip.mp4", seconds=2).read_bytes()
        samples.append(("clip.mp4", "video/mp4", video))

    file_ids = [upload_file(tenant, *sample) for sample in samples]

    for file_id in file_ids:
        body = tenant.get(f"/v1/files/{file_id}").json()
        assert body["status"] == "processed", body
        assert body["variants"] and all(v["url"] for v in body["variants"])
        assert body["jobs"][0]["progress"] == 100
        with tenant.client.stream(
            "GET", f"/v1/files/{file_id}/events", headers=tenant.headers
        ) as resp:
            assert "event: done" in "".join(resp.iter_text())

    assert len(received) == len(file_ids)
    delivered = {}
    for req in received:
        assert verify_signature(secret, req.headers["X-MediaPipe-Signature"], req.content)
        payload = json.loads(req.content)
        delivered[payload["data"]["file"]["id"]] = payload["data"]["file"]
    assert set(delivered) == set(file_ids)
    assert {f["media_type"] for f in delivered.values()} == (
        {"image", "document", "video"} if HAS_FFMPEG else {"image", "document"}
    )

    assert all(d["success"] for d in tenant.get("/v1/webhooks/deliveries").json()["items"])
