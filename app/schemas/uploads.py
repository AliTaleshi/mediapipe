import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.processors.config import PresetName
from app.schemas.files import FileOut, JobOut


class UploadCreate(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=512, examples=["holiday.jpg"])]
    content_type: Annotated[str, Field(max_length=255, examples=["image/jpeg"])]
    size: Annotated[int, Field(ge=1, description="Exact size in bytes.", examples=[482113])]
    preset: PresetName | None = Field(
        default=None, description="Processing preset name. Defaults to the built-in `default`."
    )


class SingleUpload(BaseModel):
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str] = Field(description="Send these headers with the PUT request.")


class PartUpload(BaseModel):
    part_number: int
    url: str


class MultipartUpload(BaseModel):
    part_size: int = Field(description="Every part except the last must be exactly this size.")
    parts: list[PartUpload] = Field(
        description="PUT each part to its URL and keep the ETag header."
    )


class UploadCreated(BaseModel):
    file_id: uuid.UUID
    mode: Literal["single", "multipart"]
    expires_at: datetime
    upload: SingleUpload | None = None
    multipart: MultipartUpload | None = None


class CompletedPart(BaseModel):
    part_number: Annotated[int, Field(ge=1, le=10000)]
    etag: Annotated[str, Field(min_length=1, max_length=256)]


class UploadComplete(BaseModel):
    parts: list[CompletedPart] | None = Field(
        default=None, description="Required for multipart uploads: part numbers with their ETags."
    )


class UploadCompleted(BaseModel):
    file: FileOut
    jobs: list[JobOut]
