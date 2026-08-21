from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import to_utc_iso, utc_now
from app.models import (
    ApprovalStatus,
    AuditLog,
    Employee,
    Photo,
    PhotoType,
    PhotoVisibility,
    Project,
    ReviewSession,
    ReviewSessionStatus,
    ReviewTask,
    ReviewTaskEvent,
    ReviewTaskEventType,
    ReviewTaskStatus,
    ReviewTaskType,
    User,
)
from app.services.audit import log_audit


EMPLOYEE_VISIBLE_TASK_TYPES = {
    ReviewTaskType.retake_photo,
    ReviewTaskType.clarify_photo,
    ReviewTaskType.correction_followup,
}
MANAGER_TASK_ACTIONS = {"cancel", "comment", "reopen"}
EMPLOYEE_TASK_ACTIONS = {"acknowledge", "comment", "complete"}
FORBIDDEN_TASK_MESSAGE_TERMS = {
    "must",
    "urgent",
    "priority",
    "recommend",
    "should",
    "ensure",
    "performance",
    "ranking",
    "score",
    "blame",
    "fault",
    "责任",
    "绩效",
    "排名",
    "评分",
    "不努力",
    "必须",
    "紧急",
    "优先",
    "建议",
}


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _task_message(value: str | None, *, required: bool = True) -> str:
    text = " ".join((value or "").strip().split())
    if required and not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="message is required")
    lowered = text.casefold()
    blocked = sorted(term for term in FORBIDDEN_TASK_MESSAGE_TERMS if term.casefold() in lowered)
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Task message contains disallowed management language", "blocked_terms": blocked},
        )
    return text


def coerce_review_task_type(value: str) -> ReviewTaskType:
    try:
        return ReviewTaskType(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid review task type") from exc


def serialize_review_task(task: ReviewTask, *, include_internal: bool = False) -> dict[str, Any]:
    if task.task_type == ReviewTaskType.internal_note and not include_internal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return {
        "public_id": task.public_id,
        "company_id": task.company_id,
        "project_id": task.project_id,
        "related_photo_id": task.related_photo_id,
        "task_type": _enum_value(task.task_type),
        "status": _enum_value(task.status),
        "created_by_user_id": task.created_by_user_id,
        "assigned_employee_id": task.assigned_employee_id,
        "message": task.message,
        "due_at": to_utc_iso(task.due_at),
        "completion_photo_id": task.completion_photo_id,
        "acknowledged_at": to_utc_iso(task.acknowledged_at),
        "completed_at": to_utc_iso(task.completed_at),
        "cancelled_at": to_utc_iso(task.cancelled_at),
        "metadata": task.metadata_json if isinstance(task.metadata_json, dict) else {},
        "created_at": to_utc_iso(task.created_at),
        "updated_at": to_utc_iso(task.updated_at),
    }


def serialize_review_session(session: ReviewSession) -> dict[str, Any]:
    return {
        "public_id": session.public_id,
        "company_id": session.company_id,
        "project_id": session.project_id,
        "review_date": session.review_date,
        "status": _enum_value(session.status),
        "created_by_user_id": session.created_by_user_id,
        "closed_by_user_id": session.closed_by_user_id,
        "summary": session.summary_json if isinstance(session.summary_json, dict) else {},
        "metadata": session.metadata_json if isinstance(session.metadata_json, dict) else {},
        "created_at": to_utc_iso(session.created_at),
        "updated_at": to_utc_iso(session.updated_at),
        "closed_at": to_utc_iso(session.closed_at),
    }


def add_review_task_event(
    db: Session,
    *,
    task: ReviewTask,
    event_type: ReviewTaskEventType,
    actor_user_id: int | None = None,
    actor_employee_id: str | None = None,
    message: str | None = None,
    payload_json: dict[str, Any] | None = None,
) -> ReviewTaskEvent:
    event = ReviewTaskEvent(
        public_id=str(uuid4()),
        task_public_id=task.public_id,
        company_id=task.company_id,
        project_id=task.project_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_employee_id=actor_employee_id,
        message=message,
        payload_json=payload_json,
        created_at=utc_now(),
    )
    db.add(event)
    return event


def _project_for_company(db: Session, *, company_id: str, project_id: str) -> Project:
    project = db.scalar(select(Project).where(Project.company_id == company_id, Project.project_id == project_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _photo_for_task_scope(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    photo_id: int | None,
    not_found_detail: str = "Photo not found",
) -> Photo | None:
    if photo_id is None:
        return None
    photo = db.get(Photo, int(photo_id))
    if (
        photo is None
        or photo.company_id != company_id
        or photo.project_id != project_id
        or photo.photo_type != PhotoType.project
        or photo.deleted
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found_detail)
    return photo


def create_review_task(
    db: Session,
    *,
    actor_user: User,
    project_id: str,
    task_type: ReviewTaskType,
    assigned_employee_id: str | None,
    message: str,
    related_photo_id: int | None = None,
    due_at: datetime | None = None,
    metadata_json: dict[str, Any] | None = None,
    request: Request | None = None,
) -> ReviewTask:
    _project_for_company(db, company_id=actor_user.company_id, project_id=project_id)
    _photo_for_task_scope(db, company_id=actor_user.company_id, project_id=project_id, photo_id=related_photo_id)
    normalized_message = _task_message(message)

    if task_type in EMPLOYEE_VISIBLE_TASK_TYPES and not assigned_employee_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="assigned_employee_id is required")
    if assigned_employee_id:
        employee = db.scalar(
            select(Employee).where(
                Employee.company_id == actor_user.company_id,
                Employee.employee_id == assigned_employee_id,
                Employee.active.is_(True),
            )
        )
        if employee is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assigned employee not found")

    now = utc_now()
    task = ReviewTask(
        public_id=str(uuid4()),
        tenant_id=actor_user.company_id,
        company_id=actor_user.company_id,
        project_id=project_id,
        related_photo_id=related_photo_id,
        task_type=task_type,
        status=ReviewTaskStatus.open,
        created_by_user_id=actor_user.id,
        assigned_employee_id=assigned_employee_id,
        message=normalized_message,
        due_at=due_at,
        metadata_json=metadata_json or {},
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.flush()
    add_review_task_event(db, task=task, event_type=ReviewTaskEventType.created, actor_user_id=actor_user.id)
    log_audit(
        db,
        action="review_task_created",
        target_type="review_task",
        target_id=task.public_id,
        actor_user_id=actor_user.id,
        company_id=actor_user.company_id,
        project_id=project_id,
        detail_json={
            "task_type": _enum_value(task_type),
            "assigned_employee_id": assigned_employee_id,
            "related_photo_id": related_photo_id,
        },
        ip_address=_client_ip(request),
    )
    return task


def get_review_task_for_manager(db: Session, *, company_id: str, public_id: str) -> ReviewTask:
    task = db.scalar(select(ReviewTask).where(ReviewTask.company_id == company_id, ReviewTask.public_id == public_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def apply_manager_review_task_action(
    db: Session,
    *,
    actor_user: User,
    task: ReviewTask,
    action: str,
    message: str | None = None,
    request: Request | None = None,
) -> ReviewTask:
    if action not in MANAGER_TASK_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task action")
    normalized_message = _task_message(message, required=(action == "comment"))
    now = utc_now()
    event_type = ReviewTaskEventType.commented
    if action == "cancel":
        if task.status in {ReviewTaskStatus.completed, ReviewTaskStatus.cancelled}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task cannot be cancelled from its current status")
        task.status = ReviewTaskStatus.cancelled
        task.cancelled_at = now
        task.cancelled_by_user_id = actor_user.id
        event_type = ReviewTaskEventType.cancelled
    elif action == "reopen":
        if task.status != ReviewTaskStatus.cancelled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only cancelled tasks can be reopened")
        task.status = ReviewTaskStatus.open
        task.cancelled_at = None
        task.cancelled_by_user_id = None
        task.completed_at = None
        task.completed_by_employee_id = None
        task.completion_photo_id = None
        event_type = ReviewTaskEventType.reopened
    task.updated_at = now
    add_review_task_event(
        db,
        task=task,
        event_type=event_type,
        actor_user_id=actor_user.id,
        message=normalized_message or None,
        payload_json={"action": action},
    )
    log_audit(
        db,
        action="review_task_action",
        target_type="review_task",
        target_id=task.public_id,
        actor_user_id=actor_user.id,
        company_id=actor_user.company_id,
        project_id=task.project_id,
        detail_json={"action": action, "status": _enum_value(task.status), "message_present": bool(normalized_message)},
        ip_address=_client_ip(request),
    )
    return task


def get_review_task_for_employee(db: Session, *, employee: Employee, public_id: str) -> ReviewTask:
    task = db.scalar(
        select(ReviewTask).where(
            ReviewTask.company_id == employee.company_id,
            ReviewTask.public_id == public_id,
            ReviewTask.assigned_employee_id == employee.employee_id,
            ReviewTask.task_type != ReviewTaskType.internal_note,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def apply_employee_review_task_action(
    db: Session,
    *,
    employee: Employee,
    task: ReviewTask,
    action: str,
    message: str | None = None,
    completion_photo_id: int | None = None,
    request: Request | None = None,
) -> ReviewTask:
    if action not in EMPLOYEE_TASK_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task action")
    normalized_message = _task_message(message, required=(action == "comment"))
    now = utc_now()
    event_type = ReviewTaskEventType.commented
    if action == "acknowledge":
        if task.status != ReviewTaskStatus.open:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task cannot be acknowledged from its current status")
        task.status = ReviewTaskStatus.acknowledged
        task.acknowledged_at = now
        task.acknowledged_by_employee_id = employee.employee_id
        event_type = ReviewTaskEventType.acknowledged
    elif action == "complete":
        if task.status not in {ReviewTaskStatus.open, ReviewTaskStatus.acknowledged}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task cannot be completed from its current status")
        completion_photo = _photo_for_task_scope(
            db,
            company_id=task.company_id,
            project_id=task.project_id,
            photo_id=completion_photo_id,
            not_found_detail="Completion photo not found",
        )
        if task.task_type == ReviewTaskType.retake_photo and completion_photo is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="completion_photo_id is required")
        if completion_photo is not None and completion_photo.employee_id != employee.employee_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="completion photo must be uploaded by the assigned employee",
            )
        if completion_photo is not None and _as_utc(completion_photo.created_at) <= _as_utc(task.created_at):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="completion photo must be uploaded after task creation",
            )
        if task.task_type == ReviewTaskType.clarify_photo and completion_photo is None and not normalized_message:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="message is required for clarification")
        task.status = ReviewTaskStatus.completed
        task.completed_at = now
        task.completed_by_employee_id = employee.employee_id
        task.completion_photo_id = completion_photo.id if completion_photo is not None else None
        event_type = ReviewTaskEventType.completed
    task.updated_at = now
    add_review_task_event(
        db,
        task=task,
        event_type=event_type,
        actor_employee_id=employee.employee_id,
        message=normalized_message or None,
        payload_json={"action": action, "completion_photo_id": completion_photo_id},
    )
    log_audit(
        db,
        action="review_task_employee_action",
        target_type="review_task",
        target_id=task.public_id,
        actor_user_id=None,
        company_id=employee.company_id,
        project_id=task.project_id,
        detail_json={
            "action": action,
            "status": _enum_value(task.status),
            "actor_employee_id": employee.employee_id,
            "completion_photo_id": completion_photo_id,
            "message_present": bool(normalized_message),
        },
        ip_address=_client_ip(request),
    )
    return task


def compute_review_session_summary(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    photo_scope = [
        Photo.company_id == company_id,
        Photo.project_id == project_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
    ]
    task_scope = [ReviewTask.company_id == company_id, ReviewTask.project_id == project_id]
    return {
        "photo_counts": {
            "total": int(db.scalar(select(func.count(Photo.id)).where(*photo_scope)) or 0),
            "pending_internal": int(
                db.scalar(
                    select(func.count(Photo.id)).where(
                        *photo_scope,
                        Photo.approval_status == ApprovalStatus.pending,
                        Photo.visibility == PhotoVisibility.internal,
                    )
                )
                or 0
            ),
            "client_visible": int(
                db.scalar(
                    select(func.count(Photo.id)).where(
                        *photo_scope,
                        Photo.approval_status == ApprovalStatus.approved,
                        Photo.visibility == PhotoVisibility.client_visible,
                    )
                )
                or 0
            ),
            "rejected": int(
                db.scalar(select(func.count(Photo.id)).where(*photo_scope, Photo.approval_status == ApprovalStatus.rejected))
                or 0
            ),
        },
        "task_counts": {
            "open": int(db.scalar(select(func.count(ReviewTask.id)).where(*task_scope, ReviewTask.status == ReviewTaskStatus.open)) or 0),
            "acknowledged": int(
                db.scalar(select(func.count(ReviewTask.id)).where(*task_scope, ReviewTask.status == ReviewTaskStatus.acknowledged)) or 0
            ),
            "completed": int(
                db.scalar(select(func.count(ReviewTask.id)).where(*task_scope, ReviewTask.status == ReviewTaskStatus.completed)) or 0
            ),
            "cancelled": int(
                db.scalar(select(func.count(ReviewTask.id)).where(*task_scope, ReviewTask.status == ReviewTaskStatus.cancelled)) or 0
            ),
        },
        "closeout_effects": {
            "auto_approves_photos": False,
            "auto_sets_client_visible": False,
            "exposes_internal_tasks_to_client": False,
        },
    }


def get_or_create_review_session(
    db: Session,
    *,
    actor_user: User,
    project_id: str,
    review_date: str,
    request: Request | None = None,
) -> tuple[ReviewSession, bool]:
    _project_for_company(db, company_id=actor_user.company_id, project_id=project_id)
    existing = db.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.company_id == actor_user.company_id,
            ReviewSession.project_id == project_id,
            ReviewSession.review_date == review_date,
        )
        .order_by(ReviewSession.created_at.desc(), ReviewSession.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing, False
    now = utc_now()
    session = ReviewSession(
        public_id=str(uuid4()),
        tenant_id=actor_user.company_id,
        company_id=actor_user.company_id,
        project_id=project_id,
        review_date=review_date,
        status=ReviewSessionStatus.open,
        created_by_user_id=actor_user.id,
        summary_json={},
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    log_audit(
        db,
        action="review_session_created",
        target_type="review_session",
        target_id=session.public_id,
        actor_user_id=actor_user.id,
        company_id=actor_user.company_id,
        project_id=project_id,
        detail_json={"review_date": review_date},
        ip_address=_client_ip(request),
    )
    return session, True


def close_review_session(
    db: Session,
    *,
    actor_user: User,
    session: ReviewSession,
    summary_json: dict[str, Any] | None = None,
    request: Request | None = None,
) -> ReviewSession:
    now = utc_now()
    if session.status != ReviewSessionStatus.closed:
        session.status = ReviewSessionStatus.closed
        session.closed_at = now
        session.closed_by_user_id = actor_user.id
    session.updated_at = now
    session.summary_json = summary_json or compute_review_session_summary(
        db,
        company_id=session.company_id,
        project_id=session.project_id,
    )
    log_audit(
        db,
        action="review_session_closed",
        target_type="review_session",
        target_id=session.public_id,
        actor_user_id=actor_user.id,
        company_id=actor_user.company_id,
        project_id=session.project_id,
        detail_json={"review_date": session.review_date, "status": _enum_value(session.status)},
        ip_address=_client_ip(request),
    )
    return session


def get_review_session_for_manager(db: Session, *, company_id: str, public_id: str) -> ReviewSession:
    session = db.scalar(select(ReviewSession).where(ReviewSession.company_id == company_id, ReviewSession.public_id == public_id))
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session not found")
    return session
