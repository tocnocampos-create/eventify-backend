#!/bin/sh
# Railway / Docker entrypoint.
# Runs Alembic migrations then starts the API server.
# PORT is injected by Railway; defaults to 8000 locally.
set -e

# Resolve PORT once so uvicorn always binds to 0.0.0.0:$PORT.
# Railway injects $PORT at runtime; fall back to 8000 for local dev.
APP_PORT="${PORT:-8000}"

echo "==> Running database migrations..."
alembic upgrade head

echo "==> Starting API server on 0.0.0.0:${APP_PORT}..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$APP_PORT" \
    --workers "${WEB_CONCURRENCY:-2}"
