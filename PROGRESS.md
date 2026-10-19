# Progress

All nine milestones are complete. Spec: the project brief (see README for the user-facing summary).

## Status
- [x] 1. Foundation
- [x] 2. Auth
- [x] 3. Uploads
- [x] 4. Image processing
- [x] 5. Documents
- [x] 6. Video
- [x] 7. Progress & webhooks
- [x] 8. Downloads & maintenance
- [x] 9. Polish

## Verification done
- `ruff check`, `ruff format --check`, `mypy --strict`: clean. `pytest`: 212 passed (video tests skip cleanly without ffmpeg).
- Coverage on `app.services` + `app.processors`: ~91% (CI enforces >= 80%).
- **Live smoke test (outside pytest)** with real Redis, a standalone Celery worker, uvicorn and a
  moto S3 server (SQLite DB): image, PDF and video uploaded through the HTTP API and processed via
  real queues; SSE stream showed progress; signed webhooks delivered and verified; a storage outage
  mid-job was retried by Celery (`attempts` 1 -> 2) and then succeeded; `celery beat` starts and
  loads the cleanup schedule. `examples/demo.sh` was run against that stack.

## Dev notes
- Venv: `uv venv --python 3.12 .venv && uv pip install -e ".[dev]"`.
- Tests need no Docker: SQLite (per-test file), moto, fakeredis, Celery eager mode.
- Workers use a *sync* SQLAlchemy engine (`DATABASE_URL` with the driver swapped); the API is async.
- Celery eager mode can't schedule retries, so `app/workers/tasks.py` loops in-process when
  `task.request.is_eager` (tests only); production uses `task.retry`.
- moto doesn't enforce presigned-URL signatures, so tests assert on what is signed instead.

## Known issues / not verified here
- Docker is not available in the cloud session: `Dockerfile` and `docker-compose.yml` are written
  and `docker compose config` parses, but `docker compose up` has not been run. The MinIO healthcheck
  uses `mc ready local`, which needs a reasonably recent `minio/minio` image.
- No PostgreSQL in the cloud session: tests and the live run used SQLite. The migration renders
  valid PostgreSQL DDL offline (`alembic upgrade head --sql`); CI applies it to a real Postgres
  service. asyncpg/psycopg code paths are therefore unexercised locally.
- Part-URL refresh for multipart uploads is not implemented (see README "Known limitations").
- Coverage under-reports async code run in the TestClient's worker thread (e.g. `services/uploads.py`).
