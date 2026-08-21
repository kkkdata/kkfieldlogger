from __future__ import annotations

import csv
import os
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


GPU_QUERY_FIELDS = [
    "index",
    "name",
    "utilization.gpu",
    "utilization.memory",
    "memory.total",
    "memory.used",
    "memory.free",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "fan.speed",
]

RAW_COLUMNS = [
    "sampled_at",
    "hostname",
    "gpu_index",
    "gpu_name",
    "utilization_gpu_percent",
    "utilization_memory_percent",
    "memory_total_mb",
    "memory_used_mb",
    "memory_free_mb",
    "temperature_c",
    "power_draw_w",
    "power_limit_w",
    "fan_speed_percent",
    "metadata_json",
    "created_at",
]

ROLLUP_COLUMNS = [
    "bucket_start",
    "hostname",
    "gpu_index",
    "gpu_name",
    "sample_count",
    "avg_gpu_utilization_percent",
    "max_gpu_utilization_percent",
    "avg_memory_utilization_percent",
    "max_memory_utilization_percent",
    "avg_memory_used_mb",
    "max_memory_used_mb",
    "avg_power_draw_w",
    "max_power_draw_w",
    "max_temperature_c",
    "saturated_sample_count",
    "memory_pressure_sample_count",
    "created_at",
    "updated_at",
]

STOP = False


def _handle_stop(signum, frame) -> None:  # noqa: ANN001
    global STOP
    STOP = True


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.strip().replace("%", "")
    if not cleaned or cleaned.upper() in {"N/A", "[N/A]", "NOT SUPPORTED", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _sql_string(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _sql_number(value: int | float | None) -> str:
    if value is None:
        return "NULL"
    return str(value)


def _sql_timestamp(value: datetime) -> str:
    return _sql_string(value.astimezone(timezone.utc).isoformat())


def _parse_db_url(project_root: Path) -> tuple[list[str], dict[str, str]]:
    env_values = _load_dotenv(project_root / ".env")
    for key, value in env_values.items():
        os.environ.setdefault(key, value)
    db_url = os.getenv("KK_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("KK_DATABASE_URL or DATABASE_URL is required")
    normalized = db_url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise RuntimeError(f"Unsupported database URL scheme: {parsed.scheme}")
    db_name = parsed.path.lstrip("/")
    if not db_name:
        raise RuntimeError("Database URL is missing database name")
    env = os.environ.copy()
    env["PGPASSWORD"] = unquote(parsed.password or "")
    command = [
        "psql",
        "-h",
        parsed.hostname or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        unquote(parsed.username or ""),
        "-d",
        db_name,
        "-v",
        "ON_ERROR_STOP=1",
        "-q",
    ]
    return command, env


def _query_gpu_once(timeout_seconds: int) -> list[dict[str, object]]:
    sampled_at = datetime.now(timezone.utc)
    command = [
        "nvidia-smi",
        f"--query-gpu={','.join(GPU_QUERY_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(1, timeout_seconds),
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "nvidia-smi failed").strip())
    rows = []
    for raw_row in csv.reader(completed.stdout.splitlines()):
        if len(raw_row) < len(GPU_QUERY_FIELDS):
            continue
        values = [item.strip() for item in raw_row]
        memory_total_mb = _parse_int(values[4])
        memory_used_mb = _parse_int(values[5])
        memory_free_mb = _parse_int(values[6])
        rows.append(
            {
                "sampled_at": sampled_at,
                "hostname": socket.gethostname(),
                "gpu_index": _parse_int(values[0]) or 0,
                "gpu_name": values[1] or None,
                "utilization_gpu_percent": _parse_float(values[2]),
                "utilization_memory_percent": _parse_float(values[3]),
                "memory_total_mb": memory_total_mb,
                "memory_used_mb": memory_used_mb,
                "memory_free_mb": memory_free_mb,
                "temperature_c": _parse_float(values[7]),
                "power_draw_w": _parse_float(values[8]),
                "power_limit_w": _parse_float(values[9]),
                "fan_speed_percent": _parse_float(values[10]),
                "metadata_json": None,
                "created_at": sampled_at,
            }
        )
    return rows


def _row_values_sql(sample: dict[str, object]) -> str:
    values = []
    for column in RAW_COLUMNS:
        value = sample.get(column)
        if isinstance(value, datetime):
            values.append(_sql_timestamp(value))
        elif isinstance(value, str):
            values.append(_sql_string(value))
        elif column == "metadata_json":
            values.append("'{}'::json")
        else:
            values.append(_sql_number(value if isinstance(value, (int, float)) else None))
    return "(" + ", ".join(values) + ")"


def _hour_bucket(sampled_at: datetime) -> datetime:
    return sampled_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _avg(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 3)


def _max(values: list[float | int | None]) -> float | int | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return max(clean)


def _rollup_values(samples: list[dict[str, object]], saturation_threshold: float, memory_threshold: float) -> list[dict[str, object]]:
    groups: dict[tuple[datetime, str, int], list[dict[str, object]]] = defaultdict(list)
    for sample in samples:
        groups[
            (
                _hour_bucket(sample["sampled_at"]),
                str(sample["hostname"]),
                int(sample["gpu_index"]),
            )
        ].append(sample)
    rows = []
    now = datetime.now(timezone.utc)
    for (bucket_start, hostname, gpu_index), items in groups.items():
        memory_values = [
            item["utilization_memory_percent"]
            if item.get("utilization_memory_percent") is not None
            else (
                ((float(item.get("memory_used_mb") or 0) / float(item.get("memory_total_mb") or 1)) * 100.0)
                if item.get("memory_total_mb")
                else None
            )
            for item in items
        ]
        rows.append(
            {
                "bucket_start": bucket_start,
                "hostname": hostname,
                "gpu_index": gpu_index,
                "gpu_name": str(items[-1].get("gpu_name") or f"GPU {gpu_index}"),
                "sample_count": len(items),
                "avg_gpu_utilization_percent": _avg([item.get("utilization_gpu_percent") for item in items]),
                "max_gpu_utilization_percent": _max([item.get("utilization_gpu_percent") for item in items]),
                "avg_memory_utilization_percent": _avg(memory_values),
                "max_memory_utilization_percent": _max(memory_values),
                "avg_memory_used_mb": _avg([item.get("memory_used_mb") for item in items]),
                "max_memory_used_mb": _max([item.get("memory_used_mb") for item in items]),
                "avg_power_draw_w": _avg([item.get("power_draw_w") for item in items]),
                "max_power_draw_w": _max([item.get("power_draw_w") for item in items]),
                "max_temperature_c": _max([item.get("temperature_c") for item in items]),
                "saturated_sample_count": sum(1 for item in items if float(item.get("utilization_gpu_percent") or 0) >= saturation_threshold),
                "memory_pressure_sample_count": sum(1 for value in memory_values if float(value or 0) >= memory_threshold),
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def _rollup_values_sql(row: dict[str, object]) -> str:
    values = []
    for column in ROLLUP_COLUMNS:
        value = row.get(column)
        if isinstance(value, datetime):
            values.append(_sql_timestamp(value))
        elif isinstance(value, str):
            values.append(_sql_string(value))
        else:
            values.append(_sql_number(value if isinstance(value, (int, float)) else None))
    return "(" + ", ".join(values) + ")"


def _flush_samples(
    psql_command: list[str],
    psql_env: dict[str, str],
    samples: list[dict[str, object]],
    *,
    raw_retention_hours: int,
    rollup_retention_days: int,
    saturation_threshold: float,
    memory_threshold: float,
) -> None:
    if not samples:
        return
    raw_values = ",\n".join(_row_values_sql(sample) for sample in samples)
    rollups = _rollup_values(samples, saturation_threshold, memory_threshold)
    rollup_values = ",\n".join(_rollup_values_sql(row) for row in rollups)
    sql = f"""
BEGIN;
INSERT INTO gpu_metric_samples ({", ".join(RAW_COLUMNS)})
VALUES
{raw_values};
INSERT INTO gpu_metric_hourly_rollups ({", ".join(ROLLUP_COLUMNS)})
VALUES
{rollup_values}
ON CONFLICT ON CONSTRAINT uq_gpu_metric_hourly_host_gpu_bucket DO UPDATE SET
    gpu_name = EXCLUDED.gpu_name,
    sample_count = gpu_metric_hourly_rollups.sample_count + EXCLUDED.sample_count,
    avg_gpu_utilization_percent = CASE
        WHEN gpu_metric_hourly_rollups.avg_gpu_utilization_percent IS NULL THEN EXCLUDED.avg_gpu_utilization_percent
        WHEN EXCLUDED.avg_gpu_utilization_percent IS NULL THEN gpu_metric_hourly_rollups.avg_gpu_utilization_percent
        ELSE ((gpu_metric_hourly_rollups.avg_gpu_utilization_percent * gpu_metric_hourly_rollups.sample_count) + (EXCLUDED.avg_gpu_utilization_percent * EXCLUDED.sample_count)) / (gpu_metric_hourly_rollups.sample_count + EXCLUDED.sample_count)
    END,
    max_gpu_utilization_percent = GREATEST(COALESCE(gpu_metric_hourly_rollups.max_gpu_utilization_percent, EXCLUDED.max_gpu_utilization_percent), COALESCE(EXCLUDED.max_gpu_utilization_percent, gpu_metric_hourly_rollups.max_gpu_utilization_percent)),
    avg_memory_utilization_percent = CASE
        WHEN gpu_metric_hourly_rollups.avg_memory_utilization_percent IS NULL THEN EXCLUDED.avg_memory_utilization_percent
        WHEN EXCLUDED.avg_memory_utilization_percent IS NULL THEN gpu_metric_hourly_rollups.avg_memory_utilization_percent
        ELSE ((gpu_metric_hourly_rollups.avg_memory_utilization_percent * gpu_metric_hourly_rollups.sample_count) + (EXCLUDED.avg_memory_utilization_percent * EXCLUDED.sample_count)) / (gpu_metric_hourly_rollups.sample_count + EXCLUDED.sample_count)
    END,
    max_memory_utilization_percent = GREATEST(COALESCE(gpu_metric_hourly_rollups.max_memory_utilization_percent, EXCLUDED.max_memory_utilization_percent), COALESCE(EXCLUDED.max_memory_utilization_percent, gpu_metric_hourly_rollups.max_memory_utilization_percent)),
    avg_memory_used_mb = CASE
        WHEN gpu_metric_hourly_rollups.avg_memory_used_mb IS NULL THEN EXCLUDED.avg_memory_used_mb
        WHEN EXCLUDED.avg_memory_used_mb IS NULL THEN gpu_metric_hourly_rollups.avg_memory_used_mb
        ELSE ((gpu_metric_hourly_rollups.avg_memory_used_mb * gpu_metric_hourly_rollups.sample_count) + (EXCLUDED.avg_memory_used_mb * EXCLUDED.sample_count)) / (gpu_metric_hourly_rollups.sample_count + EXCLUDED.sample_count)
    END,
    max_memory_used_mb = GREATEST(COALESCE(gpu_metric_hourly_rollups.max_memory_used_mb, EXCLUDED.max_memory_used_mb), COALESCE(EXCLUDED.max_memory_used_mb, gpu_metric_hourly_rollups.max_memory_used_mb)),
    avg_power_draw_w = CASE
        WHEN gpu_metric_hourly_rollups.avg_power_draw_w IS NULL THEN EXCLUDED.avg_power_draw_w
        WHEN EXCLUDED.avg_power_draw_w IS NULL THEN gpu_metric_hourly_rollups.avg_power_draw_w
        ELSE ((gpu_metric_hourly_rollups.avg_power_draw_w * gpu_metric_hourly_rollups.sample_count) + (EXCLUDED.avg_power_draw_w * EXCLUDED.sample_count)) / (gpu_metric_hourly_rollups.sample_count + EXCLUDED.sample_count)
    END,
    max_power_draw_w = GREATEST(COALESCE(gpu_metric_hourly_rollups.max_power_draw_w, EXCLUDED.max_power_draw_w), COALESCE(EXCLUDED.max_power_draw_w, gpu_metric_hourly_rollups.max_power_draw_w)),
    max_temperature_c = GREATEST(COALESCE(gpu_metric_hourly_rollups.max_temperature_c, EXCLUDED.max_temperature_c), COALESCE(EXCLUDED.max_temperature_c, gpu_metric_hourly_rollups.max_temperature_c)),
    saturated_sample_count = gpu_metric_hourly_rollups.saturated_sample_count + EXCLUDED.saturated_sample_count,
    memory_pressure_sample_count = gpu_metric_hourly_rollups.memory_pressure_sample_count + EXCLUDED.memory_pressure_sample_count,
    updated_at = NOW();
DELETE FROM gpu_metric_samples WHERE sampled_at < NOW() - INTERVAL '{int(raw_retention_hours)} hours';
DELETE FROM gpu_metric_hourly_rollups WHERE bucket_start < NOW() - INTERVAL '{int(rollup_retention_days)} days';
COMMIT;
"""
    completed = subprocess.run(
        psql_command,
        input=sql,
        text=True,
        capture_output=True,
        env=psql_env,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "psql flush failed").strip())


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    project_root = _project_root()
    _load_dotenv(project_root / ".env")
    for key, value in _load_dotenv(project_root / ".env").items():
        os.environ.setdefault(key, value)
    if not _bool_env("KK_GPU_METRICS_ENABLED", True):
        print("gpu_metrics_host_collector disabled by KK_GPU_METRICS_ENABLED", flush=True)
        return 0
    sample_interval = max(1, _int_env("KK_GPU_METRICS_SAMPLE_INTERVAL_SECONDS", 1))
    flush_interval = max(sample_interval, _int_env("KK_GPU_METRICS_FLUSH_INTERVAL_SECONDS", 30))
    query_timeout = max(1, _int_env("KK_GPU_METRICS_QUERY_TIMEOUT_SECONDS", 2))
    raw_retention_hours = max(1, _int_env("KK_GPU_METRICS_RAW_RETENTION_HOURS", 72))
    rollup_retention_days = max(1, _int_env("KK_GPU_METRICS_ROLLUP_RETENTION_DAYS", 180))
    saturation_threshold = _float_env("KK_GPU_METRICS_SATURATION_THRESHOLD_PERCENT", 85.0)
    memory_threshold = _float_env("KK_GPU_METRICS_MEMORY_PRESSURE_THRESHOLD_PERCENT", 85.0)
    psql_command, psql_env = _parse_db_url(project_root)
    buffer: list[dict[str, object]] = []
    next_flush = time.monotonic() + flush_interval
    print(
        "gpu_metrics_host_collector started "
        f"sample_interval={sample_interval}s flush_interval={flush_interval}s raw_retention={raw_retention_hours}h",
        flush=True,
    )
    while not STOP:
        loop_started = time.monotonic()
        try:
            buffer.extend(_query_gpu_once(query_timeout))
        except Exception as exc:  # noqa: BLE001
            print(f"gpu_metrics_sample_failed: {exc}", file=sys.stderr, flush=True)
        if buffer and (time.monotonic() >= next_flush or len(buffer) >= max(1, flush_interval // sample_interval) * 4):
            to_flush = buffer
            buffer = []
            try:
                _flush_samples(
                    psql_command,
                    psql_env,
                    to_flush,
                    raw_retention_hours=raw_retention_hours,
                    rollup_retention_days=rollup_retention_days,
                    saturation_threshold=saturation_threshold,
                    memory_threshold=memory_threshold,
                )
                print(f"gpu_metrics_flushed samples={len(to_flush)}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"gpu_metrics_flush_failed: {exc}", file=sys.stderr, flush=True)
                buffer = to_flush + buffer
            next_flush = time.monotonic() + flush_interval
        elapsed = time.monotonic() - loop_started
        time.sleep(max(0.1, sample_interval - elapsed))
    if buffer:
        _flush_samples(
            psql_command,
            psql_env,
            buffer,
            raw_retention_hours=raw_retention_hours,
            rollup_retention_days=rollup_retention_days,
            saturation_threshold=saturation_threshold,
            memory_threshold=memory_threshold,
        )
    print("gpu_metrics_host_collector stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
