from celery import Celery

from app.core.config import get_settings
from app.core.logging import setup_logging

settings = get_settings()
setup_logging(settings.log_level, settings.log_json)

celery_app = Celery("mediapipe", broker=settings.broker_url, backend=settings.result_backend)
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    task_default_queue="celery",
    task_routes={
        "mediapipe.process_image": {"queue": "images"},
        "mediapipe.process_video": {"queue": "video"},
        "mediapipe.process_document": {"queue": "documents"},
        "mediapipe.deliver_webhook": {"queue": "webhooks"},
        "mediapipe.dispatch_webhooks": {"queue": "webhooks"},
    },
    # State lives in PostgreSQL; Celery results are not needed.
    task_ignore_result=True,
    result_expires=3600,
    # Acknowledge after the task finishes so a crashed worker's job is redelivered.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Must exceed the longest task, otherwise Redis redelivers it to another worker.
    broker_transport_options={"visibility_timeout": 8 * 60 * 60},
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    enable_utc=True,
    include=["app.workers.tasks"],
)
