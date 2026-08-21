from __future__ import annotations

from datetime import timedelta
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import utc_now
from app.models import GeneratedReport, Photo, ProgressReport, ProgressReportStatus, ReportStatus, TaskJob, TaskStatus
from app.services.audit import log_audit

logger = get_logger("kkfieldlogger.job_queue")

PHOTO_AI_TASK = "photo_ai_pipeline"
REPORT_TASK = "report_generation"
VIDEO_MEDIA_TASK = "video_media_pipeline"
ANNOTATION_TRANSCRIPTION_TASK = "annotation_transcription"
PROGRESS_REPORT_TASK = "progress_compare_generation"
COPILOT_CHAT_TASK = "copilot_chat_generation"
DEFAULT_TASK_DURATION_SECONDS = {
    PHOTO_AI_TASK: 45,
    REPORT_TASK: 90,
    VIDEO_MEDIA_TASK: 180,
    ANNOTATION_TRANSCRIPTION_TASK: 30,
    PROGRESS_REPORT_TASK: 240,
    COPILOT_CHAT_TASK: 180,
}


def _active_statuses() -> tuple[TaskStatus, ...]:
    return (TaskStatus.queued, TaskStatus.running, TaskStatus.failed)


def get_company_queue_counts(db: Session, company_id: str) -> dict[str, int]:
    rows = db.execute(
        select(TaskJob.task_type, TaskJob.status, func.count())
        .where(TaskJob.company_id == company_id)
        .group_by(TaskJob.task_type, TaskJob.status)
    ).all()
    counts: dict[str, int] = {}
    for task_type, status, count in rows:
        counts[f"{task_type}:{status.value if hasattr(status, 'value') else status}"] = int(count)
    return counts


def average_task_duration_seconds(db: Session, company_id: str, task_type: str) -> float:
    completed_jobs = list(
        db.scalars(
            select(TaskJob)
            .where(
                TaskJob.company_id == company_id,
                TaskJob.task_type == task_type,
                TaskJob.status == TaskStatus.completed,
                TaskJob.started_at.is_not(None),
                TaskJob.completed_at.is_not(None),
            )
            .order_by(TaskJob.completed_at.desc())
            .limit(50)
        )
    )
    durations: list[float] = []
    for job in completed_jobs:
        if job.started_at is None or job.completed_at is None:
            continue
        durations.append(max(1.0, (job.completed_at - job.started_at).total_seconds()))
    if durations:
        return round(sum(durations) / len(durations), 2)
    return float(DEFAULT_TASK_DURATION_SECONDS.get(task_type, 60))


def get_task_queue_snapshot(db: Session, company_id: str, task_type: str) -> dict[str, int | float]:
    rows = db.execute(
        select(TaskJob.status, func.count())
        .where(TaskJob.company_id == company_id, TaskJob.task_type == task_type)
        .group_by(TaskJob.status)
    ).all()
    queued_count = 0
    running_count = 0
    retry_pending_count = 0
    for status, count in rows:
        if status == TaskStatus.queued:
            queued_count = int(count)
        elif status == TaskStatus.running:
            running_count = int(count)
        elif status == TaskStatus.failed:
            retry_pending_count = int(count)
    active_count = queued_count + running_count + retry_pending_count
    average_duration_seconds = average_task_duration_seconds(db, company_id, task_type)
    estimated_wait_seconds = int(round(max(1, active_count) * average_duration_seconds)) if active_count else 0
    return {
        "queued_count": queued_count,
        "running_count": running_count,
        "retry_pending_count": retry_pending_count,
        "active_count": active_count,
        "average_duration_seconds": average_duration_seconds,
        "estimated_wait_seconds": estimated_wait_seconds,
    }


def _count_active_jobs_for_tenant(db: Session, company_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(
            select(TaskJob.id)
            .where(TaskJob.company_id == company_id, TaskJob.status.in_(_active_statuses()))
            .subquery()
        )
    ) or 0


def assert_tenant_queue_capacity(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
) -> None:
    active_jobs = _count_active_jobs_for_tenant(db, company_id)
    if active_jobs >= app_settings.queue_max_pending_jobs_per_tenant:
        logger.warning(
            "queue_capacity_exceeded",
            company_id=company_id,
            active_jobs=active_jobs,
            limit=app_settings.queue_max_pending_jobs_per_tenant,
        )
        raise RuntimeError("Tenant queue capacity exceeded")


def _warn_if_queue_backlog(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    tenant_id: str | None,
) -> None:
    active_jobs = _count_active_jobs_for_tenant(db, company_id)
    if active_jobs >= app_settings.queue_backlog_warning_threshold:
        logger.warning(
            "queue_backlog_warning",
            company_id=company_id,
            tenant_id=tenant_id or company_id,
            active_jobs=active_jobs,
            threshold=app_settings.queue_backlog_warning_threshold,
        )


def enqueue_task_job(
    db: Session,
    *,
    company_id: str,
    tenant_id: str | None,
    task_type: str,
    payload_json: dict,
    related_type: str,
    related_id: str,
    actor_user_id: int | None,
    priority: int,
    max_attempts: int,
) -> TaskJob:
    job = TaskJob(
        public_id=str(uuid4()),
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        created_by_user_id=actor_user_id,
        task_type=task_type,
        status=TaskStatus.queued,
        priority=priority,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=utc_now(),
        payload_json=payload_json,
        related_type=related_type,
        related_id=related_id,
    )
    db.add(job)
    db.flush()
    logger.info(
        "task_job_enqueued",
        task_id=job.public_id,
        task_type=job.task_type,
        company_id=company_id,
        tenant_id=tenant_id or company_id,
        related_type=related_type,
        related_id=related_id,
        priority=priority,
        max_attempts=max_attempts,
    )
    return job


def enqueue_photo_ai_task(
    db: Session,
    *,
    app_settings: Settings,
    photo: Photo,
    actor_user_id: int | None,
    custom_prompt: str | None,
    trigger_source: str,
    priority: str,
    batch_id: str | None = None,
) -> TaskJob:
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=photo.company_id)
    job = enqueue_task_job(
        db,
        company_id=photo.company_id,
        tenant_id=photo.tenant_id,
        task_type=PHOTO_AI_TASK,
        payload_json={
            "photo_id": photo.id,
            "batch_id": batch_id,
            "custom_prompt": custom_prompt,
            "triggered_by_user_id": actor_user_id,
            "trigger_source": trigger_source,
            "priority": priority,
        },
        related_type="photo",
        related_id=str(photo.id),
        actor_user_id=actor_user_id,
        priority=200 if priority == "high" else 100,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=photo.company_id, tenant_id=photo.tenant_id)
    return job


def enqueue_report_task(
    db: Session,
    *,
    app_settings: Settings,
    report: GeneratedReport,
) -> TaskJob:
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=report.company_id)
    job = enqueue_task_job(
        db,
        company_id=report.company_id,
        tenant_id=report.tenant_id,
        task_type=REPORT_TASK,
        payload_json={"report_id": report.id},
        related_type="report",
        related_id=report.public_id,
        actor_user_id=report.created_by_user_id,
        priority=150,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=report.company_id, tenant_id=report.tenant_id)
    return job


def enqueue_video_asset_task(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    tenant_id: str | None,
    asset_id: str,
    actor_user_id: int | None,
    source: str,
) -> TaskJob:
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=company_id)
    job = enqueue_task_job(
        db,
        company_id=company_id,
        tenant_id=tenant_id,
        task_type=VIDEO_MEDIA_TASK,
        payload_json={
            "asset_id": asset_id,
            "source": source,
        },
        related_type="media_asset",
        related_id=asset_id,
        actor_user_id=actor_user_id,
        priority=120,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=company_id, tenant_id=tenant_id)
    return job


def enqueue_annotation_transcription_task(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    tenant_id: str | None,
    annotation_ids: list[str],
    actor_user_id: int | None,
) -> TaskJob:
    if not annotation_ids:
        raise ValueError("annotation_ids must not be empty")
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=company_id)
    job = enqueue_task_job(
        db,
        company_id=company_id,
        tenant_id=tenant_id,
        task_type=ANNOTATION_TRANSCRIPTION_TASK,
        payload_json={
            "annotation_ids": annotation_ids,
            "company_id": company_id,
        },
        related_type="media_annotation",
        related_id=str(annotation_ids[0]),
        actor_user_id=actor_user_id,
        priority=110,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=company_id, tenant_id=tenant_id)
    return job


def enqueue_progress_report_task(
    db: Session,
    *,
    app_settings: Settings,
    report: ProgressReport,
    custom_prompt: str | None = None,
) -> TaskJob:
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=report.company_id)
    job = enqueue_task_job(
        db,
        company_id=report.company_id,
        tenant_id=report.tenant_id,
        task_type=PROGRESS_REPORT_TASK,
        payload_json={"report_id": report.id, "custom_prompt": custom_prompt},
        related_type="progress_report",
        related_id=report.id,
        actor_user_id=report.created_by_user_id,
        priority=160,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=report.company_id, tenant_id=report.tenant_id)
    return job


def enqueue_copilot_message_task(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    tenant_id: str | None,
    message_id: str,
    actor_user_id: int | None,
) -> TaskJob:
    assert_tenant_queue_capacity(db, app_settings=app_settings, company_id=company_id)
    job = enqueue_task_job(
        db,
        company_id=company_id,
        tenant_id=tenant_id,
        task_type=COPILOT_CHAT_TASK,
        payload_json={"message_id": message_id},
        related_type="copilot_message",
        related_id=message_id,
        actor_user_id=actor_user_id,
        priority=170,
        max_attempts=app_settings.queue_max_attempts,
    )
    _warn_if_queue_backlog(db, app_settings=app_settings, company_id=company_id, tenant_id=tenant_id)
    return job


def schedule_job_worker(
    schedule_task,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    *,
    worker_name: str = "inline-request",
) -> None:
    if not app_settings.queue_inline_request_worker_enabled:
        return
    schedule_task(
        process_due_jobs,
        session_maker,
        app_settings,
        app_settings.queue_worker_batch_size,
        worker_name,
    )


def _claim_next_job(db: Session, *, worker_name: str) -> TaskJob | None:
    now = utc_now()
    stmt = (
        select(TaskJob)
        .where(
            TaskJob.status.in_([TaskStatus.queued, TaskStatus.failed]),
            TaskJob.available_at <= now,
            TaskJob.attempt_count < TaskJob.max_attempts,
        )
        .order_by(TaskJob.priority.desc(), TaskJob.created_at.asc())
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    job = db.scalars(stmt).first()
    if job is None:
        return None

    job.status = TaskStatus.running
    job.attempt_count += 1
    job.started_at = now
    job.locked_by = worker_name
    job.last_error = None
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _mark_job_completed(db: Session, job_id: int) -> None:
    job = db.get(TaskJob, job_id)
    if job is None:
        return
    job.status = TaskStatus.completed
    job.completed_at = utc_now()
    job.locked_by = None
    job.last_error = None
    db.add(job)
    db.commit()


def _reset_related_target_for_retry(db: Session, job: TaskJob) -> None:
    if job.related_type == "photo" and job.related_id and job.related_id.isdigit():
        photo = db.get(Photo, int(job.related_id))
        if photo is not None:
            photo.labeling_status = "pending"
            db.add(photo)
    elif job.related_type == "report" and job.related_id:
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == job.related_id))
        if report is not None:
            report.status = ReportStatus.queued
            report.error_message = None
            db.add(report)
    elif job.related_type == "progress_report" and job.related_id:
        report = db.get(ProgressReport, job.related_id)
        if report is not None:
            report.status = ProgressReportStatus.pending
            report.error_message = None
            db.add(report)
    elif job.related_type == "copilot_message" and job.related_id:
        from app.models import CopilotMessage, CopilotMessageStatus

        message = db.get(CopilotMessage, job.related_id)
        if message is not None:
            message.status = CopilotMessageStatus.pending
            message.error_message = None
            db.add(message)
    elif job.related_type == "media_asset" and job.related_id:
        from app.models import MediaAsset, MediaAssetStatus

        asset = db.get(MediaAsset, job.related_id)
        if asset is not None and asset.status not in {MediaAssetStatus.deleted, MediaAssetStatus.archived}:
            asset.status = MediaAssetStatus.processing
            asset.error_message = None
            db.add(asset)
    elif job.related_type == "media_annotation":
        from app.models import AnnotationStatus, MediaAnnotation

        annotation_ids = []
        if isinstance(job.payload_json, dict):
            raw_ids = job.payload_json.get("annotation_ids") or []
            annotation_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        if not annotation_ids and job.related_id:
            annotation_ids = [str(job.related_id)]
        for annotation_id in annotation_ids:
            annotation = db.get(MediaAnnotation, annotation_id)
            if annotation is not None:
                annotation.status = AnnotationStatus.processing
                annotation.content_text = None
                annotation.source_language = None
                annotation.translations_json = None
                db.add(annotation)


def requeue_task_job(
    db: Session,
    *,
    app_settings: Settings,
    job: TaskJob,
    actor_user_id: int | None,
    reason: str = "manual_retry",
) -> TaskJob:
    if job.status not in {TaskStatus.failed, TaskStatus.dead_letter}:
        raise ValueError("Only failed or manual-review tasks can be retried")

    job.status = TaskStatus.queued
    job.attempt_count = 0
    job.max_attempts = max(int(job.max_attempts or 0), int(app_settings.queue_max_attempts or 1), 1)
    job.available_at = utc_now()
    job.started_at = None
    job.completed_at = None
    job.locked_by = None
    job.last_error = None
    _reset_related_target_for_retry(db, job)
    db.add(job)
    log_audit(
        db,
        action="task_job_manual_retry_requested",
        target_type="task_job",
        target_id=job.public_id,
        actor_user_id=actor_user_id,
        tenant_id=job.tenant_id or job.company_id,
        company_id=job.company_id,
        detail_json={
            "task_type": job.task_type,
            "related_type": job.related_type,
            "related_id": job.related_id,
            "reason": reason,
            "max_attempts": job.max_attempts,
        },
    )
    logger.info(
        "task_job_manual_retry_requested",
        task_id=job.public_id,
        task_type=job.task_type,
        company_id=job.company_id,
        tenant_id=job.tenant_id or job.company_id,
        actor_user_id=actor_user_id,
    )
    return job


def _mark_related_target_as_failed(db: Session, job: TaskJob, *, error_message: str) -> None:
    if job.related_type == "photo" and job.related_id and job.related_id.isdigit():
        photo = db.get(Photo, int(job.related_id))
        if photo is not None:
            photo.labeling_status = "failed"
            db.add(photo)
        return

    if job.related_type == "report" and job.related_id:
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == job.related_id))
        if report is not None:
            report.status = ReportStatus.failed
            report.error_message = error_message
            db.add(report)
        return

    if job.related_type == "progress_report" and job.related_id:
        report = db.get(ProgressReport, job.related_id)
        if report is not None:
            report.status = ProgressReportStatus.failed
            report.error_message = error_message
            db.add(report)
        return

    if job.related_type == "copilot_message" and job.related_id:
        from app.models import CopilotMessage, CopilotMessageStatus

        message = db.get(CopilotMessage, job.related_id)
        if message is not None:
            message.status = CopilotMessageStatus.failed
            message.error_message = error_message
            message.completed_at = utc_now()
            db.add(message)
        return

    if job.related_type == "media_asset" and job.related_id:
        from app.models import MediaAsset, MediaAssetStatus

        asset = db.get(MediaAsset, job.related_id)
        if asset is not None:
            asset.status = MediaAssetStatus.failed
            asset.error_message = error_message
            db.add(asset)
        return

    if job.related_type == "media_annotation":
        from app.models import AnnotationStatus, MediaAnnotation

        annotation_ids = []
        if isinstance(job.payload_json, dict):
            raw_ids = job.payload_json.get("annotation_ids") or []
            annotation_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        if not annotation_ids and job.related_id:
            annotation_ids = [str(job.related_id)]
        for annotation_id in annotation_ids:
            annotation = db.get(MediaAnnotation, annotation_id)
            if annotation is not None:
                annotation.status = AnnotationStatus.failed
                db.add(annotation)


def recover_stale_running_jobs(
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    *,
    max_jobs: int | None = None,
    worker_name: str = "stale-recovery",
) -> int:
    stale_timeout_seconds = max(60, int(app_settings.queue_running_timeout_seconds))
    recovery_limit = max_jobs or max(1, int(app_settings.queue_stale_recovery_batch_size))
    stale_before = utc_now() - timedelta(seconds=stale_timeout_seconds)

    with session_maker() as db:
        stmt = (
            select(TaskJob)
            .where(
                TaskJob.status == TaskStatus.running,
                TaskJob.started_at.is_not(None),
                TaskJob.started_at <= stale_before,
            )
            .order_by(TaskJob.started_at.asc())
            .limit(recovery_limit)
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        jobs = list(db.scalars(stmt))
        if not jobs:
            return 0

        now = utc_now()
        recovered = 0
        for job in jobs:
            stale_seconds = 0
            if job.started_at is not None:
                started_at = job.started_at
                comparison_now = now
                if getattr(started_at, "tzinfo", None) is None and getattr(comparison_now, "tzinfo", None) is not None:
                    comparison_now = comparison_now.replace(tzinfo=None)
                elif getattr(started_at, "tzinfo", None) is not None and getattr(comparison_now, "tzinfo", None) is None:
                    started_at = started_at.replace(tzinfo=None)
                stale_seconds = max(0, int((comparison_now - started_at).total_seconds()))
            error_message = (
                f"Recovered stale running job after {stale_seconds} seconds without completion."
            )
            job.locked_by = None
            job.last_error = error_message
            if job.attempt_count >= job.max_attempts:
                job.status = TaskStatus.dead_letter
                job.completed_at = now
                _mark_related_target_as_failed(db, job, error_message=error_message)
                logger.error(
                    "task_job_stale_dead_lettered",
                    task_id=job.public_id,
                    task_type=job.task_type,
                    company_id=job.company_id,
                    tenant_id=job.tenant_id or job.company_id,
                    related_type=job.related_type,
                    related_id=job.related_id,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    stale_seconds=stale_seconds,
                    worker_name=worker_name,
                )
                log_audit(
                    db,
                    action="task_job_stale_dead_lettered",
                    target_type="task_job",
                    target_id=job.public_id,
                    actor_user_id=job.created_by_user_id,
                    tenant_id=job.tenant_id or job.company_id,
                    company_id=job.company_id,
                    detail_json={
                        "task_type": job.task_type,
                        "related_type": job.related_type,
                        "related_id": job.related_id,
                        "attempt_count": job.attempt_count,
                        "max_attempts": job.max_attempts,
                        "stale_seconds": stale_seconds,
                    },
                )
            else:
                job.status = TaskStatus.failed
                job.available_at = now + timedelta(
                    seconds=app_settings.queue_retry_delay_seconds * max(1, job.attempt_count)
                )
                job.completed_at = None
                _reset_related_target_for_retry(db, job)
                logger.warning(
                    "task_job_stale_recovered",
                    task_id=job.public_id,
                    task_type=job.task_type,
                    company_id=job.company_id,
                    tenant_id=job.tenant_id or job.company_id,
                    related_type=job.related_type,
                    related_id=job.related_id,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    stale_seconds=stale_seconds,
                    worker_name=worker_name,
                )
                log_audit(
                    db,
                    action="task_job_stale_recovered",
                    target_type="task_job",
                    target_id=job.public_id,
                    actor_user_id=job.created_by_user_id,
                    tenant_id=job.tenant_id or job.company_id,
                    company_id=job.company_id,
                    detail_json={
                        "task_type": job.task_type,
                        "related_type": job.related_type,
                        "related_id": job.related_id,
                        "attempt_count": job.attempt_count,
                        "max_attempts": job.max_attempts,
                        "stale_seconds": stale_seconds,
                    },
                )
            db.add(job)
            recovered += 1
        db.commit()
        return recovered


def _mark_job_failed(
    db: Session,
    *,
    app_settings: Settings,
    job_id: int,
    error: Exception,
) -> None:
    job = db.get(TaskJob, job_id)
    if job is None:
        return

    job.locked_by = None
    job.last_error = str(error)
    if job.attempt_count >= job.max_attempts:
        job.status = TaskStatus.dead_letter
        job.completed_at = utc_now()
        _mark_related_target_as_failed(db, job, error_message=str(error))
        logger.error(
            "task_job_dead_lettered",
            task_id=job.public_id,
            task_type=job.task_type,
            company_id=job.company_id,
            tenant_id=job.tenant_id or job.company_id,
            related_type=job.related_type,
            related_id=job.related_id,
            attempts=job.attempt_count,
            max_attempts=job.max_attempts,
            error=str(error),
        )
        log_audit(
            db,
            action="task_job_dead_lettered",
            target_type="task_job",
            target_id=job.public_id,
            actor_user_id=job.created_by_user_id,
            tenant_id=job.tenant_id or job.company_id,
            company_id=job.company_id,
            detail_json={
                "task_type": job.task_type,
                "related_type": job.related_type,
                "related_id": job.related_id,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "error": str(error),
            },
        )
    else:
        job.status = TaskStatus.failed
        job.available_at = utc_now() + timedelta(seconds=app_settings.queue_retry_delay_seconds * job.attempt_count)
        _reset_related_target_for_retry(db, job)
        retry_delay_seconds = app_settings.queue_retry_delay_seconds * job.attempt_count
        logger.warning(
            "task_job_retry_scheduled",
            task_id=job.public_id,
            task_type=job.task_type,
            company_id=job.company_id,
            tenant_id=job.tenant_id or job.company_id,
            related_type=job.related_type,
            related_id=job.related_id,
            attempt=job.attempt_count,
            max_attempts=job.max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            error=str(error),
        )
    db.add(job)
    db.commit()


def _dispatch_job(job: TaskJob, session_maker: sessionmaker[Session], app_settings: Settings) -> None:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    if job.task_type == PHOTO_AI_TASK:
        from app.services.ai_pipeline import process_photo_with_ai

        process_photo_with_ai(
            int(payload["photo_id"]),
            session_maker,
            app_settings,
            custom_prompt=payload.get("custom_prompt"),
            triggered_by_user_id=payload.get("triggered_by_user_id"),
            trigger_source=payload.get("trigger_source") or "queue",
            priority=payload.get("priority") or "normal",
            raise_on_failure=True,
            batch_id=payload.get("batch_id"),
        )
        return

    if job.task_type == REPORT_TASK:
        from app.services.reports import process_generated_report

        process_generated_report(
            int(payload["report_id"]),
            session_maker,
            app_settings,
            True,
        )
        return

    if job.task_type == VIDEO_MEDIA_TASK:
        from app.services.media_pipeline import process_media_asset

        process_media_asset(
            str(payload["asset_id"]),
            session_maker,
            app_settings,
            raise_on_failure=True,
        )
        return

    if job.task_type == ANNOTATION_TRANSCRIPTION_TASK:
        from app.services.annotations import process_voice_annotation_batch

        process_voice_annotation_batch(
            [str(item) for item in (payload.get("annotation_ids") or []) if str(item).strip()],
            session_maker,
            app_settings,
            company_id=str(payload.get("company_id") or job.company_id),
            raise_on_failure=True,
        )
        return

    if job.task_type == COPILOT_CHAT_TASK:
        from app.services.evidence_copilot import process_copilot_assistant_message

        process_copilot_assistant_message(
            str(payload["message_id"]),
            session_maker,
            app_settings,
            raise_on_failure=True,
        )
        return

    if job.task_type == PROGRESS_REPORT_TASK:
        from app.services.reports import process_progress_report

        process_progress_report(
            str(payload["report_id"]),
            session_maker,
            app_settings,
            custom_prompt=str(payload.get("custom_prompt") or "").strip() or None,
            raise_on_failure=True,
        )
        return

    raise RuntimeError(f"Unsupported task type: {job.task_type}")


def process_due_jobs(
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    max_jobs: int | None = None,
    worker_name: str = "inline-request",
) -> int:
    processed = 0
    batch_limit = max_jobs or app_settings.queue_worker_batch_size
    recover_stale_running_jobs(
        session_maker,
        app_settings,
        max_jobs=app_settings.queue_stale_recovery_batch_size,
        worker_name=f"{worker_name}:recovery",
    )
    while processed < batch_limit:
        with session_maker() as db:
            job = _claim_next_job(db, worker_name=worker_name)
        if job is None:
            break

        started = perf_counter()
        try:
            _dispatch_job(job, session_maker, app_settings)
            with session_maker() as db:
                _mark_job_completed(db, job.id)
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.info(
                "task_job_completed",
                task_id=job.public_id,
                task_type=job.task_type,
                company_id=job.company_id,
                tenant_id=job.tenant_id or job.company_id,
                duration_ms=duration_ms,
                attempt_count=job.attempt_count,
                worker_name=worker_name,
                related_type=job.related_type,
                related_id=job.related_id,
            )
        except Exception as exc:
            with session_maker() as db:
                _mark_job_failed(db, app_settings=app_settings, job_id=job.id, error=exc)
        processed += 1
    return processed
