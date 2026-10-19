from pathlib import Path

from tests.conftest import BUCKET, Tenant
from tests.helpers import make_video, requires_ffmpeg, upload_file


@requires_ffmpeg
def test_video_is_processed_end_to_end(tenant: Tenant, tmp_path: Path, s3) -> None:  # type: ignore[no-untyped-def]
    data = make_video(tmp_path / "clip.mp4", seconds=2, size="320x240").read_bytes()
    file_id = upload_file(tenant, "clip.mp4", "video/mp4", data)

    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "processed", body
    assert body["detected_type"] == "video/mp4"
    assert body["metadata"]["width"] == 320 and body["metadata"]["height"] == 240
    assert 1.8 < body["metadata"]["duration"] < 2.3
    names = [v["name"] for v in body["variants"]]
    assert names == [
        "poster.jpg",
        "preview_1.jpg",
        "preview_2.jpg",
        "preview_3.jpg",
        "transcoded.mp4",
    ]
    mp4 = next(v for v in body["variants"] if v["name"] == "transcoded.mp4")
    assert mp4["content_type"] == "video/mp4" and mp4["duration"] and mp4["width"] == 320

    key = f"projects/{tenant.project_id}/files/{file_id}/variants/transcoded.mp4"
    stored = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert stored[4:8] == b"ftyp"

    job = body["jobs"][0]
    assert job["type"] == "video" and job["status"] == "succeeded" and job["progress"] == 100


@requires_ffmpeg
def test_corrupt_video_fails_without_retries(tenant: Tenant) -> None:
    bad = b"\x00\x00\x00\x18ftypisom" + b"junk" * 200  # passes sniffing, not decoding
    file_id = upload_file(tenant, "bad.mp4", "video/mp4", bad)
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["status"] == "failed"
    assert "corrupt" in body["error"].lower() or "unsupported" in body["error"].lower()
    assert body["jobs"][0]["attempts"] == 1
