"""Upload orchestration: create presigned uploads, then verify and hand off to processing."""

import asyncio
import math
import os
import re
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, UnprocessableError
from app.db.base import utcnow
from app.db.models import File, FileStatus, Job, JobStatus
from app.schemas.uploads import (
    MultipartUpload,
    PartUpload,
    SingleUpload,
    UploadComplete,
    UploadCreate,
    UploadCreated,
)
from app.services.filetypes import (
    ALLOWED_TYPES,
    DOCX,
    SNIFF_BYTES,
    is_docx,
    normalize_content_type,
    sniff_bytes,
)
from app.services.presets import resolve_preset
from app.services.storage import ObjectNotFoundError, Storage, StorageError

MAX_MULTIPART_PARTS = 10_000
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def clean_filename(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/"))
    return _CONTROL_CHARS.sub("", base).strip()[:512] or "file"


def original_key(project_id: uuid.UUID, file_id: uuid.UUID, extension: str) -> str:
    return f"projects/{project_id}/files/{file_id}/original.{extension}"


def plan_parts(size: int, part_size: int) -> tuple[int, int]:
    """Returns (part_size, part_count), growing the part size to stay within S3's 10,000 limit."""
    part_size = max(part_size, math.ceil(size / MAX_MULTIPART_PARTS))
    return part_size, math.ceil(size / part_size)


async def create_upload(
    session: AsyncSession,
    storage: Storage,
    settings: Settings,
    project_id: uuid.UUID,
    req: UploadCreate,
) -> UploadCreated:
    content_type = normalize_content_type(req.content_type)
    file_type = ALLOWED_TYPES.get(content_type)
    if file_type is None:
        raise UnprocessableError(
            f"Content type '{content_type}' is not supported.",
            code="unsupported_type",
            details={"allowed": sorted(ALLOWED_TYPES)},
        )
    media_type = file_type.media_type
    limit = settings.max_bytes_for(media_type)
    if req.size > limit:
        raise AppError(
            f"{media_type.capitalize()} files may be at most {limit} bytes.",
            code="file_too_large",
            status_code=413,
            details={"max_bytes": limit, "size": req.size},
        )
    preset_name, _ = await resolve_preset(session, project_id, media_type, req.preset)

    file_id = uuid.uuid4()
    key = original_key(project_id, file_id, file_type.extension)
    expires = settings.upload_url_expires_seconds
    expires_at = utcnow() + timedelta(seconds=expires)
    file = File(
        id=file_id,
        project_id=project_id,
        original_name=clean_filename(req.filename),
        content_type=content_type,
        media_type=media_type,
        size=req.size,
        storage_key=key,
        preset=preset_name,
    )

    if req.size > settings.multipart_threshold_bytes:
        part_size, count = plan_parts(req.size, settings.multipart_part_size_bytes)
        upload_id = await asyncio.to_thread(storage.create_multipart, key, content_type)
        file.multipart_upload_id = upload_id
        parts = [
            PartUpload(part_number=n, url=storage.presign_part(key, upload_id, n, expires))
            for n in range(1, count + 1)
        ]
        response = UploadCreated(
            file_id=file_id,
            mode="multipart",
            expires_at=expires_at,
            multipart=MultipartUpload(part_size=part_size, parts=parts),
        )
    else:
        url = storage.presign_put(key, content_type, expires)
        response = UploadCreated(
            file_id=file_id,
            mode="single",
            expires_at=expires_at,
            upload=SingleUpload(url=url, headers={"Content-Type": content_type}),
        )
    session.add(file)
    await session.commit()
    return response


def detect_stored_type(storage: Storage, key: str, size: int, max_zip_bytes: int) -> str | None:
    """Sniffs the uploaded object. ZIP containers are opened to tell DOCX from other archives."""
    head = storage.read_range(key, 0, min(size, SNIFF_BYTES) - 1)
    detected = sniff_bytes(head)
    if detected != "application/zip" or size > max_zip_bytes:
        return detected
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.zip"
        storage.download(key, path)
        return DOCX if is_docx(path) else "application/zip"


async def _reject(
    session: AsyncSession,
    storage: Storage,
    file: File,
    code: str,
    message: str,
    details: dict[str, object],
) -> UnprocessableError:
    file.status = FileStatus.REJECTED
    file.error = message
    await session.commit()
    await asyncio.to_thread(storage.delete, [file.storage_key])
    return UnprocessableError(message, code=code, details=details)


async def _finalize_multipart(storage: Storage, file: File, body: UploadComplete) -> None:
    assert file.multipart_upload_id is not None
    if not body.parts:
        raise UnprocessableError(
            "Multipart uploads need the list of uploaded parts.", code="parts_required"
        )
    numbers = [p.part_number for p in body.parts]
    if len(set(numbers)) != len(numbers):
        raise UnprocessableError("Duplicate part numbers.", code="invalid_parts")
    parts = sorted((p.part_number, p.etag) for p in body.parts)
    try:
        await asyncio.to_thread(
            storage.complete_multipart, file.storage_key, file.multipart_upload_id, parts
        )
    except ObjectNotFoundError:
        pass  # already finalized by an earlier attempt; the size check below decides
    except StorageError as exc:
        if exc.__class__ is not StorageError:
            raise  # transient problems surface as 503
        raise UnprocessableError(
            "Storage could not assemble the parts.", code="invalid_parts"
        ) from exc


async def complete_upload(
    session: AsyncSession,
    storage: Storage,
    settings: Settings,
    file: File,
    body: UploadComplete,
) -> Job | None:
    """Verifies the stored object and creates the processing job.

    Returns the new job, or ``None`` when the upload had already been completed (idempotent).
    """
    if file.status == FileStatus.REJECTED:
        raise ConflictError("This upload was rejected.", code="upload_rejected")
    if file.status != FileStatus.PENDING_UPLOAD:
        return None

    if file.multipart_upload_id:
        await _finalize_multipart(storage, file, body)

    actual_size = await asyncio.to_thread(storage.head, file.storage_key)
    if actual_size is None:
        raise ConflictError("The file has not been uploaded yet.", code="upload_not_found")
    if actual_size != file.size:
        raise await _reject(
            session,
            storage,
            file,
            "size_mismatch",
            f"Uploaded size ({actual_size}) does not match the declared size ({file.size}).",
            {"declared": file.size, "actual": actual_size},
        )

    max_zip = settings.max_document_bytes
    detected = await asyncio.to_thread(
        detect_stored_type, storage, file.storage_key, actual_size, max_zip
    )
    if detected != file.content_type:
        raise await _reject(
            session,
            storage,
            file,
            "type_mismatch",
            "The file content does not match the declared content type.",
            {"declared": file.content_type, "detected": detected},
        )

    _, config = await resolve_preset(session, file.project_id, file.media_type, file.preset)
    job = Job(
        file_id=file.id,
        type=file.media_type,
        status=JobStatus.QUEUED,
        config=config.model_dump(),
    )
    # Claim the transition atomically so concurrent "complete" calls create only one job.
    claimed = cast(
        CursorResult[tuple[()]],
        await session.execute(
            update(File)
            .where(File.id == file.id, File.status == FileStatus.PENDING_UPLOAD)
            .values(
                status=FileStatus.UPLOADED,
                detected_type=detected,
                multipart_upload_id=None,
                updated_at=utcnow(),
            )
        ),
    )
    if claimed.rowcount == 0:
        await session.rollback()
        return None
    session.add(job)
    await session.commit()
    return job
