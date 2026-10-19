import asyncio
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import Auth, Session
from app.core.config import Settings, get_settings
from app.core.errors import error_docs
from app.db.models import File
from app.db.session import get_sessionmaker
from app.schemas.files import FileList, FileOut, FileSummary
from app.services.events import file_event_stream
from app.services.files import get_owned_file, to_file_out
from app.services.storage import Storage, get_storage

router = APIRouter(prefix="/v1/files", tags=["files"])


@router.get(
    "/{file_id}/events",
    summary="Stream processing progress (SSE)",
    response_class=StreamingResponse,
    responses={
        **error_docs(401, 404),
        200: {
            "description": "`text/event-stream` of `status` events, ending with `done`.",
            "content": {
                "text/event-stream": {
                    "example": 'event: status\ndata: {"file_id":"...","status":"processing",'
                    '"jobs":[{"type":"video","status":"processing","progress":42}]}\n\n'
                }
            },
        },
    },
)
async def file_events(
    file_id: uuid.UUID,
    auth: Auth,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    """Server-Sent Events: a `status` event on every change of file or job state (including
    `progress`), then `done`. Send the API key in the `Authorization` header (use `fetch`, not
    the browser `EventSource`, which cannot set headers)."""
    await get_owned_file(session, auth.project_id, file_id)  # 404 before the stream starts
    stream = file_event_stream(
        get_sessionmaker(),
        auth.project_id,
        file_id,
        poll_interval=settings.sse_poll_interval_seconds,
        max_seconds=settings.sse_max_seconds,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/{file_id}",
    response_model=FileOut,
    summary="Get file details",
    responses=error_docs(401, 404),
)
async def get_file(
    file_id: uuid.UUID,
    auth: Auth,
    session: Session,
    storage: Annotated[Storage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileOut:
    """File metadata, status, jobs and every variant with a presigned download URL (1 hour)."""
    file = await get_owned_file(session, auth.project_id, file_id, with_children=True)
    return to_file_out(file, storage, settings)


@router.get(
    "",
    response_model=FileList,
    summary="List files",
    responses=error_docs(401, 422),
)
async def list_files(
    auth: Auth,
    session: Session,
    status: Annotated[
        Literal["pending_upload", "uploaded", "processing", "processed", "failed", "rejected"]
        | None,
        Query(description="Filter by file status."),
    ] = None,
    type: Annotated[
        Literal["image", "video", "document"] | None, Query(description="Filter by media type.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileList:
    """Files of the authenticated project, newest first."""
    base = select(File).where(File.project_id == auth.project_id)
    if status:
        base = base.where(File.status == status)
    if type:
        base = base.where(File.media_type == type)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.order_by(File.created_at.desc(), File.id).limit(limit).offset(offset)
    )
    return FileList(
        items=[FileSummary.model_validate(f) for f in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/{file_id}",
    status_code=204,
    summary="Delete a file",
    responses=error_docs(401, 404),
)
async def delete_file(
    file_id: uuid.UUID,
    auth: Auth,
    session: Session,
    storage: Annotated[Storage, Depends(get_storage)],
) -> None:
    """Deletes the file, all its variants and their storage objects. Storage is cleaned first,
    so if storage is unavailable nothing is deleted and the call can simply be repeated."""
    file = await get_owned_file(session, auth.project_id, file_id, with_children=True)
    keys = [file.storage_key, *(v.storage_key for v in file.variants)]
    if file.multipart_upload_id:
        await asyncio.to_thread(storage.abort_multipart, file.storage_key, file.multipart_upload_id)
    await asyncio.to_thread(storage.delete, keys)
    await session.delete(file)
    await session.commit()
