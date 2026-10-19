"""Server-Sent Events: stream a file's processing status until it reaches a final state."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models import File, FileStatus

HEARTBEAT_SECONDS = 15.0


async def snapshot(
    session: AsyncSession, project_id: uuid.UUID, file_id: uuid.UUID
) -> dict[str, Any] | None:
    session.expire_all()
    file = await session.scalar(
        select(File)
        .where(File.id == file_id, File.project_id == project_id)
        .options(selectinload(File.jobs))
    )
    if file is None:
        return None
    return {
        "file_id": str(file.id),
        "status": file.status,
        "error": file.error,
        "jobs": [
            {
                "id": str(j.id),
                "type": j.type,
                "status": j.status,
                "progress": j.progress,
                "attempts": j.attempts,
                "error": j.error,
            }
            for j in file.jobs
        ],
    }


def _frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def file_event_stream(
    sessionmaker: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    poll_interval: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    """Yields SSE frames: ``status`` whenever something changes, then ``done`` (or ``timeout``).

    Progress is read from the database, so it works across any number of API replicas without
    a pub/sub layer. Comment frames every 15s keep proxies from closing idle connections.
    """
    deadline = time.monotonic() + max_seconds
    last_sent: str | None = None
    last_beat = time.monotonic()
    async with sessionmaker() as session:
        while True:
            snap = await snapshot(session, project_id, file_id)
            if snap is None:
                yield _frame("deleted", {"file_id": str(file_id)})
                return
            encoded = json.dumps(snap, sort_keys=True)
            if encoded != last_sent:
                last_sent = encoded
                yield _frame("status", snap)
                last_beat = time.monotonic()
            if FileStatus(snap["status"]).is_terminal:
                yield _frame("done", {"file_id": snap["file_id"], "status": snap["status"]})
                return
            now = time.monotonic()
            if now >= deadline:
                yield _frame("timeout", {"file_id": snap["file_id"], "status": snap["status"]})
                return
            if now - last_beat >= HEARTBEAT_SECONDS:
                last_beat = now
                yield ": keep-alive\n\n"
            await asyncio.sleep(poll_interval)
