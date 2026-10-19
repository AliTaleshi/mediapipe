import uuid

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import Auth, Session
from app.core.errors import ConflictError, NotFoundError, error_docs
from app.db.models import Preset
from app.schemas.presets import PresetCreate, PresetOut
from app.services.presets import DEFAULT_PRESET, builtin_presets

router = APIRouter(prefix="/v1/presets", tags=["presets"])
MAX_PRESETS_PER_PROJECT = 50


def _out(p: Preset) -> PresetOut:
    return PresetOut(
        id=p.id,
        name=p.name,
        media_type=p.media_type,
        builtin=False,
        config=p.config,
        created_at=p.created_at,
    )


@router.get("", response_model=list[PresetOut], summary="List presets", responses=error_docs(401))
async def list_presets(auth: Auth, session: Session) -> list[PresetOut]:
    """Built-in `default` presets (one per media type) plus this project's own presets. A project
    preset named `default` overrides the built-in one for its media type."""
    rows = await session.scalars(
        select(Preset).where(Preset.project_id == auth.project_id).order_by(Preset.created_at)
    )
    custom = [_out(p) for p in rows]
    overridden = {(p.media_type, p.name) for p in custom}
    builtin = [
        PresetOut(
            id=None,
            name=DEFAULT_PRESET,
            media_type=media,
            builtin=True,
            config=cfg.model_dump(),
        )
        for media, cfg in builtin_presets().items()
        if (media, DEFAULT_PRESET) not in overridden
    ]
    return [*builtin, *custom]


@router.post(
    "",
    response_model=PresetOut,
    status_code=201,
    summary="Create a preset",
    responses=error_docs(401, 409, 422),
)
async def create_preset(body: PresetCreate, auth: Auth, session: Session) -> PresetOut:
    """Stores a named processing configuration. Reference it with `preset` when creating an
    upload. Jobs snapshot the configuration when they are created, so later edits or deletions
    never change files already queued."""
    existing = await session.scalar(
        select(Preset.id).where(
            Preset.project_id == auth.project_id,
            Preset.media_type == body.media_type,
            Preset.name == body.name,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A {body.media_type} preset named '{body.name}' already exists.",
            code="preset_exists",
        )
    count = await session.scalar(
        select(func.count()).select_from(Preset).where(Preset.project_id == auth.project_id)
    )
    if (count or 0) >= MAX_PRESETS_PER_PROJECT:
        raise ConflictError(
            f"At most {MAX_PRESETS_PER_PROJECT} presets per project.", code="preset_limit"
        )
    preset = Preset(
        project_id=auth.project_id, name=body.name, media_type=body.media_type, config=body.config
    )
    session.add(preset)
    await session.commit()
    return _out(preset)


@router.delete(
    "/{preset_id}", status_code=204, summary="Delete a preset", responses=error_docs(401, 404)
)
async def delete_preset(preset_id: uuid.UUID, auth: Auth, session: Session) -> None:
    preset = await session.scalar(
        select(Preset).where(Preset.id == preset_id, Preset.project_id == auth.project_id)
    )
    if preset is None:
        raise NotFoundError("Preset not found.", code="preset_not_found")
    await session.delete(preset)
    await session.commit()
