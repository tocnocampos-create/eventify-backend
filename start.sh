#!/bin/sh
# Railway / Docker entrypoint.
# Runs Alembic migrations then starts the API server.
# PORT is injected by Railway; defaults to 8000 locally.
set -e

echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting API server on port ${PORT:-8000}..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}"
