import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(examples=["thumb.webp"])
    content_type: str
    size: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    url: str | None = Field(default=None, description="Presigned GET URL (valid 1 hour).")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    type: str
    status: str = Field(
        examples=["processing"], description="queued | processing | succeeded | failed"
    )
    progress: int = Field(ge=0, le=100)
    attempts: int
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class FileSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    original_name: str
    content_type: str = Field(description="Content type declared by the client.")
    detected_type: str | None = Field(default=None, description="Type sniffed from the bytes.")
    media_type: str = Field(examples=["image"])
    size: int
    status: str = Field(
        examples=["processed"],
        description="pending_upload | uploaded | processing | processed | failed | rejected",
    )
    preset: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict, validation_alias="meta", description="Extracted metadata."
    )
    created_at: datetime
    updated_at: datetime


class FileOut(FileSummary):
    variants: list[VariantOut] = []
    jobs: list[JobOut] = []


class FileList(BaseModel):
    items: list[FileSummary]
    total: int
    limit: int
    offset: int
