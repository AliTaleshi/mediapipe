from app.core.config import get_settings
from app.services.maintenance import cleanup_abandoned_uploads
from app.services.storage import get_storage
from app.workers.celery_app import celery_app


@celery_app.task(name="mediapipe.cleanup_abandoned_uploads")
def cleanup_abandoned_uploads_task() -> int:
    return cleanup_abandoned_uploads(get_storage(), get_settings())
