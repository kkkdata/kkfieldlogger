from __future__ import annotations

from datetime import datetime, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings
from app.core.config import Settings
from app.core.observability import get_logger
from app.services.audit import log_audit
from app.services.site_inquiries import (
    create_site_inquiry,
    send_site_inquiry_notification,
    update_site_inquiry_delivery,
)


router = APIRouter(prefix="/api/v2/public", tags=["public"])
logger = get_logger(__name__)


class HomeInquiryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    location: str = Field(min_length=2, max_length=160)
    use: str = Field(min_length=2, max_length=200)
    preferred_plan: str = Field(min_length=2, max_length=160)
    home_count: int = Field(ge=1, le=1000)
    target_living_area_sq_ft: int | None = Field(default=None, ge=200, le=100000)
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    bathrooms: float | None = Field(default=None, ge=0.5, le=100)
    land: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=4000)
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    entry_path: str | None = Field(default=None, max_length=2000)
    website: str = Field(default="", max_length=200)
    form_started_at: datetime

    @field_validator("form_started_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("form_started_at must include a timezone")
        return value


@router.post("/home-inquiry", status_code=status.HTTP_201_CREATED)
def submit_home_inquiry(
    request: Request,
    payload: HomeInquiryRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if payload.website:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "received"})

    now = datetime.now(timezone.utc)
    elapsed_seconds = (now - payload.form_started_at.astimezone(timezone.utc)).total_seconds()
    if elapsed_seconds < settings.site_inquiry_min_submit_seconds:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Please wait before submitting")

    source_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
    public_payload = payload.model_dump(mode="json", exclude={"website", "form_started_at"})
    try:
        inquiry_id, record_path = create_site_inquiry(
            settings=settings,
            payload=public_payload,
            source_ip=source_ip,
            host=request.headers.get("host"),
            origin=request.headers.get("origin"),
            user_agent=request.headers.get("user-agent"),
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.error("site_inquiry_record_failed", error=str(exc), source_ip=source_ip)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The inquiry could not be recorded",
        ) from exc

    notification_sent = send_site_inquiry_notification(
        db,
        settings=settings,
        payload=public_payload,
        inquiry_id=inquiry_id,
    )
    delivery_status = "notification_sent" if notification_sent else "notification_failed"
    try:
        update_site_inquiry_delivery(record_path, delivery_status)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("site_inquiry_status_update_failed", inquiry_id=inquiry_id, error=str(exc))

    try:
        primary_email = settings.site_inquiry_email.strip().lower()
        copy_email = (settings.site_inquiry_copy_email or "").strip().lower()
        recipients = [primary_email]
        if copy_email and copy_email != primary_email:
            recipients.append(copy_email)
        log_audit(
            db,
            action="site_inquiry_received",
            target_type="site_inquiry",
            target_id=inquiry_id,
            detail_json={
                "location": public_payload["location"],
                "preferred_plan": public_payload["preferred_plan"],
                "notification_sent": notification_sent,
                "recipients": recipients,
            },
            ip_address=source_ip,
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("site_inquiry_audit_failed", inquiry_id=inquiry_id, error=str(exc))

    if not notification_sent:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "recorded_notification_failed",
                "inquiry_id": inquiry_id,
                "recorded": True,
            },
        )
    return {"status": "notified", "inquiry_id": inquiry_id, "recorded": True}
