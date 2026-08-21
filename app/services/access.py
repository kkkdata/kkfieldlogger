from __future__ import annotations

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import Session

from app.models import ApprovalStatus, Employee, Photo, PhotoType, PhotoVisibility, Project, ProjectMember, User, UserRole
from app.services.rbac import is_client, is_manager, is_tenant_admin, is_worker


def _tenant_match(column, tenant_id: str):
    return or_(column == tenant_id, column.is_(None))


def get_user_project_ids(db: Session, user: User) -> list[str]:
    if is_tenant_admin(user):
        return list(
            db.scalars(
                select(Project.project_id).where(
                    Project.company_id == user.company_id,
                    _tenant_match(Project.tenant_id, user.company_id),
                )
            )
        )
    return list(
        db.scalars(
            select(ProjectMember.project_id)
            .join(Project, Project.project_id == ProjectMember.project_id)
            .where(
                ProjectMember.user_id == user.id,
                Project.company_id == user.company_id,
                _tenant_match(Project.tenant_id, user.company_id),
            )
        )
    )


def resolve_worker_employee_id(user: User) -> str | None:
    return user.employee_id or user.username


def apply_photo_scope(
    db: Session,
    statement,
    user: User,
    *,
    photo_type: PhotoType | None = None,
    include_deleted: bool = False,
    gallery_only: bool = False,
):
    statement = statement.join(Employee, Employee.employee_id == Photo.employee_id, isouter=True)
    conditions = [Photo.company_id == user.company_id, _tenant_match(Photo.tenant_id, user.company_id)]
    if not include_deleted:
        conditions.append(Photo.deleted.is_(False))
    if photo_type is not None:
        conditions.append(Photo.photo_type == photo_type)
    if gallery_only:
        conditions.extend(
            [
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.approval_status == ApprovalStatus.approved,
                Photo.visibility == PhotoVisibility.client_visible,
            ]
        )

    if is_tenant_admin(user):
        return statement.where(*conditions)

    if user.role in {UserRole.project_manager, UserRole.manager, UserRole.client, UserRole.client_viewer}:
        project_ids = get_user_project_ids(db, user)
        if not project_ids:
            return statement.where(false())
        if gallery_only or photo_type == PhotoType.project:
            conditions.append(Photo.project_id.in_(project_ids))
        elif photo_type == PhotoType.invoice:
            conditions.append(
                or_(
                    Photo.project_id.in_(project_ids),
                    and_(
                        Employee.company_id == user.company_id,
                        _tenant_match(Employee.tenant_id, user.company_id),
                        Employee.project_id.in_(project_ids),
                    ),
                )
            )
        else:
            conditions.append(
                or_(
                    Photo.project_id.in_(project_ids),
                    and_(
                        Photo.photo_type == PhotoType.invoice,
                        Employee.company_id == user.company_id,
                        _tenant_match(Employee.tenant_id, user.company_id),
                        Employee.project_id.in_(project_ids),
                    ),
                )
            )
        return statement.where(*conditions)

    if is_worker(user):
        worker_employee_id = resolve_worker_employee_id(user)
        if not worker_employee_id:
            return statement.where(false())
        conditions.append(Photo.employee_id == worker_employee_id)
        return statement.where(*conditions)

    return statement.where(false())


def can_access_project(db: Session, user: User, project_id: str) -> bool:
    if is_tenant_admin(user):
        return (
            db.scalar(
                select(Project.id).where(
                    Project.project_id == project_id,
                    Project.company_id == user.company_id,
                    _tenant_match(Project.tenant_id, user.company_id),
                )
            )
            is not None
        )
    if is_worker(user):
        return False
    return project_id in set(get_user_project_ids(db, user))


def can_access_photo(db: Session, user: User, photo: Photo) -> bool:
    if photo.company_id != user.company_id:
        return False
    if photo.tenant_id not in {None, user.company_id}:
        return False
    if is_tenant_admin(user):
        return True
    if is_worker(user):
        return photo.employee_id == resolve_worker_employee_id(user)
    if is_manager(user) and not is_tenant_admin(user):
        allowed_projects = set(get_user_project_ids(db, user))
        if photo.photo_type == PhotoType.project:
            return bool(photo.project_id and photo.project_id in allowed_projects)
        employee = db.scalar(
            select(Employee).where(
                Employee.employee_id == photo.employee_id,
                Employee.company_id == user.company_id,
                _tenant_match(Employee.tenant_id, user.company_id),
            )
        )
        effective_project = photo.project_id if photo.project_id and photo.project_id != "invoice" else (
            employee.project_id if employee else None
        )
        return bool(effective_project and effective_project in allowed_projects)
    if is_client(user):
        allowed_projects = set(get_user_project_ids(db, user))
        return bool(
            photo.photo_type == PhotoType.project
            and photo.project_id in allowed_projects
            and not photo.deleted
            and photo.approval_status == ApprovalStatus.approved
            and photo.visibility == PhotoVisibility.client_visible
        )
    return False
