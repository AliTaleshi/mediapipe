import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.processors.config import PresetName, parse_config

MediaTypeName = Literal["image", "video", "document"]


class PresetCreate(BaseModel):
    name: PresetName = Field(examples=["social-square"])
    media_type: MediaTypeName
    config: Annotated[
        dict[str, Any],
        Field(
            description="Settings for the media type; unknown keys are rejected. Omitted keys "
            "use the built-in defaults.",
            examples=[
                {
                    "variants": [{"name": "square", "width": 600, "height": 600, "fit": "cover"}],
                    "formats": ["webp"],
                }
            ],
        ),
    ] = {}

    @model_validator(mode="after")
    def _validate_config(self) -> "PresetCreate":
        parse_config(self.media_type, self.config)  # raises ValidationError -> 422
        return self


class PresetOut(BaseModel):
    id: uuid.UUID | None = Field(description="Null for built-in presets.")
    name: str
    media_type: str
    builtin: bool
    config: dict[str, Any]
    created_at: datetime | None = None
