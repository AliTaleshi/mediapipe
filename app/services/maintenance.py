"""Periodic housekeeping (run from Celery Beat)."""

from datetime import timedelta

from sqlalchemy import delete, select

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import File, FileStatus
from app.db.session import sync_session
from app.services.storage import Storage, StorageError

log = get_logger(__name__)
BATCH_SIZE = 500


def cleanup_abandoned_uploads(storage: Storage, settings: Settings) -> int:
    """Deletes uploads never completed within ``upload_cleanup_after_hours``.

    Removes the storage object (or aborts the multipart upload) before the database row, so a
    storage outage leaves the row in place to be retried on the next run instead of orphaning
    objects. Returns the number of uploads removed.
    """
    cutoff = utcnow() - timedelta(hours=settings.upload_cleanup_after_hours)
    with sync_session() as s:
        stale = [
            (f.id, f.storage_key, f.multipart_upload_id)
            for f in s.scalars(
                select(File)
                .where(File.status == FileStatus.PENDING_UPLOAD, File.created_at < cutoff)
                .order_by(File.created_at)
                .limit(BATCH_SIZE)
            )
        ]

    removed = 0
    for file_id, key, upload_id in stale:
        try:
            if upload_id:
                storage.abort_multipart(key, upload_id)
            storage.delete([key])
        except StorageError as exc:
            log.warning("could not clean up upload %s (will retry): %s", file_id, exc)
            continue
        with sync_session() as s:
            result = s.execute(
                delete(File).where(File.id == file_id, File.status == FileStatus.PENDING_UPLOAD)
            )
            removed += result.rowcount  # type: ignore[attr-defined]
    if removed:
        log.info("removed %d abandoned uploads", removed)
    return removed
