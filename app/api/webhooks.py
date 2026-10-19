import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import Auth, Session
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, error_docs
from app.core.security import generate_webhook_secret
from app.db.models import WebhookDelivery, WebhookEndpoint
from app.schemas.webhooks import (
    DeliveryList,
    DeliveryOut,
    ResendOut,
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
)
from app.services.webhooks import validate_webhook_url

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
MAX_ENDPOINTS_PER_PROJECT = 10


@router.post(
    "",
    response_model=WebhookCreated,
    status_code=201,
    summary="Register a webhook endpoint",
    responses=error_docs(401, 409, 422),
)
async def create_webhook(
    body: WebhookCreate,
    auth: Auth,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookCreated:
    """Each delivery is a JSON `POST` signed with HMAC-SHA256. Verify
    `X-MediaPipe-Signature: t=<timestamp>,v1=<hex>` over `"<timestamp>.<raw body>"` using the
    returned `secret` (shown only once), and reject old timestamps."""
    validate_webhook_url(body.url, allow_private=settings.webhook_allow_private_urls)
    count = await session.scalar(
        select(func.count())
        .select_from(WebhookEndpoint)
        .where(WebhookEndpoint.project_id == auth.project_id)
    )
    if (count or 0) >= MAX_ENDPOINTS_PER_PROJECT:
        raise ConflictError(
            f"At most {MAX_ENDPOINTS_PER_PROJECT} webhook endpoints per project.",
            code="endpoint_limit",
        )
    endpoint = WebhookEndpoint(
        project_id=auth.project_id,
        url=body.url,
        events=sorted(set(body.events)),
        secret=generate_webhook_secret(),
    )
    session.add(endpoint)
    await session.commit()
    return WebhookCreated(
        **WebhookOut.model_validate(endpoint).model_dump(), secret=endpoint.secret
    )


@router.get(
    "", response_model=list[WebhookOut], summary="List webhook endpoints", responses=error_docs(401)
)
async def list_webhooks(auth: Auth, session: Session) -> list[WebhookEndpoint]:
    rows = await session.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.project_id == auth.project_id)
        .order_by(WebhookEndpoint.created_at)
    )
    return list(rows)


# NOTE: declared before "/{endpoint_id}" routes so "deliveries" is not parsed as an id.
@router.get(
    "/deliveries",
    response_model=DeliveryList,
    summary="List recent webhook deliveries",
    responses=error_docs(401),
)
async def list_deliveries(
    auth: Auth,
    session: Session,
    endpoint_id: Annotated[uuid.UUID | None, Query()] = None,
    success: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeliveryList:
    """Every delivery *attempt*, newest first, including the receiver's HTTP status."""
    base = (
        select(WebhookDelivery)
        .join(WebhookEndpoint)
        .where(WebhookEndpoint.project_id == auth.project_id)
    )
    if endpoint_id:
        base = base.where(WebhookDelivery.endpoint_id == endpoint_id)
    if success is not None:
        base = base.where(WebhookDelivery.success.is_(success))
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.order_by(WebhookDelivery.created_at.desc()).limit(limit).offset(offset)
    )
    return DeliveryList(
        items=[DeliveryOut.model_validate(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/deliveries/{delivery_id}/resend",
    response_model=ResendOut,
    status_code=202,
    summary="Resend a delivery",
    responses=error_docs(401, 404, 409),
)
async def resend_delivery(delivery_id: uuid.UUID, auth: Auth, session: Session) -> ResendOut:
    """Queues the same payload again (fresh timestamp and signature)."""
    row = await session.scalar(
        select(WebhookDelivery)
        .join(WebhookEndpoint)
        .where(WebhookDelivery.id == delivery_id, WebhookEndpoint.project_id == auth.project_id)
    )
    if row is None:
        raise NotFoundError("Delivery not found.", code="delivery_not_found")
    endpoint = await session.get(WebhookEndpoint, row.endpoint_id)
    if endpoint is None or not endpoint.active:
        raise ConflictError("The endpoint is no longer active.", code="endpoint_inactive")

    from app.workers.webhook_tasks import deliver_webhook

    await asyncio.to_thread(
        deliver_webhook.apply_async,
        args=(str(row.endpoint_id), row.payload, 1),
        queue="webhooks",
    )
    return ResendOut(endpoint_id=row.endpoint_id, event=row.event)


@router.delete(
    "/{endpoint_id}",
    status_code=204,
    summary="Delete a webhook endpoint",
    responses=error_docs(401, 404),
)
async def delete_webhook(endpoint_id: uuid.UUID, auth: Auth, session: Session) -> None:
    endpoint = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id, WebhookEndpoint.project_id == auth.project_id
        )
    )
    if endpoint is None:
        raise NotFoundError("Webhook endpoint not found.", code="webhook_not_found")
    await session.delete(endpoint)
    await session.commit()
