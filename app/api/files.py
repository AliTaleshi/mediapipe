import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import Auth, Session
from app.core.config import Settings, get_settings
from app.core.errors import error_docs
from app.schemas.files import FileOut
from app.services.files import get_owned_file, to_file_out
from app.services.storage import Storage, get_storage

router = APIRouter(prefix="/v1/files", tags=["files"])


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
