from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.security import generate_api_key
from app.models import Employee, User, UserRole
from app.services.access import can_access_project, get_user_project_ids
from app.services.rbac import is_manager, is_tenant_admin, is_worker
from app.services.tenant import scoped_identifier_for_company


def _tenant_match(column, tenant_id: str):
    return or_(column == tenant_id, column.is_(None))


def list_accessible_employees(db: Session, user: User) -> list[Employee]:
    base = select(Employee).where(
        Employee.company_id == user.company_id,
        _tenant_match(Employee.tenant_id, user.company_id),
    )
    if is_tenant_admin(user):
        return list(db.scalars(base.order_by(Employee.employee_id)))
    if is_worker(user):
        if not user.employee_id:
            return []
        return list(
            db.scalars(base.where(Employee.employee_id == user.employee_id).order_by(Employee.employee_id))
        )
    if user.role == UserRole.project_manager:
        project_ids = get_user_project_ids(db, user)
        if not project_ids:
            return []
        return list(db.scalars(base.where(Employee.project_id.in_(project_ids)).order_by(Employee.employee_id)))
    if is_manager(user):
        # Employees are company-level capture identities. Project access controls
        # review/report visibility, not which worker can upload to a project.
        return list(db.scalars(base.order_by(Employee.employee_id)))
    return []


def get_manageable_employee(db: Session, user: User, employee_id: str) -> Employee | None:
    employee = db.scalar(
        select(Employee).where(
            Employee.employee_id == employee_id,
            Employee.company_id == user.company_id,
            _tenant_match(Employee.tenant_id, user.company_id),
        )
    )
    if employee is None:
        return None
    if is_tenant_admin(user):
        return employee
    if is_manager(user):
        return employee
    if is_worker(user) and user.employee_id == employee.employee_id:
        return employee
    return None


def create_employee_for_user(
    db: Session,
    *,
    user: User,
    employee_id: str,
    name: str,
    role_name: str,
    project_id: str | None,
) -> Employee:
    if not (is_tenant_admin(user) or user.role in {UserRole.project_manager, UserRole.manager}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    raw_employee_id = employee_id.strip()
    if not raw_employee_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee ID is required")
    normalized_name = name.strip()
    if not normalized_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Employee name is required")

    try:
        normalized_employee_id = scoped_identifier_for_company(
            db,
            company_id=user.company_id,
            raw_value=raw_employee_id,
            kind="employee",
        )
        normalized_project_id = (
            scoped_identifier_for_company(
                db,
                company_id=user.company_id,
                raw_value=project_id.strip(),
                kind="project",
            )
            if project_id and project_id.strip()
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if user.role == UserRole.project_manager and normalized_project_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project ID is required")
    if normalized_project_id and not can_access_project(db, user, normalized_project_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project access denied")

    existing = db.scalar(select(Employee).where(Employee.employee_id == normalized_employee_id))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee ID already exists")

    employee = Employee(
        tenant_id=user.company_id,
        company_id=user.company_id,
        employee_id=normalized_employee_id,
        name=normalized_name,
        api_key=generate_api_key(),
        role=(role_name or "worker").strip() or "worker",
        project_id=normalized_project_id,
        active=True,
    )
    db.add(employee)
    db.flush()
    return employee


def assign_employee_project_for_user(
    db: Session,
    *,
    user: User,
    employee: Employee,
    project_id: str | None,
) -> Employee:
    if not (is_tenant_admin(user) or user.role in {UserRole.project_manager, UserRole.manager}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        normalized_project_id = (
            scoped_identifier_for_company(
                db,
                company_id=user.company_id,
                raw_value=project_id.strip(),
                kind="project",
            )
            if project_id and project_id.strip()
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if user.role == UserRole.project_manager and normalized_project_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project ID is required")
    if normalized_project_id and not can_access_project(db, user, normalized_project_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project access denied")

    if not is_tenant_admin(user):
        if employee.company_id != user.company_id or employee.tenant_id not in {None, user.company_id}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    employee.project_id = normalized_project_id
    employee.tenant_id = user.company_id
    employee.company_id = user.company_id
    db.add(employee)
    db.flush()
    return employee
