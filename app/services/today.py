"""Aggregates the manager "today workbench" (今日工作台) landing page.

One service call answers "what needs my attention right now" from data that
already exists: pending approvals, the company-application queue, recovered
or failed mobile uploads, queue health, per-project upload pulse, and a
short activity feed. Everything is scoped to the current user's company.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models import (
    AuditLog,
    CompanyApplication,
    CompanyApplicationStatus,
    ApprovalStatus,
    Photo,
    PhotoType,
    Project,
    TaskJob,
    TaskStatus,
    User,
)
from app.services.access import apply_photo_scope
from app.services.rbac import can_manage_companies

FEED_ACTIONS = (
    "mobile_upload",
    "mobile_upload_project_recovered",
    "photo_labeling_completed",
    "photo_labeling_failed",
    "company_application_submitted",
    "task_job_dead_lettered",
)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def build_today_workbench(
    db: Session,
    current_user: User,
    *,
    accessible_projects: list[Project],
) -> dict[str, Any]:
    now = utc_now()
    week_ago = now - timedelta(days=7)
    two_days_ago = now - timedelta(hours=48)
    day_ago = now - timedelta(hours=24)

    pending_base = apply_photo_scope(
        db, select(Photo.id), current_user, photo_type=PhotoType.project
    ).where(Photo.approval_status == ApprovalStatus.pending)
    pending_recent = _count(db, pending_base.where(Photo.created_at >= week_ago))
    pending_backlog = _count(db, pending_base.where(Photo.created_at < week_ago))

    pending_applications = 0
    if can_manage_companies(current_user):
        pending_applications = int(
            db.scalar(
                select(func.count(CompanyApplication.id)).where(
                    CompanyApplication.status == CompanyApplicationStatus.pending
                )
            )
            or 0
        )

    recovered_uploads = int(
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.company_id == current_user.company_id,
                AuditLog.action == "mobile_upload_project_recovered",
                AuditLog.created_at >= two_days_ago,
            )
        )
        or 0
    )
    auth_failures = int(
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.company_id == current_user.company_id,
                AuditLog.action == "mobile_auth_failed",
                AuditLog.created_at >= two_days_ago,
            )
        )
        or 0
    )

    queue_active = int(
        db.scalar(
            select(func.count(TaskJob.id)).where(
                TaskJob.status.in_([TaskStatus.queued, TaskStatus.running]),
                TaskJob.company_id == current_user.company_id,
            )
        )
        or 0
    )
    dead_letter_24h = int(
        db.scalar(
            select(func.count(TaskJob.id)).where(
                TaskJob.status == TaskStatus.dead_letter,
                TaskJob.company_id == current_user.company_id,
                TaskJob.updated_at >= day_ago,
            )
        )
        or 0
    )

    uploads_yesterday = _count(
        db,
        apply_photo_scope(db, select(Photo.id), current_user, photo_type=PhotoType.project).where(
            Photo.created_at >= day_ago
        ),
    )

    project_ids = [project.project_id for project in accessible_projects]
    daily_counts: dict[str, dict[str, int]] = {}
    last_upload_at: dict[str, Any] = {}
    if project_ids:
        rows = db.execute(
            select(
                Photo.project_id,
                func.date(Photo.created_at),
                func.count(Photo.id),
            )
            .where(
                Photo.project_id.in_(project_ids),
                Photo.company_id == current_user.company_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.created_at >= week_ago,
            )
            .group_by(Photo.project_id, func.date(Photo.created_at))
        ).all()
        for project_id, day, count in rows:
            daily_counts.setdefault(project_id, {})[str(day)] = int(count)
        for project_id, latest in db.execute(
            select(Photo.project_id, func.max(Photo.created_at))
            .where(
                Photo.project_id.in_(project_ids),
                Photo.company_id == current_user.company_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
            )
            .group_by(Photo.project_id)
        ).all():
            last_upload_at[project_id] = latest

    day_keys = [(now - timedelta(days=offset)).date().isoformat() for offset in range(6, -1, -1)]
    pulse = []
    for project in accessible_projects:
        per_day = [daily_counts.get(project.project_id, {}).get(day, 0) for day in day_keys]
        total_week = sum(per_day)
        latest = last_upload_at.get(project.project_id)
        if latest is not None and latest.tzinfo is None:
            days_silent = (now.replace(tzinfo=None) - latest).days
        elif latest is not None:
            days_silent = (now - latest).days
        else:
            days_silent = None
        if total_week == 0 and days_silent is not None and days_silent >= 7:
            status = "stalled"
        elif total_week == 0:
            status = "slowing"
        elif sum(per_day[4:]) == 0:
            status = "slowing"
        else:
            status = "ok"
        pulse.append(
            {
                "project_id": project.project_id,
                "project_name": project.project_name or project.project_id,
                "per_day": per_day,
                "total_week": total_week,
                "days_silent": days_silent,
                "status": status,
            }
        )
    pulse.sort(key=lambda item: (item["status"] != "stalled", item["status"] != "slowing", -item["total_week"]))

    feed_rows = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.company_id == current_user.company_id,
                AuditLog.action.in_(FEED_ACTIONS),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(8)
        )
    )

    # Pending approvals deliberately excluded: client curation is dormant by
    # design (no client-viewing plan), so the count is informational, not an
    # action item.
    action_count = sum(
        1
        for flag in (pending_applications > 0, recovered_uploads + auth_failures > 0)
        if flag
    )

    return {
        "today_data": {
            "action_count": action_count,
            "uploads_yesterday": uploads_yesterday,
            "pending_recent": pending_recent,
            "pending_backlog": pending_backlog,
            "pending_applications": pending_applications,
            "recovered_uploads": recovered_uploads,
            "auth_failures": auth_failures,
            "queue_active": queue_active,
            "dead_letter_24h": dead_letter_24h,
            "pulse": pulse[:8],
            "feed": feed_rows,
        }
    }
