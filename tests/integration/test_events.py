import asyncio
import json
import uuid
from typing import Any

from app.db.models import File, FileStatus, Job, JobStatus, Project
from app.db.session import get_sessionmaker, sync_session
from app.services.events import file_event_stream
from tests.conftest import Tenant
from tests.helpers import image_bytes, upload_file


def parse(frames: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in frames:
        if frame.startswith(":"):
            continue
        event, data = frame.strip().split("\n")
        out.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def make_file(status: str = FileStatus.PROCESSING) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with sync_session() as s:
        project = Project(name="p")
        s.add(project)
        s.flush()
        file = File(
            project_id=project.id,
            original_name="a.jpg",
            content_type="image/jpeg",
            media_type="image",
            size=1,
            storage_key="k",
            status=status,
        )
        s.add(file)
        s.flush()
        job = Job(file_id=file.id, type="image", status=JobStatus.PROCESSING, progress=10)
        s.add(job)
        s.flush()
        return project.id, file.id, job.id


def update(file_id: uuid.UUID, job_id: uuid.UUID, **job_fields: Any) -> None:
    with sync_session() as s:
        job = s.get(Job, job_id)
        assert job is not None
        for k, v in job_fields.items():
            setattr(job, k, v)
        if job.status == JobStatus.SUCCEEDED:
            file = s.get(File, file_id)
            assert file is not None
            file.status = FileStatus.PROCESSED


async def test_stream_emits_changes_then_done() -> None:
    project_id, file_id, job_id = make_file()
    stream = file_event_stream(
        get_sessionmaker(), project_id, file_id, poll_interval=0.01, max_seconds=5
    )
    frames: list[str] = [await anext(stream)]  # initial state
    update(file_id, job_id, progress=60)
    frames.append(await anext(stream))
    await asyncio.sleep(0.05)  # nothing changed -> nothing emitted
    update(file_id, job_id, progress=100, status=JobStatus.SUCCEEDED)
    frames += [f async for f in stream]

    events = parse(frames)
    assert [e for e, _ in events] == ["status", "status", "status", "done"]
    assert [d["jobs"][0]["progress"] for e, d in events if e == "status"] == [10, 60, 100]
    assert events[-1][1]["status"] == "processed"


async def test_stream_for_finished_file_ends_immediately() -> None:
    project_id, file_id, _ = make_file(FileStatus.FAILED)
    frames = [
        f
        async for f in file_event_stream(
            get_sessionmaker(), project_id, file_id, poll_interval=0.01, max_seconds=5
        )
    ]
    assert [e for e, _ in parse(frames)] == ["status", "done"]


async def test_stream_times_out_and_reports_deleted_files() -> None:
    project_id, file_id, _ = make_file()
    frames = [
        f
        async for f in file_event_stream(
            get_sessionmaker(), project_id, file_id, poll_interval=0.01, max_seconds=0.05
        )
    ]
    assert [e for e, _ in parse(frames)] == ["status", "timeout"]

    with sync_session() as s:
        s.delete(s.get(File, file_id))
    gone = [
        f
        async for f in file_event_stream(
            get_sessionmaker(), project_id, file_id, poll_interval=0.01, max_seconds=5
        )
    ]
    assert [e for e, _ in parse(gone)] == ["deleted"]


async def test_stream_is_scoped_to_project() -> None:
    _, file_id, _ = make_file()
    frames = [
        f
        async for f in file_event_stream(
            get_sessionmaker(), uuid.uuid4(), file_id, poll_interval=0.01, max_seconds=5
        )
    ]
    assert [e for e, _ in parse(frames)] == ["deleted"]


def test_sse_endpoint(tenant: Tenant, other_tenant: Tenant) -> None:
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    with tenant.client.stream("GET", f"/v1/files/{file_id}/events", headers=tenant.headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        body = "".join(resp.iter_text())
    events = parse([f for f in body.split("\n\n") if f])
    assert [e for e, _ in events] == ["status", "done"]
    assert events[0][1]["jobs"][0]["progress"] == 100

    assert other_tenant.get(f"/v1/files/{file_id}/events").status_code == 404
    assert tenant.client.get(f"/v1/files/{file_id}/events").status_code == 401
    assert tenant.get(f"/v1/files/{uuid.uuid4()}/events").status_code == 404
