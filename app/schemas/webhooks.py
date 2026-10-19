import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WebhookEvent = Literal["file.processed", "file.failed"]


class WebhookCreate(BaseModel):
    url: Annotated[str, Field(max_length=2048, examples=["https://example.com/hooks/mediapipe"])]
    events: list[WebhookEvent] = Field(
        default=["file.processed", "file.failed"], min_length=1, description="Events to receive."
    )


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    events: list[str]
    active: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    secret: str = Field(
        description="Signing secret (`whsec_...`). Shown once; use it to verify "
        "`X-MediaPipe-Signature`."
    )


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event: str
    attempt: int
    success: bool
    status_code: int | None
    error: str | None
    duration_ms: int | None
    created_at: datetime
    delivered_at: datetime | None
    payload: dict[str, Any]


class DeliveryList(BaseModel):
    items: list[DeliveryOut]
    total: int
    limit: int
    offset: int


class ResendOut(BaseModel):
    queued: bool = True
    endpoint_id: uuid.UUID
    event: str
