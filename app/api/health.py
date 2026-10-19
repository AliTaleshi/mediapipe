import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session
from app.services.redis import get_redis
from app.services.storage import Storage, get_storage

router = APIRouter(tags=["health"])
log = get_logger(__name__)


class HealthStatus(BaseModel):
    status: str


class ReadyStatus(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/health", response_model=HealthStatus, summary="Liveness probe")
async def health() -> HealthStatus:
    """Returns 200 as long as the process is up. Does not touch dependencies."""
    return HealthStatus(status="ok")


@router.get(
    "/ready",
    response_model=ReadyStatus,
    summary="Readiness probe",
    responses={503: {"model": ReadyStatus, "description": "A dependency is unavailable"}},
)
async def ready(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    storage: Storage = Depends(get_storage),
) -> JSONResponse:
    """Checks PostgreSQL, Redis and the storage bucket."""
    checks: dict[str, str] = {}

    async def run(name: str, coro_fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await coro_fn()
            checks[name] = "ok"
        except Exception as exc:
            log.warning("readiness check %s failed: %s", name, exc)
            checks[name] = "unavailable"

    async def db() -> None:
        await session.execute(text("SELECT 1"))

    async def redis_ping() -> None:
        await redis.ping()

    async def s3() -> None:
        await asyncio.to_thread(storage.check)

    await asyncio.gather(run("database", db), run("redis", redis_ping), run("storage", s3))
    ok = all(v == "ok" for v in checks.values())
    body = ReadyStatus(status="ok" if ok else "unavailable", checks=checks)
    return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())
