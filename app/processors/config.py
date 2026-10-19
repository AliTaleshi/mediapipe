"""Preset configuration models. Pure pydantic: shared by the API (validation) and processors."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

VariantName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]
PresetName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageVariantSpec(_Config):
    name: VariantName
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    fit: Literal["cover", "inside"] = Field(
        default="inside",
        description="`cover` crops to exactly width x height; `inside` keeps the aspect ratio.",
    )

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        if self.width is None and self.height is None:
            raise ValueError("width or height is required")
        if self.fit == "cover" and (self.width is None or self.height is None):
            raise ValueError("fit=cover needs both width and height")
        return self


def _default_image_variants() -> list[ImageVariantSpec]:
    return [
        ImageVariantSpec(name="thumb", width=200, height=200, fit="cover"),
        ImageVariantSpec(name="medium", width=800),
        ImageVariantSpec(name="large", width=1600),
    ]


class ImagePresetConfig(_Config):
    variants: list[ImageVariantSpec] = Field(
        default_factory=_default_image_variants, min_length=1, max_length=12
    )
    formats: list[Literal["webp", "original"]] = Field(
        default=["webp", "original"], min_length=1, description="Output formats per variant."
    )
    quality: int = Field(default=82, ge=1, le=100)
    strip_metadata: bool = Field(default=True, description="Drop EXIF/ICC data from outputs.")

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [v.name for v in self.variants]
        if len(set(names)) != len(names):
            raise ValueError("variant names must be unique")
        return self


class VideoPresetConfig(_Config):
    max_height: int = Field(default=1080, ge=144, le=2160)
    crf: int = Field(default=23, ge=16, le=35, description="H.264 quality (lower is better).")
    encoder_preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = (
        "veryfast"
    )
    audio_bitrate_kbps: int = Field(default=128, ge=32, le=320)
    poster_at_seconds: float = Field(default=1.0, ge=0)
    thumbnail_count: int = Field(default=3, ge=0, le=10)
    thumbnail_width: int = Field(default=320, ge=64, le=1280)


class DocumentPresetConfig(_Config):
    extract_text: bool = True
    max_text_chars: int = Field(default=10_000_000, ge=1_000)
    preview: bool = Field(default=True, description="Render a first-page preview for PDFs.")


PresetConfig = ImagePresetConfig | VideoPresetConfig | DocumentPresetConfig

CONFIG_MODELS: dict[str, type[PresetConfig]] = {
    "image": ImagePresetConfig,
    "video": VideoPresetConfig,
    "document": DocumentPresetConfig,
}


def parse_config(media_type: str, raw: dict[str, object] | None) -> PresetConfig:
    return CONFIG_MODELS[media_type].model_validate(raw or {})
