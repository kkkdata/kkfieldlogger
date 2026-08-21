#!/bin/sh
set -e

mkdir -p "${KK_MEDIA_ROOT}/photos" "${KK_MEDIA_ROOT}/reports" "${KK_LOG_DIR:-/app/logs}"

if [ "${KK_WORKER_WAIT_FOR_MIGRATIONS:-1}" != "0" ]; then
  timeout_seconds="${KK_WORKER_MIGRATION_WAIT_SECONDS:-300}"
  waited_seconds=0
  head_revision="$(alembic heads --resolve-dependencies | awk 'NF {print $1}' | tail -n 1)"

  if [ -z "$head_revision" ]; then
    echo "Unable to determine Alembic head revision." >&2
    exit 1
  fi

  until current_revision="$(alembic current 2>/dev/null | awk 'NF {print $1}' | tail -n 1)" \
    && [ "$current_revision" = "$head_revision" ]; do
    if [ "$waited_seconds" -ge "$timeout_seconds" ]; then
      echo "Timed out waiting for database migrations: current=${current_revision:-none} head=$head_revision" >&2
      exit 1
    fi
    sleep 2
    waited_seconds=$((waited_seconds + 2))
  done
fi

exec python -m app.worker
