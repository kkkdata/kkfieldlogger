from __future__ import annotations

import threading
from datetime import timedelta
from time import monotonic
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import collect_system_snapshot, get_logger
from app.core.time import utc_now
from app.models import Company, TaskJob, TaskStatus
from app.services.audit import log_audit
from app.services.billing import get_company_subscription, get_plan
from app.services.email import send_email
from app.services.job_queue import get_company_queue_counts
from app.services.media_pipeline import get_media_storage_monitor
from app.services.settings import get_system_settings


logger = get_logger("kkfieldlogger.system_health")

DEFAULT_SYSTEM_STORAGE_ALERT_THRESHOLD_PERCENT = 85.0
DEFAULT_SYSTEM_ALERT_COOLDOWN_SECONDS = 1800.0
_ALERT_LOCK = threading.Lock()
_LAST_ALERT_AT: dict[str, float] = {}


def _coerce_non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _company_queue_active_jobs(db: Session, company_id: str) -> int:
    counts = get_company_queue_counts(db, company_id)
    total = 0
    for key, value in counts.items():
        if key.endswith(":queued") or key.endswith(":running") or key.endswith(":failed"):
            total += int(value)
    return total


def _storage_limit_mb_for_company(db: Session, company_id: str) -> int | None:
    subscription = get_company_subscription(db, company_id)
    plan = get_plan(db, subscription.plan_id)
    return int(plan.storage_limit_mb or 0)


def _should_emit_alert(key: str, cooldown_seconds: float) -> bool:
    now = monotonic()
    with _ALERT_LOCK:
        last_emitted_at = _LAST_ALERT_AT.get(key, 0.0)
        if now - last_emitted_at < cooldown_seconds:
            return False
        _LAST_ALERT_AT[key] = now
    return True


def get_system_health_overview(
    db: Session,
    app_settings: Settings,
    session_maker: sessionmaker[Session],
    *,
    company_id: str,
    storage_limit_mb: int | None = None,
) -> dict[str, Any]:
    snapshot = collect_system_snapshot(app_settings, session_maker)
    system_settings = get_system_settings(db, app_settings)
    storage_alert_threshold_percent = _coerce_non_negative_float(
        system_settings.get("system_health_storage_alert_threshold_percent"),
        DEFAULT_SYSTEM_STORAGE_ALERT_THRESHOLD_PERCENT,
    ) or DEFAULT_SYSTEM_STORAGE_ALERT_THRESHOLD_PERCENT
    resolved_storage_limit_mb = storage_limit_mb if storage_limit_mb is not None else _storage_limit_mb_for_company(db, company_id)
    storage_monitor = get_media_storage_monitor(
        db,
        app_settings,
        company_id=company_id,
        storage_limit_mb=resolved_storage_limit_mb,
    )
    queue_threshold = app_settings.queue_backlog_warning_threshold
    global_queue_active_jobs = int(snapshot.get("queue_active_jobs") or 0)
    company_queue_active_jobs = _company_queue_active_jobs(db, company_id)
    queue_warning = global_queue_active_jobs >= queue_threshold
    storage_alert = float(storage_monitor.get("usage_rate_percent") or 0.0) >= storage_alert_threshold_percent
    warning_messages: list[str] = []
    if storage_alert:
        warning_messages.append(
            f"Storage usage is above {int(round(storage_alert_threshold_percent))}% of plan capacity."
        )
    if queue_warning:
        warning_messages.append(
            f"Queue backlog is above the configured threshold ({queue_threshold} active jobs)."
        )
    return {
        "cpu_percent": float(snapshot.get("cpu_percent") or 0.0),
        "memory_percent": float(snapshot.get("memory_percent") or 0.0),
        "disk_used_percent": float(snapshot.get("disk_used_percent") or 0.0),
        "postgres_connection_count": snapshot.get("postgres_connection_count"),
        "global_queue_active_jobs": global_queue_active_jobs,
        "company_queue_active_jobs": company_queue_active_jobs,
        "queue_threshold": queue_threshold,
        "storage_usage_rate_percent": float(storage_monitor.get("usage_rate_percent") or 0.0),
        "storage_alert_threshold_percent": storage_alert_threshold_percent,
        "storage_limit_label": storage_monitor.get("storage_limit_label"),
        "warning": bool(storage_alert or queue_warning),
        "storage_warning": storage_alert,
        "queue_warning": queue_warning,
        "warning_messages": warning_messages,
    }


def perform_system_health_audit(session_maker: sessionmaker[Session], app_settings: Settings) -> dict[str, Any]:
    snapshot = collect_system_snapshot(app_settings, session_maker)
    summary = {
        "checked_companies": 0,
        "storage_alerts": 0,
        "queue_alerts": 0,
        "queue_active_jobs": int(snapshot.get("queue_active_jobs") or 0),
    }
    with session_maker() as db:
        system_settings = get_system_settings(db, app_settings)
        storage_alert_threshold_percent = _coerce_non_negative_float(
            system_settings.get("system_health_storage_alert_threshold_percent"),
            DEFAULT_SYSTEM_STORAGE_ALERT_THRESHOLD_PERCENT,
        ) or DEFAULT_SYSTEM_STORAGE_ALERT_THRESHOLD_PERCENT
        alert_cooldown_seconds = _coerce_positive_int(
            system_settings.get("system_health_alert_cooldown_seconds"),
            int(DEFAULT_SYSTEM_ALERT_COOLDOWN_SECONDS),
        )
        company_ids = list(
            db.scalars(
                select(Company.company_id)
                .where(Company.active.is_(True))
                .order_by(Company.company_id.asc())
            )
        )
        summary["checked_companies"] = len(company_ids)

        if summary["queue_active_jobs"] >= app_settings.queue_backlog_warning_threshold and _should_emit_alert(
            "queue_backlog",
            alert_cooldown_seconds,
        ):
            sent = send_email(
                db,
                settings=app_settings,
                to_email=app_settings.support_email,
                subject="[KK] Queue backlog warning",
                html_content=(
                    f"<p>Queue backlog reached <strong>{summary['queue_active_jobs']}</strong> active jobs.</p>"
                    f"<p>Configured threshold: {app_settings.queue_backlog_warning_threshold}</p>"
                ),
                action="system_queue_backlog_alert_sent",
            )
            log_audit(
                db,
                action="system_queue_backlog_alert_sent",
                target_type="system_health",
                target_id="queue_backlog",
                detail_json={
                    "queue_active_jobs": summary["queue_active_jobs"],
                    "threshold": app_settings.queue_backlog_warning_threshold,
                    "email_sent": sent,
                },
            )
            logger.warning(
                "system_queue_backlog_alert_triggered",
                queue_active_jobs=summary["queue_active_jobs"],
                threshold=app_settings.queue_backlog_warning_threshold,
                email_sent=sent,
            )
            summary["queue_alerts"] += 1

        # A total AI-backend outage drains queued jobs into dead_letter, so the
        # active-jobs alert above goes quiet exactly when things are worst.
        # Watch the dead-letter growth rate and the oldest queued job directly.
        now = utc_now()
        dead_letter_24h = int(
            db.scalar(
                select(func.count(TaskJob.id)).where(
                    TaskJob.status == TaskStatus.dead_letter,
                    TaskJob.updated_at >= now - timedelta(hours=24),
                )
            )
            or 0
        )
        summary["dead_letter_jobs_24h"] = dead_letter_24h
        if dead_letter_24h >= app_settings.queue_dead_letter_alert_threshold_24h and _should_emit_alert(
            "queue_dead_letter",
            alert_cooldown_seconds,
        ):
            sent = send_email(
                db,
                settings=app_settings,
                to_email=app_settings.support_email,
                subject="[KK] Job dead-letter warning",
                html_content=(
                    f"<p><strong>{dead_letter_24h}</strong> jobs were dead-lettered in the last 24 hours.</p>"
                    f"<p>Threshold: {app_settings.queue_dead_letter_alert_threshold_24h}. "
                    f"Check AI backend health and replay with the requeue-dead-letter CLI command.</p>"
                ),
                action="system_queue_dead_letter_alert_sent",
            )
            log_audit(
                db,
                action="system_queue_dead_letter_alert_sent",
                target_type="system_health",
                target_id="queue_dead_letter",
                detail_json={
                    "dead_letter_jobs_24h": dead_letter_24h,
                    "threshold": app_settings.queue_dead_letter_alert_threshold_24h,
                    "email_sent": sent,
                },
            )
            logger.warning(
                "system_queue_dead_letter_alert_triggered",
                dead_letter_jobs_24h=dead_letter_24h,
                threshold=app_settings.queue_dead_letter_alert_threshold_24h,
                email_sent=sent,
            )
            summary["queue_alerts"] += 1

        oldest_queued_at = db.scalar(
            select(func.min(TaskJob.created_at)).where(TaskJob.status == TaskStatus.queued)
        )
        if oldest_queued_at is not None:
            if oldest_queued_at.tzinfo is None:
                oldest_age_minutes = (now.replace(tzinfo=None) - oldest_queued_at).total_seconds() / 60.0
            else:
                oldest_age_minutes = (now - oldest_queued_at).total_seconds() / 60.0
            summary["oldest_queued_job_age_minutes"] = round(oldest_age_minutes, 1)
            if oldest_age_minutes >= app_settings.queue_oldest_job_age_warning_minutes and _should_emit_alert(
                "queue_oldest_job",
                alert_cooldown_seconds,
            ):
                sent = send_email(
                    db,
                    settings=app_settings,
                    to_email=app_settings.support_email,
                    subject="[KK] Stalled job queue warning",
                    html_content=(
                        f"<p>The oldest queued job has been waiting <strong>{oldest_age_minutes:.0f} minutes</strong>.</p>"
                        f"<p>Threshold: {app_settings.queue_oldest_job_age_warning_minutes} minutes. "
                        f"Workers may be down or stuck.</p>"
                    ),
                    action="system_queue_stalled_alert_sent",
                )
                log_audit(
                    db,
                    action="system_queue_stalled_alert_sent",
                    target_type="system_health",
                    target_id="queue_oldest_job",
                    detail_json={
                        "oldest_queued_job_age_minutes": round(oldest_age_minutes, 1),
                        "threshold_minutes": app_settings.queue_oldest_job_age_warning_minutes,
                        "email_sent": sent,
                    },
                )
                logger.warning(
                    "system_queue_stalled_alert_triggered",
                    oldest_queued_job_age_minutes=round(oldest_age_minutes, 1),
                    threshold_minutes=app_settings.queue_oldest_job_age_warning_minutes,
                    email_sent=sent,
                )
                summary["queue_alerts"] += 1

        for company_id in company_ids:
            storage_limit_mb = _storage_limit_mb_for_company(db, company_id)
            storage_monitor = get_media_storage_monitor(
                db,
                app_settings,
                company_id=company_id,
                storage_limit_mb=storage_limit_mb,
            )
            usage_rate_percent = float(storage_monitor.get("usage_rate_percent") or 0.0)
            if usage_rate_percent < storage_alert_threshold_percent:
                continue
            if not _should_emit_alert(f"storage:{company_id}", alert_cooldown_seconds):
                continue
            sent = send_email(
                db,
                settings=app_settings,
                to_email=app_settings.support_email,
                subject=f"[KK] Storage capacity warning for {company_id}",
                html_content=(
                    f"<p>Media storage usage for company <strong>{company_id}</strong> has reached "
                    f"<strong>{usage_rate_percent:.2f}%</strong>.</p>"
                    f"<p>Threshold: {storage_alert_threshold_percent:.2f}%</p>"
                    f"<p>Total media: {storage_monitor.get('total_media_label')}</p>"
                    f"<p>Storage limit: {storage_monitor.get('storage_limit_label') or 'unlimited'}</p>"
                ),
                action="system_storage_capacity_alert_sent",
                company_id=company_id,
            )
            log_audit(
                db,
                action="system_storage_capacity_alert_sent",
                target_type="system_health",
                target_id=company_id,
                company_id=company_id,
                detail_json={
                    "usage_rate_percent": usage_rate_percent,
                    "threshold": storage_alert_threshold_percent,
                    "email_sent": sent,
                },
            )
            logger.warning(
                "system_storage_capacity_alert_triggered",
                company_id=company_id,
                usage_rate_percent=usage_rate_percent,
                threshold=storage_alert_threshold_percent,
                email_sent=sent,
            )
            summary["storage_alerts"] += 1
        db.commit()

    logger.info("system_health_audit_completed", **summary)
    return summary
