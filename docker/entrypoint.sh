#!/bin/sh
set -e

mkdir -p "${KK_MEDIA_ROOT}/photos" "${KK_MEDIA_ROOT}/reports" "${KK_LOG_DIR:-/app/logs}"

# Pre-upgrade safety dump: migrations are forward-only (no downgrade path),
# so when the schema is behind head, snapshot the database before touching
# it. A NAS user clicking "update" runs exactly this path unattended.
if [ "${KK_PREUPGRADE_DUMP:-1}" != "0" ] && command -v pg_dump >/dev/null 2>&1; then
  head_rev="$(alembic heads --resolve-dependencies 2>/dev/null | awk 'NF {print $1}' | tail -n 1)"
  current_rev="$(alembic current 2>/dev/null | awk 'NF {print $1}' | tail -n 1)"
  if [ -n "$current_rev" ] && [ -n "$head_rev" ] && [ "$current_rev" != "$head_rev" ]; then
    dump_dir="${KK_BACKUP_DIR:-${KK_MEDIA_ROOT}/backups}"
    mkdir -p "$dump_dir"
    dsn="$(python -c 'import os, re; print(re.sub(r"\+\w+", "", os.environ.get("KK_DATABASE_URL") or os.environ.get("DATABASE_URL", "")))')"
    if [ -n "$dsn" ]; then
      ts="$(date +%Y%m%d_%H%M%S)"
      echo "Schema upgrade pending ($current_rev -> $head_rev); dumping database to $dump_dir first."
      if pg_dump -Fc "$dsn" > "$dump_dir/pre_upgrade_${ts}.dump"; then
        # keep the five most recent pre-upgrade dumps
        ls -1t "$dump_dir"/pre_upgrade_*.dump 2>/dev/null | tail -n +6 | xargs -r rm -f
      else
        echo "WARNING: pre-upgrade dump failed; continuing with the upgrade." >&2
      fi
    fi
  fi
fi

# Safe for both first boot and upgrades:
# - empty database: applies the full schema
# - existing database: applies only pending migrations
alembic upgrade head

# Private/NAS deployments create their admin through the /setup first-run
# wizard instead of a baked-in default credential.
if [ "${KK_DEPLOYMENT_PROFILE:-saas}" != "private" ]; then
  python -m app.cli create-admin --from-env --if-missing
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
