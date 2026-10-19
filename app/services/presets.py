import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnprocessableError
from app.db.models import Preset
from app.processors.config import CONFIG_MODELS, PresetConfig, parse_config

DEFAULT_PRESET = "default"


def builtin_presets() -> dict[str, PresetConfig]:
    """Built-in defaults, one per media type, available to every project."""
    return {media: model() for media, model in CONFIG_MODELS.items()}


async def resolve_preset(
    session: AsyncSession, project_id: uuid.UUID, media_type: str, name: str | None
) -> tuple[str, PresetConfig]:
    """Project preset first, then the built-in ``default``. Raises if the name is unknown."""
    name = name or DEFAULT_PRESET
    row = await session.scalar(
        select(Preset).where(
            Preset.project_id == project_id, Preset.media_type == media_type, Preset.name == name
        )
    )
    if row is not None:
        return name, parse_config(media_type, row.config)
    if name == DEFAULT_PRESET:
        return name, builtin_presets()[media_type]
    raise UnprocessableError(
        f"No {media_type} preset named '{name}'.",
        code="unknown_preset",
        details={"preset": name, "media_type": media_type},
    )
