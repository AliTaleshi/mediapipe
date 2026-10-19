"""Image processing with Pillow: orientation fix, metadata stripping, resized variants."""

import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.processors.config import ImagePresetConfig, ImageVariantSpec
from app.processors.errors import PermanentError
from app.processors.types import ProcessingResult, ProcessorInput, ProducedFile, ProgressCallback

Image.MAX_IMAGE_PIXELS = 100_000_000  # ~100 MP; larger images are treated as decompression bombs

_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
}


def target_size(src: tuple[int, int], spec: ImageVariantSpec) -> tuple[int, int]:
    """Output size for a variant. Images are never upscaled."""
    sw, sh = src
    if spec.fit == "cover":
        assert spec.width is not None and spec.height is not None
        crop_w, crop_h = _cover_crop_size(src, spec.width, spec.height)
        if crop_w >= spec.width:
            return spec.width, spec.height
        return crop_w, crop_h  # small source: keep the cropped region as is
    limit_w = spec.width / sw if spec.width else float("inf")
    limit_h = spec.height / sh if spec.height else float("inf")
    scale = min(limit_w, limit_h, 1.0)
    return max(1, round(sw * scale)), max(1, round(sh * scale))


def _cover_crop_size(src: tuple[int, int], width: int, height: int) -> tuple[int, int]:
    sw, sh = src
    target_ratio = width / height
    if sw / sh > target_ratio:  # source is wider: trim the sides
        return max(1, round(sh * target_ratio)), sh
    return sw, max(1, round(sw / target_ratio))  # source is taller: trim top and bottom


def make_variant(img: Image.Image, spec: ImageVariantSpec) -> Image.Image:
    size = target_size(img.size, spec)
    if spec.fit == "cover":
        assert spec.width is not None and spec.height is not None
        crop_w, crop_h = _cover_crop_size(img.size, spec.width, spec.height)
        left, top = (img.width - crop_w) // 2, (img.height - crop_h) // 2
        img = img.crop((left, top, left + crop_w, top + crop_h))
    if img.size == size:
        return img.copy()
    return img.resize(size, Image.Resampling.LANCZOS)


def _prepare_for(fmt: str, img: Image.Image) -> Image.Image:
    """Convert to a mode the target format can store."""
    if fmt == "JPEG":
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            return flat
        return img.convert("RGB") if img.mode != "RGB" else img
    if fmt == "WEBP":
        if img.mode in ("RGB", "RGBA"):
            return img
        return img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    if fmt == "PNG":
        return img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else img.convert("RGBA")
    return img


def _open(path: Path) -> tuple[Image.Image, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(path)
            fmt = img.format or ""
            img.seek(0)
            img.load()  # force a full decode so truncated files fail here
    except Image.DecompressionBombError as exc:
        raise PermanentError("Image is too large to process.") from exc
    except Image.DecompressionBombWarning as exc:
        raise PermanentError("Image is too large to process.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise PermanentError(f"Corrupt or unsupported image: {exc}") from exc
    if fmt not in _FORMATS:
        raise PermanentError(f"Unsupported image format: {fmt or 'unknown'}")
    return img, fmt


def _save(img: Image.Image, fmt: str, dest: Path, quality: int, extras: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {}
    if fmt == "JPEG":
        kwargs = {"quality": quality, "optimize": True, "progressive": True, **extras}
    elif fmt == "WEBP":
        kwargs = {"quality": quality, "method": 4, **extras}
    elif fmt == "PNG":
        kwargs = {"optimize": True, **extras}
    img.save(dest, fmt, **kwargs)


def process_image(
    inp: ProcessorInput, config: ImagePresetConfig, on_progress: ProgressCallback
) -> ProcessingResult:
    source, source_format = _open(inp.path)
    frames = getattr(source, "n_frames", 1)
    img = ImageOps.exif_transpose(source)  # applies EXIF orientation and clears the tag
    extras: dict[str, Any] = {}
    if not config.strip_metadata:
        if exif := img.getexif():
            extras["exif"] = exif.tobytes()
        if icc := source.info.get("icc_profile"):
            extras["icc_profile"] = icc

    formats: list[tuple[str, str, str]] = []  # (extension, Pillow format, content type)
    if "webp" in config.formats:
        formats.append(("webp", "WEBP", "image/webp"))
    if "original" in config.formats and not (source_format == "WEBP" and formats):
        ext, ctype = _FORMATS[source_format]
        formats.append((ext, source_format, ctype))

    total = len(config.variants) * len(formats)
    outputs: list[ProducedFile] = []
    for spec in config.variants:
        resized = make_variant(img, spec)
        for ext, fmt, ctype in formats:
            dest = inp.workdir / f"{spec.name}.{ext}"
            prepared = _prepare_for(fmt, resized)
            try:
                _save(prepared, fmt, dest, config.quality, extras if fmt != "GIF" else {})
            except (OSError, ValueError) as exc:
                raise PermanentError(f"Could not encode {spec.name}.{ext}: {exc}") from exc
            outputs.append(
                ProducedFile(
                    name=f"{spec.name}.{ext}",
                    path=dest,
                    content_type=ctype,
                    width=resized.width,
                    height=resized.height,
                )
            )
            on_progress(round(len(outputs) / total * 100))

    metadata = {
        "width": img.width,
        "height": img.height,
        "format": source_format.lower(),
        "frames": frames,
    }
    return ProcessingResult(outputs=outputs, metadata=metadata)
