import uuid

QUEUES = {"image": "images", "video": "video", "document": "documents"}


def enqueue_job(job_id: uuid.UUID, media_type: str) -> None:
    """Sends a job to the queue for its media type (imported lazily to avoid import cycles)."""
    from app.workers.tasks import TASKS_BY_MEDIA

    TASKS_BY_MEDIA[media_type].apply_async(args=[str(job_id)], queue=QUEUES[media_type])
