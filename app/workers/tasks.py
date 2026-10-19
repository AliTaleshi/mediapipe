from __future__ import annotations

import uuid
from typing import Any

from celery import Task

from app.core.config import get_settings
from app.services.job_runner import RetryRequested, run_job
from app.services.storage import get_storage
from app.workers.celery_app import celery_app

# Attempt counting lives in the database (jobs.attempts), so Celery's own retry limit is off.
TASK_OPTIONS: dict[str, Any] = {"bind": True, "max_retries": None}


def _run(task: Task[Any, Any], job_id: str) -> None:
    settings, storage = get_settings(), get_storage()
    while True:
        try:
            run_job(uuid.UUID(job_id), storage, settings)
            return
        except RetryRequested as retry:
            if task.request.is_eager:
                continue  # Celery's eager mode cannot schedule retries; loop instead (tests)
            raise task.retry(countdown=retry.countdown, exc=retry) from retry


@celery_app.task(name="mediapipe.process_image", **TASK_OPTIONS)
def process_image_task(self: Task[Any, Any], job_id: str) -> None:
    _run(self, job_id)


@celery_app.task(name="mediapipe.process_video", **TASK_OPTIONS)
def process_video_task(self: Task[Any, Any], job_id: str) -> None:
    _run(self, job_id)


@celery_app.task(name="mediapipe.process_document", **TASK_OPTIONS)
def process_document_task(self: Task[Any, Any], job_id: str) -> None:
    _run(self, job_id)


TASKS_BY_MEDIA: dict[str, Any] = {
    "image": process_image_task,
    "video": process_video_task,
    "document": process_document_task,
}
