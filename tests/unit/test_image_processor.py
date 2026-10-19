import io
from pathlib import Path

import pytest
from PIL import Image

from app.processors.config import ImagePresetConfig, ImageVariantSpec
from app.processors.errors import PermanentError
from app.processors.image import make_variant, process_image, target_size
from app.processors.types import (
    ProcessingResult,
    ProcessorInput,
    ProducedFile,
    noop_progress,
)
from tests.helpers import image_bytes


def run(
    tmp_path: Path,
    data: bytes,
    config: ImagePresetConfig | None = None,
    progress: list[int] | None = None,
) -> ProcessingResult:
    src = tmp_path / "src"
    src.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    cb = progress.append if progress is not None else noop_progress
    return process_image(ProcessorInput(src, "image/jpeg", out), config or ImagePresetConfig(), cb)


def by_name(result: ProcessingResult) -> dict[str, ProducedFile]:
    return {o.name: o for o in result.outputs}


def test_default_preset_produces_webp_and_original_variants(tmp_path: Path) -> None:
    progress: list[int] = []
    result = run(tmp_path, image_bytes("JPEG", (2000, 1000)), progress=progress)
    outs = by_name(result)
    assert sorted(outs) == [
        "large.jpg",
        "large.webp",
        "medium.jpg",
        "medium.webp",
        "thumb.jpg",
        "thumb.webp",
    ]
    assert (outs["thumb.webp"].width, outs["thumb.webp"].height) == (200, 200)  # cropped square
    assert (outs["medium.webp"].width, outs["medium.webp"].height) == (800, 400)
    assert (outs["large.jpg"].width, outs["large.jpg"].height) == (1600, 800)
    assert outs["thumb.webp"].content_type == "image/webp"
    assert outs["thumb.jpg"].content_type == "image/jpeg"
    for out in outs.values():
        with Image.open(out.path) as img:
            assert img.size == (out.width, out.height)
    assert result.metadata == {"width": 2000, "height": 1000, "format": "jpeg", "frames": 1}
    assert progress == sorted(progress) and progress[-1] == 100


def test_never_upscales(tmp_path: Path) -> None:
    outs = by_name(run(tmp_path, image_bytes("PNG", (100, 60))))
    assert (outs["large.webp"].width, outs["large.webp"].height) == (100, 60)
    assert (outs["medium.png"].width, outs["medium.png"].height) == (100, 60)
    # thumb crops to the target ratio but keeps the pixels it has
    assert (outs["thumb.png"].width, outs["thumb.png"].height) == (60, 60)


def test_exif_orientation_is_applied_and_metadata_stripped(tmp_path: Path) -> None:
    img = Image.new("RGB", (100, 50), "blue")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW when displayed
    exif[0x010F] = "SecretCamera"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)

    result = run(tmp_path, buf.getvalue())
    assert (result.metadata["width"], result.metadata["height"]) == (50, 100)
    out = by_name(result)["medium.jpg"]
    assert (out.width, out.height) == (50, 100)
    with Image.open(out.path) as processed:
        assert len(processed.getexif()) == 0
        assert "exif" not in processed.info
    with Image.open(by_name(result)["medium.webp"].path) as processed:
        assert len(processed.getexif()) == 0


def test_keep_metadata_when_not_stripping(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    buf = io.BytesIO()
    Image.new("RGB", (100, 50), "blue").save(buf, "JPEG", exif=exif)
    config = ImagePresetConfig(strip_metadata=False, formats=["original"])
    out = by_name(run(tmp_path, buf.getvalue(), config))["medium.jpg"]
    with Image.open(out.path) as processed:
        assert processed.getexif().get(0x010F) == "SecretCamera"


def test_alpha_png_to_jpeg_and_webp(tmp_path: Path) -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (50, 50), (255, 0, 0, 0)).save(buf, "PNG")
    outs = by_name(run(tmp_path, buf.getvalue()))
    with Image.open(outs["medium.webp"].path) as img:
        assert img.mode == "RGBA"
    assert outs["medium.png"].content_type == "image/png"


def test_webp_source_does_not_duplicate_outputs(tmp_path: Path) -> None:
    outs = by_name(run(tmp_path, image_bytes("WEBP", (300, 200))))
    assert sorted(outs) == ["large.webp", "medium.webp", "thumb.webp"]


def test_original_only_format_and_gif_source(tmp_path: Path) -> None:
    config = ImagePresetConfig(
        formats=["original"], variants=[ImageVariantSpec(name="s", width=50)]
    )
    outs = by_name(run(tmp_path, image_bytes("GIF", (200, 100)), config))
    assert list(outs) == ["s.gif"]
    assert (outs["s.gif"].width, outs["s.gif"].height) == (50, 25)


def test_custom_variant_height_only() -> None:
    spec = ImageVariantSpec(name="h", height=100)
    assert target_size((400, 200), spec) == (200, 100)
    assert make_variant(Image.new("RGB", (400, 200)), spec).size == (200, 100)


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", image_bytes("JPEG")[:200], b"GIF89a" + b"\0" * 5],
)
def test_corrupt_images_are_permanent_errors(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(PermanentError):
        run(tmp_path, data)


def test_decompression_bomb_is_permanent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(PermanentError, match="too large"):
        run(tmp_path, image_bytes("PNG", (100, 100)))


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        ImageVariantSpec(name="x")
    with pytest.raises(ValueError):
        ImageVariantSpec(name="x", width=10, fit="cover")
    with pytest.raises(ValueError):
        ImagePresetConfig(
            variants=[ImageVariantSpec(name="a", width=1), ImageVariantSpec(name="a", width=2)]
        )
    with pytest.raises(ValueError):
        ImageVariantSpec(name="Bad Name", width=10)
