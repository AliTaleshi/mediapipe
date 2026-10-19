from __future__ import annotations

import uuid
from typing import Any

from app.core.backoff import backoff_delay
from app.core.config import get_settings
from app.services.webhooks import attempt_delivery, prepare_event
from app.workers.celery_app import celery_app


@celery_app.task(name="mediapipe.dispatch_webhooks")
def dispatch_webhooks(file_id: str, event: str) -> None:
    """Builds the payload once and queues one delivery per subscribed endpoint."""
    prepared = prepare_event(uuid.UUID(file_id), event)
    if prepared is None:
        return
    payload, endpoint_ids = prepared
    for endpoint_id in endpoint_ids:
        deliver_webhook.apply_async(args=(str(endpoint_id), payload, 1), queue="webhooks")


@celery_app.task(name="mediapipe.deliver_webhook")
def deliver_webhook(endpoint_id: str, payload: dict[str, Any], attempt: int = 1) -> None:
    """One delivery attempt. Failures schedule the next attempt with exponential backoff."""
    settings = get_settings()
    result = attempt_delivery(uuid.UUID(endpoint_id), payload, attempt, settings)
    if result is not None and result.retry:
        delay = backoff_delay(
            attempt, settings.webhook_retry_base_seconds, settings.webhook_retry_base_seconds * 360
        )
        deliver_webhook.apply_async(
            args=(endpoint_id, payload, attempt + 1), countdown=delay, queue="webhooks"
        )
