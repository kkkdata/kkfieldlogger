#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/kkfieldlogger-v2/app/photoserver}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-kkfieldlogger-v2}"
APP_CONTAINER="${APP_CONTAINER:-kkfieldlogger-v2-app-1}"

cd "$PROJECT_DIR"

echo "== Docker services =="
docker compose -p "$COMPOSE_PROJECT" ps

echo
echo "== Queue and GPU read-only report =="
docker exec -i "$APP_CONTAINER" python - <<'PY'
from __future__ import annotations

from sqlalchemy import text

from app.core.config import load_settings
from app.db.session import create_engine_from_settings


def print_rows(title: str, rows) -> None:
    print(f"\n-- {title} --")
    rows = list(rows)
    if not rows:
        print("(none)")
        return
    for row in rows:
        print(tuple(row))


settings = load_settings()
engine = create_engine_from_settings(settings)

with engine.connect() as conn:
    print_rows(
        "task_jobs by status",
        conn.execute(
            text(
                """
                select status, count(*)
                from task_jobs
                group by status
                order by status
                """
            )
        ),
    )

    print_rows(
        "task_jobs by company/type/status",
        conn.execute(
            text(
                """
                select company_id, task_type, status, count(*)
                from task_jobs
                group by company_id, task_type, status
                order by company_id, task_type, status
                """
            )
        ),
    )

    print_rows(
        "latest dead_letter jobs",
        conn.execute(
            text(
                """
                select id, task_type, company_id, related_type, related_id,
                       attempt_count, max_attempts,
                       left(coalesce(last_error, ''), 180) as last_error,
                       updated_at
                from task_jobs
                where status = 'dead_letter'
                order by updated_at desc
                limit 20
                """
            )
        ),
    )

    print_rows(
        "running jobs older than 30 minutes",
        conn.execute(
            text(
                """
                select id, task_type, company_id, related_type, related_id,
                       locked_by, started_at, updated_at
                from task_jobs
                where status = 'running'
                  and started_at < now() - interval '30 minutes'
                order by started_at asc
                limit 20
                """
            )
        ),
    )

    print_rows(
        "latest GPU samples",
        conn.execute(
            text(
                """
                select sampled_at, gpu_name, utilization_gpu_percent,
                       memory_used_mb, memory_total_mb,
                       temperature_c, power_draw_w
                from gpu_metric_samples
                order by sampled_at desc
                limit 10
                """
            )
        ),
    )
PY
