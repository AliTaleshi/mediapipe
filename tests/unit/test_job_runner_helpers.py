from typing import Any, cast

from app.db.models import File, FileStatus, JobStatus
from app.services.job_runner import backoff_delay
from app.services.job_runner import refresh_file_status as _refresh


def refresh_file_status(f: Any) -> str | None:
    return _refresh(cast(File, f))


def test_backoff_grows_exponentially_with_jitter_and_cap() -> None:
    low, high = (lambda: 0.0), (lambda: 1.0)
    assert backoff_delay(1, 10, 1000, low) == 5 and backoff_delay(1, 10, 1000, high) == 10
    assert backoff_delay(2, 10, 1000, low) == 10 and backoff_delay(2, 10, 1000, high) == 20
    assert backoff_delay(3, 10, 1000, high) == 40
    assert backoff_delay(10, 10, 60, high) == 60  # capped


def test_backoff_jitter_is_actually_random() -> None:
    assert len({backoff_delay(3, 10, 1000) for _ in range(20)}) > 1


class _Job:
    def __init__(self, status: str, error: str | None = None) -> None:
        self.status, self.error = status, error


class _File:  # duck-typed stand-in for the ORM File
    def __init__(self, status: str, jobs: list[_Job]) -> None:
        self.status, self.jobs, self.error = status, jobs, None


def test_file_status_follows_jobs() -> None:
    f = _File(FileStatus.PROCESSING, [_Job(JobStatus.SUCCEEDED)])
    assert refresh_file_status(f) == "file.processed"
    assert f.status == FileStatus.PROCESSED
    assert refresh_file_status(f) is None

    f = _File(FileStatus.PROCESSING, [_Job(JobStatus.SUCCEEDED), _Job(JobStatus.FAILED, "boom")])
    assert refresh_file_status(f) == "file.failed"
    assert f.error == "boom"

    f = _File(FileStatus.UPLOADED, [_Job(JobStatus.PROCESSING)])
    assert refresh_file_status(f) is None
    assert f.status == FileStatus.PROCESSING
