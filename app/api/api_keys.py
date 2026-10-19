import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import Auth, Session
from app.core.errors import ConflictError, NotFoundError, error_docs
from app.db.base import utcnow
from app.db.models import ApiKey
from app.services.auth import count_active_keys, create_api_key

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreate(BaseModel):
    name: Annotated[str, Field(max_length=100, examples=["ci-pipeline"])] = ""


class ApiKeyCreated(ApiKeyOut):
    api_key: str = Field(description="Full API key. Shown once; store it securely.")


@router.get("", response_model=list[ApiKeyOut], summary="List API keys", responses=error_docs(401))
async def list_keys(auth: Auth, session: Session) -> list[ApiKey]:
    rows = await session.scalars(
        select(ApiKey).where(ApiKey.project_id == auth.project_id).order_by(ApiKey.created_at)
    )
    return list(rows)


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=201,
    summary="Create API key",
    responses=error_docs(401),
)
async def create_key(body: ApiKeyCreate, auth: Auth, session: Session) -> ApiKeyCreated:
    """Creates an additional key for rotation. The full key is returned once."""
    key, full = await create_api_key(session, auth.project_id, body.name)
    await session.commit()
    return ApiKeyCreated.model_validate(
        {**ApiKeyOut.model_validate(key).model_dump(), "api_key": full}
    )


@router.delete(
    "/{key_id}",
    status_code=204,
    summary="Revoke API key",
    responses=error_docs(401, 404, 409),
)
async def revoke_key(key_id: uuid.UUID, auth: Auth, session: Session) -> None:
    """Revokes a key immediately. The last active key of a project cannot be revoked."""
    key = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == auth.project_id)
    )
    if key is None:
        raise NotFoundError("API key not found.", code="api_key_not_found")
    if key.revoked_at is None:
        if await count_active_keys(session, auth.project_id) <= 1:
            raise ConflictError("Cannot revoke the last active API key.", code="last_api_key")
        key.revoked_at = utcnow()
        await session.commit()
