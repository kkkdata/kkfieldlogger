"""Daily retention/compaction pass.

Derived data grows ~70 rows per photo (analysis logs, observations, audit
entries, job history); on an appliance sharing disk with the media this is
the realistic way the box fills up. Every rule is off when its setting is 0.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import utc_now
from app.models import (
    AIAnalysisLog,
    AIAnalysisStatus,
    AuditLog,
    Photo,
    PhotoLensObservationRun,
    TaskJob,
    TaskStatus,
)

logger = get_logger("kkfieldlogger.retention")

RECYCLE_BATCH_SIZE = 50


def run_retention_pass(session_maker: sessionmaker[Session], settings: Settings) -> dict[str, Any]:
    now = utc_now()
    summary: dict[str, Any] = {}

    with session_maker() as db:
        if settings.retention_superseded_ai_log_days > 0:
            cutoff = now - timedelta(days=settings.retention_superseded_ai_log_days)
            referenced = select(PhotoLensObservationRun.ai_analysis_log_id).where(
                PhotoLensObservationRun.ai_analysis_log_id.is_not(None)
            )
            result = db.execute(
                delete(AIAnalysisLog).where(
                    AIAnalysisLog.status != AIAnalysisStatus.active,
                    AIAnalysisLog.created_at < cutoff,
                    AIAnalysisLog.id.not_in(referenced),
                )
            )
            summary["superseded_ai_logs_deleted"] = int(result.rowcount or 0)

        if settings.retention_audit_log_days > 0:
            cutoff = now - timedelta(days=settings.retention_audit_log_days)
            result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
            summary["audit_logs_deleted"] = int(result.rowcount or 0)

        if settings.retention_completed_job_days > 0:
            cutoff = now - timedelta(days=settings.retention_completed_job_days)
            result = db.execute(
                delete(TaskJob).where(
                    TaskJob.status == TaskStatus.completed,
                    TaskJob.updated_at < cutoff,
                )
            )
            summary["completed_jobs_deleted"] = int(result.rowcount or 0)

        db.commit()

    if settings.retention_recycle_bin_days > 0:
        from app.services.photos import hard_delete_photo

        cutoff = now - timedelta(days=settings.retention_recycle_bin_days)
        purged = 0
        with session_maker() as db:
            photos = list(
                db.scalars(
                    select(Photo)
                    .where(Photo.deleted.is_(True), Photo.soft_deleted_at < cutoff)
                    .limit(RECYCLE_BATCH_SIZE)
                )
            )
            for photo in photos:
                try:
                    hard_delete_photo(
                        db,
                        app_settings=settings,
                        photo=photo,
                        actor_user=None,
                        request=None,
                        detail={"reason": "recycle_bin_retention", "retention_days": settings.retention_recycle_bin_days},
                    )
                    purged += 1
                except Exception:
                    logger.exception("retention_recycle_purge_failed", photo_id=photo.id)
                    db.rollback()
        summary["recycled_photos_purged"] = purged

    if summary:
        logger.info("retention_pass_completed", **summary)
    return summary
