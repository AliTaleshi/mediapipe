import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import (
    generate_api_key,
    parse_api_key_prefix,
    verify_api_key,
)
from app.db.base import utcnow
from app.db.models import ApiKey, Project

LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True)
class AuthContext:
    project_id: uuid.UUID
    api_key_id: uuid.UUID


async def create_project(session: AsyncSession, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    await session.flush()
    return project


async def create_api_key(
    session: AsyncSession, project_id: uuid.UUID, name: str = ""
) -> tuple[ApiKey, str]:
    """Creates a key; returns the row and the plaintext key (never stored)."""
    full, prefix, key_hash = generate_api_key()
    key = ApiKey(project_id=project_id, name=name, prefix=prefix, key_hash=key_hash)
    session.add(key)
    await session.flush()
    return key, full


async def authenticate(session: AsyncSession, token: str | None) -> AuthContext:
    invalid = UnauthorizedError("Invalid or missing API key.", code="invalid_api_key")
    if not token or (prefix := parse_api_key_prefix(token)) is None:
        raise invalid
    key = await session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if key is None or key.revoked_at is not None or not verify_api_key(token, key.key_hash):
        raise invalid

    now = utcnow()
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
        key.last_used_at = now
        await session.commit()
    return AuthContext(project_id=key.project_id, api_key_id=key.id)


async def count_active_keys(session: AsyncSession, project_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(ApiKey.project_id == project_id, ApiKey.revoked_at.is_(None))
    return int(await session.scalar(stmt) or 0)
