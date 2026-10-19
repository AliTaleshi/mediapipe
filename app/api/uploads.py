import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends

from app.api.deps import Auth, Session
from app.core.config import Settings, get_settings
from app.core.errors import error_docs
from app.schemas.uploads import UploadComplete, UploadCompleted, UploadCreate, UploadCreated
from app.services import uploads as svc
from app.services.files import get_owned_file, to_file_out
from app.services.jobs import enqueue_job
from app.services.storage import Storage, get_storage

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])


@router.post(
    "",
    response_model=UploadCreated,
    status_code=201,
    summary="Create an upload",
    responses=error_docs(401, 413, 422),
)
async def create_upload(
    body: Annotated[
        UploadCreate,
        Body(
            examples=[
                {
                    "filename": "holiday.jpg",
                    "content_type": "image/jpeg",
                    "size": 482113,
                    "preset": "default",
                }
            ]
        ),
    ],
    auth: Auth,
    session: Session,
    storage: Annotated[Storage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadCreated:
    """Registers a file and returns a presigned **PUT** URL (valid 15 minutes).

    Files over 100 MB get `mode: "multipart"` with one presigned URL per part. Upload the bytes
    directly to storage, then call `POST /v1/uploads/{file_id}/complete`.
    """
    return await svc.create_upload(session, storage, settings, auth.project_id, body)


@router.post(
    "/{file_id}/complete",
    response_model=UploadCompleted,
    summary="Confirm an upload and start processing",
    responses=error_docs(401, 404, 409, 422),
)
async def complete_upload(
    file_id: uuid.UUID,
    auth: Auth,
    session: Session,
    storage: Annotated[Storage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[UploadComplete | None, Body()] = None,
) -> UploadCompleted:
    """Checks that the object exists with the declared size, sniffs its real type, and queues
    processing. Calling it again is safe: it returns the current state without a second job.

    For multipart uploads send `{"parts": [{"part_number": 1, "etag": "..."}]}`.
    """
    file = await get_owned_file(session, auth.project_id, file_id)
    job = await svc.complete_upload(session, storage, settings, file, body or UploadComplete())
    if job is not None:
        await asyncio.to_thread(enqueue_job, job.id, file.media_type)
    session.expire_all()
    file = await get_owned_file(session, auth.project_id, file_id, with_children=True)
    out = to_file_out(file, storage, settings)
    return UploadCompleted(file=out, jobs=out.jobs)
