"""Webhook signing, payloads and delivery.

Signature scheme (similar to Stripe's): the ``X-MediaPipe-Signature`` header is
``t=<unix timestamp>,v1=<hex HMAC-SHA256>`` where the HMAC covers ``"<timestamp>.<raw body>"``
using the endpoint's secret. Receivers must recompute it over the raw bytes and reject
timestamps outside a tolerance window (default 5 minutes) to prevent replays.
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import UnprocessableError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import File, WebhookDelivery, WebhookEndpoint
from app.db.session import sync_session

log = get_logger(__name__)

SIGNATURE_HEADER = "X-MediaPipe-Signature"
EVENTS = ("file.processed", "file.failed")


# --- signing ------------------------------------------------------------------------------


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute_signature(secret, ts, body)}"


def verify_signature(
    secret: str, header: str, body: bytes, *, tolerance_seconds: int = 300, now: float | None = None
) -> bool:
    """What a receiving app should do. Constant-time compare plus a replay window."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        supplied = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    return hmac.compare_digest(supplied, compute_signature(secret, timestamp, body))


# --- URL safety ---------------------------------------------------------------------------


def _is_public_ip(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_global


def validate_webhook_url(url: str, *, allow_private: bool) -> None:
    """Static checks at registration time. Hostnames are resolved again at delivery time."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnprocessableError("Webhook URL must be an http(s) URL.", code="invalid_webhook_url")
    if parts.username or parts.password:
        raise UnprocessableError(
            "Webhook URL must not contain credentials.", code="invalid_webhook_url"
        )
    if allow_private:
        return
    host = parts.hostname
    try:
        blocked = not _is_public_ip(host)
    except ValueError:  # not an IP literal
        blocked = host.lower() == "localhost" or host.lower().endswith(".localhost")
    if blocked:
        raise UnprocessableError(
            "Webhook URL must not point at a private or local address.",
            code="invalid_webhook_url",
        )


def resolves_to_private(host: str) -> bool:
    """True if any address the host resolves to is not publicly routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # let the HTTP client report the DNS failure
    return any(not _is_public_ip(str(info[4][0])) for info in infos)


# --- payloads -----------------------------------------------------------------------------


def build_payload(event: str, file: File) -> dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "event": event,
        "created_at": utcnow().isoformat(),
        "data": {
            "file": {
                "id": str(file.id),
                "original_name": file.original_name,
                "media_type": file.media_type,
                "content_type": file.detected_type or file.content_type,
                "size": file.size,
                "status": file.status,
                "error": file.error,
                "metadata": file.meta,
                "variants": [
                    {
                        "name": v.name,
                        "content_type": v.content_type,
                        "size": v.size,
                        "width": v.width,
                        "height": v.height,
                        "duration": v.duration,
                    }
                    for v in file.variants
                ],
            }
        },
    }


def endpoints_for_event(project_id: uuid.UUID, event: str) -> list[uuid.UUID]:
    with sync_session() as s:
        rows = s.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.project_id == project_id, WebhookEndpoint.active.is_(True)
            )
        )
        return [e.id for e in rows if event in e.events]


def prepare_event(file_id: uuid.UUID, event: str) -> tuple[dict[str, Any], list[uuid.UUID]] | None:
    """Builds a file event payload and finds subscribed endpoints (None if nothing to do)."""
    from sqlalchemy.orm import selectinload

    with sync_session() as s:
        file = s.scalar(select(File).where(File.id == file_id).options(selectinload(File.variants)))
        if file is None:
            return None
        project_id, payload = file.project_id, build_payload(event, file)
    endpoint_ids = endpoints_for_event(project_id, event)
    return (payload, endpoint_ids) if endpoint_ids else None


# --- delivery -----------------------------------------------------------------------------


def build_http_client(settings: Settings) -> httpx.Client:
    """Overridable in tests (inject a MockTransport)."""
    return httpx.Client(timeout=settings.webhook_timeout_seconds, follow_redirects=False)


@dataclass(frozen=True)
class AttemptResult:
    delivery_id: uuid.UUID
    success: bool
    retry: bool
    status_code: int | None
    error: str | None


def attempt_delivery(
    endpoint_id: uuid.UUID, payload: dict[str, Any], attempt: int, settings: Settings
) -> AttemptResult | None:
    """Sends one signed POST and logs the attempt. Returns None if the endpoint is gone/inactive."""
    with sync_session() as s:
        endpoint = s.get(WebhookEndpoint, endpoint_id)
        if endpoint is None or not endpoint.active:
            return None
        url, secret = endpoint.url, endpoint.secret

    event = str(payload["event"])
    body = json.dumps(payload, separators=(",", ":")).encode()
    delivery_id = uuid.uuid4()
    status_code: int | None = None
    error: str | None = None
    retry = True
    started = time.monotonic()

    host = urlsplit(url).hostname or ""
    if not settings.webhook_allow_private_urls and resolves_to_private(host):
        error, retry = "Blocked: URL resolves to a private address.", False
    else:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MediaPipe-Webhooks/1.0",
            SIGNATURE_HEADER: signature_header(secret, body),
            "X-MediaPipe-Event": event,
            "X-MediaPipe-Delivery": str(delivery_id),
        }
        try:
            with build_http_client(settings) as client:
                response = client.post(url, content=body, headers=headers)
            status_code = response.status_code
            if not 200 <= status_code < 300:
                error = f"Receiver responded with HTTP {status_code}"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]

    success = error is None
    retry = retry and not success and attempt < settings.webhook_max_attempts
    now = utcnow()
    with sync_session() as s:
        s.add(
            WebhookDelivery(
                id=delivery_id,
                endpoint_id=endpoint_id,
                event=event,
                payload=payload,
                status_code=status_code,
                attempt=attempt,
                success=success,
                error=error,
                duration_ms=round((time.monotonic() - started) * 1000),
                delivered_at=now if success else None,
            )
        )
    log.info("webhook %s attempt %d -> %s", delivery_id, attempt, status_code or error)
    return AttemptResult(delivery_id, success, retry, status_code, error)
