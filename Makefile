.PHONY: dev test lint format migrate install

install:
	pip install -e ".[dev]"

dev:  ## start the full stack (api, worker, beat, postgres, redis, minio)
	docker compose up --build

test:
	pytest --cov=app.services --cov=app.processors --cov-report=term-missing

lint:
	ruff check .
	ruff format --check .
	mypy app tests

format:
	ruff check --fix .
	ruff format .

migrate:  ## apply database migrations to $$DATABASE_URL
	alembic upgrade head
