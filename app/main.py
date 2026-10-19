import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, api_keys, health, uploads
from app.core.config import get_settings
from app.core.errors import ErrorResponse, register_exception_handlers
from app.core.logging import setup_logging
from app.services.storage import get_storage

DESCRIPTION = """
Upload files straight to object storage with presigned URLs; MediaPipe processes them in the
background (image variants, video transcodes, text extraction) and notifies your app with signed
webhooks.

Authenticate with `Authorization: Bearer <api_key>`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    if settings.s3_auto_create_bucket:
        await asyncio.to_thread(get_storage().ensure_bucket)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=get_settings().app_name,
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        responses={"default": {"model": ErrorResponse, "description": "Error"}},
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(api_keys.router)
    app.include_router(uploads.router)
    return app


app = create_app()
