from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Company, Employee, User
from app.services.audit import log_audit


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request):
    session = request.app.state.session_maker()
    try:
        yield session
    finally:
        session.close()


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.active:
        request.session.clear()
        return None
    company = db.scalar(select(Company).where(Company.company_id == user.company_id))
    if company is None or not company.active:
        request.session.clear()
        return None
    request.state.current_user = user
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def require_roles(*allowed_roles):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return dependency


def get_mobile_employee(request: Request, db: Session = Depends(get_db)) -> Employee:
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        log_audit(
            db,
            action="mobile_auth_failed",
            target_type="employee",
            target_id=None,
            detail_json={"reason": "missing_api_key"},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")

    employee = db.scalar(select(Employee).where(Employee.api_key == api_key, Employee.active.is_(True)))
    if employee is None:
        log_audit(
            db,
            action="mobile_auth_failed",
            target_type="employee",
            target_id=None,
            detail_json={"reason": "invalid_api_key"},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    company = db.scalar(select(Company).where(Company.company_id == employee.company_id))
    if company is None or not company.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Company is inactive")
    request.state.current_employee = employee
    return employee
