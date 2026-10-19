"""Thin wrapper around boto3 for S3-compatible storage (MinIO locally, AWS S3 in prod).

Errors are translated into ``TransientStorageError`` (worth retrying: timeouts, 5xx,
throttling) and ``StorageError`` (permanent: missing key, access denied).
Presigned URLs are signed against ``S3_PUBLIC_ENDPOINT_URL`` so they work from outside
the docker network, while server-side calls use ``S3_ENDPOINT_URL``.
"""

import contextlib
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_TRANSIENT_CODES = {
    "RequestTimeout",
    "RequestTimeTooSkewed",
    "SlowDown",
    "ServiceUnavailable",
    "InternalError",
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
}


class StorageError(Exception):
    """Permanent storage failure."""


class ObjectNotFoundError(StorageError):
    pass


class TransientStorageError(StorageError):
    """Temporary failure; the caller may retry."""


def _translate(exc: Exception) -> StorageError:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if code in {"404", "NoSuchKey", "NotFound", "NoSuchUpload"}:
            return ObjectNotFoundError(f"Object not found ({code})")
        if status >= 500 or code in _TRANSIENT_CODES:
            return TransientStorageError(f"Storage error {code or status}")
        return StorageError(f"Storage error {code or status}: {exc}")
    # Connection resets, DNS failures, connect/read timeouts, ...
    return TransientStorageError(f"Storage connection problem: {exc}")


@contextlib.contextmanager
def _translating() -> Iterator[None]:
    try:
        yield
    except StorageError:
        raise
    except (ClientError, BotoCoreError, OSError) as exc:
        raise _translate(exc) from exc


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.region = settings.s3_region
        self._auto_create = settings.s3_auto_create_bucket
        common: dict[str, Any] = {
            "region_name": settings.s3_region,
            "aws_access_key_id": settings.s3_access_key_id or None,
            "aws_secret_access_key": settings.s3_secret_access_key or None,
        }
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": settings.s3_addressing_style},  # type: ignore[arg-type]
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        )
        self._client: S3Client = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint_url, config=cfg, **common
        )
        public = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._presigner: S3Client = (
            self._client
            if public == settings.s3_endpoint_url
            else boto3.client("s3", endpoint_url=public, config=cfg, **common)
        )

    # -- bucket -------------------------------------------------------------
    def ensure_bucket(self) -> None:
        if not self._auto_create:
            return
        with _translating():
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self._client.create_bucket(Bucket=self.bucket)

    def check(self) -> None:
        """Raises if the bucket is unreachable (used by /ready)."""
        with _translating():
            self._client.head_bucket(Bucket=self.bucket)

    # -- presigned urls -----------------------------------------------------
    def presign_put(self, key: str, content_type: str, expires: int) -> str:
        return self._presigner.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    def presign_get(
        self,
        key: str,
        expires: int,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if filename:
            safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
            params["ResponseContentDisposition"] = f'inline; filename="{safe}"'
        if content_type:
            params["ResponseContentType"] = content_type
        return self._presigner.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )

    # -- multipart ----------------------------------------------------------
    def create_multipart(self, key: str, content_type: str) -> str:
        with _translating():
            resp = self._client.create_multipart_upload(
                Bucket=self.bucket, Key=key, ContentType=content_type
            )
        return resp["UploadId"]

    def presign_part(self, key: str, upload_id: str, part_number: int, expires: int) -> str:
        return self._presigner.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires,
        )

    def complete_multipart(self, key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
        with _translating():
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": [{"PartNumber": n, "ETag": e} for n, e in parts]},
            )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        with _translating(), contextlib.suppress(ObjectNotFoundError):
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    # -- objects ------------------------------------------------------------
    def head(self, key: str) -> int | None:
        """Object size in bytes, or None if it does not exist."""
        try:
            with _translating():
                return int(self._client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except ObjectNotFoundError:
            return None

    def read_range(self, key: str, start: int, end: int) -> bytes:
        with _translating():
            resp = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}"
            )
            return bytes(resp["Body"].read())

    def download(self, key: str, dest: Path) -> None:
        with _translating():
            self._client.download_file(self.bucket, key, str(dest))

    def upload(self, src: Path, key: str, content_type: str) -> None:
        with _translating():
            self._client.upload_file(
                str(src), self.bucket, key, ExtraArgs={"ContentType": content_type}
            )

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        with _translating():
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )

    def delete(self, keys: list[str]) -> None:
        """Delete objects; missing keys are not an error."""
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            with _translating():
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )


@lru_cache
def get_storage() -> Storage:
    return Storage(get_settings())
