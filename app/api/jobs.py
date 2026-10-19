import asyncio
import uuid

from fastapi import APIRouter
from sqlalchemy import select, update

from app.api.deps import Auth, Session
from app.core.errors import ConflictError, NotFoundError, error_docs
from app.db.models import File, FileStatus, Job, JobStatus
from app.schemas.files import JobOut
from app.services.jobs import enqueue_job

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


async def _get_job(session: Session, project_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = await session.scalar(
        select(Job).join(File).where(Job.id == job_id, File.project_id == project_id)
    )
    if job is None:
        raise NotFoundError("Job not found.", code="job_not_found")
    return job


@router.get(
    "/{job_id}", response_model=JobOut, summary="Get job status", responses=error_docs(401, 404)
)
async def get_job(job_id: uuid.UUID, auth: Auth, session: Session) -> Job:
    """Poll this endpoint for `status` (`queued → processing → succeeded | failed`) and
    `progress` (0-100). For push updates use `GET /v1/files/{file_id}/events`."""
    return await _get_job(session, auth.project_id, job_id)


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    summary="Retry a failed job",
    responses=error_docs(401, 404, 409),
)
async def retry_job(job_id: uuid.UUID, auth: Auth, session: Session) -> Job:
    """Re-queues a `failed` job with a fresh attempt budget."""
    job = await _get_job(session, auth.project_id, job_id)
    claimed = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.FAILED)
        .values(status=JobStatus.QUEUED, attempts=0, progress=0, error=None, finished_at=None)
    )
    if claimed.rowcount == 0:  # type: ignore[attr-defined]
        raise ConflictError("Only failed jobs can be retried.", code="job_not_failed")
    await session.execute(
        update(File).where(File.id == job.file_id).values(status=FileStatus.PROCESSING, error=None)
    )
    await session.commit()
    await asyncio.to_thread(enqueue_job, job.id, job.type)
    session.expire_all()
    return await _get_job(session, auth.project_id, job_id)
