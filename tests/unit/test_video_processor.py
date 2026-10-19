import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.processors.config import VideoPresetConfig
from app.processors.errors import PermanentError
from app.processors.types import ProcessingResult, ProcessorInput, noop_progress
from app.processors.video import (
    VideoTools,
    build_transcode_command,
    parse_progress_line,
    poster_time,
    probe,
    process_video,
)
from tests.helpers import make_video, requires_ffmpeg

TOOLS = VideoTools()


# --- pure helpers: run everywhere, no ffmpeg needed --------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=5000000\n", 0.5),
        ("out_time_ms=2500000\n", 0.25),  # microseconds despite the name
        ("out_time_us=99000000", 1.0),  # clamped
        ("out_time_us=N/A", None),
        ("out_time_us=-1000", 0.0),
        ("out_time=00:00:05.000000", None),
        ("frame=120", None),
        ("progress=continue", None),
        ("progress=end", 1.0),
        ("garbage", None),
    ],
)
def test_parse_progress_line(line: str, expected: float | None) -> None:
    assert parse_progress_line(line, duration=10.0) == expected


def test_parse_progress_with_unknown_duration() -> None:
    assert parse_progress_line("out_time_us=1000", duration=0) is None


def test_poster_time() -> None:
    assert poster_time(10, 1.0) == 1.0
    assert poster_time(0.6, 1.0) == 0.3


def test_transcode_command_is_h264_aac_without_shell() -> None:
    cmd = build_transcode_command(
        Path("in"), Path("out.mp4"), VideoPresetConfig(max_height=720, crf=20), True, TOOLS
    )
    assert cmd[0] == "ffmpeg" and cmd[-1] == "out.mp4"
    assert "libx264" in cmd and "aac" in cmd and "+faststart" in cmd
    assert cmd[cmd.index("-crf") + 1] == "20"
    assert "min(ih,720)" in cmd[cmd.index("-vf") + 1]
    assert "-an" not in cmd
    assert "-an" in build_transcode_command(Path("i"), Path("o"), VideoPresetConfig(), False, TOOLS)
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"


def test_missing_ffmpeg_is_a_clear_permanent_error(tmp_path: Path) -> None:
    src = tmp_path / "s"
    src.write_bytes(b"x")
    tools = VideoTools(ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/ffprobe")
    with pytest.raises(PermanentError, match="not installed"):
        probe(src, tools)


# --- real ffmpeg ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_video(tmp_path_factory.mktemp("video") / "sample.mp4", seconds=3, size="640x480")


def run(
    tmp_path: Path,
    src: Path,
    config: VideoPresetConfig | None = None,
    progress: list[int] | None = None,
) -> ProcessingResult:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    cb = progress.append if progress is not None else noop_progress
    return process_video(ProcessorInput(src, "video/mp4", out), config or VideoPresetConfig(), cb)


def stream_info(path: Path) -> dict[str, dict[str, object]]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return {s["codec_type"]: s for s in json.loads(proc.stdout)["streams"]}


@requires_ffmpeg
def test_probe(sample_video: Path) -> None:
    info = probe(sample_video, TOOLS)
    assert (info.width, info.height, info.video_codec, info.has_audio) == (640, 480, "h264", True)
    assert 2.9 < info.duration < 3.2


@requires_ffmpeg
def test_transcode_poster_thumbnails_and_progress(tmp_path: Path, sample_video: Path) -> None:
    progress: list[int] = []
    result = run(tmp_path, sample_video, progress=progress)

    names = [o.name for o in result.outputs]
    assert names == [
        "transcoded.mp4",
        "poster.jpg",
        "preview_1.jpg",
        "preview_2.jpg",
        "preview_3.jpg",
    ]
    mp4 = result.outputs[0]
    streams = stream_info(mp4.path)
    assert streams["video"]["codec_name"] == "h264" and streams["audio"]["codec_name"] == "aac"
    assert (mp4.width, mp4.height) == (640, 480) and mp4.duration and 2.5 < mp4.duration < 3.5

    poster = result.outputs[1]
    with Image.open(poster.path) as img:
        assert img.size == (640, 480) == (poster.width, poster.height)
    for thumb in result.outputs[2:]:
        assert thumb.width == 320 and thumb.height == 240

    assert result.metadata["width"] == 640 and result.metadata["has_audio"] is True
    assert 2.9 < result.metadata["duration"] < 3.2
    assert progress == sorted(progress)
    assert progress[0] == 5 and progress[-1] == 100 and len(set(progress)) >= 4


@requires_ffmpeg
def test_downscales_to_max_height_and_keeps_even_width(tmp_path: Path, sample_video: Path) -> None:
    result = run(tmp_path, sample_video, VideoPresetConfig(max_height=240))
    mp4 = result.outputs[0]
    assert (mp4.width, mp4.height) == (320, 240)
    assert result.outputs[1].height == 240  # poster follows max_height too


@requires_ffmpeg
def test_never_upscales(tmp_path: Path) -> None:
    small = make_video(tmp_path / "small.mp4", seconds=1, size="160x120")
    result = run(tmp_path, small, VideoPresetConfig(max_height=1080))
    assert (result.outputs[0].width, result.outputs[0].height) == (160, 120)


@requires_ffmpeg
def test_video_without_audio_and_short_clip_poster(tmp_path: Path) -> None:
    clip = make_video(tmp_path / "mute.mp4", seconds=0.6, audio=False)
    result = run(tmp_path, clip, VideoPresetConfig(thumbnail_count=0))
    assert "audio" not in stream_info(result.outputs[0].path)
    assert [o.name for o in result.outputs] == ["transcoded.mp4", "poster.jpg"]
    assert result.metadata["has_audio"] is False


@requires_ffmpeg
def test_webm_input(tmp_path: Path) -> None:
    webm = tmp_path / "clip.webm"
    subprocess.run(
        [
            *("ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi"),
            *("-i", "testsrc=duration=1:size=160x120", "-c:v", "libvpx", str(webm)),
        ],
        check=True,
    )
    result = run(tmp_path, webm)
    assert stream_info(result.outputs[0].path)["video"]["codec_name"] == "h264"


@requires_ffmpeg
@pytest.mark.parametrize("kind", ["garbage", "truncated", "empty"])
def test_corrupt_video_is_permanent_error(tmp_path: Path, sample_video: Path, kind: str) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(
        {
            "garbage": b"\x00\x00\x00\x18ftypisom" + b"junk" * 100,
            "truncated": sample_video.read_bytes()[:3000],
            "empty": b"",
        }[kind]
    )
    with pytest.raises(PermanentError, match="(?i)unsupported or corrupt|failed"):
        run(tmp_path, bad)


@requires_ffmpeg
def test_audio_only_file_is_rejected(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(audio)],
        check=True,
    )
    with pytest.raises(PermanentError, match="no usable video stream"):
        run(tmp_path, audio)


@requires_ffmpeg
def test_timeout_kills_ffmpeg(tmp_path: Path, sample_video: Path) -> None:
    tools = VideoTools(timeout_seconds=0)
    out = tmp_path / "out"
    out.mkdir()
    big = make_video(tmp_path / "long.mp4", seconds=20, size="1280x720")
    with pytest.raises(PermanentError, match="timed out"):
        process_video(
            ProcessorInput(big, "video/mp4", out), VideoPresetConfig(), noop_progress, tools
        )
