"""Application settings. Everything is read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024
GB = 1024 * MB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # General
    app_name: str = "MediaPipe"
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False
    admin_token: str = Field(
        default="",
        description="Token for /v1/admin/* endpoints (project creation). Empty disables them.",
    )

    # Database / queue
    database_url: str = "postgresql+asyncpg://mediapipe:mediapipe@localhost:5432/mediapipe"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    celery_task_always_eager: bool = False

    # Storage (S3-compatible)
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "mediapipe"
    s3_addressing_style: str = "path"
    s3_auto_create_bucket: bool = False

    # Uploads
    upload_url_expires_seconds: int = 15 * 60
    download_url_expires_seconds: int = 60 * 60
    multipart_threshold_bytes: int = 100 * MB
    multipart_part_size_bytes: int = 64 * MB
    max_image_bytes: int = 25 * MB
    max_document_bytes: int = 50 * MB
    max_video_bytes: int = 2 * GB
    upload_cleanup_after_hours: int = 24

    # Rate limiting
    rate_limit_per_minute: int = 60

    # Jobs
    job_max_attempts: int = 3
    job_retry_base_seconds: float = 5.0
    job_retry_max_seconds: float = 300.0
    video_timeout_seconds: int = 6 * 60 * 60
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # Webhooks
    webhook_max_attempts: int = 5
    webhook_timeout_seconds: float = 10.0
    webhook_retry_base_seconds: float = 10.0
    webhook_allow_private_urls: bool = False
    webhook_signature_tolerance_seconds: int = 300

    # SSE
    sse_poll_interval_seconds: float = 1.0
    sse_max_seconds: int = 3600

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def sync_database_url(self) -> str:
        """Driver URL for the synchronous engine used by Celery workers."""
        return (
            self.database_url.replace("+asyncpg", "+psycopg")
            .replace("+aiosqlite", "")
            .replace("postgresql://", "postgresql+psycopg://")
        )

    def max_bytes_for(self, media_type: str) -> int:
        return {
            "image": self.max_image_bytes,
            "document": self.max_document_bytes,
            "video": self.max_video_bytes,
        }[media_type]


@lru_cache
def get_settings() -> Settings:
    return Settings()
