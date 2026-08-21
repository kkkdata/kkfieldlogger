from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
import ipaddress
from io import BytesIO
import json
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_current_user_optional, get_db, get_settings
from app.core.config import Settings
from app.core.observability import get_logger
from app.core.security import hash_password, verify_password
from app.core.time import build_date_range, to_utc_iso, utc_now
from app.models import (
    AIAnalysisLog,
    AIAnalysisStatus,
    AnnotationVisibility,
    ApprovalStatus,
    AuditLog,
    CameraProtocol,
    CameraStatus,
    Company,
    CompanyApplication,
    CompanyApplicationStatus,
    CopilotConversation,
    CopilotMessage,
    Employee,
    GeneratedReport,
    IPCamera,
    MediaAnnotation,
    MediaAsset,
    MediaType,
    Membership,
    MembershipStatus,
    PasswordResetToken,
    Photo,
    PhotoType,
    PhotoVisibility,
    PlanCode,
    ProgressReport,
    Project,
    ReportStatus,
    ReviewTask,
    ReviewTaskStatus,
    SubscriptionStatus,
    Tenant,
    TenantStatus,
    TaskJob,
    TaskStatus,
    User,
    UserRole,
)
from app.services.auth_tokens import consume_password_reset_token, create_password_reset_token
from app.services.annotations import (
    AnnotationTargetRef,
    annotation_role_for_user,
    create_text_annotations,
    create_voice_annotations,
    ensure_employee_annotation_user,
    filter_visible_annotations,
    list_target_annotations,
    save_annotation_audio_file,
    serialize_annotation_tree,
)
from app.services.audit import log_audit
from app.services.ai_pipeline import (
    active_ai_log_counts_by_photo,
    ai_confidence_level,
    list_photo_ai_logs,
    normalize_operator_prompt,
    photo_matches_ai_keyword,
    promote_ai_analysis_log_to_primary,
    queue_photo_reprocess,
    rollback_ai_analysis_batch,
    semantic_search_photos,
    serialize_ai_analysis_log,
    set_ai_analysis_log_status,
)
from app.services.billing import ensure_subscription
from app.services.bootstrap import bootstrap_platform_data
from app.services.employees import assign_employee_project_for_user, create_employee_for_user, get_manageable_employee
from app.services.email import send_email
from app.services.evidence_copilot import (
    COPILOT_ALLOWED_MODES,
    create_copilot_conversation,
    get_copilot_conversation_payload,
    list_copilot_backend_options,
    list_copilot_conversations,
    queue_copilot_message_generation,
    serialize_copilot_message,
)
from app.services.access import apply_photo_scope, can_access_photo, can_access_project, get_user_project_ids
from app.services.comments import stage_photo_comment
from app.services.media_pipeline import (
    _coerce_media_type,
    create_media_asset_from_chunks,
    ingest_local_media_file,
    media_asset_preview_filenames,
    media_asset_thumbnail_filename,
    media_asset_timeline_markers,
    next_media_asset_id,
    queue_media_asset_processing,
    resolve_media_asset_file_path,
    resolve_media_preview_frame_path,
    save_media_upload_chunk,
)
from app.services.job_queue import enqueue_annotation_transcription_task, get_task_queue_snapshot, schedule_job_worker
from app.services.rbac import is_manager, is_tenant_admin
from app.services.session import login_user, logout_user
from app.services.tenant import generate_company_code, generate_company_id, normalize_company_id
from app.services.photos import (
    photo_media_asset_id,
    resolve_public_url,
    serialize_photo,
    stage_photo_approval_status,
    stage_photo_display_update,
)
from app.services.reports import (
    queue_progress_report_generation,
    queue_report_generation,
    retry_report_generation,
    serialize_generated_report,
    serialize_progress_report,
)
from app.services.review_workflow import (
    apply_manager_review_task_action,
    close_review_session,
    coerce_review_task_type,
    compute_review_session_summary,
    create_review_task,
    get_or_create_review_session,
    get_review_session_for_manager,
    get_review_task_for_manager,
    serialize_review_session,
    serialize_review_task,
)
from app.services.settings import get_system_settings
from app.services.login_throttle import check_login_rate_limit, clear_failed_logins, record_failed_login
from app.services.rbac import is_client

router = APIRouter(prefix="/api/v2")
logger = get_logger("kkfieldlogger.api_v2")

# Shared with /auth/login and /portal/login via app.services.login_throttle.
MANAGEMENT_ACTIONS = {
    "approve_client_visible",
    "approve_internal",
    "keep_internal",
    "reject",
    "comment",
}


def _require_management_user(user: User) -> User:
    if not is_manager(user) or is_client(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Management access required")
    return user


def _management_date_window(
    *,
    start_date: str | None,
    end_date: str | None,
    window_days: int,
    timezone_name: str,
) -> dict[str, Any]:
    if start_date or end_date:
        start_dt, end_dt = build_date_range(start_date, end_date, timezone_name)
    else:
        end_dt = utc_now()
        start_dt = end_dt - timedelta(days=max(1, min(int(window_days), 365)))
    return {
        "start_utc": start_dt,
        "end_utc": end_dt,
        "start": to_utc_iso(start_dt),
        "end": to_utc_iso(end_dt),
        "timezone": timezone_name,
        "window_days": max(1, min(int(window_days), 365)),
    }


def _accessible_management_projects(db: Session, user: User) -> list[Project]:
    _require_management_user(user)
    stmt = select(Project).where(
        Project.company_id == user.company_id,
        or_(Project.tenant_id == user.company_id, Project.tenant_id.is_(None)),
    )
    if not is_tenant_admin(user):
        project_ids = get_user_project_ids(db, user)
        if not project_ids:
            return []
        stmt = stmt.where(Project.project_id.in_(project_ids))
    return list(db.scalars(stmt.order_by(Project.project_name.asc(), Project.project_id.asc())))


def _count_project_photos(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    timestamp_field,
    start_dt: datetime | None,
    end_dt: datetime | None,
    extra_conditions: list[Any] | None = None,
) -> int:
    conditions = [
        Photo.company_id == company_id,
        Photo.project_id == project_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
    ]
    if start_dt is not None:
        conditions.append(timestamp_field >= start_dt)
    if end_dt is not None:
        conditions.append(timestamp_field < end_dt)
    if extra_conditions:
        conditions.extend(extra_conditions)
    return int(db.scalar(select(func.count(Photo.id)).where(*conditions)) or 0)


def _late_upload_count(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> int:
    conditions = [
        Photo.company_id == company_id,
        Photo.project_id == project_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
    ]
    if start_dt is not None:
        conditions.append(Photo.created_at >= start_dt)
    if end_dt is not None:
        conditions.append(Photo.created_at < end_dt)
    rows = list(db.scalars(select(Photo).where(*conditions).limit(1000)))
    return sum(
        1
        for photo in rows
        if photo.created_at
        and photo.captured_at_utc
        and photo.created_at - photo.captured_at_utc > timedelta(hours=12)
    )


def _management_project_counts(
    db: Session,
    *,
    project: Project,
    window: dict[str, Any],
) -> dict[str, int]:
    start_dt = window["start_utc"]
    end_dt = window["end_utc"]
    pending_internal_conditions = [
        Photo.approval_status == ApprovalStatus.pending,
        Photo.visibility == PhotoVisibility.internal,
    ]
    client_visible_conditions = [
        Photo.approval_status == ApprovalStatus.approved,
        Photo.visibility == PhotoVisibility.client_visible,
    ]
    return {
        "uploaded_count": _count_project_photos(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            timestamp_field=Photo.created_at,
            start_dt=start_dt,
            end_dt=end_dt,
        ),
        "captured_count": _count_project_photos(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            timestamp_field=Photo.captured_at_utc,
            start_dt=start_dt,
            end_dt=end_dt,
        ),
        "pending_internal_count": _count_project_photos(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            timestamp_field=Photo.created_at,
            start_dt=None,
            end_dt=None,
            extra_conditions=pending_internal_conditions,
        ),
        "pending_internal_window_count": _count_project_photos(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            timestamp_field=Photo.created_at,
            start_dt=start_dt,
            end_dt=end_dt,
            extra_conditions=pending_internal_conditions,
        ),
        "client_visible_count": _count_project_photos(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            timestamp_field=Photo.created_at,
            start_dt=None,
            end_dt=None,
            extra_conditions=client_visible_conditions,
        ),
        "late_upload_count": _late_upload_count(
            db,
            company_id=project.company_id,
            project_id=project.project_id,
            start_dt=start_dt,
            end_dt=end_dt,
        ),
    }


def _management_photo_payload(
    photo: Photo,
    *,
    request: Request,
    settings: Settings,
    storage_base_url: str | None,
) -> dict[str, Any]:
    serialized = serialize_photo(photo, request, settings, storage_base_url=storage_base_url).model_dump()
    return {
        "id": serialized["id"],
        "employee_id": serialized["employee_id"],
        "project_id": serialized["project_id"],
        "photo_type": serialized["photo_type"],
        "image_url": serialized["image_url"],
        "thumb_url": serialized["thumb_url"],
        "media_url": serialized["media_url"],
        "media_kind": serialized["media_kind"],
        "media_asset_id": serialized["media_asset_id"],
        "original_file_name": serialized["original_file_name"],
        "captured_at_utc": serialized["captured_at_utc"],
        "created_at": serialized["created_at"],
        "visibility": serialized["visibility"],
        "approval_status": serialized["approval_status"],
        "client_visible_ready": (
            photo.approval_status == ApprovalStatus.approved and photo.visibility == PhotoVisibility.client_visible
        ),
        "labeling_status": serialized["labeling_status"],
        "note": serialized["note"],
        "location": serialized["location"],
        "gps_lat": serialized["gps_lat"],
        "gps_lon": serialized["gps_lon"],
        "device_model": serialized["device_model"],
        "app_version": serialized["app_version"],
    }


def _project_photo_ids_for_window(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> list[str]:
    conditions = [
        Photo.company_id == company_id,
        Photo.project_id == project_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
    ]
    if start_dt is not None:
        conditions.append(Photo.created_at >= start_dt)
    if end_dt is not None:
        conditions.append(Photo.created_at < end_dt)
    return [str(photo_id) for photo_id in db.scalars(select(Photo.id).where(*conditions).limit(2000))]


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=160)
    tenant_slug: str | None = Field(default=None, max_length=64)
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=128)
    contact_phone: str | None = Field(default=None, max_length=64)
    contact_title: str | None = Field(default=None, max_length=120)
    website_url: str | None = Field(default=None, max_length=255)
    company_intro: str | None = Field(default=None, max_length=4000)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=10, max_length=128)


class SwitchTenantRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=64)


class DeleteAccountRequest(BaseModel):
    email: EmailStr
    reason: str = Field(min_length=5, max_length=2000)


class SupportRequest(BaseModel):
    email: EmailStr
    subject: str = Field(min_length=3, max_length=255)
    message: str = Field(min_length=10, max_length=4000)


class EmployeeCreateRequest(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    role_name: str = Field(default="worker", min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)


class EmployeeAssignProjectRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)


class BulkReprocessRequest(BaseModel):
    photo_ids: list[int] = Field(min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=4000)


class ManagementReviewActionRequest(BaseModel):
    photo_ids: list[int] = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=40)
    comment: str | None = Field(default=None, max_length=2000)


class ManagementReviewTaskCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    task_type: str = Field(min_length=1, max_length=64)
    assigned_employee_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    related_photo_id: int | None = None
    due_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class ManagementReviewTaskActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    message: str | None = Field(default=None, max_length=2000)


class ManagementReviewSessionCurrentRequest(BaseModel):
    review_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class PhotoSemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=20, ge=1, le=100)
    project_id: str | None = Field(default=None, max_length=64)


class ReportGenerateRequest(BaseModel):
    photo_ids: list[int] = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=5, max_length=8000)


class ProgressCompareRequest(BaseModel):
    photo_ids: list[int] = Field(min_length=2, max_length=50)
    prompt: str | None = Field(default=None, max_length=4000)


class CopilotConversationCreateRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    preferred_backend_id: str | None = Field(default=None, max_length=64)
    preferred_mode: str = Field(default="standard", max_length=32)


class CopilotMessageCreateRequest(BaseModel):
    content_text: str = Field(min_length=2, max_length=4000)
    language: str = Field(default="en", min_length=2, max_length=16)
    preferred_backend_id: str | None = Field(default=None, max_length=64)
    preferred_mode: str | None = Field(default=None, max_length=32)


class AIAnalysisLogStatusRequest(BaseModel):
    status: AIAnalysisStatus = Field(default=AIAnalysisStatus.rejected)


class RTMPRecordingDoneRequest(BaseModel):
    file_path: str = Field(min_length=1, max_length=2048)
    camera_id: int | None = None
    company_id: str | None = Field(default=None, max_length=64)
    source: str = Field(default="ip_camera", min_length=1, max_length=64)
    metadata_json: dict[str, object] | None = None


class MediaUploadResponse(BaseModel):
    asset_id: str
    media_type: str
    status: str
    upload_complete: bool
    received_chunks: int
    total_chunks: int
    duration_seconds: float | None = None
    message: str


class AnnotationTargetInput(BaseModel):
    photo_id: int | None = None
    media_asset_id: str | None = Field(default=None, max_length=64)


class TextAnnotationCreateRequest(BaseModel):
    target_ids: list[object] | None = Field(default=None, min_length=1, max_length=100)
    target_id: int | str | None = None
    content_text: str | None = Field(default=None, min_length=1, max_length=8000)
    body_text: str | None = Field(default=None, min_length=1, max_length=8000)
    visibility: AnnotationVisibility = Field(default=AnnotationVisibility.public)
    parent_id: str | None = Field(default=None, max_length=36)
    created_at: str | None = None


def _require_internal_webhook_token(header_value: str | None, settings: Settings) -> None:
    expected_token = (settings.camera_webhook_token or "").strip()
    if not expected_token or header_value != expected_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token")


def _ip_allowed_by_whitelist(host: str, allowlist: str) -> bool:
    entries = [item.strip() for item in allowlist.split(",") if item.strip()]
    if not entries:
        return False
    for entry in entries:
        if host == entry:
            return True
    try:
        remote_ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for entry in entries:
        try:
            if remote_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _require_internal_webhook_access(request: Request, header_value: str | None, settings: Settings) -> None:
    _require_internal_webhook_token(header_value, settings)
    remote_host = request.client.host if request.client else ""
    if not _ip_allowed_by_whitelist(remote_host, settings.camera_webhook_ip_allowlist):
        logger.warning(
            "internal_webhook_ip_rejected",
            path=request.url.path,
            client_ip=remote_host,
            configured_allowlist=settings.camera_webhook_ip_allowlist,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook IP is not allowed")


def _resolve_local_recording_path(raw_path: str, settings: Settings) -> Path:
    recording_path = Path(raw_path).expanduser()
    if not recording_path.is_absolute():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording file path must be absolute")
    if not recording_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording file not found")
    resolved_path = recording_path.resolve()
    media_root = settings.media_root_path.resolve()
    try:
        resolved_path.relative_to(media_root)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording file path is outside media root") from exc
    return resolved_path


def _camera_payload(camera: IPCamera | None) -> dict[str, object]:
    if camera is None:
        return {}
    return {
        "camera_id": camera.id,
        "camera_name": camera.name,
        "stream_url": camera.stream_url,
        "protocol": camera.protocol.value,
    }


_check_rate_limit = check_login_rate_limit
_record_failed_login = record_failed_login
_clear_failed_logins = clear_failed_logins


def _company_report(db: Session, company_id: str, report_public_id: str) -> GeneratedReport | None:
    return db.scalar(
        select(GeneratedReport).where(
            GeneratedReport.public_id == report_public_id,
            GeneratedReport.company_id == company_id,
        )
    )


def _company_progress_report(db: Session, company_id: str, report_id: str) -> ProgressReport | None:
    return db.scalar(
        select(ProgressReport).where(
            ProgressReport.id == report_id,
            ProgressReport.company_id == company_id,
        )
    )


def _company_copilot_conversation(db: Session, company_id: str, conversation_id: str) -> CopilotConversation | None:
    return db.scalar(
        select(CopilotConversation).where(
            CopilotConversation.id == conversation_id,
            CopilotConversation.company_id == company_id,
        )
    )


def _company_copilot_message(db: Session, company_id: str, message_id: str) -> CopilotMessage | None:
    return db.scalar(
        select(CopilotMessage)
        .join(CopilotConversation, CopilotConversation.id == CopilotMessage.conversation_id)
        .where(CopilotMessage.id == message_id, CopilotConversation.company_id == company_id)
    )


def _accessible_photo_for_user(db: Session, current_user: User, photo_id: int) -> Photo | None:
    return db.scalar(apply_photo_scope(db, select(Photo), current_user).where(Photo.id == photo_id))


def _photo_ai_history_payload_v2(db: Session, photo: Photo) -> dict[str, object]:
    logs = list_photo_ai_logs(db, photo.id)
    active_logs = [item for item in logs if item.status == AIAnalysisStatus.active]
    active_log_count = len(active_logs)
    confidence_level = ai_confidence_level(active_log_count)
    return {
        "photo_id": photo.id,
        "snapshot": photo.tag_json or {"ai_summary": None, "labels": [], "defects": []},
        "logs": [serialize_ai_analysis_log(item) for item in logs],
        "primary_log_id": active_logs[0].id if active_logs else None,
        "active_log_count": active_log_count,
        "confidence_level": confidence_level,
    }


def _parse_annotation_visibility(raw_value: str | AnnotationVisibility | None) -> AnnotationVisibility:
    if isinstance(raw_value, AnnotationVisibility):
        return raw_value
    try:
        return AnnotationVisibility(str(raw_value or AnnotationVisibility.public.value).strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid annotation visibility") from exc


def _parse_annotation_created_at(raw_value: str | None) -> datetime:
    normalized_value = str(raw_value or "").strip()
    if not normalized_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="created_at is required")
    candidate = normalized_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="created_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_annotation_created_at(raw_value: str | None) -> datetime | None:
    normalized_value = str(raw_value or "").strip()
    if not normalized_value:
        return None
    return _parse_annotation_created_at(normalized_value)


def _annotation_target_from_scalar(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, int):
        return {"photo_id": value}
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each target must include photo_id or media_asset_id")
    if normalized_value.isdigit():
        return {"photo_id": int(normalized_value)}
    return {"media_asset_id": normalized_value}


def _annotation_targets_from_items(items: list[object]) -> list[AnnotationTargetRef]:
    targets: list[AnnotationTargetRef] = []
    seen: set[tuple[int | None, str | None]] = set()
    for item in items:
        payload = item.model_dump() if isinstance(item, AnnotationTargetInput) else _annotation_target_from_scalar(item)
        photo_id = payload.get("photo_id")
        media_asset_id = str(payload.get("media_asset_id") or "").strip() or None
        if photo_id is not None and media_asset_id is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each target must reference only one media object")
        if photo_id is None and media_asset_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Each target must include photo_id or media_asset_id")
        try:
            normalized_photo_id = int(photo_id) if photo_id is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="photo_id must be an integer") from exc
        target = AnnotationTargetRef(photo_id=normalized_photo_id, media_asset_id=media_asset_id)
        dedupe_key = (target.photo_id, target.media_asset_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        targets.append(target)
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one target is required")
    return targets


def _parse_annotation_targets_json(raw_value: str) -> list[AnnotationTargetRef]:
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target_ids must be valid JSON") from exc
    if not isinstance(payload, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target_ids must be a JSON array")
    normalized_items: list[object] = []
    for item in payload:
        normalized_items.append(_annotation_target_from_scalar(item))
    return _annotation_targets_from_items(normalized_items)


def _accessible_photo_for_employee(db: Session, employee: Employee, photo_id: int) -> Photo | None:
    return db.scalar(
        select(Photo).where(
            Photo.id == photo_id,
            Photo.employee_id == employee.employee_id,
            Photo.company_id == employee.company_id,
            Photo.deleted.is_(False),
        )
    )


def _validate_annotation_targets_for_actor(
    db: Session,
    *,
    current_user: User | None,
    employee: Employee | None,
    targets: list[AnnotationTargetRef],
) -> list[AnnotationTargetRef]:
    validated: list[AnnotationTargetRef] = []
    for target in targets:
        if target.photo_id is not None:
            photo = (
                _accessible_photo_for_user(db, current_user, target.photo_id)
                if current_user is not None
                else _accessible_photo_for_employee(db, employee, target.photo_id)
            )
            if photo is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Photo not found: {target.photo_id}")
            validated.append(AnnotationTargetRef(photo_id=photo.id))
            continue
        assert target.media_asset_id is not None
        company_id = current_user.company_id if current_user is not None else employee.company_id
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.asset_id == target.media_asset_id,
                MediaAsset.company_id == company_id,
            )
        )
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Media asset not found: {target.media_asset_id}")
        validated.append(AnnotationTargetRef(media_asset_id=asset.asset_id))
    return validated


def _resolve_parent_annotation(
    db: Session,
    *,
    current_user: User | None,
    employee: Employee | None,
    parent_id: str | None,
    targets: list[AnnotationTargetRef],
) -> MediaAnnotation | None:
    resolved_parent_id = str(parent_id or "").strip()
    if not resolved_parent_id:
        return None
    annotation = db.get(MediaAnnotation, resolved_parent_id)
    if annotation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent annotation not found")
    parent_target = AnnotationTargetRef(photo_id=annotation.photo_id, media_asset_id=annotation.media_asset_id)
    _validate_annotation_targets_for_actor(db, current_user=current_user, employee=employee, targets=[parent_target])
    if len(targets) != 1 or targets[0] != parent_target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Replies must target the same media item as the parent annotation")
    return annotation


def _annotation_tree_response(
    db: Session,
    *,
    current_user: User | None = None,
    photo_id: int | None = None,
    media_asset_id: str | None = None,
    public_only: bool = False,
) -> dict[str, object]:
    annotations = list_target_annotations(db, photo_id=photo_id, media_asset_id=media_asset_id)
    annotations = filter_visible_annotations(
        annotations,
        include_manager_only=(
            not public_only
            and current_user is not None
            and annotation_role_for_user(current_user).value == "manager"
        ),
    )
    return {
        "photo_id": photo_id,
        "media_asset_id": media_asset_id,
        "items": serialize_annotation_tree(db, annotations=annotations),
    }


def _api_media_stream_url(request: Request | None, settings: Settings, asset_id: str) -> str:
    return resolve_public_url(request, settings, f"/api/v2/media/{asset_id}/stream")


def _api_media_preview_url(request: Request | None, settings: Settings, asset_id: str, frame_name: str) -> str:
    return resolve_public_url(request, settings, f"/api/v2/media/{asset_id}/preview/{frame_name}")


def _get_media_request_user_or_employee(
    request: Request,
    db: Session,
) -> tuple[User | None, Employee | None]:
    api_key = str(request.headers.get("X-API-Key") or "").strip()
    if api_key:
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
        return None, employee
    current_user = get_current_user_optional(request, db)
    if current_user is not None:
        return current_user, None
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _annotation_request_actor(
    request: Request,
    db: Session,
) -> tuple[User | None, Employee | None, User]:
    current_user, employee = _get_media_request_user_or_employee(request, db)
    if current_user is not None:
        return current_user, None, current_user
    assert employee is not None
    actor_user = ensure_employee_annotation_user(db, employee)
    return None, employee, actor_user


def _accessible_photo_for_request(
    request: Request,
    db: Session,
    photo_id: int,
) -> tuple[Photo, User | None, Employee | None]:
    current_user, employee = _get_media_request_user_or_employee(request, db)
    photo = (
        _accessible_photo_for_user(db, current_user, photo_id)
        if current_user is not None
        else _accessible_photo_for_employee(db, employee, photo_id)
    )
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    return photo, current_user, employee


def _accessible_media_asset_for_request(
    request: Request,
    db: Session,
    asset_id: str,
) -> tuple[MediaAsset, User | None, Employee | None]:
    current_user, employee = _get_media_request_user_or_employee(request, db)
    company_id = current_user.company_id if current_user is not None else employee.company_id
    asset = db.scalar(
        select(MediaAsset).where(
            MediaAsset.asset_id == asset_id,
            MediaAsset.company_id == company_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
    return asset, current_user, employee


def _media_asset_detail_payload_v2(
    asset: MediaAsset,
    *,
    request: Request | None,
    settings: Settings,
) -> dict[str, object]:
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    duration_seconds = float(asset.duration_seconds or metadata_json.get("duration_seconds") or 0.0)
    thumbnail_name = media_asset_thumbnail_filename(asset)
    preview_frames = media_asset_preview_filenames(asset)
    stream_path = resolve_media_asset_file_path(asset)
    return {
        "asset_id": asset.asset_id,
        "media_type": asset.media_type.value if hasattr(asset.media_type, "value") else str(asset.media_type),
        "status": asset.status.value if hasattr(asset.status, "value") else str(asset.status),
        "source": asset.source,
        "original_file_name": asset.original_file_name,
        "mime_type": asset.mime_type,
        "duration_seconds": duration_seconds,
        "stream_url": _api_media_stream_url(request, settings, asset.asset_id) if stream_path is not None else None,
        "thumbnail_url": _api_media_preview_url(request, settings, asset.asset_id, thumbnail_name) if thumbnail_name else None,
        "preview_items": [
            {
                "name": frame_name,
                "url": _api_media_preview_url(request, settings, asset.asset_id, frame_name),
            }
            for frame_name in preview_frames
        ],
        "timeline_markers": media_asset_timeline_markers(asset),
        "ai_snapshot": metadata_json.get("ai_snapshot") or {"ai_summary": None, "labels": [], "defects": []},
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
        "completed_at": asset.completed_at.isoformat() if asset.completed_at else None,
    }


def _progress_report_payload_v2(
    report: ProgressReport,
    *,
    request: Request,
    settings: Settings,
    photos: list[Photo],
) -> dict[str, object]:
    ordered_ids = [int(photo_id) for photo_id in (report.source_photo_ids or []) if str(photo_id).isdigit()]
    photo_map = {photo.id: photo for photo in photos}
    ordered_photos = [photo_map[photo_id] for photo_id in ordered_ids if photo_id in photo_map]
    return serialize_progress_report(report, settings=settings, request=request, photos=ordered_photos)


def _media_kind_from_asset(asset: MediaAsset) -> str:
    return "video" if asset.media_type == MediaType.video else "photo"


def _media_detail_response_from_asset(
    db: Session,
    request: Request,
    settings: Settings,
    asset: MediaAsset,
    *,
    current_user: User | None,
    public_only: bool,
) -> dict[str, object]:
    media_payload = _media_asset_detail_payload_v2(asset, request=request, settings=settings)
    annotation_tree = _annotation_tree_response(
        db,
        current_user=current_user,
        media_asset_id=asset.asset_id,
        public_only=public_only,
    )
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    media_kind = _media_kind_from_asset(asset)
    authorized_employee_ids = metadata_json.get("authorized_employee_ids")
    if not isinstance(authorized_employee_ids, list):
        employee_id = str(metadata_json.get("employee_id") or "").strip()
        authorized_employee_ids = [employee_id] if employee_id else []
    ai_snapshot = media_payload.get("ai_snapshot") or {}
    return {
        "id": asset.asset_id,
        "file": asset.file_path,
        "image_url": media_payload["thumbnail_url"] or media_payload["stream_url"],
        "thumb_url": media_payload["thumbnail_url"],
        "media_url": media_payload["stream_url"],
        "stream_url": media_payload["stream_url"],
        "thumbnail_url": media_payload["thumbnail_url"],
        "preview_items": media_payload["preview_items"],
        "media_kind": media_kind,
        "created_at": media_payload["created_at"],
        "project_id": metadata_json.get("project_id"),
        "photo_type": metadata_json.get("photo_type") or PhotoType.project.value,
        "duration_seconds": media_payload["duration_seconds"],
        "ai_conclusion": ai_snapshot.get("ai_summary"),
        "authorized_employee_ids": authorized_employee_ids,
        "can_reply": True,
        "annotations": annotation_tree["items"],
        "annotation_tree": annotation_tree,
        "media": media_payload,
    }


def _media_detail_response_from_photo(
    db: Session,
    request: Request,
    settings: Settings,
    photo: Photo,
    *,
    current_user: User | None,
    public_only: bool,
) -> dict[str, object]:
    serialized_photo = serialize_photo(photo, request, settings)
    linked_media_asset = None
    linked_media_asset_id = photo_media_asset_id(photo)
    if linked_media_asset_id and linked_media_asset_id != str(photo.id):
        linked_media_asset = db.get(MediaAsset, linked_media_asset_id)
    annotation_tree = _annotation_tree_response(
        db,
        current_user=current_user,
        photo_id=photo.id,
        public_only=public_only,
    )
    if linked_media_asset is not None:
        media_payload = _media_asset_detail_payload_v2(linked_media_asset, request=request, settings=settings)
    else:
        media_payload = {
            "asset_id": str(photo.id),
            "media_type": serialized_photo.media_kind,
            "status": photo.labeling_status or "completed",
            "source": "legacy_photo",
            "original_file_name": photo.original_file_name,
            "mime_type": photo.mime_type,
            "duration_seconds": serialized_photo.duration_seconds,
            "stream_url": serialized_photo.media_url,
            "thumbnail_url": serialized_photo.thumb_url,
            "preview_items": [],
            "timeline_markers": [],
            "ai_snapshot": photo.tag_json or {"ai_summary": None, "labels": [], "defects": []},
            "created_at": serialized_photo.created_at,
            "updated_at": serialized_photo.created_at,
            "completed_at": serialized_photo.created_at,
        }
    ai_snapshot = media_payload["ai_snapshot"] if isinstance(media_payload["ai_snapshot"], dict) else {}
    stream_url = media_payload.get("stream_url") or serialized_photo.media_url
    thumbnail_url = media_payload.get("thumbnail_url") or serialized_photo.thumb_url
    preview_items = media_payload.get("preview_items") or []
    display_image_url = thumbnail_url if serialized_photo.media_kind == "video" else serialized_photo.image_url
    return {
        "id": photo.id,
        "file": photo.file_path,
        "image_url": display_image_url or serialized_photo.image_url,
        "thumb_url": thumbnail_url,
        "media_url": stream_url,
        "stream_url": stream_url,
        "thumbnail_url": thumbnail_url,
        "preview_items": preview_items,
        "media_kind": serialized_photo.media_kind,
        "media_asset_id": serialized_photo.media_asset_id,
        "created_at": serialized_photo.created_at,
        "project_id": photo.project_id,
        "photo_type": photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type),
        "duration_seconds": serialized_photo.duration_seconds or 0.0,
        "ai_conclusion": ai_snapshot.get("ai_summary"),
        "authorized_employee_ids": [photo.employee_id] if photo.employee_id else [],
        "can_reply": True,
        "annotations": annotation_tree["items"],
        "annotation_tree": annotation_tree,
        "media": media_payload,
    }


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@router.post("/auth/register", status_code=status.HTTP_202_ACCEPTED)
def register_v2(
    request: Request,
    payload: RegisterRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    bootstrap_platform_data(db, settings)
    email = payload.email.lower()
    normalized_username = email
    if db.scalar(
        select(User).where(
            or_(User.email == email, User.username == normalized_username),
            User.active.is_(True),
        )
    ) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    requested_company_id = normalize_company_id(payload.tenant_slug) if payload.tenant_slug else None
    if requested_company_id and db.scalar(select(Tenant).where(Tenant.slug == requested_company_id)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant slug already in use")
    if requested_company_id and db.scalar(select(Company).where(Company.company_id == requested_company_id)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Company ID already in use")
    if db.scalar(
        select(CompanyApplication).where(
            or_(
                CompanyApplication.contact_email == email,
                CompanyApplication.requested_username == normalized_username,
            ),
            CompanyApplication.status == CompanyApplicationStatus.pending,
        )
    ) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workspace application is already pending review")

    application = CompanyApplication(
        public_id=str(uuid4()),
        company_name=payload.tenant_name.strip(),
        requested_company_id=requested_company_id,
        requested_company_code=None,
        contact_name=payload.display_name.strip(),
        contact_email=email,
        contact_phone=(payload.contact_phone or "").strip() or None,
        contact_title=(payload.contact_title or "").strip() or None,
        website_url=(payload.website_url or "").strip() or None,
        company_intro=(payload.company_intro or "Submitted from API registration endpoint.").strip(),
        requested_username=normalized_username,
        password_hash=hash_password(payload.password),
        status=CompanyApplicationStatus.pending,
    )
    db.add(application)
    db.flush()
    # Deliberately no email on submission: applications sit silently in the
    # review queue and the applicant is only emailed after a human approves
    # or rejects. Bots can at worst fill the queue, not burn mail reputation.
    log_audit(
        db,
        action="v2_company_application_submitted",
        target_type="company_application",
        target_id=str(application.id),
        actor_user_id=None,
        detail_json={
            "company_name": application.company_name,
            "requested_company_id": application.requested_company_id,
            "contact_email": email,
        },
        ip_address=request.client.host if request.client else None,
        company_id=None,
    )
    db.commit()
    return {
        "status": "pending_review",
        "message": "Workspace application submitted for platform review.",
        "application_id": application.public_id,
        "company": {"name": application.company_name, "requested_company_id": application.requested_company_id},
        "contact": {"email": email, "display_name": application.contact_name},
    }


@router.post("/auth/login")
def login_v2(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
):
    email = payload.email.lower()
    _check_rate_limit(request, email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        _record_failed_login(request, email)
        log_audit(
            db,
            action="v2_login_failed",
            target_type="user",
            target_id=str(user.id) if user else None,
            detail_json={"email": email},
            ip_address=request.client.host if request.client else None,
            company_id=user.company_id if user else None,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    login_user(request, user.id)
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    _clear_failed_logins(request, email)
    log_audit(
        db,
        action="v2_login",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        ip_address=request.client.host if request.client else None,
        company_id=user.company_id,
    )
    db.commit()
    return {"message": "ok", "tenant_slug": user.company_id, "future_token_mode": "not_enabled"}


@router.post("/auth/logout")
def logout_v2(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    log_audit(
        db,
        action="v2_logout",
        target_type="user",
        target_id=str(current_user.id),
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
        company_id=current_user.company_id,
    )
    db.commit()
    logout_user(request)
    return {"message": "ok"}


@router.get("/auth/me")
def me_v2(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memberships = list(
        db.scalars(
            select(Membership).where(Membership.user_id == current_user.id, Membership.status == MembershipStatus.active)
        )
    )
    return {
        "user": {
            "id": current_user.public_id or str(current_user.id),
            "email": current_user.email,
            "display_name": current_user.display_name,
            "role": current_user.role.value,
            "is_verified": current_user.is_verified,
            "active_tenant": current_user.company_id,
        },
        "active_membership_role": next((item.role for item in memberships if item.tenant_id == current_user.company_id), current_user.role.value),
        "memberships": [{"tenant_slug": item.tenant_id, "role": item.role, "status": item.status.value} for item in memberships],
    }


@router.post("/auth/switch-tenant")
def switch_tenant_v2(
    payload: SwitchTenantRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == current_user.id,
            Membership.tenant_id == payload.tenant_slug,
            Membership.status == MembershipStatus.active,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant access denied")
    current_user.company_id = payload.tenant_slug
    try:
        current_user.role = UserRole(membership.role)
    except ValueError:
        pass
    db.add(current_user)
    db.commit()
    return {"message": "ok", "tenant_slug": payload.tenant_slug}


@router.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee_v2(
    request: Request,
    payload: EmployeeCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee = create_employee_for_user(
        db,
        user=current_user,
        employee_id=payload.employee_id,
        name=payload.name,
        role_name=payload.role_name,
        project_id=payload.project_id,
    )
    log_audit(
        db,
        action="v2_employee_created",
        target_type="employee",
        target_id=employee.employee_id,
        actor_user_id=current_user.id,
        project_id=employee.project_id,
        ip_address=request.client.host if request.client else None,
        company_id=current_user.company_id,
    )
    db.commit()
    return {
        "employee": {
            "employee_id": employee.employee_id,
            "name": employee.name,
            "role": employee.role,
            "project_id": employee.project_id,
            "company_id": employee.company_id,
            "tenant_id": employee.tenant_id,
            "active": employee.active,
            "api_key": employee.api_key,
        }
    }


@router.post("/employees/{employee_id}/assign-project")
def assign_employee_project_v2(
    employee_id: str,
    request: Request,
    payload: EmployeeAssignProjectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee = get_manageable_employee(db, current_user, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    updated_employee = assign_employee_project_for_user(
        db,
        user=current_user,
        employee=employee,
        project_id=payload.project_id,
    )
    log_audit(
        db,
        action="v2_employee_project_assigned",
        target_type="employee",
        target_id=updated_employee.employee_id,
        actor_user_id=current_user.id,
        project_id=updated_employee.project_id,
        detail_json={"project_id": updated_employee.project_id},
        ip_address=request.client.host if request.client else None,
        company_id=current_user.company_id,
    )
    db.commit()
    return {
        "employee": {
            "employee_id": updated_employee.employee_id,
            "project_id": updated_employee.project_id,
            "company_id": updated_employee.company_id,
            "tenant_id": updated_employee.tenant_id,
        }
    }


@router.get("/photos")
def list_photos_v2(
    request: Request,
    semantic_query: str | None = Query(None),
    ai_keyword: str | None = Query(None),
    project_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    visibility: str | None = Query(None),
    approval_status: str | None = Query(None),
    include_rejected_ai: bool = Query(False),
    confidence: str | None = Query(None),
    include_deleted: bool = Query(False),
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.project,
        include_deleted=include_deleted and is_tenant_admin(current_user),
        gallery_only=is_client(current_user),
    )
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    range_start, range_end = build_date_range(start_date, end_date, timezone_name)
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)
    if employee_id:
        stmt = stmt.where(Photo.employee_id == employee_id)
    if visibility:
        try:
            stmt = stmt.where(Photo.visibility == PhotoVisibility(visibility))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid visibility filter") from exc
    if approval_status:
        try:
            stmt = stmt.where(Photo.approval_status == ApprovalStatus(approval_status))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid approval filter") from exc
    if range_start:
        stmt = stmt.where(Photo.captured_at_utc >= range_start)
    if range_end:
        stmt = stmt.where(Photo.captured_at_utc < range_end)

    photos = list(db.scalars(stmt.order_by(Photo.captured_at_utc.desc())))
    normalized_ai_keyword = (ai_keyword or "").strip()
    normalized_confidence = (confidence or "").strip().lower()
    if normalized_confidence and normalized_confidence not in {"low", "medium", "high"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confidence filter")

    active_counts = active_ai_log_counts_by_photo(db, [photo.id for photo in photos])
    filtered_photos: list[Photo] = []
    for photo in photos:
        if normalized_confidence and ai_confidence_level(active_counts.get(photo.id, 0)) != normalized_confidence:
            continue
        if normalized_ai_keyword and not photo_matches_ai_keyword(
            db,
            photo,
            normalized_ai_keyword,
            include_rejected=include_rejected_ai,
        ):
            continue
        filtered_photos.append(photo)

    search_scores: dict[int, float] = {}
    normalized_semantic_query = (semantic_query or "").strip()
    if normalized_semantic_query:
        try:
            ranked_results = semantic_search_photos(
                db,
                settings,
                tenant_slug=current_user.company_id,
                query=normalized_semantic_query,
                candidate_photo_ids=[photo.id for photo in filtered_photos],
                limit=limit,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        filtered_photos = [photo for photo, _ in ranked_results]
        search_scores = {photo.id: round(similarity, 6) for photo, similarity in ranked_results}
    else:
        filtered_photos = filtered_photos[:limit]

    storage_base_url = get_system_settings(db, settings).get("storage_base_url")
    return {
        "results": [
            {
                "photo": serialize_photo(
                    photo,
                    request,
                    settings,
                    storage_base_url=storage_base_url,
                ).model_dump(),
                "similarity": search_scores.get(photo.id),
                "confidence_level": ai_confidence_level(active_counts.get(photo.id, 0)),
                "active_log_count": active_counts.get(photo.id, 0),
            }
            for photo in filtered_photos
        ]
    }


@router.get("/management/projects/overview")
def management_projects_overview_v2(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    window_days: int = Query(1, ge=1, le=365),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    window = _management_date_window(
        start_date=start_date,
        end_date=end_date,
        window_days=window_days,
        timezone_name=timezone_name,
    )
    projects = _accessible_management_projects(db, current_user)
    project_rows: list[dict[str, Any]] = []
    totals = {
        "uploaded_count": 0,
        "captured_count": 0,
        "pending_internal_count": 0,
        "pending_internal_window_count": 0,
        "client_visible_count": 0,
        "late_upload_count": 0,
    }
    for project in projects:
        counts = _management_project_counts(db, project=project, window=window)
        for key in totals:
            totals[key] += int(counts.get(key) or 0)
        project_rows.append(
            {
                "project": {
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                    "client_name": project.client_name,
                    "location": project.location,
                    "status": project.status.value if hasattr(project.status, "value") else str(project.status),
                },
                "counts": counts,
                "needs_review": counts["pending_internal_count"] > 0,
                "has_recent_uploads": counts["uploaded_count"] > 0,
            }
        )
    return {
        "contract_version": "management_projects_overview:v1",
        "window": {key: value for key, value in window.items() if key not in {"start_utc", "end_utc"}},
        "guardrails": {
            "requires_management_user": True,
            "uses_database_facts_only": True,
            "exposes_raw_ai_output": False,
            "employee_api_key_allowed": False,
        },
        "totals": totals,
        "projects": project_rows,
    }


@router.get("/management/projects/{project_id}/review-summary")
def management_project_review_summary_v2(
    project_id: str,
    request: Request,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    window_days: int = Query(1, ge=1, le=365),
    recent_limit: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project = db.scalar(
        select(Project).where(Project.company_id == current_user.company_id, Project.project_id == project_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    system_settings = get_system_settings(db, settings)
    window = _management_date_window(
        start_date=start_date,
        end_date=end_date,
        window_days=window_days,
        timezone_name=timezone_name,
    )
    counts = _management_project_counts(db, project=project, window=window)
    latest_photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == current_user.company_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
            )
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .limit(recent_limit)
        )
    )
    report_counts = {
        (report_status.value if hasattr(report_status, "value") else str(report_status)): int(count)
        for report_status, count in db.execute(
            select(ProgressReport.status, func.count(ProgressReport.id))
            .where(ProgressReport.company_id == current_user.company_id, ProgressReport.project_id == project_id)
            .group_by(ProgressReport.status)
        )
    }
    return {
        "contract_version": "management_project_review_summary:v1",
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
            "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        },
        "window": {key: value for key, value in window.items() if key not in {"start_utc", "end_utc"}},
        "counts": counts,
        "status_flags": {
            "needs_review": counts["pending_internal_count"] > 0,
            "has_recent_uploads": counts["uploaded_count"] > 0,
            "has_late_uploads": counts["late_upload_count"] > 0,
        },
        "processing": {
            "photo_ai_queue": get_task_queue_snapshot(db, current_user.company_id, "photo_ai_pipeline"),
            "progress_report_status_counts": report_counts,
        },
        "recent_photos": [
            _management_photo_payload(
                photo,
                request=request,
                settings=settings,
                storage_base_url=system_settings.get("storage_base_url"),
            )
            for photo in latest_photos
        ],
    }


@router.get("/management/projects/{project_id}/review-inbox")
def management_project_review_inbox_v2(
    project_id: str,
    request: Request,
    status_filter: str = Query("pending", pattern="^(pending|failed_processing|client_visible|all)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    stmt = select(Photo).where(
        Photo.company_id == current_user.company_id,
        Photo.project_id == project_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
    )
    if status_filter == "pending":
        stmt = stmt.where(Photo.approval_status == ApprovalStatus.pending, Photo.visibility == PhotoVisibility.internal)
    elif status_filter == "failed_processing":
        stmt = stmt.where(Photo.labeling_status == "failed")
    elif status_filter == "client_visible":
        stmt = stmt.where(
            Photo.approval_status == ApprovalStatus.approved,
            Photo.visibility == PhotoVisibility.client_visible,
        )
    photos = list(db.scalars(stmt.order_by(Photo.created_at.desc(), Photo.id.desc()).limit(limit)))
    storage_base_url = get_system_settings(db, settings).get("storage_base_url")
    return {
        "contract_version": "management_review_inbox:v1",
        "project_id": project_id,
        "status_filter": status_filter,
        "items": [
            _management_photo_payload(photo, request=request, settings=settings, storage_base_url=storage_base_url)
            for photo in photos
            if can_access_photo(db, current_user, photo)
        ],
    }


@router.get("/management/projects/{project_id}/upload-health")
def management_project_upload_health_v2(
    project_id: str,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    window_days: int = Query(1, ge=1, le=365),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    window = _management_date_window(
        start_date=start_date,
        end_date=end_date,
        window_days=window_days,
        timezone_name=timezone_name,
    )
    project = db.scalar(
        select(Project).where(Project.company_id == current_user.company_id, Project.project_id == project_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    counts = _management_project_counts(db, project=project, window=window)
    photo_ids = _project_photo_ids_for_window(
        db,
        company_id=current_user.company_id,
        project_id=project_id,
        start_dt=window["start_utc"],
        end_dt=window["end_utc"],
    )
    task_counts: dict[str, int] = {}
    if photo_ids:
        for task_status, count in db.execute(
            select(TaskJob.status, func.count(TaskJob.id))
            .where(TaskJob.company_id == current_user.company_id, TaskJob.related_type == "photo", TaskJob.related_id.in_(photo_ids))
            .group_by(TaskJob.status)
        ):
            task_counts[task_status.value if hasattr(task_status, "value") else str(task_status)] = int(count)
    diagnostic_conditions = [
        AuditLog.company_id == current_user.company_id,
        AuditLog.action == "mobile_diagnostic_log_uploaded",
    ]
    if window["start_utc"] is not None:
        diagnostic_conditions.append(AuditLog.created_at >= window["start_utc"])
    if window["end_utc"] is not None:
        diagnostic_conditions.append(AuditLog.created_at < window["end_utc"])
    diagnostic_count = int(db.scalar(select(func.count(AuditLog.id)).where(*diagnostic_conditions)) or 0)
    return {
        "contract_version": "management_upload_health:v1",
        "project_id": project_id,
        "window": {key: value for key, value in window.items() if key not in {"start_utc", "end_utc"}},
        "upload_counts": counts,
        "photo_ai_queue_company_snapshot": get_task_queue_snapshot(db, current_user.company_id, "photo_ai_pipeline"),
        "project_photo_task_status_counts": task_counts,
        "diagnostics": {
            "company_mobile_diagnostic_logs_in_window": diagnostic_count,
            "project_attribution": "not_available_in_current_diagnostic_log_schema",
        },
        "health_flags": {
            "has_recent_uploads": counts["uploaded_count"] > 0,
            "has_late_uploads": counts["late_upload_count"] > 0,
            "has_processing_dead_letters": int(task_counts.get(TaskStatus.dead_letter.value, 0)) > 0,
        },
    }


@router.post("/management/review-actions")
def management_review_actions_v2(
    request: Request,
    payload: ManagementReviewActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    action = payload.action.strip()
    if action not in MANAGEMENT_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid management review action")
    comment = (payload.comment or "").strip()
    if action == "comment" and not comment:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="comment is required for comment action")
    photo_ids = sorted({int(photo_id) for photo_id in payload.photo_ids})
    photos = list(db.scalars(select(Photo).where(Photo.id.in_(photo_ids), Photo.company_id == current_user.company_id)))
    photos_by_id = {photo.id: photo for photo in photos}
    missing_ids = [photo_id for photo_id in photo_ids if photo_id not in photos_by_id]
    inaccessible_ids = [photo.id for photo in photos if not can_access_photo(db, current_user, photo)]
    if missing_ids or inaccessible_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more photos were not found")

    updated_ids: list[int] = []
    comment_ids: list[int] = []
    for photo in photos:
        if action == "approve_client_visible":
            stage_photo_approval_status(
                db,
                photo=photo,
                actor_user=current_user,
                approval_status=ApprovalStatus.approved,
                request=request,
            )
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                visibility=PhotoVisibility.client_visible,
                request=request,
                action_name="photo_visibility_changed",
            )
            updated_ids.append(photo.id)
        elif action == "approve_internal":
            stage_photo_approval_status(
                db,
                photo=photo,
                actor_user=current_user,
                approval_status=ApprovalStatus.approved,
                request=request,
            )
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                visibility=PhotoVisibility.internal,
                request=request,
                action_name="photo_visibility_changed",
            )
            updated_ids.append(photo.id)
        elif action == "keep_internal":
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                visibility=PhotoVisibility.internal,
                request=request,
                action_name="photo_visibility_changed",
            )
            updated_ids.append(photo.id)
        elif action == "reject":
            stage_photo_approval_status(
                db,
                photo=photo,
                actor_user=current_user,
                approval_status=ApprovalStatus.rejected,
                request=request,
            )
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                visibility=PhotoVisibility.internal,
                request=request,
                action_name="photo_visibility_changed",
            )
            updated_ids.append(photo.id)
        if comment:
            entry = stage_photo_comment(
                db,
                photo_id=photo.id,
                actor_user=current_user,
                comment=comment,
                project_id=photo.project_id,
                ip_address=request.client.host if request.client else None,
            )
            comment_ids.append(entry.id)

    log_audit(
        db,
        action="management_review_action",
        target_type="photo_batch",
        target_id=str(len(photos)),
        actor_user_id=current_user.id,
        company_id=current_user.company_id,
        detail_json={
            "action": action,
            "photo_ids": photo_ids,
            "updated_ids": updated_ids,
            "comment_ids": comment_ids,
            "comment_present": bool(comment),
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {
        "message": "ok",
        "action": action,
        "updated_photo_ids": updated_ids,
        "comment_ids": comment_ids,
        "processed_count": len(photos),
    }


@router.post("/management/review-tasks")
def management_create_review_task_v2(
    request: Request,
    payload: ManagementReviewTaskCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, payload.project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    task = create_review_task(
        db,
        actor_user=current_user,
        project_id=payload.project_id,
        task_type=coerce_review_task_type(payload.task_type),
        assigned_employee_id=payload.assigned_employee_id,
        message=payload.message,
        related_photo_id=payload.related_photo_id,
        due_at=payload.due_at,
        metadata_json=payload.metadata or {},
        request=request,
    )
    db.commit()
    db.refresh(task)
    return {
        "message": "ok",
        "contract_version": "management_review_tasks:v1",
        "task": serialize_review_task(task, include_internal=True),
    }


@router.get("/management/projects/{project_id}/review-tasks")
def management_list_project_review_tasks_v2(
    project_id: str,
    status_filter: str = Query("active", pattern="^(active|open|acknowledged|completed|cancelled|all)$"),
    limit: int = Query(100, ge=1, le=300),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    stmt = select(ReviewTask).where(ReviewTask.company_id == current_user.company_id, ReviewTask.project_id == project_id)
    if status_filter == "active":
        stmt = stmt.where(ReviewTask.status.in_([ReviewTaskStatus.open, ReviewTaskStatus.acknowledged]))
    elif status_filter != "all":
        stmt = stmt.where(ReviewTask.status == ReviewTaskStatus(status_filter))
    tasks = list(db.scalars(stmt.order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc()).limit(limit)))
    return {
        "contract_version": "management_review_tasks:v1",
        "project_id": project_id,
        "status_filter": status_filter,
        "items": [serialize_review_task(task, include_internal=True) for task in tasks],
    }


@router.post("/management/review-tasks/{public_id}/actions")
def management_review_task_action_v2(
    public_id: str,
    request: Request,
    payload: ManagementReviewTaskActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    task = get_review_task_for_manager(db, company_id=current_user.company_id, public_id=public_id)
    if not can_access_project(db, current_user, task.project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    task = apply_manager_review_task_action(
        db,
        actor_user=current_user,
        task=task,
        action=payload.action.strip(),
        message=payload.message,
        request=request,
    )
    db.commit()
    db.refresh(task)
    return {
        "message": "ok",
        "contract_version": "management_review_tasks:v1",
        "task": serialize_review_task(task, include_internal=True),
    }


@router.post("/management/projects/{project_id}/review-sessions/current")
def management_current_review_session_v2(
    project_id: str,
    request: Request,
    payload: ManagementReviewSessionCurrentRequest | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    review_date = (payload.review_date if payload else None) or utc_now().astimezone(ZoneInfo(timezone_name)).date().isoformat()
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid review_date") from exc
    session, created = get_or_create_review_session(
        db,
        actor_user=current_user,
        project_id=project_id,
        review_date=review_date,
        request=request,
    )
    db.commit()
    db.refresh(session)
    return {
        "message": "ok",
        "contract_version": "management_review_sessions:v1",
        "created": created,
        "session": serialize_review_session(session),
    }


@router.post("/management/review-sessions/{public_id}/close")
def management_close_review_session_v2(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user = _require_management_user(current_user)
    session = get_review_session_for_manager(db, company_id=current_user.company_id, public_id=public_id)
    if not can_access_project(db, current_user, session.project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session not found")
    summary = compute_review_session_summary(
        db,
        company_id=current_user.company_id,
        project_id=session.project_id,
    )
    session = close_review_session(
        db,
        actor_user=current_user,
        session=session,
        summary_json=summary,
        request=request,
    )
    db.commit()
    db.refresh(session)
    return {
        "message": "ok",
        "contract_version": "management_review_sessions:v1",
        "session": serialize_review_session(session),
    }


@router.post("/media/upload", response_model=MediaUploadResponse)
async def upload_media_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    media_type: str = Form(...),
    source: str = Form("manual_upload"),
    company_id: str | None = Form(None),
    upload_id: str | None = Form(None),
    chunk_index: int = Form(0),
    total_chunks: int = Form(1),
    filename: str | None = Form(None),
    metadata_json: str | None = Form(None),
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
    webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
):
    resolved_media_type = _coerce_media_type(media_type)
    internal_upload = current_user is None
    if internal_upload:
        _require_internal_webhook_access(request, webhook_token, settings)
        resolved_company_id = (company_id or "").strip()
        if not resolved_company_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="company_id is required for token-auth uploads")
        actor_user_id = None
    else:
        assert current_user is not None
        resolved_company_id = current_user.company_id
        actor_user_id = current_user.id
    company = db.scalar(select(Company).where(Company.company_id == resolved_company_id, Company.active.is_(True)))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    asset_id = next_media_asset_id(upload_id)
    if db.get(MediaAsset, asset_id) is not None:
        existing_asset = db.get(MediaAsset, asset_id)
        assert existing_asset is not None
        return MediaUploadResponse(
            asset_id=existing_asset.asset_id,
            media_type=existing_asset.media_type.value,
            status=existing_asset.status.value,
            upload_complete=True,
            received_chunks=total_chunks,
            total_chunks=total_chunks,
            duration_seconds=existing_asset.duration_seconds,
            message="Upload already finalized",
        )

    try:
        parsed_metadata = json.loads(metadata_json) if (metadata_json or "").strip() else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="metadata_json must be valid JSON") from exc
    if not isinstance(parsed_metadata, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="metadata_json must be a JSON object")

    body = await upload.read()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    original_file_name = filename or upload.filename or f"{asset_id}.{resolved_media_type.value}"
    try:
        chunk_state = save_media_upload_chunk(
            settings,
            asset_id=asset_id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            original_file_name=original_file_name,
            media_type=resolved_media_type,
            source=source,
            mime_type=upload.content_type,
            metadata_json=parsed_metadata,
            payload=body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not chunk_state["complete"]:
        return MediaUploadResponse(
            asset_id=asset_id,
            media_type=resolved_media_type.value,
            status="uploading",
            upload_complete=False,
            received_chunks=int(chunk_state["received_chunks"]),
            total_chunks=total_chunks,
            duration_seconds=None,
            message="Chunk accepted. Upload is resumable until all chunks are received.",
        )

    asset = create_media_asset_from_chunks(
        db,
        app_settings=settings,
        asset_id=asset_id,
        company_id=resolved_company_id,
        tenant_id=resolved_company_id,
        uploaded_by_user_id=actor_user_id,
        media_type=resolved_media_type,
        source=source,
        original_file_name=original_file_name,
        mime_type=upload.content_type,
        metadata_json=parsed_metadata,
    )
    log_audit(
        db,
        action="media_asset_uploaded",
        target_type="media_asset",
        target_id=asset.asset_id,
        actor_user_id=actor_user_id,
        tenant_id=resolved_company_id,
        company_id=resolved_company_id,
        ip_address=request.client.host if request.client else None,
        detail_json={
            "media_type": asset.media_type.value,
            "source": asset.source,
            "file_size": asset.file_size,
            "chunked_upload": total_chunks > 1,
            "internal_upload": internal_upload,
        },
    )
    if asset.media_type.value == "video":
        queue_media_asset_processing(
            db,
            app_settings=settings,
            asset=asset,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
        )
    db.commit()
    db.refresh(asset)
    return MediaUploadResponse(
        asset_id=asset.asset_id,
        media_type=asset.media_type.value,
        status=asset.status.value,
        upload_complete=True,
        received_chunks=int(chunk_state["received_chunks"]),
        total_chunks=total_chunks,
        duration_seconds=asset.duration_seconds,
        message=(
            "Video upload completed and processing has started."
            if asset.media_type.value == "video"
            else "Media upload completed."
        ),
    )


@router.get("/media/{asset_id}/detail")
def media_detail_v2(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if asset_id.isdigit():
        photo, current_user, employee = _accessible_photo_for_request(request, db, int(asset_id))
        return _media_detail_response_from_photo(
            db,
            request,
            settings,
            photo,
            current_user=current_user,
            public_only=True,
        )
    asset, current_user, employee = _accessible_media_asset_for_request(request, db, asset_id)
    return _media_detail_response_from_asset(
        db,
        request,
        settings,
        asset,
        current_user=current_user,
        public_only=True,
    )


@router.get("/media/{asset_id}/stream")
def media_stream_v2(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    asset, _, _ = _accessible_media_asset_for_request(request, db, asset_id)
    file_path = resolve_media_asset_file_path(asset)
    if file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file is missing")
    return FileResponse(path=str(file_path), media_type=asset.mime_type or "video/mp4", filename=file_path.name)


@router.get("/media/{asset_id}/preview/{frame_name}")
def media_preview_v2(
    asset_id: str,
    frame_name: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    asset, _, _ = _accessible_media_asset_for_request(request, db, asset_id)
    preview_path = resolve_media_preview_frame_path(settings, asset, frame_name)
    if preview_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview frame not found")
    return FileResponse(path=str(preview_path), media_type="image/jpeg", filename=preview_path.name)


@router.post("/annotations/voice", status_code=status.HTTP_201_CREATED)
async def create_voice_annotations_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    target_ids: str = Form(...),
    source: str = Form(...),
    created_at: str = Form(...),
    parent_id: str | None = Form(None),
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    normalized_source = str(source or "").strip()
    if not normalized_source:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source is required")
    current_user, employee, actor_user = _annotation_request_actor(request, db)
    resolved_targets = _validate_annotation_targets_for_actor(
        db,
        current_user=current_user,
        employee=employee,
        targets=_parse_annotation_targets_json(target_ids),
    )
    resolved_parent = _resolve_parent_annotation(
        db,
        current_user=current_user,
        employee=employee,
        parent_id=parent_id,
        targets=resolved_targets,
    )
    audio_body = await audio.read()
    if not audio_body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Voice upload is empty")
    resolved_created_at = _parse_annotation_created_at(created_at)
    audio_file_path = save_annotation_audio_file(
        settings,
        company_id=actor_user.company_id,
        original_file_name=audio.filename or "voice-note.webm",
        body=audio_body,
    )
    annotations = create_voice_annotations(
        db,
        targets=resolved_targets,
        user=actor_user,
        audio_file_path=audio_file_path,
        visibility=AnnotationVisibility.public,
        parent_id=resolved_parent.id if resolved_parent is not None else None,
        created_at=resolved_created_at,
    )
    enqueue_annotation_transcription_task(
        db,
        app_settings=settings,
        company_id=actor_user.company_id,
        tenant_id=actor_user.company_id,
        annotation_ids=[annotation.id for annotation in annotations],
        actor_user_id=actor_user.id,
    )
    for annotation in annotations:
        log_audit(
            db,
            action="media_annotation_voice_created",
            target_type="media_annotation",
            target_id=annotation.id,
            actor_user_id=actor_user.id,
            company_id=actor_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={
                "target_type": "photo" if annotation.photo_id is not None else "media_asset",
                "photo_id": annotation.photo_id,
                "media_asset_id": annotation.media_asset_id,
                "visibility": annotation.visibility.value,
                "status": annotation.status.value,
                "source": normalized_source,
                "client_created_at": resolved_created_at.isoformat(),
                "role_at_time": annotation.role_at_time.value,
                "parent_id": annotation.parent_id,
            },
        )
    db.commit()
    schedule_job_worker(background_tasks.add_task, request.app.state.session_maker, settings)
    return {
        "message": "Voice annotations accepted and queued for transcription.",
        "status": "processing",
        "annotation_ids": [annotation.id for annotation in annotations],
        "target_count": len(annotations),
    }


@router.post("/annotations/text", status_code=status.HTTP_201_CREATED)
def create_text_annotations_v2(
    request: Request,
    payload: TextAnnotationCreateRequest,
    db: Session = Depends(get_db),
):
    current_user, employee, actor_user = _annotation_request_actor(request, db)
    normalized_content = str(payload.content_text or payload.body_text or "").strip()
    if not normalized_content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="content_text cannot be empty")
    raw_targets: list[object] | None = payload.target_ids
    if raw_targets is None:
        if payload.target_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target_ids or target_id is required")
        raw_targets = [payload.target_id]
    resolved_targets = _validate_annotation_targets_for_actor(
        db,
        current_user=current_user,
        employee=employee,
        targets=_annotation_targets_from_items(raw_targets),
    )
    resolved_parent = _resolve_parent_annotation(
        db,
        current_user=current_user,
        employee=employee,
        parent_id=payload.parent_id,
        targets=resolved_targets,
    )
    annotations = create_text_annotations(
        db,
        targets=resolved_targets,
        user=actor_user,
        content_text=normalized_content,
        visibility=payload.visibility,
        parent_id=resolved_parent.id if resolved_parent is not None else None,
        created_at=_parse_optional_annotation_created_at(payload.created_at),
    )
    for annotation in annotations:
        log_audit(
            db,
            action="media_annotation_text_created",
            target_type="media_annotation",
            target_id=annotation.id,
            actor_user_id=actor_user.id,
            company_id=actor_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={
                "target_type": "photo" if annotation.photo_id is not None else "media_asset",
                "photo_id": annotation.photo_id,
                "media_asset_id": annotation.media_asset_id,
                "visibility": annotation.visibility.value,
                "role_at_time": annotation.role_at_time.value,
                "parent_id": annotation.parent_id,
            },
        )
    db.commit()
    return {
        "message": "Text annotations created.",
        "annotation_ids": [annotation.id for annotation in annotations],
        "target_count": len(annotations),
    }


@router.get("/annotations/{target_id}")
def list_annotations_v2(
    request: Request,
    target_id: str,
    target_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    normalized_target_type = str(target_type or "").strip().lower()
    if normalized_target_type not in {"", "photo", "media_asset"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid target_type")
    current_user, employee = _get_media_request_user_or_employee(request, db)
    if normalized_target_type == "photo" or (not normalized_target_type and target_id.isdigit()):
        photo = (
            _accessible_photo_for_user(db, current_user, int(target_id))
            if current_user is not None
            else _accessible_photo_for_employee(db, employee, int(target_id))
        )
        if photo is not None:
            return _annotation_tree_response(
                db,
                current_user=current_user,
                photo_id=photo.id,
                public_only=employee is not None,
            )
        if normalized_target_type == "photo":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    asset = db.scalar(
        select(MediaAsset).where(
            MediaAsset.asset_id == target_id,
            MediaAsset.company_id == (current_user.company_id if current_user is not None else employee.company_id),
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
    return _annotation_tree_response(
        db,
        current_user=current_user,
        media_asset_id=asset.asset_id,
        public_only=employee is not None,
    )


@router.head("/annotations/{annotation_id}/audio")
@router.get("/annotations/{annotation_id}/audio")
def annotation_audio_v2(
    annotation_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    annotation = db.get(MediaAnnotation, annotation_id)
    if annotation is None or not annotation.audio_file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation audio not found")
    current_user, employee = _get_media_request_user_or_employee(request, db)
    if annotation.photo_id is not None:
        photo = (
            _accessible_photo_for_user(db, current_user, annotation.photo_id)
            if current_user is not None
            else _accessible_photo_for_employee(db, employee, annotation.photo_id)
        )
        if photo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation audio not found")
    elif annotation.media_asset_id is not None:
        company_id = current_user.company_id if current_user is not None else employee.company_id
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.asset_id == annotation.media_asset_id,
                MediaAsset.company_id == company_id,
            )
        )
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation audio not found")
    if employee is not None and annotation.visibility != AnnotationVisibility.public:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation audio not found")
    audio_path = Path(annotation.audio_file_path)
    if not audio_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Annotation audio not found")
    return FileResponse(
        path=str(audio_path),
        media_type="audio/wav" if audio_path.suffix.lower() == ".wav" else "audio/webm",
        filename=audio_path.name,
    )


@router.post("/webhooks/rtmp_recording_done", response_model=MediaUploadResponse)
def rtmp_recording_done_webhook(
    payload: RTMPRecordingDoneRequest,
    request: Request,
    webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _require_internal_webhook_access(request, webhook_token, settings)
    camera = db.get(IPCamera, payload.camera_id) if payload.camera_id is not None else None
    if camera is not None:
        if camera.company_id != (payload.company_id or camera.company_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="camera_id and company_id do not match")
        resolved_company_id = camera.company_id
    else:
        resolved_company_id = (payload.company_id or "").strip()
        if not resolved_company_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="company_id is required when camera_id is omitted")
    company = db.scalar(select(Company).where(Company.company_id == resolved_company_id, Company.active.is_(True)))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    recording_path = _resolve_local_recording_path(payload.file_path, settings)

    metadata_json = dict(payload.metadata_json or {})
    metadata_json.update(_camera_payload(camera))
    metadata_json["capture_mode"] = "rtmp_push"
    metadata_json["ingested_via"] = "rtmp_recording_done"
    asset = ingest_local_media_file(
        db,
        app_settings=settings,
        source_path=recording_path,
        company_id=resolved_company_id,
        tenant_id=resolved_company_id,
        uploaded_by_user_id=None,
        media_type=MediaType.video,
        source=payload.source,
        original_file_name=recording_path.name,
        metadata_json=metadata_json,
    )
    if camera is not None:
        camera.status = CameraStatus.online
        camera.last_check_at = datetime.now(timezone.utc)
        camera.error_log = None
        db.add(camera)
    log_audit(
        db,
        action="rtmp_recording_ingested",
        target_type="media_asset",
        target_id=asset.asset_id,
        tenant_id=resolved_company_id,
        company_id=resolved_company_id,
        ip_address=request.client.host if request.client else None,
        detail_json={
            "file_path": str(recording_path),
            "camera_id": camera.id if camera is not None else None,
            "source": payload.source,
        },
    )
    db.commit()
    db.refresh(asset)
    return MediaUploadResponse(
        asset_id=asset.asset_id,
        media_type=asset.media_type.value,
        status=asset.status.value,
        upload_complete=True,
        received_chunks=1,
        total_chunks=1,
        duration_seconds=asset.duration_seconds,
        message="RTMP recording ingested and queued for processing.",
    )


@router.post("/photos/bulk-reprocess")
def bulk_reprocess_photos_v2(
    request: Request,
    payload: BulkReprocessRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    requested_ids = list(dict.fromkeys(payload.photo_ids))
    photos = list(
        db.scalars(
            apply_photo_scope(db, select(Photo), current_user).where(Photo.id.in_(requested_ids))
        )
    )
    try:
        queued_ids = queue_photo_reprocess(
            db,
            photos=photos,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=settings,
            actor_user_id=current_user.id,
            source="api_v2_bulk",
            custom_prompt=payload.prompt,
            ip_address=request.client.host if request.client else None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    skipped_ids = [photo_id for photo_id in requested_ids if photo_id not in set(queued_ids)]
    log_audit(
        db,
        action="bulk_photo_reprocess_requested",
        target_type="photo_batch",
        target_id=str(len(queued_ids)),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={
            "photo_ids": queued_ids,
            "skipped_photo_ids": skipped_ids,
            "custom_prompt_supplied": normalize_operator_prompt(payload.prompt) is not None,
            "source": "api_v2_bulk",
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {
        "message": "queued",
        "queued_count": len(queued_ids),
        "queued_photo_ids": queued_ids,
        "skipped_photo_ids": skipped_ids,
    }


@router.post("/photos/search")
def semantic_search_photos_v2(
    request: Request,
    payload: PhotoSemanticSearchRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    scoped_ids_stmt = apply_photo_scope(
        db,
        select(Photo.id),
        current_user,
        photo_type=PhotoType.project,
        gallery_only=is_client(current_user),
    )
    if payload.project_id:
        scoped_ids_stmt = scoped_ids_stmt.where(Photo.project_id == payload.project_id)
    candidate_photo_ids = list(db.scalars(scoped_ids_stmt))
    if not candidate_photo_ids:
        return {"results": []}

    try:
        results = semantic_search_photos(
            db,
            settings,
            tenant_slug=current_user.company_id,
            query=payload.query,
            candidate_photo_ids=candidate_photo_ids,
            limit=payload.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    storage_base_url = get_system_settings(db, settings).get("storage_base_url")
    return {
        "results": [
            {
                "photo": serialize_photo(
                    photo,
                    request,
                    settings,
                    storage_base_url=storage_base_url,
                ).model_dump(),
                "similarity": round(similarity, 6),
            }
            for photo, similarity in results
        ]
    }


@router.get("/photos/{photo_id}/ai_logs")
def photo_ai_logs_v2(
    photo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    photo = _accessible_photo_for_user(db, current_user, photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    return _photo_ai_history_payload_v2(db, photo)


@router.patch("/ai_logs/{log_id}/status")
def update_ai_log_status_v2(
    log_id: str,
    payload: AIAnalysisLogStatusRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    analysis_log = db.get(AIAnalysisLog, log_id)
    if analysis_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI log not found")
    photo = _accessible_photo_for_user(db, current_user, analysis_log.photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    updated_log, snapshot = set_ai_analysis_log_status(
        db,
        app_settings=settings,
        analysis_log=analysis_log,
        status=payload.status,
        actor_user_id=current_user.id,
        source="api_v2_ai_log_status",
    )
    db.commit()
    db.refresh(updated_log)
    db.refresh(photo)
    updated_payload = _photo_ai_history_payload_v2(db, photo)
    return {
        "photo_id": photo.id,
        "snapshot": snapshot,
        "log": serialize_ai_analysis_log(updated_log),
        "active_log_count": updated_payload["active_log_count"],
        "confidence_level": updated_payload["confidence_level"],
        "primary_log_id": updated_payload["primary_log_id"],
    }


@router.post("/ai_logs/{log_id}/promote")
def promote_ai_log_v2(
    log_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    analysis_log = db.get(AIAnalysisLog, log_id)
    if analysis_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI log not found")
    photo = _accessible_photo_for_user(db, current_user, analysis_log.photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    promoted_log, snapshot = promote_ai_analysis_log_to_primary(
        db,
        app_settings=settings,
        analysis_log=analysis_log,
        actor_user_id=current_user.id,
        source="api_v2_ai_log_promote",
    )
    db.commit()
    db.refresh(promoted_log)
    db.refresh(photo)
    updated_payload = _photo_ai_history_payload_v2(db, photo)
    return {
        "photo_id": photo.id,
        "snapshot": snapshot,
        "log": serialize_ai_analysis_log(promoted_log),
        "active_log_count": updated_payload["active_log_count"],
        "confidence_level": updated_payload["confidence_level"],
        "primary_log_id": updated_payload["primary_log_id"],
    }


@router.post("/ai_logs/batch/{batch_id}/rollback")
def rollback_ai_log_batch_v2(
    batch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    candidate_logs = list(db.scalars(select(AIAnalysisLog).where(AIAnalysisLog.batch_id == batch_id)))
    if not candidate_logs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI batch not found")
    allowed_photo_ids = [
        photo_id
        for photo_id in {log.photo_id for log in candidate_logs}
        if _accessible_photo_for_user(db, current_user, photo_id) is not None
    ]
    if not allowed_photo_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI batch not found")
    rolled_back_count, affected_photo_ids = rollback_ai_analysis_batch(
        db,
        app_settings=settings,
        batch_id=batch_id,
        actor_user_id=current_user.id,
        source="api_v2_ai_batch_rollback",
        allowed_photo_ids=allowed_photo_ids,
    )
    db.commit()
    return {
        "batch_id": batch_id,
        "rolled_back_count": rolled_back_count,
        "affected_photo_ids": affected_photo_ids,
    }


@router.post("/reports/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_report_v2(
    request: Request,
    payload: ReportGenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    requested_ids = list(dict.fromkeys(payload.photo_ids))
    accessible_photos = list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
            ).where(Photo.id.in_(requested_ids))
        )
    )
    ordered_photos = {photo.id: photo for photo in accessible_photos}
    photos = [ordered_photos[photo_id] for photo_id in requested_ids if photo_id in ordered_photos]
    if not photos:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No accessible photos selected")

    try:
        report = queue_report_generation(
            db,
            photos=photos,
            prompt=payload.prompt,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=settings,
            actor_user_id=current_user.id,
            company_id=current_user.company_id,
            tenant_id=current_user.company_id,
            source="api_v2_reports",
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    db.commit()
    db.refresh(report)
    return serialize_generated_report(report)


@router.post("/reports/compare-progress", status_code=status.HTTP_202_ACCEPTED)
def compare_progress_report_v2(
    request: Request,
    payload: ProgressCompareRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    requested_ids = list(dict.fromkeys(payload.photo_ids))
    accessible_photos = list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
            ).where(Photo.id.in_(requested_ids))
        )
    )
    ordered_photos = {photo.id: photo for photo in accessible_photos}
    photos = [ordered_photos[photo_id] for photo_id in requested_ids if photo_id in ordered_photos]
    if len(photos) < 2:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="At least two accessible project photos are required")

    try:
        report = queue_progress_report_generation(
            db,
            photos=photos,
            custom_prompt=payload.prompt,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=settings,
            actor_user_id=current_user.id,
            company_id=current_user.company_id,
            tenant_id=current_user.company_id,
            source="api_v2_progress_compare",
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    db.commit()
    db.refresh(report)
    return _progress_report_payload_v2(report, request=request, settings=settings, photos=photos)


@router.get("/reports/compare-progress/{report_id}")
def get_compare_progress_report_v2(
    report_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    report = _company_progress_report(db, current_user.company_id, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Progress report not found")
    source_ids = [int(photo_id) for photo_id in (report.source_photo_ids or []) if str(photo_id).isdigit()]
    photos = list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
            ).where(Photo.id.in_(source_ids))
        )
    ) if source_ids else []
    return _progress_report_payload_v2(report, request=request, settings=settings, photos=photos)


@router.get("/copilot/backends")
def list_copilot_backends_v2(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return {"items": list_copilot_backend_options(db, settings, current_user.company_id)}


@router.get("/copilot/conversations")
def list_copilot_conversations_v2(
    project_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    project_ids = [project_id] if project_id else None
    return {"items": list_copilot_conversations(db, company_id=current_user.company_id, project_ids=project_ids)}


@router.post("/copilot/conversations", status_code=status.HTTP_201_CREATED)
def create_copilot_conversation_v2(
    payload: CopilotConversationCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    preferred_mode = payload.preferred_mode if payload.preferred_mode in COPILOT_ALLOWED_MODES else "standard"
    conversation = create_copilot_conversation(
        db,
        company_id=current_user.company_id,
        tenant_id=current_user.company_id,
        project_id=payload.project_id,
        created_by_user_id=current_user.id,
        title=payload.title,
        preferred_backend_id=payload.preferred_backend_id,
        preferred_mode=preferred_mode,
    )
    db.commit()
    db.refresh(conversation)
    return get_copilot_conversation_payload(db, conversation)


@router.get("/copilot/conversations/{conversation_id}")
def get_copilot_conversation_v2(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    conversation = _company_copilot_conversation(db, current_user.company_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return get_copilot_conversation_payload(db, conversation)


@router.post("/copilot/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
def create_copilot_message_v2(
    conversation_id: str,
    payload: CopilotMessageCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    conversation = _company_copilot_conversation(db, current_user.company_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    preferred_mode = payload.preferred_mode if payload.preferred_mode in COPILOT_ALLOWED_MODES else conversation.preferred_mode
    user_message, assistant_message = queue_copilot_message_generation(
        db,
        app_settings=settings,
        conversation=conversation,
        prompt_text=payload.content_text,
        language=payload.language,
        actor_user_id=current_user.id,
        preferred_backend_id=payload.preferred_backend_id,
        preferred_mode=preferred_mode,
    )
    log_audit(
        db,
        action="copilot_message_requested",
        target_type="copilot_message",
        target_id=assistant_message.id,
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        project_id=conversation.project_id,
        detail_json={"conversation_id": conversation.id, "mode": preferred_mode},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    schedule_job_worker(background_tasks.add_task, request.app.state.session_maker, settings)
    return {
        "conversation_id": conversation.id,
        "user_message": serialize_copilot_message(user_message, db),
        "assistant_message": serialize_copilot_message(assistant_message, db),
    }


@router.get("/copilot/messages/{message_id}")
def get_copilot_message_v2(
    message_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    message = _company_copilot_message(db, current_user.company_id, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return serialize_copilot_message(message, db)


@router.get("/reports/{report_public_id}")
def get_report_status_v2(
    report_public_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    report = _company_report(db, current_user.company_id, report_public_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return serialize_generated_report(report)


@router.get("/reports/{report_public_id}/download")
def download_report_v2(
    report_public_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    report = _company_report(db, current_user.company_id, report_public_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if report.status != ReportStatus.completed or not report.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Report is not ready for download")

    file_path = Path(report.file_path)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report file is missing")

    response = StreamingResponse(
        BytesIO(file_path.read_bytes()),
        media_type=report.mime_type or "application/pdf",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{file_path.name}"'
    return response


@router.post("/reports/{report_public_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_report_v2(
    report_public_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    report = _company_report(db, current_user.company_id, report_public_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    try:
        retry_report_generation(
            db,
            report=report,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=settings,
            actor_user_id=current_user.id,
            source="api_v2_report_retry",
            ip_address=request.client.host if request.client else None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    db.commit()
    db.refresh(report)
    return serialize_generated_report(report)


@router.post("/auth/forgot-password")
def forgot_password_v2(
    request: Request,
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = db.scalar(select(User).where(User.email == payload.email.lower(), User.active.is_(True)))
    if user is not None:
        token = create_password_reset_token(db, user=user, settings=settings)
        reset_url = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
        send_email(
            db,
            settings=settings,
            to_email=user.email or payload.email,
            subject="Reset your KK Field Logger password",
            html_content=f"<p><a href=\"{reset_url}\">Reset password</a></p>",
            action="password_reset_email_sent",
            company_id=user.company_id,
        )
        log_audit(
            db,
            action="v2_forgot_password",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            ip_address=request.client.host if request.client else None,
            company_id=user.company_id,
        )
        db.commit()
    return {"message": "If the account exists, reset instructions have been sent."}


@router.post("/auth/reset-password")
def reset_password_v2(
    request: Request,
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
):
    user = consume_password_reset_token(db, payload.token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")
    user.password_hash = hash_password(payload.new_password)
    db.add(user)
    log_audit(
        db,
        action="v2_reset_password",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        ip_address=request.client.host if request.client else None,
        company_id=user.company_id,
    )
    db.commit()
    return {"message": "Password updated"}


@router.post("/public/delete-account-request")
def delete_account_request(
    request: Request,
    payload: DeleteAccountRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    send_email(
        db,
        settings=settings,
        to_email=payload.email.lower(),
        subject="KK Field Logger account deletion request received",
        html_content=(
            "<p>We received your account deletion request.</p>"
            f"<p>Our support team will contact you from {settings.support_email} if verification is required.</p>"
        ),
        action="delete_account_ack_sent",
    )
    log_audit(
        db,
        action="delete_account_request",
        target_type="public_request",
        target_id=payload.email.lower(),
        detail_json={"reason": payload.reason},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    return {"message": "Request received"}


def _write_public_lead_fallback(
    *,
    settings: Settings,
    request: Request,
    requester_email: str,
    subject: str,
    message: str,
    support_email: str,
    source_ip: str | None,
) -> None:
    record: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requester_email": requester_email,
        "support_email": support_email,
        "subject": subject,
        "message": message,
        "source_ip": source_ip,
        "host": request.headers.get("host"),
        "origin": request.headers.get("origin"),
        "user_agent": request.headers.get("user-agent"),
    }
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    lead_log = log_dir / "public_support_requests.jsonl"
    with lead_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@router.post("/public/support-request")
def support_request(
    request: Request,
    payload: SupportRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    requester_email = payload.email.lower()
    support_email = settings.support_email.lower()
    escaped_subject = escape(payload.subject)
    escaped_message = escape(payload.message).replace("\\n", "<br>")
    source_ip = request.client.host if request.client else None

    send_email(
        db,
        settings=settings,
        to_email=requester_email,
        subject=f"Support request received: {payload.subject}",
        html_content=(
            "<p>Your support request has been received.</p>"
            f"<p>If needed, our team will reply from {settings.support_email}.</p>"
        ),
        action="support_ack_sent",
    )
    if support_email and support_email != requester_email:
        send_email(
            db,
            settings=settings,
            to_email=support_email,
            subject=f"Website request: {payload.subject}",
            html_content=(
                "<p>A public website request was submitted.</p>"
                f"<p><strong>From:</strong> {escape(requester_email)}</p>"
                f"<p><strong>Subject:</strong> {escaped_subject}</p>"
                f"<p><strong>Source IP:</strong> {escape(source_ip or 'unknown')}</p>"
                f"<hr><p>{escaped_message}</p>"
            ),
            action="support_notification_sent",
        )

    try:
        _write_public_lead_fallback(
            settings=settings,
            request=request,
            requester_email=requester_email,
            subject=payload.subject,
            message=payload.message,
            support_email=support_email,
            source_ip=source_ip,
        )
    except Exception as exc:
        logger.warning("public_lead_fallback_write_failed", error=str(exc), email=requester_email)
    log_audit(
        db,
        action="support_request",
        target_type="public_request",
        target_id=requester_email,
        detail_json={"subject": payload.subject, "message": payload.message, "notified": support_email},
        ip_address=source_ip,
    )
    db.commit()
    return {"message": "Request received"}
