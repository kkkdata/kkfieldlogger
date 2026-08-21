from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user_optional, get_db, get_settings
from app.core.config import Settings
from app.models import Company, Employee, Photo, User
from app.services.media_access import (
    MEDIA_EXPIRES_PARAM,
    MEDIA_SIGNATURE_PARAM,
    resolve_media_relative_path,
    verify_media_url_signature,
)

router = APIRouter()


def _photo_for_relative_url(db: Session, relative_url: str) -> Photo | None:
    return db.scalar(
        select(Photo).where(
            or_(Photo.image_url == relative_url, Photo.thumb_url == relative_url),
            Photo.deleted.is_(False),
        )
    )


def _mobile_employee_from_header(request: Request, db: Session) -> Employee | None:
    api_key = str(request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        return None
    employee = db.scalar(select(Employee).where(Employee.api_key == api_key, Employee.active.is_(True)))
    if employee is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    company = db.scalar(select(Company).where(Company.company_id == employee.company_id))
    if company is None or not company.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Company is inactive")
    return employee


def _authorized_for_photo(photo: Photo | None, current_user: User | None, employee: Employee | None) -> bool:
    if photo is None:
        return False
    if current_user is not None:
        return photo.company_id == current_user.company_id
    if employee is not None:
        return photo.company_id == employee.company_id and photo.employee_id == employee.employee_id
    return False


@router.head("/media/{media_path:path}")
@router.get("/media/{media_path:path}")
def protected_media_file(
    media_path: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    relative_url = f"{settings.media_url_prefix.rstrip('/')}/{media_path.strip('/')}"
    file_path = resolve_media_relative_path(settings, media_path)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    signed = verify_media_url_signature(
        relative_url,
        request.query_params.get(MEDIA_EXPIRES_PARAM),
        request.query_params.get(MEDIA_SIGNATURE_PARAM),
        settings,
    )
    if not signed:
        employee = _mobile_employee_from_header(request, db)
        photo = _photo_for_relative_url(db, relative_url)
        if not _authorized_for_photo(photo, current_user, employee):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(path=str(file_path), media_type=media_type, filename=file_path.name)
