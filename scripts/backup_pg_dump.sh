#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ -z "${KK_DATABASE_URL:-}" ]]; then
  echo "KK_DATABASE_URL must be set" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
PG_DUMP_URL="${KK_DATABASE_URL/postgresql+psycopg/postgresql}"
OUTPUT_FILE="${BACKUP_DIR}/kk_field_logger_${TIMESTAMP}.dump"

pg_dump "${PG_DUMP_URL}" --format=custom --file="${OUTPUT_FILE}"
echo "Backup written to ${OUTPUT_FILE}"
