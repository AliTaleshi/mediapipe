"""Video processing with ffmpeg: H.264/AAC MP4 transcode, poster, preview thumbnails.

ffmpeg is invoked through ``subprocess`` with argument lists (never a shell). Progress comes
from ffmpeg's machine-readable ``-progress pipe:1`` output.
"""

import json
import subprocess  # noqa: S404
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.config import get_settings
from app.processors.config import VideoPresetConfig
from app.processors.errors import PermanentError
from app.processors.types import ProcessingResult, ProcessorInput, ProducedFile, ProgressCallback


@dataclass(frozen=True)
class VideoTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout_seconds: int = 6 * 60 * 60

    @classmethod
    def from_settings(cls) -> "VideoTools":
        s = get_settings()
        return cls(s.ffmpeg_path, s.ffprobe_path, s.video_timeout_seconds)


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    video_codec: str
    has_audio: bool


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise PermanentError(f"{cmd[0]} is not installed on this worker.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PermanentError(f"{Path(cmd[0]).name} timed out.") from exc


def probe(path: Path, tools: VideoTools) -> VideoInfo:
    proc = _run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        raise PermanentError(f"Unsupported or corrupt video: {_last_line(proc.stderr)}")
    try:
        data = json.loads(proc.stdout)
        streams: list[dict[str, Any]] = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise PermanentError("Unsupported or corrupt video: no usable video stream.") from exc
    if duration <= 0:
        raise PermanentError("Unsupported or corrupt video: duration is unknown.")
    if _rotation(video) in (90, 270):
        width, height = height, width  # phone videos store rotation as metadata
    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        video_codec=str(video.get("codec_name", "unknown")),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _rotation(video: dict[str, Any]) -> int:
    raw = video.get("tags", {}).get("rotate")
    for side in video.get("side_data_list", []):
        raw = side.get("rotation", raw)
    try:
        return int(float(raw)) % 360 if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no details from ffmpeg"


def parse_progress_line(line: str, duration: float) -> float | None:
    """Turns one line of ``ffmpeg -progress`` output into a 0..1 fraction (None if unrelated)."""
    key, _, value = line.strip().partition("=")
    if key == "progress":
        return 1.0 if value == "end" else None
    if key not in ("out_time_us", "out_time_ms"):  # both are microseconds despite the name
        return None
    try:
        seconds = int(value) / 1_000_000
    except ValueError:  # "N/A" at the very start
        return None
    return max(0.0, min(seconds / duration, 1.0)) if duration > 0 else None


def build_transcode_command(
    src: Path, dest: Path, config: VideoPresetConfig, has_audio: bool, tools: VideoTools
) -> list[str]:
    cmd = [
        tools.ffmpeg,
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-i",
        str(src),
        "-vf",
        f"scale=-2:'min(ih,{config.max_height})'",
        "-c:v",
        "libx264",
        "-preset",
        config.encoder_preset,
        "-crf",
        str(config.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", f"{config.audio_bitrate_kbps}k"]
    else:
        cmd += ["-an"]
    return [*cmd, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dest)]


def transcode(
    src: Path,
    dest: Path,
    info: VideoInfo,
    config: VideoPresetConfig,
    tools: VideoTools,
    on_progress: ProgressCallback,
) -> None:
    cmd = build_transcode_command(src, dest, config, info.has_audio, tools)
    with tempfile.TemporaryFile(mode="w+") as stderr:
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd, stdout=subprocess.PIPE, stderr=stderr, text=True
            )
        except FileNotFoundError as exc:
            raise PermanentError(f"{tools.ffmpeg} is not installed on this worker.") from exc
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(tools.timeout_seconds, kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                fraction = parse_progress_line(line, info.duration)
                if fraction is not None:
                    on_progress(round(fraction * 100))
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        stderr.seek(0)
        error_text = stderr.read()
    if timed_out.is_set():
        raise PermanentError("Video transcoding timed out.")
    if proc.returncode != 0:
        raise PermanentError(f"Video transcoding failed: {_last_line(error_text)}")


def extract_frame(
    src: Path, dest: Path, at: float, tools: VideoTools, *, width: int | None, max_height: int
) -> tuple[int, int]:
    scale = f"scale={width}:-2" if width else f"scale=-2:'min(ih,{max_height})'"
    proc = _run(
        [
            tools.ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-q:v",
            "3",
            str(dest),
        ],
        timeout=120,
    )
    if proc.returncode != 0 or not dest.exists():
        raise PermanentError(f"Could not extract a frame: {_last_line(proc.stderr)}")
    with Image.open(dest) as img:
        return img.width, img.height


def poster_time(duration: float, requested: float) -> float:
    """The requested second, or the middle of clips shorter than that."""
    return requested if requested < duration else duration / 2


def process_video(
    inp: ProcessorInput,
    config: VideoPresetConfig,
    on_progress: ProgressCallback,
    tools: VideoTools | None = None,
) -> ProcessingResult:
    tools = tools or VideoTools.from_settings()
    info = probe(inp.path, tools)
    on_progress(5)

    mp4 = inp.workdir / "transcoded.mp4"
    transcode(
        inp.path,
        mp4,
        info,
        config,
        tools,
        lambda pct: on_progress(5 + round(pct * 0.85)),
    )
    out_info = probe(mp4, tools)
    outputs = [
        ProducedFile(
            "transcoded.mp4",
            mp4,
            "video/mp4",
            width=out_info.width,
            height=out_info.height,
            duration=out_info.duration,
        )
    ]
    on_progress(90)

    poster = inp.workdir / "poster.jpg"
    w, h = extract_frame(
        inp.path,
        poster,
        poster_time(info.duration, config.poster_at_seconds),
        tools,
        width=None,
        max_height=config.max_height,
    )
    outputs.append(ProducedFile("poster.jpg", poster, "image/jpeg", width=w, height=h))

    for i in range(1, config.thumbnail_count + 1):
        thumb = inp.workdir / f"preview_{i}.jpg"
        at = info.duration * i / (config.thumbnail_count + 1)
        w, h = extract_frame(
            inp.path, thumb, at, tools, width=config.thumbnail_width, max_height=config.max_height
        )
        outputs.append(ProducedFile(thumb.name, thumb, "image/jpeg", width=w, height=h))
        on_progress(90 + round(i / config.thumbnail_count * 10))
    on_progress(100)

    metadata = {
        "duration": round(info.duration, 3),
        "width": info.width,
        "height": info.height,
        "video_codec": info.video_codec,
        "has_audio": info.has_audio,
    }
    return ProcessingResult(outputs=outputs, metadata=metadata)
