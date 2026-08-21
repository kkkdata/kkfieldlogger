from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import to_utc_iso, utc_now
from app.models import GpuMetricHourlyRollup, GpuMetricSample, TaskJob, TaskStatus


def _round(value: float | int | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100.0, 1)


def _avg(values: list[float | int | None]) -> float | None:
    clean_values = [float(value) for value in values if value is not None]
    if not clean_values:
        return None
    return round(sum(clean_values) / len(clean_values), 1)


def _max(values: list[float | int | None]) -> float | None:
    clean_values = [float(value) for value in values if value is not None]
    if not clean_values:
        return None
    return round(max(clean_values), 1)


def _weighted_average(rows: list[GpuMetricHourlyRollup], attr: str) -> float | None:
    weighted_sum = 0.0
    total_count = 0
    for row in rows:
        value = getattr(row, attr)
        if value is None or row.sample_count <= 0:
            continue
        weighted_sum += float(value) * int(row.sample_count)
        total_count += int(row.sample_count)
    if not total_count:
        return None
    return round(weighted_sum / total_count, 1)


def _latest_gpu_rows(samples: list[GpuMetricSample]) -> list[dict[str, Any]]:
    latest_by_gpu: dict[tuple[str, int], GpuMetricSample] = {}
    for sample in samples:
        key = (sample.hostname, sample.gpu_index)
        current = latest_by_gpu.get(key)
        if current is None or sample.sampled_at > current.sampled_at:
            latest_by_gpu[key] = sample
    rows = []
    now = utc_now()
    for sample in sorted(latest_by_gpu.values(), key=lambda item: (item.hostname, item.gpu_index)):
        memory_total = sample.memory_total_mb or 0
        memory_used = sample.memory_used_mb or 0
        memory_used_percent = (
            sample.utilization_memory_percent
            if sample.utilization_memory_percent is not None
            else _percent(memory_used, memory_total)
        )
        rows.append(
            {
                "hostname": sample.hostname,
                "gpu_index": sample.gpu_index,
                "gpu_name": sample.gpu_name or f"GPU {sample.gpu_index}",
                "sampled_at": to_utc_iso(sample.sampled_at),
                "age_seconds": max(0, int((now - sample.sampled_at).total_seconds())),
                "utilization_gpu_percent": _round(sample.utilization_gpu_percent),
                "utilization_memory_percent": _round(memory_used_percent),
                "memory_used_mb": sample.memory_used_mb,
                "memory_total_mb": sample.memory_total_mb,
                "temperature_c": _round(sample.temperature_c),
                "power_draw_w": _round(sample.power_draw_w),
                "power_limit_w": _round(sample.power_limit_w),
                "fan_speed_percent": _round(sample.fan_speed_percent),
            }
        )
    return rows


def _aggregate_recent(samples: list[GpuMetricSample], settings: Settings) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "avg_gpu_utilization_percent": None,
            "max_gpu_utilization_percent": None,
            "avg_memory_utilization_percent": None,
            "max_memory_utilization_percent": None,
            "avg_power_draw_w": None,
            "max_temperature_c": None,
            "saturated_sample_ratio_percent": 0.0,
            "memory_pressure_sample_ratio_percent": 0.0,
            "chart_points": [],
        }
    gpu_values = [sample.utilization_gpu_percent for sample in samples]
    memory_values = [
        sample.utilization_memory_percent
        if sample.utilization_memory_percent is not None
        else _percent(sample.memory_used_mb or 0, sample.memory_total_mb or 0)
        for sample in samples
    ]
    saturated_count = sum(
        1
        for sample in samples
        if (sample.utilization_gpu_percent or 0) >= settings.gpu_metrics_saturation_threshold_percent
    )
    memory_pressure_count = sum(
        1
        for value in memory_values
        if (value or 0) >= settings.gpu_metrics_memory_pressure_threshold_percent
    )
    chart_stride = max(1, len(samples) // 60)
    chart_points = [
        {
            "sampled_at": to_utc_iso(sample.sampled_at),
            "gpu": _round(sample.utilization_gpu_percent or 0),
            "memory": _round(
                sample.utilization_memory_percent
                if sample.utilization_memory_percent is not None
                else _percent(sample.memory_used_mb or 0, sample.memory_total_mb or 0)
            ),
        }
        for sample in samples[::chart_stride]
    ][-60:]
    return {
        "sample_count": len(samples),
        "avg_gpu_utilization_percent": _avg(gpu_values),
        "max_gpu_utilization_percent": _max(gpu_values),
        "avg_memory_utilization_percent": _avg(memory_values),
        "max_memory_utilization_percent": _max(memory_values),
        "avg_power_draw_w": _avg([sample.power_draw_w for sample in samples]),
        "max_temperature_c": _max([sample.temperature_c for sample in samples]),
        "saturated_sample_ratio_percent": _percent(saturated_count, len(samples)),
        "memory_pressure_sample_ratio_percent": _percent(memory_pressure_count, len(samples)),
        "chart_points": chart_points,
    }


def _aggregate_rollups(rows: list[GpuMetricHourlyRollup], settings: Settings) -> dict[str, Any]:
    if not rows:
        return {
            "sample_count": 0,
            "avg_gpu_utilization_percent": None,
            "max_gpu_utilization_percent": None,
            "avg_memory_utilization_percent": None,
            "max_memory_utilization_percent": None,
            "avg_power_draw_w": None,
            "max_temperature_c": None,
            "saturated_sample_ratio_percent": 0.0,
            "memory_pressure_sample_ratio_percent": 0.0,
            "estimated_energy_kwh": None,
            "estimated_energy_cost": None,
        }
    sample_count = sum(int(row.sample_count or 0) for row in rows)
    avg_power = _weighted_average(rows, "avg_power_draw_w")
    estimated_energy_kwh = round((avg_power or 0.0) * 24.0 / 1000.0, 3) if avg_power is not None else None
    estimated_energy_cost = (
        round(estimated_energy_kwh * settings.gpu_metrics_electricity_rate_per_kwh, 2)
        if estimated_energy_kwh is not None
        else None
    )
    return {
        "sample_count": sample_count,
        "avg_gpu_utilization_percent": _weighted_average(rows, "avg_gpu_utilization_percent"),
        "max_gpu_utilization_percent": _max([row.max_gpu_utilization_percent for row in rows]),
        "avg_memory_utilization_percent": _weighted_average(rows, "avg_memory_utilization_percent"),
        "max_memory_utilization_percent": _max([row.max_memory_utilization_percent for row in rows]),
        "avg_power_draw_w": avg_power,
        "max_temperature_c": _max([row.max_temperature_c for row in rows]),
        "saturated_sample_ratio_percent": _percent(
            sum(int(row.saturated_sample_count or 0) for row in rows),
            sample_count,
        ),
        "memory_pressure_sample_ratio_percent": _percent(
            sum(int(row.memory_pressure_sample_count or 0) for row in rows),
            sample_count,
        ),
        "estimated_energy_kwh": estimated_energy_kwh,
        "estimated_energy_cost": estimated_energy_cost,
    }


def _procurement_assessment(
    *,
    recent: dict[str, Any],
    daily: dict[str, Any],
    latest_rows: list[dict[str, Any]],
    active_ai_jobs: int,
) -> dict[str, Any]:
    avg_gpu = float(daily.get("avg_gpu_utilization_percent") or recent.get("avg_gpu_utilization_percent") or 0.0)
    max_gpu = float(daily.get("max_gpu_utilization_percent") or recent.get("max_gpu_utilization_percent") or 0.0)
    saturation = float(daily.get("saturated_sample_ratio_percent") or recent.get("saturated_sample_ratio_percent") or 0.0)
    max_memory = float(daily.get("max_memory_utilization_percent") or recent.get("max_memory_utilization_percent") or 0.0)
    max_temp = float(daily.get("max_temperature_c") or recent.get("max_temperature_c") or 0.0)
    newest_age = min((row.get("age_seconds") or 999999 for row in latest_rows), default=999999)
    signals: list[dict[str, Any]] = []
    level = "healthy"
    if newest_age > 180:
        level = "stale"
        signals.append({"kind": "collector_stale", "severity": "warning", "value": f"{newest_age}s"})
    if avg_gpu >= 70 or saturation >= 35:
        level = "pressure"
        signals.append({"kind": "gpu_saturation", "severity": "high", "value": f"{saturation:.1f}%"})
    elif avg_gpu >= 45 or saturation >= 10:
        level = "watch" if level == "healthy" else level
        signals.append({"kind": "gpu_utilization", "severity": "medium", "value": f"{avg_gpu:.1f}%"})
    if max_memory >= 90:
        level = "pressure"
        signals.append({"kind": "vram_pressure", "severity": "high", "value": f"{max_memory:.1f}%"})
    elif max_memory >= 75:
        level = "watch" if level == "healthy" else level
        signals.append({"kind": "vram_watch", "severity": "medium", "value": f"{max_memory:.1f}%"})
    if max_temp >= 84:
        level = "watch" if level == "healthy" else level
        signals.append({"kind": "thermal_watch", "severity": "medium", "value": f"{max_temp:.1f}C"})
    if active_ai_jobs > 0 and avg_gpu < 25 and max_gpu < 55:
        signals.append({"kind": "not_gpu_bound", "severity": "info", "value": str(active_ai_jobs)})
    if not signals and latest_rows:
        signals.append({"kind": "healthy_headroom", "severity": "info", "value": f"{max_gpu:.1f}%"})
    return {
        "level": level,
        "signals": signals,
        "active_ai_jobs": active_ai_jobs,
    }


def get_gpu_metrics_overview(db: Session, settings: Settings) -> dict[str, Any]:
    now = utc_now()
    recent_since = now - timedelta(minutes=60)
    latest_since = now - timedelta(minutes=10)
    rollup_since = now - timedelta(hours=24)
    recent_samples = list(
        db.scalars(
            select(GpuMetricSample)
            .where(GpuMetricSample.sampled_at >= recent_since)
            .order_by(GpuMetricSample.sampled_at.asc())
        )
    )
    latest_samples = [sample for sample in recent_samples if sample.sampled_at >= latest_since]
    rollups = list(
        db.scalars(
            select(GpuMetricHourlyRollup)
            .where(GpuMetricHourlyRollup.bucket_start >= rollup_since)
            .order_by(GpuMetricHourlyRollup.bucket_start.asc())
        )
    )
    latest_rows = _latest_gpu_rows(latest_samples or recent_samples[-120:])
    recent = _aggregate_recent(recent_samples, settings)
    daily = _aggregate_rollups(rollups, settings)
    active_ai_job_count = db.scalar(
        select(func.count())
        .select_from(TaskJob)
        .where(TaskJob.status.in_([TaskStatus.queued, TaskStatus.running, TaskStatus.failed]))
    ) or 0
    procurement = _procurement_assessment(
        recent=recent,
        daily=daily,
        latest_rows=latest_rows,
        active_ai_jobs=int(active_ai_job_count or 0),
    )
    return {
        "enabled": settings.gpu_metrics_enabled,
        "available": bool(latest_rows),
        "updated_at": to_utc_iso(now),
        "sample_interval_seconds": settings.gpu_metrics_sample_interval_seconds,
        "flush_interval_seconds": settings.gpu_metrics_flush_interval_seconds,
        "raw_retention_hours": settings.gpu_metrics_raw_retention_hours,
        "rollup_retention_days": settings.gpu_metrics_rollup_retention_days,
        "latest": latest_rows,
        "recent_60m": recent,
        "last_24h": daily,
        "procurement": procurement,
    }
