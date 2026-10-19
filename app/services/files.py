import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.models import File
from app.schemas.files import FileOut
from app.services.storage import Storage


async def get_owned_file(
    session: AsyncSession, project_id: uuid.UUID, file_id: uuid.UUID, *, with_children: bool = False
) -> File:
    """Fetches a file by id *within a project*. Other projects' files look like missing ones."""
    stmt = select(File).where(File.id == file_id, File.project_id == project_id)
    if with_children:
        stmt = stmt.options(selectinload(File.variants), selectinload(File.jobs))
    file = await session.scalar(stmt)
    if file is None:
        raise NotFoundError("File not found.", code="file_not_found")
    return file


def to_file_out(file: File, storage: Storage, settings: Settings) -> FileOut:
    """Serialises a file (with ``variants`` and ``jobs`` loaded) plus presigned variant URLs."""
    out = FileOut.model_validate(file)
    for variant_out, variant in zip(out.variants, file.variants, strict=True):
        variant_out.url = storage.presign_get(
            variant.storage_key,
            settings.download_url_expires_seconds,
            filename=f"{file.id}-{variant.name}",
            content_type=variant.content_type,
        )
    return out
