from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Employee, Photo, Project, User


def _resolve_company_id(
    db: Session,
    *,
    company_id: str | None,
    actor_user_id: int | None,
    target_type: str,
    target_id: str | None,
    project_id: str | None,
) -> str | None:
    if company_id:
        return company_id
    if actor_user_id is not None:
        actor = db.get(User, actor_user_id)
        if actor is not None:
            return actor.company_id
    if target_type == "employee" and target_id:
        employee = db.scalar(select(Employee).where(Employee.employee_id == target_id))
        if employee is not None:
            return employee.company_id
    if target_type == "photo" and target_id and target_id.isdigit():
        photo = db.get(Photo, int(target_id))
        if photo is not None:
            return photo.company_id
    if project_id:
        project = db.scalar(select(Project).where(Project.project_id == project_id))
        if project is not None:
            return project.company_id
    return None


def log_audit(
    db: Session,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    actor_user_id: int | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    detail_json: dict | list | None = None,
    ip_address: str | None = None,
    company_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=tenant_id,
        company_id=_resolve_company_id(
            db,
            company_id=company_id,
            actor_user_id=actor_user_id,
            target_type=target_type,
            target_id=target_id,
            project_id=project_id,
        ),
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        project_id=project_id,
        detail_json=detail_json,
        ip_address=ip_address,
    )
    db.add(entry)
    return entry
