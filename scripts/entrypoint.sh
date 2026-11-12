#!/bin/sh
set -eu

PORT="${PORT:-8000}"

if [ "${1:-api}" = "api" ]; then
  alembic upgrade head
  exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
fi

if [ "$1" = "worker" ]; then
  alembic upgrade head
  exec python -m app.workers.consumer
fi

exec "$@"
