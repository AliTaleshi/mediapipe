# Progress

Spec: see the project brief. Work milestone by milestone; commit after each.

## Status
- [x] 1. Foundation
- [x] 2. Auth
- [ ] 3. Uploads
- [ ] 4. Image processing
- [ ] 5. Documents
- [ ] 6. Video
- [ ] 7. Progress & webhooks
- [ ] 8. Downloads & maintenance
- [ ] 9. Polish

## Dev notes
- Venv: `uv venv --python 3.12 .venv && uv pip install -e ".[dev]"`.
- Tests need no Docker: SQLite (per-test file), moto, fakeredis, Celery eager.
- Workers use a *sync* SQLAlchemy engine (`DATABASE_URL` with driver swapped); the API uses async.

## Known issues / blocked
- Docker is not available in the cloud session, so `docker compose up` is untested here.
- No PostgreSQL in the cloud session; migration SQL for PostgreSQL verified offline only (`alembic upgrade head --sql`). CI runs it against a real Postgres service.
