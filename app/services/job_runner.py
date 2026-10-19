"""Runs one processing job: download -> process -> upload variants -> record result.

Synchronous on purpose (it runs inside Celery workers). Safe to run twice for the same job:
variants are keyed by (file, name) and written to deterministic storage keys, so a repeat
run overwrites instead of duplicating.
"""

import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.backoff import backoff_delay
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import File, FileStatus, Job, JobStatus, Variant
from app.db.session import sync_session
from app.processors.config import parse_config
from app.processors.document import process_document
from app.processors.errors import PermanentError
from app.processors.image import process_image
from app.processors.types import ProcessingResult, ProcessorInput, ProgressCallback
from app.processors.video import process_video
from app.services.storage import Storage, TransientStorageError

log = get_logger(__name__)

# Overall progress budget: download 0-5, processing 5-90, upload 90-99, done 100.
DOWNLOAD_END, PROCESS_END, UPLOAD_END = 5, 90, 99

FileEvent = tuple[str, uuid.UUID]  # (webhook event name, file id)
Processor = Callable[[ProcessorInput, Any, ProgressCallback], ProcessingResult]
PROCESSORS: dict[str, Processor] = {
    "image": process_image,
    "document": process_document,
    "video": process_video,
}


class RetryRequested(Exception):
    """Raised after a transient failure; the task should re-run after ``countdown`` seconds."""

    def __init__(self, countdown: float, reason: str) -> None:
        super().__init__(reason)
        self.countdown = countdown


def variant_key(file: File, name: str) -> str:
    return f"projects/{file.project_id}/files/{file.id}/variants/{name}"


def refresh_file_status(file: File) -> str | None:
    """Derives the file status from its jobs. Returns a webhook event on a terminal transition."""
    jobs = file.jobs
    previous = file.status
    if any(j.status == JobStatus.FAILED for j in jobs):
        file.status = FileStatus.FAILED
        file.error = next(j.error for j in jobs if j.status == JobStatus.FAILED)
    elif jobs and all(j.status == JobStatus.SUCCEEDED for j in jobs):
        file.status = FileStatus.PROCESSED
        file.error = None
    else:
        file.status = FileStatus.PROCESSING
    if file.status == previous:
        return None
    return {FileStatus.PROCESSED: "file.processed", FileStatus.FAILED: "file.failed"}.get(
        FileStatus(file.status)
    )


def _set_progress(job_id: uuid.UUID, value: int) -> None:
    with sync_session() as s:
        s.execute(update(Job).where(Job.id == job_id, Job.progress < value).values(progress=value))


def _progress_reporter(job_id: uuid.UUID, start: int, end: int) -> ProgressCallback:
    """Maps a processor's 0-100 onto [start, end], writing to the DB at most ~2x per second."""
    state = {"last_value": -1, "last_time": 0.0}

    def report(pct: int) -> None:
        value = start + round((end - start) * max(0, min(pct, 100)) / 100)
        now = time.monotonic()
        if value == state["last_value"] or (value < end and now - state["last_time"] < 0.5):
            return
        state["last_value"], state["last_time"] = value, now
        _set_progress(job_id, value)

    return report


def _begin(job_id: uuid.UUID) -> tuple[uuid.UUID, dict[str, Any], str, str, int] | None:
    """Marks the job as processing. Returns what the worker needs, or None to skip."""
    with sync_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            log.warning("job %s no longer exists", job_id)
            return None
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            log.info("job %s already %s; skipping duplicate delivery", job_id, job.status)
            return None
        file = s.get(File, job.file_id)
        if file is None:
            return None
        job.status = JobStatus.PROCESSING
        job.attempts += 1
        job.started_at = job.started_at or utcnow()
        job.error = None
        job.progress = 0
        file.status = FileStatus.PROCESSING
        return file.id, job.config, job.type, file.content_type, job.attempts


def _finish(job_id: uuid.UUID, error: str | None) -> FileEvent | None:
    """Records the terminal state. Returns the webhook event to emit, if any."""
    with sync_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        job.status = JobStatus.FAILED if error else JobStatus.SUCCEEDED
        job.error = error
        job.finished_at = utcnow()
        if not error:
            job.progress = 100
        file = s.get(File, job.file_id)
        if file is None:
            return None
        s.refresh(file, ["jobs"])
        event = refresh_file_status(file)
        return (event, file.id) if event else None


def _store_results(
    s: Session, storage: Storage, file: File, result: ProcessingResult, on_upload: ProgressCallback
) -> None:
    """Uploads outputs and upserts variants by (file, name); removes variants no longer produced."""
    existing = {v.name: v for v in s.scalars(select(Variant).where(Variant.file_id == file.id))}
    total = max(len(result.outputs), 1)
    for i, out in enumerate(result.outputs, 1):
        key = variant_key(file, out.name)
        storage.upload(out.path, key, out.content_type)
        variant = existing.pop(out.name, None) or Variant(file_id=file.id, name=out.name)
        variant.content_type = out.content_type
        variant.size = out.path.stat().st_size
        variant.width, variant.height, variant.duration = out.width, out.height, out.duration
        variant.storage_key = key
        s.add(variant)
        on_upload(round(i / total * 100))
    stale = list(existing.values())
    if stale:
        storage.delete([v.storage_key for v in stale])
        for v in stale:
            s.delete(v)
    file.meta = {**file.meta, **result.metadata}


def _process(
    job_id: uuid.UUID,
    file_id: uuid.UUID,
    job_type: str,
    content_type: str,
    config: dict[str, Any],
    storage: Storage,
) -> bool:
    """Does the actual work. Returns False if the file vanished mid-flight."""
    with tempfile.TemporaryDirectory(prefix="mediapipe-") as tmp:
        workdir = Path(tmp)
        (workdir / "out").mkdir()
        with sync_session() as s:
            file = s.get(File, file_id)
            if file is None:
                return False
            storage_key = file.storage_key
        src = workdir / "source"
        storage.download(storage_key, src)
        _set_progress(job_id, DOWNLOAD_END)

        result = PROCESSORS[job_type](
            ProcessorInput(src, content_type, workdir / "out"),
            parse_config(job_type, config),
            _progress_reporter(job_id, DOWNLOAD_END, PROCESS_END),
        )
        with sync_session() as s:
            file = s.get(File, file_id)
            if file is None:
                return False
            _store_results(
                s, storage, file, result, _progress_reporter(job_id, PROCESS_END, UPLOAD_END)
            )
    return True


def run_job(job_id: uuid.UUID, storage: Storage, settings: Settings) -> FileEvent | None:
    """Runs a job once. Returns (webhook event, file id) when the file reached a terminal state.

    Raises ``RetryRequested`` for transient failures while attempts remain.
    """
    started = _begin(job_id)
    if started is None:
        return None
    file_id, config, job_type, content_type, attempt = started
    try:
        ok = _process(job_id, file_id, job_type, content_type, config, storage)
        if not ok:
            log.info("file %s deleted while job %s ran", file_id, job_id)
            return None
        return _finish(job_id, None)
    except TransientStorageError as exc:
        if attempt < settings.job_max_attempts:
            delay = backoff_delay(
                attempt, settings.job_retry_base_seconds, settings.job_retry_max_seconds
            )
            with sync_session() as s:
                s.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(status=JobStatus.QUEUED, error=f"Transient error: {exc}")
                )
            log.warning(
                "job %s attempt %d failed transiently; retry in %.1fs", job_id, attempt, delay
            )
            raise RetryRequested(delay, str(exc)) from exc
        return _finish(job_id, f"Gave up after {attempt} attempts: {exc}")
    except PermanentError as exc:
        return _finish(job_id, str(exc))
    except Exception as exc:
        log.exception("job %s failed unexpectedly", job_id)
        return _finish(job_id, f"Unexpected processing error ({type(exc).__name__}).")
