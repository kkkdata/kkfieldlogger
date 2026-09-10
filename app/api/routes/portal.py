from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from functools import lru_cache

from app.api.deps import get_current_user, get_current_user_optional, get_db, get_settings
from app.core.config import Settings
from app.core.security import generate_api_key, hash_password, normalize_api_key, verify_password
from app.core.time import build_date_range, to_local_display, to_utc_iso, utc_now
from app.models import (
    AIAnalysisLog,
    AIAnalysisStatus,
    ApprovalStatus,
    AuditLog,
    CameraApprovalStatus,
    CameraProtocol,
    CameraStatus,
    Company,
    CompanyApplication,
    CompanyApplicationStatus,
    Employee,
    GeneratedReport,
    IPCamera,
    Membership,
    MediaAnnotation,
    MembershipStatus,
    MediaAsset,
    MediaAssetStatus,
    MediaType,
    ProgressReport,
    Plan,
    PlanCode,
    Photo,
    PhotoComment,
    PhotoType,
    PhotoVisibility,
    Project,
    ProjectMember,
    ProjectStatus,
    ReportStatus,
    Subscription,
    SubscriptionStatus,
    TaskJob,
    TaskStatus,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.services.access import apply_photo_scope, can_access_photo, can_access_project, get_user_project_ids
from app.services.audit import log_audit
from app.services.camera_scheduler import camera_health_summary, get_camera_failure_alert_threshold, test_ip_camera_connection
from app.services.ai_pipeline import (
    AIBackendError,
    AIBackendNode,
    MAX_AI_MAX_CONCURRENT_REQUESTS,
    MIN_AI_MAX_CONCURRENT_REQUESTS,
    GEMINI_TYPE,
    OLLAMA_TYPE,
    active_ai_log_counts_by_photo,
    ai_confidence_level,
    configure_ai_concurrency_limit,
    configure_ai_dynamic_fallback_enabled,
    get_ai_dynamic_fallback_enabled,
    get_ai_max_concurrent_requests,
    get_system_ai_backends,
    get_tenant_ai_backends,
    localized_ai_summary,
    promote_ai_analysis_log_to_primary,
    normalize_operator_prompt,
    photo_matches_ai_keyword,
    queue_photo_reprocess,
    save_tenant_ai_backends,
    sync_ai_runtime_settings,
    semantic_search_photos,
    test_ai_backend_connection,
)
from app.services.billing import ensure_subscription, get_company_subscription, get_company_usage_summary, get_plan
from app.services.cache import TTLMemoryCache
from app.services.comments import add_photo_comment, get_comments_by_photo_ids, stage_photo_comment
from app.services.employees import (
    assign_employee_project_for_user,
    create_employee_for_user,
    get_manageable_employee,
    list_accessible_employees,
)
from app.services.evidence_copilot import list_copilot_backend_options, list_copilot_conversations
from app.services.email import send_email
from app.services.gpu_monitor import get_gpu_metrics_overview
from app.services.i18n import get_language, set_language, translate
from app.services.media_pipeline import (
    get_media_storage_monitor,
    media_asset_preview_filenames,
    media_asset_thumbnail_filename,
    media_asset_timeline_markers,
    resolve_media_preview_frame_path,
)
from app.services.photos import (
    approve_photo,
    hard_delete_photo,
    restore_photo,
    serialize_photo,
    set_photo_approval_status,
    stage_photo_approval_status,
    stage_photo_display_update,
    stage_photo_soft_delete,
    soft_delete_photo,
    update_photo_visibility,
)
from app.services.rbac import can_manage_companies, is_client, is_manager, is_tenant_admin, is_worker, role_matches
from app.services.ai_pipeline import (
    list_media_asset_ai_logs,
    list_media_asset_ai_logs_by_asset_ids,
    list_photo_ai_logs,
    list_photo_ai_logs_by_photo_ids,
    rollback_ai_analysis_batch,
    serialize_ai_analysis_log,
    set_ai_analysis_log_status,
)
from app.services.reports import (
    queue_progress_report_generation,
    queue_report_generation,
    retry_report_generation,
    serialize_generated_report,
    serialize_progress_report,
)
from app.services.login_throttle import check_login_rate_limit, clear_failed_logins, record_failed_login
from app.services.today import build_today_workbench
from app.services.session import flash, login_user, logout_user, validate_csrf
from app.services.settings import get_system_settings, update_system_settings
from app.services.system_health import get_system_health_overview
from app.services.tenant import delete_company_workspace, provision_company_workspace, scoped_identifier_for_company
from app.services.view import build_template_context
from app.services.job_queue import (
    ANNOTATION_TRANSCRIPTION_TASK,
    COPILOT_CHAT_TASK,
    PHOTO_AI_TASK,
    PROGRESS_REPORT_TASK,
    REPORT_TASK,
    VIDEO_MEDIA_TASK,
)
from app.services.job_queue import average_task_duration_seconds, get_task_queue_snapshot, requeue_task_job

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
templates.env.filters["utc_iso"] = to_utc_iso
templates.env.filters["local_dt"] = to_local_display
PORTAL_PHOTOS_CACHE = TTLMemoryCache(default_ttl_seconds=30, max_entries=128)


class AIAnalysisLogStatusRequest(BaseModel):
    status: AIAnalysisStatus = Field(default=AIAnalysisStatus.rejected)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _require_portal_user(current_user: User | None):
    if current_user is None:
        return _redirect("/portal/login")
    return None


def _t(request: Request, key: str, **kwargs) -> str:
    return translate(key, get_language(request), **kwargs)


def _workspace_mode(current_user: User) -> str:
    if can_manage_companies(current_user):
        return "platform"
    if current_user.role in {UserRole.owner, UserRole.admin}:
        return "company"
    if current_user.role in {UserRole.manager}:
        return "finance"
    if current_user.role == UserRole.project_manager:
        return "project"
    if is_worker(current_user):
        return "worker"
    if is_client(current_user):
        return "client"
    return "general"


def _workspace_focus_payload(request: Request, current_user: User, page: str) -> dict[str, object]:
    mode = _workspace_mode(current_user)
    tags_by_page = {
        "ai_center": {
            "platform": [_t(request, "system_health_title"), _t(request, "gpu_monitor_title"), _t(request, "storage_monitor_title"), _t(request, "nav_ai_backends")],
            "company": [_t(request, "photos_review"), _t(request, "nav_reports"), _t(request, "nav_projects")],
            "finance": [_t(request, "nav_reports"), _t(request, "nav_invoices"), _t(request, "label_storage_cost_estimate")],
            "project": [_t(request, "photos_review"), _t(request, "label_camera_attention"), _t(request, "nav_reports")],
        },
        "ai_backends": {
            "platform": [_t(request, "nav_platform"), _t(request, "storage_monitor_title"), _t(request, "system_health_title")],
            "company": [_t(request, "field_provider"), _t(request, "field_plan"), _t(request, "nav_reports")],
            "finance": [_t(request, "label_storage_cost_estimate"), _t(request, "field_provider"), _t(request, "field_plan")],
            "project": [_t(request, "field_ai_max_concurrent_requests"), _t(request, "field_ai_enable_dynamic_fallback"), _t(request, "nav_reports")],
        },
        "billing": {
            "platform": [_t(request, "nav_billing"), _t(request, "field_plan"), _t(request, "field_storage_usage")],
            "company": [_t(request, "dashboard_monthly_usage"), _t(request, "field_storage_usage"), _t(request, "nav_reports")],
            "finance": [_t(request, "nav_invoices"), _t(request, "field_provider"), _t(request, "label_storage_cost_estimate")],
            "project": [_t(request, "dashboard_monthly_usage"), _t(request, "projects_recent_gallery"), _t(request, "nav_reports")],
        },
        "copilot": {
            "platform": [_t(request, "nav_platform"), _t(request, "nav_reports"), _t(request, "copilot_sources")],
            "company": [_t(request, "copilot_quick_safety"), _t(request, "copilot_quick_finance"), _t(request, "copilot_quick_workforce")],
            "finance": [_t(request, "copilot_quick_finance"), _t(request, "copilot_quick_inventory"), _t(request, "nav_invoices")],
            "project": [_t(request, "copilot_quick_safety"), _t(request, "copilot_quick_quality"), _t(request, "copilot_quick_workforce")],
        },
    }
    role_copy_keys = {
        "platform": "workspace_mode_platform_copy",
        "company": "workspace_mode_company_copy",
        "finance": "workspace_mode_finance_copy",
        "project": "workspace_mode_project_copy",
        "worker": "workspace_mode_worker_copy",
        "client": "workspace_mode_client_copy",
    }
    role_label_keys = {
        "platform": "workspace_mode_platform_label",
        "company": "workspace_mode_company_label",
        "finance": "workspace_mode_finance_label",
        "project": "workspace_mode_project_label",
        "worker": "workspace_mode_worker_label",
        "client": "workspace_mode_client_label",
    }
    return {
        "mode": mode,
        "label": _t(request, role_label_keys.get(mode, "workspace_mode_project_label")),
        "description": _t(request, role_copy_keys.get(mode, "workspace_mode_project_copy")),
        "tags": tags_by_page.get(page, {}).get(mode) or tags_by_page.get(page, {}).get("project") or [],
    }


def _copilot_starter_questions(request: Request, current_user: User) -> list[str]:
    mode = _workspace_mode(current_user)
    if mode == "finance":
        return [
            _t(request, "copilot_quick_finance"),
            _t(request, "copilot_quick_inventory"),
            _t(request, "copilot_quick_workforce"),
            _t(request, "copilot_quick_safety"),
            _t(request, "copilot_quick_quality"),
        ]
    if mode == "platform":
        return [
            _t(request, "copilot_quick_safety"),
            _t(request, "copilot_quick_finance"),
            _t(request, "copilot_quick_quality"),
            _t(request, "copilot_quick_workforce"),
            _t(request, "copilot_quick_inventory"),
        ]
    return [
        _t(request, "copilot_quick_safety"),
        _t(request, "copilot_quick_quality"),
        _t(request, "copilot_quick_workforce"),
        _t(request, "copilot_quick_inventory"),
        _t(request, "copilot_quick_finance"),
    ]


def _localized_ai_snapshot(snapshot: dict[str, object] | None, language: str) -> dict[str, object]:
    normalized_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {"ai_summary": None, "labels": [], "defects": []}
    normalized_snapshot["ai_summary"] = localized_ai_summary(normalized_snapshot, language)
    return normalized_snapshot


def _localized_ai_log_payload(log: AIAnalysisLog, language: str) -> dict[str, object]:
    payload = serialize_ai_analysis_log(log)
    result_data = payload.get("result_data")
    if isinstance(result_data, dict):
        payload["result_data"] = _localized_ai_snapshot(result_data, language)
    return payload


def _render(request: Request, db: Session, settings: Settings, current_user: User | None, template_name: str, **context):
    return templates.TemplateResponse(
        request,
        template_name,
        build_template_context(request, db, settings, current_user, **context),
    )


def _count_from_statement(db: Session, statement) -> int:
    return db.scalar(select(func.count()).select_from(statement.subquery())) or 0


def _platform_activity_profile(request: Request, *, uploads_last_14d: int, active_uploaders_last_14d: int) -> dict[str, str | int]:
    if uploads_last_14d >= 80 or active_uploaders_last_14d >= 12:
        level = "high"
    elif uploads_last_14d >= 20 or active_uploaders_last_14d >= 5:
        level = "steady"
    elif uploads_last_14d >= 1 or active_uploaders_last_14d >= 1:
        level = "light"
    else:
        level = "dormant"
    return {
        "level": level,
        "label": _t(request, f"platform_activity_{level}_label"),
        "copy": _t(
            request,
            f"platform_activity_{level}_copy",
            uploads=uploads_last_14d,
            workers=active_uploaders_last_14d,
        ),
        "uploads_last_14d": uploads_last_14d,
        "active_uploaders_last_14d": active_uploaders_last_14d,
    }


def _platform_company_metrics(db: Session, request: Request) -> dict[str, dict[str, object]]:
    recent_threshold = utc_now() - timedelta(days=14)
    employee_counts = {
        company_id: count
        for company_id, count in db.execute(
            select(Employee.company_id, func.count(Employee.id)).group_by(Employee.company_id)
        ).all()
    }
    photo_counts = {
        company_id: count
        for company_id, count in db.execute(
            select(Photo.company_id, func.count(Photo.id))
            .where(Photo.deleted.is_(False))
            .group_by(Photo.company_id)
        ).all()
    }
    video_counts = {
        company_id: count
        for company_id, count in db.execute(
            select(MediaAsset.company_id, func.count(MediaAsset.asset_id))
            .where(
                MediaAsset.media_type == MediaType.video,
                MediaAsset.status != MediaAssetStatus.deleted,
            )
            .group_by(MediaAsset.company_id)
        ).all()
    }
    recent_uploads = {
        company_id: count
        for company_id, count in db.execute(
            select(Photo.company_id, func.count(Photo.id))
            .where(Photo.deleted.is_(False), Photo.created_at >= recent_threshold)
            .group_by(Photo.company_id)
        ).all()
    }
    recent_active_uploaders = {
        company_id: count
        for company_id, count in db.execute(
            select(Photo.company_id, func.count(func.distinct(Photo.employee_id)))
            .where(Photo.deleted.is_(False), Photo.created_at >= recent_threshold)
            .group_by(Photo.company_id)
        ).all()
    }
    company_ids = set(employee_counts) | set(photo_counts) | set(video_counts) | set(recent_uploads) | set(recent_active_uploaders)
    metrics: dict[str, dict[str, object]] = {}
    for company_id in company_ids:
        uploads_last_14d = int(recent_uploads.get(company_id, 0) or 0)
        active_uploaders_last_14d = int(recent_active_uploaders.get(company_id, 0) or 0)
        metrics[company_id] = {
            "employee_count": int(employee_counts.get(company_id, 0) or 0),
            "photo_count": int(photo_counts.get(company_id, 0) or 0),
            "video_count": int(video_counts.get(company_id, 0) or 0),
            "activity": _platform_activity_profile(
                request,
                uploads_last_14d=uploads_last_14d,
                active_uploaders_last_14d=active_uploaders_last_14d,
            ),
        }
    return metrics


def _platform_redirect_target(next_url: str | None) -> str:
    normalized_next_url = str(next_url or "").strip()
    if normalized_next_url.startswith("/portal/platform"):
        return normalized_next_url
    return "/portal/platform"


def _platform_tenant_row_matches(row: dict[str, object], query: str) -> bool:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return True
    haystack = " ".join(
        str(
            row.get(key)
            or ""
        )
        for key in ("slug", "name", "company_code", "owner_username", "owner_email", "plan_name", "billing_status")
    ).lower()
    return normalized_query in haystack


def _accessible_projects(db: Session, current_user: User) -> list[Project]:
    if is_tenant_admin(current_user):
        return list(
            db.scalars(
                select(Project)
                .where(
                    Project.company_id == current_user.company_id,
                    or_(Project.tenant_id == current_user.company_id, Project.tenant_id.is_(None)),
                )
                .order_by(Project.project_name)
            )
        )
    if is_worker(current_user):
        return []
    project_ids = get_user_project_ids(db, current_user)
    if not project_ids:
        return []
    return list(
        db.scalars(
            select(Project)
            .where(
                Project.company_id == current_user.company_id,
                or_(Project.tenant_id == current_user.company_id, Project.tenant_id.is_(None)),
                Project.project_id.in_(project_ids),
            )
            .order_by(Project.project_name)
        )
    )


def _accessible_employees(db: Session, current_user: User) -> list[Employee]:
    return list_accessible_employees(db, current_user)


def _selected_projects_for_user(db: Session, user_id: int) -> set[str]:
    return set(
        db.scalars(
            select(ProjectMember.project_id)
            .join(Project, Project.project_id == ProjectMember.project_id)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.user_id == user_id, Project.company_id == User.company_id)
        )
    )


def _require_role(current_user: User, *roles: UserRole) -> None:
    if not role_matches(current_user, *roles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@lru_cache(maxsize=1)
def _deployment_settings() -> Settings:
    from app.core.config import load_settings

    return load_settings()


def _require_platform_control(current_user: User) -> None:
    if can_manage_companies(current_user):
        return
    # A private (NAS) deployment has no platform tier: the workspace admin
    # owns the box and controls platform-level settings (AI backends etc.).
    if _deployment_settings().is_private_deployment and is_tenant_admin(current_user):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _require_company_admin_workspace(current_user: User) -> None:
    if can_manage_companies(current_user) or not is_tenant_admin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _can_create_projects(current_user: User) -> bool:
    return is_tenant_admin(current_user)


def _can_manage_project_settings(current_user: User) -> bool:
    return is_tenant_admin(current_user) or current_user.role == UserRole.project_manager


def _creatable_user_roles(current_user: User) -> list[UserRole]:
    if can_manage_companies(current_user):
        return [
            UserRole.owner,
            UserRole.admin,
            UserRole.client_viewer,
            UserRole.super_admin,
            UserRole.project_manager,
            UserRole.client,
        ]
    if is_tenant_admin(current_user):
        return [
            UserRole.admin,
            UserRole.client_viewer,
            UserRole.project_manager,
            UserRole.client,
        ]
    return []


def _role_requires_project_scope(role: UserRole) -> bool:
    return role in {
        UserRole.project_manager,
        UserRole.manager,
        UserRole.client,
        UserRole.client_viewer,
    }


def _build_comment_users(db: Session, comments_by_photo: dict[int, list[PhotoComment]]) -> dict[int, User]:
    user_ids = sorted({comment.user_id for comments in comments_by_photo.values() for comment in comments})
    if not user_ids:
        return {}
    return {user.id: user for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()}


def _build_photo_activity_timeline(
    photos: list[Photo],
    comments_by_photo: dict[int, list[PhotoComment]],
    comment_users: dict[int, User],
    *,
    language: str,
) -> dict[int, list[dict[str, object]]]:
    timeline_by_photo: dict[int, list[dict[str, object]]] = {}
    for photo in photos:
        entries: list[dict[str, object]] = []
        if photo.note:
            entries.append(
                {
                    "created_at": photo.created_at or photo.captured_at_utc,
                    "actor_label": translate("field_note", language),
                    "content": photo.note,
                    "entry_type": "note",
                }
            )
        for comment in comments_by_photo.get(photo.id, []):
            author = comment_users.get(comment.user_id)
            entries.append(
                {
                    "created_at": comment.created_at,
                    "actor_label": author.display_name if author is not None else translate("table_user", language),
                    "content": comment.comment,
                    "entry_type": "comment",
                }
            )
        entries.sort(
            key=lambda item: (
                item.get("created_at") is not None,
                item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        timeline_by_photo[photo.id] = entries
    return timeline_by_photo


def _company_user(db: Session, current_user: User, user_id: int) -> User | None:
    return db.scalar(select(User).where(User.id == user_id, User.company_id == current_user.company_id))


def _company_project(db: Session, current_user: User, project_id: str) -> Project | None:
    return db.scalar(
        select(Project).where(
            Project.project_id == project_id,
            Project.company_id == current_user.company_id,
            or_(Project.tenant_id == current_user.company_id, Project.tenant_id.is_(None)),
        )
    )


def _company_employee(db: Session, current_user: User, employee_id: str) -> Employee | None:
    return db.scalar(
        select(Employee).where(
            Employee.employee_id == employee_id,
            Employee.company_id == current_user.company_id,
            or_(Employee.tenant_id == current_user.company_id, Employee.tenant_id.is_(None)),
        )
    )


def _company_photo(db: Session, current_user: User, photo_id: int) -> Photo | None:
    return db.scalar(select(Photo).where(Photo.id == photo_id, Photo.company_id == current_user.company_id))


def _selected_project_photos(
    db: Session,
    current_user: User,
    photo_ids: list[int],
    *,
    include_deleted: bool = False,
) -> list[Photo]:
    requested_ids = sorted({photo_id for photo_id in photo_ids})
    if not requested_ids:
        return []
    return list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
                include_deleted=include_deleted,
            ).where(Photo.id.in_(requested_ids))
        )
    )


def _company_report(db: Session, current_user: User, report_public_id: str) -> GeneratedReport | None:
    return db.scalar(
        select(GeneratedReport).where(
            GeneratedReport.public_id == report_public_id,
            GeneratedReport.company_id == current_user.company_id,
        )
    )


def _company_camera(db: Session, current_user: User, camera_id: int) -> IPCamera | None:
    return db.scalar(select(IPCamera).where(IPCamera.id == camera_id, IPCamera.company_id == current_user.company_id))


def _camera_review_label(request: Request, approval_status: CameraApprovalStatus | str) -> str:
    normalized = approval_status.value if hasattr(approval_status, "value") else str(approval_status)
    return _t(request, f"camera_approval_{normalized}")


def _reset_camera_review(camera: IPCamera) -> None:
    camera.approval_status = CameraApprovalStatus.pending
    camera.review_notes = None
    camera.reviewed_by_user_id = None
    camera.reviewed_at = None


def _platform_camera_review_rows(db: Session, request: Request) -> list[dict[str, object]]:
    companies = {company.company_id: company for company in db.scalars(select(Company)).all()}
    owners = {
        user.company_id: user
        for user in db.scalars(
            select(User).where(User.role.in_([UserRole.owner, UserRole.admin])).order_by(User.created_at.asc())
        )
    }
    rows: list[dict[str, object]] = []
    cameras = list(
        db.scalars(
            select(IPCamera)
            .where(IPCamera.approval_status == CameraApprovalStatus.pending)
            .order_by(IPCamera.updated_at.desc(), IPCamera.id.desc())
        )
    )
    for camera in cameras:
        company = companies.get(camera.company_id)
        owner = owners.get(camera.company_id)
        rows.append(
            {
                "id": camera.id,
                "company_id": camera.company_id,
                "company_name": company.company_name if company else camera.company_id,
                "company_code": company.company_code if company else "",
                "owner_email": owner.email if owner else "-",
                "owner_username": owner.username if owner else "-",
                "name": camera.name,
                "protocol": camera.protocol.value,
                "stream_url": camera.stream_url,
                "is_enabled": camera.is_enabled,
                "status": camera.status.value,
                "status_label": _t(request, f"status_{camera.status.value}"),
                "approval_label": _camera_review_label(request, camera.approval_status),
                "last_check_at": camera.last_check_at,
                "error_log": camera.error_log,
                "consecutive_failures": camera.consecutive_failures,
                "created_at": camera.created_at,
                "updated_at": camera.updated_at,
            }
        )
    return rows


def _company_media_asset(db: Session, current_user: User, asset_id: str) -> MediaAsset | None:
    return db.scalar(
        select(MediaAsset).where(
            MediaAsset.asset_id == asset_id,
            MediaAsset.company_id == current_user.company_id,
        )
    )


def _company_cameras(db: Session, current_user: User, camera_ids: list[int]) -> list[IPCamera]:
    if not camera_ids:
        return []
    unique_ids = sorted(set(camera_ids))
    return list(
        db.scalars(
            select(IPCamera)
            .where(
                IPCamera.company_id == current_user.company_id,
                IPCamera.id.in_(unique_ids),
            )
            .order_by(IPCamera.id.asc())
        )
    )


def _accessible_portal_photo(db: Session, current_user: User, photo_id: int) -> Photo | None:
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or not can_access_photo(db, current_user, photo):
        return None
    return photo


def _photo_ai_history_payload(db: Session, request: Request, photo: Photo) -> dict[str, object]:
    language = get_language(request)
    logs = list_photo_ai_logs(db, photo.id)
    active_logs = [item for item in logs if item.status == AIAnalysisStatus.active]
    active_log_count = len(active_logs)
    confidence_level = ai_confidence_level(active_log_count)
    return {
        "photo_id": photo.id,
        "snapshot": _localized_ai_snapshot(photo.tag_json, language),
        "logs": [_localized_ai_log_payload(item, language) for item in logs],
        "primary_log_id": active_logs[0].id if active_logs else None,
        "active_log_count": active_log_count,
        "confidence_level": confidence_level,
        "confidence_label": _t(request, f"label_ai_confidence_{confidence_level}") if confidence_level else None,
    }


def _filter_photos_by_ai_history(
    db: Session,
    photos: list[Photo],
    *,
    ai_keyword: str | None,
    include_rejected_ai: bool,
    confidence: str | None,
) -> list[Photo]:
    normalized_keyword = (ai_keyword or "").strip()
    normalized_confidence = (confidence or "").strip().lower()
    if not normalized_keyword and not normalized_confidence:
        return photos

    active_counts = active_ai_log_counts_by_photo(db, [photo.id for photo in photos])
    filtered: list[Photo] = []
    for photo in photos:
        active_count = active_counts.get(photo.id, 0)
        if normalized_confidence and ai_confidence_level(active_count) != normalized_confidence:
            continue
        if normalized_keyword and not photo_matches_ai_keyword(
            db,
            photo,
            normalized_keyword,
            include_rejected=include_rejected_ai,
        ):
            continue
        filtered.append(photo)
    return filtered


def _portal_photos_cache_key(
    *,
    current_user: User,
    semantic_query: str,
    ai_keyword: str,
    report_id: str,
    project_id: str,
    employee_id: str,
    start_date: str,
    end_date: str,
    visibility: str,
    approval_status: str,
    include_rejected_ai: bool,
    include_deleted: bool,
    recycle_bin: bool,
    confidence: str,
    view: str,
) -> dict[str, object]:
    return {
        "user_id": current_user.id,
        "company_id": current_user.company_id,
        "role": current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role),
        "semantic_query": semantic_query,
        "ai_keyword": ai_keyword,
        "report_id": report_id,
        "project_id": project_id,
        "employee_id": employee_id,
        "start_date": start_date,
        "end_date": end_date,
        "visibility": visibility,
        "approval_status": approval_status,
        "include_rejected_ai": include_rejected_ai,
        "include_deleted": include_deleted,
        "recycle_bin": recycle_bin,
        "confidence": confidence,
        "view": view,
    }


def _task_status_rank(status: TaskStatus) -> int:
    order = {
        TaskStatus.running: 0,
        TaskStatus.queued: 1,
        TaskStatus.failed: 2,
        TaskStatus.completed: 3,
        TaskStatus.dead_letter: 4,
    }
    return order.get(status, 99)


def _format_eta_label(request: Request, eta_seconds: int | None) -> str:
    if eta_seconds is None:
        return _t(request, "label_eta_unknown")
    if eta_seconds <= 0:
        return _t(request, "label_eta_done")
    if eta_seconds < 60:
        return _t(request, "label_eta_seconds", value=max(1, int(round(eta_seconds))))
    minutes = max(1, int(round(eta_seconds / 60)))
    return _t(request, "label_eta_minutes", value=minutes)


def _format_duration_label(total_seconds: float | int | None) -> str:
    whole_seconds = max(0, int(round(float(total_seconds or 0.0))))
    minutes, seconds = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _compact_audit_detail(detail: object, *, limit: int = 180) -> str:
    if not detail:
        return ""
    if isinstance(detail, dict):
        pieces: list[str] = []
        for key, value in detail.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=False, default=str)
            else:
                value_text = str(value)
            value_text = " ".join(value_text.split())
            if len(value_text) > 56:
                value_text = f"{value_text[:53]}..."
            pieces.append(f"{key}: {value_text}")
            if len(pieces) >= 3:
                break
        summary = "; ".join(pieces) or "{}"
    else:
        summary = " ".join(str(detail).split())
    if len(summary) > limit:
        return f"{summary[: limit - 3]}..."
    return summary


def _audit_detail_json(detail: object) -> str:
    if not detail:
        return ""
    try:
        return json.dumps(detail, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(detail)


def _estimate_task_progress(job: TaskJob, *, average_duration_seconds: float | None, queue_position: int) -> tuple[int, int | None]:
    if job.status == TaskStatus.completed:
        return 100, 0
    if job.status == TaskStatus.dead_letter:
        return 100, None

    baseline_duration = average_duration_seconds or (90 if job.task_type == REPORT_TASK else 45)
    if job.status == TaskStatus.running:
        if job.started_at is not None:
            elapsed_seconds = max(1.0, (utc_now() - job.started_at).total_seconds())
            progress = min(95, max(15, int(round((elapsed_seconds / baseline_duration) * 100))))
            eta_seconds = max(5, int(round(baseline_duration - elapsed_seconds)))
            return progress, eta_seconds
        return 60, int(round(baseline_duration))

    if job.status == TaskStatus.failed:
        return 35, max(5, int(round(baseline_duration)))

    queued_position = max(1, queue_position)
    progress = min(25, 8 + (queued_position - 1) * 4)
    eta_seconds = max(5, int(round(baseline_duration * queued_position)))
    return progress, eta_seconds


def _camera_health_payload(db: Session, request: Request, company_id: str) -> dict[str, object]:
    failure_threshold = get_camera_failure_alert_threshold(db, request.app.state.settings)
    summary = camera_health_summary(db, company_id, failure_threshold=failure_threshold)
    summary["attention_label"] = _t(request, "label_camera_attention")
    return summary


def _storage_monitor_payload(db: Session, request: Request, current_user: User) -> dict[str, object]:
    subscription = get_company_subscription(db, current_user.company_id)
    plan = get_plan(db, subscription.plan_id)
    summary = get_media_storage_monitor(
        db,
        request.app.state.settings,
        company_id=current_user.company_id,
        storage_limit_mb=plan.storage_limit_mb,
    )
    summary["warning_message"] = (
        _t(
            request,
            "msg_storage_warning",
            percent=int(round(float(summary["usage_rate_percent"]))),
        )
        if summary["warning"]
        else None
    )
    return summary


def _system_health_payload(db: Session, request: Request, current_user: User) -> dict[str, object]:
    subscription = get_company_subscription(db, current_user.company_id)
    plan = get_plan(db, subscription.plan_id)
    summary = get_system_health_overview(
        db,
        request.app.state.settings,
        request.app.state.session_maker,
        company_id=current_user.company_id,
        storage_limit_mb=plan.storage_limit_mb,
    )
    summary["warning_title"] = _t(request, "system_health_warning_title")
    summary["warning_messages"] = [
        _t(
            request,
            "msg_system_storage_warning",
            percent=int(round(float(summary["storage_usage_rate_percent"]))),
            threshold=int(round(float(summary["storage_alert_threshold_percent"]))),
        )
        if summary.get("storage_warning")
        else None,
        _t(
            request,
            "msg_system_queue_warning",
            active_jobs=int(summary["global_queue_active_jobs"]),
            threshold=int(summary["queue_threshold"]),
        )
        if summary.get("queue_warning")
        else None,
    ]
    summary["warning_messages"] = [message for message in summary["warning_messages"] if message]
    return summary


def _media_asset_payload(db: Session, request: Request, asset: MediaAsset) -> dict[str, object]:
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    duration_seconds = float(asset.duration_seconds or metadata_json.get("duration_seconds") or 0.0)
    preview_frames = media_asset_preview_filenames(asset)
    thumbnail_name = media_asset_thumbnail_filename(asset)
    timeline_markers = []
    for marker in media_asset_timeline_markers(asset):
        time_seconds = float(marker.get("time_seconds") or 0.0)
        position_percent = round((time_seconds / duration_seconds) * 100.0, 2) if duration_seconds > 0 else 0.0
        timeline_markers.append(
            {
                **marker,
                "position_percent": max(0.0, min(100.0, position_percent)),
            }
        )
    preview_items = [
        {
            "name": frame_name,
            "url": f"/portal/media/{asset.asset_id}/preview/{frame_name}",
        }
        for frame_name in preview_frames
    ]
    camera_id = metadata_json.get("camera_id")
    camera = db.get(IPCamera, int(camera_id)) if str(camera_id or "").isdigit() else None
    stream_path = Path(asset.file_path) if asset.file_path else None
    return {
        "asset_id": asset.asset_id,
        "media_type": asset.media_type.value if hasattr(asset.media_type, "value") else str(asset.media_type),
        "status": asset.status.value if hasattr(asset.status, "value") else str(asset.status),
        "source": asset.source,
        "original_file_name": asset.original_file_name,
        "duration_seconds": duration_seconds,
        "duration_label": _format_duration_label(duration_seconds),
        "stream_url": f"/portal/media/{asset.asset_id}/stream" if stream_path is not None and stream_path.is_file() else None,
        "thumbnail_url": (
            f"/portal/media/{asset.asset_id}/preview/{thumbnail_name}"
            if thumbnail_name
            else None
        ),
        "preview_items": preview_items,
        "timeline_markers": timeline_markers,
        "ai_snapshot": metadata_json.get("ai_snapshot") or {"ai_summary": None, "labels": [], "defects": []},
        "camera": {
            "id": camera.id,
            "name": camera.name,
            "status": camera.status.value,
        } if camera is not None else None,
        "storage_tier": metadata_json.get("storage_tier") or "hot",
        "notification_message": metadata_json.get("notification_message"),
    }


def _ai_center_payload(db: Session, request: Request, current_user: User) -> dict[str, object]:
    recent_cutoff = utc_now() - timedelta(hours=24)
    timezone_name = get_system_settings(db, request.app.state.settings).get(
        "timezone",
        request.app.state.settings.default_timezone,
    )
    jobs = list(
        db.scalars(
            select(TaskJob).where(
                TaskJob.company_id == current_user.company_id,
                or_(
                    TaskJob.status.in_([TaskStatus.queued, TaskStatus.running, TaskStatus.failed, TaskStatus.dead_letter]),
                    TaskJob.created_at >= recent_cutoff,
                ),
            )
        )
    )
    jobs.sort(key=lambda job: (_task_status_rank(job.status), -job.priority, job.created_at), reverse=False)
    active_jobs = [job for job in jobs if job.status in {TaskStatus.queued, TaskStatus.running, TaskStatus.failed, TaskStatus.dead_letter}]
    queue_order = sorted(active_jobs, key=lambda job: (-job.priority, job.created_at))
    queue_positions = {job.public_id: index + 1 for index, job in enumerate(queue_order)}

    photo_ids = sorted(
        {
            int(job.related_id)
            for job in jobs
            if job.related_type == "photo" and isinstance(job.related_id, str) and job.related_id.isdigit()
        }
    )
    report_ids = sorted(
        {
            job.related_id
            for job in jobs
            if job.related_type == "report" and isinstance(job.related_id, str) and job.related_id
        }
    )
    progress_report_ids = sorted(
        {
            job.related_id
            for job in jobs
            if job.related_type == "progress_report" and isinstance(job.related_id, str) and job.related_id
        }
    )
    media_asset_ids = sorted(
        {
            job.related_id
            for job in jobs
            if job.related_type == "media_asset" and isinstance(job.related_id, str) and job.related_id
        }
    )
    annotation_ids = sorted(
        {
            annotation_id
            for job in jobs
            if job.related_type == "media_annotation" or job.task_type == ANNOTATION_TRANSCRIPTION_TASK
            for annotation_id in (
                [
                    str(item).strip()
                    for item in (job.payload_json.get("annotation_ids") if isinstance(job.payload_json, dict) else []) or []
                    if str(item).strip()
                ]
                + ([str(job.related_id).strip()] if isinstance(job.related_id, str) and job.related_id.strip() else [])
            )
        }
    )
    annotations_by_id = {
        annotation.id: annotation
        for annotation in db.scalars(select(MediaAnnotation).where(MediaAnnotation.id.in_(annotation_ids)))
    } if annotation_ids else {}
    annotation_photo_ids = {
        annotation.photo_id
        for annotation in annotations_by_id.values()
        if annotation.photo_id is not None
    }
    annotation_media_asset_ids = {
        annotation.media_asset_id
        for annotation in annotations_by_id.values()
        if annotation.media_asset_id
    }
    photo_ids = sorted(set(photo_ids) | set(annotation_photo_ids))
    media_asset_ids = sorted(set(media_asset_ids) | set(annotation_media_asset_ids))
    photos_by_id = {
        photo.id: photo
        for photo in db.scalars(
            apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(Photo.id.in_(photo_ids))
        )
    } if photo_ids else {}
    reports_by_public_id = {
        report.public_id: report
        for report in db.scalars(
            select(GeneratedReport).where(
                GeneratedReport.company_id == current_user.company_id,
                GeneratedReport.public_id.in_(report_ids),
            )
        )
    } if report_ids else {}
    progress_reports_by_id = {
        report.id: report
        for report in db.scalars(
            select(ProgressReport).where(
                ProgressReport.company_id == current_user.company_id,
                ProgressReport.id.in_(progress_report_ids),
            )
        )
    } if progress_report_ids else {}
    media_assets_by_id = {
        asset.asset_id: asset
        for asset in db.scalars(
            select(MediaAsset).where(
                MediaAsset.company_id == current_user.company_id,
                MediaAsset.asset_id.in_(media_asset_ids),
            )
        )
    } if media_asset_ids else {}
    camera_ids = sorted(
        {
            int(asset.metadata_json.get("camera_id"))
            for asset in media_assets_by_id.values()
            if isinstance(asset.metadata_json, dict) and str(asset.metadata_json.get("camera_id") or "").isdigit()
        }
    )
    cameras_by_id = {
        camera.id: camera
        for camera in db.scalars(
            select(IPCamera).where(
                IPCamera.company_id == current_user.company_id,
                IPCamera.id.in_(camera_ids),
            )
        )
    } if camera_ids else {}
    camera_health = _camera_health_payload(db, request, current_user.company_id)
    storage_monitor = _storage_monitor_payload(db, request, current_user)
    system_health = _system_health_payload(db, request, current_user)
    gpu_monitor = get_gpu_metrics_overview(db, request.app.state.settings) if can_manage_companies(current_user) else None
    average_durations = {
        ANNOTATION_TRANSCRIPTION_TASK: average_task_duration_seconds(db, current_user.company_id, ANNOTATION_TRANSCRIPTION_TASK),
        PHOTO_AI_TASK: average_task_duration_seconds(db, current_user.company_id, PHOTO_AI_TASK),
        REPORT_TASK: average_task_duration_seconds(db, current_user.company_id, REPORT_TASK),
        PROGRESS_REPORT_TASK: average_task_duration_seconds(db, current_user.company_id, PROGRESS_REPORT_TASK),
        VIDEO_MEDIA_TASK: average_task_duration_seconds(db, current_user.company_id, VIDEO_MEDIA_TASK),
        COPILOT_CHAT_TASK: average_task_duration_seconds(db, current_user.company_id, COPILOT_CHAT_TASK),
    }

    items: list[dict[str, object]] = []
    for job in jobs:
        average_duration = average_durations.get(job.task_type)
        progress_percent, eta_seconds = _estimate_task_progress(
            job,
            average_duration_seconds=average_duration,
            queue_position=queue_positions.get(job.public_id, 1),
        )
        related_title = None
        related_url = "/portal/photos"
        source_label = None
        task_category = "photo"
        camera_status = None
        camera_status_label = None
        camera_attention = False
        if job.related_type == "photo" and job.related_id and job.related_id.isdigit():
            photo = photos_by_id.get(int(job.related_id))
            if photo is not None:
                related_title = photo.original_file_name
                related_url = f"/portal/photos?focus_photo_id={photo.id}#photo-card-{photo.id}"
        elif job.related_type == "report" and job.related_id:
            report = reports_by_public_id.get(job.related_id)
            if report is not None:
                related_title = report.title
                related_url = f"/portal/reports?tab=generated"
            task_category = "report"
        elif job.related_type == "progress_report" and job.related_id:
            report = progress_reports_by_id.get(job.related_id)
            if report is not None:
                related_title = f"{report.project_id} Progress Compare"
                related_url = f"/portal/reports/progress/{report.id}"
            task_category = "report"
        elif job.related_type == "media_asset" and job.related_id:
            asset = media_assets_by_id.get(job.related_id)
            task_category = "camera"
            related_url = f"/portal/media/{job.related_id}"
            if asset is not None:
                related_title = asset.original_file_name
                if isinstance(asset.metadata_json, dict):
                    camera_id = asset.metadata_json.get("camera_id")
                    if str(camera_id or "").isdigit():
                        camera = cameras_by_id.get(int(camera_id))
                        if camera is not None:
                            related_title = f"{camera.name} - {asset.original_file_name}"
                            camera_status = camera.status.value
                            camera_status_label = _t(request, f"status_{camera.status.value}")
                            camera_attention = camera.is_enabled and camera.status in {CameraStatus.offline, CameraStatus.error}
                            source_label = _t(request, "label_ip_camera_source")
        elif job.task_type == ANNOTATION_TRANSCRIPTION_TASK or job.related_type == "media_annotation":
            task_category = "annotation"
            related_title = _t(request, "media_annotations_title")
            job_annotation_ids = []
            if isinstance(job.payload_json, dict):
                raw_annotation_ids = job.payload_json.get("annotation_ids") or []
                job_annotation_ids.extend(str(item).strip() for item in raw_annotation_ids if str(item).strip())
            if isinstance(job.related_id, str) and job.related_id.strip():
                job_annotation_ids.append(job.related_id.strip())
            annotation = next((annotations_by_id.get(annotation_id) for annotation_id in job_annotation_ids if annotations_by_id.get(annotation_id)), None)
            if annotation is not None and annotation.photo_id is not None:
                photo = photos_by_id.get(annotation.photo_id)
                if photo is not None:
                    related_title = photo.original_file_name
                    related_url = f"/portal/photos?focus_photo_id={photo.id}#photo-card-{photo.id}"
            elif annotation is not None and annotation.media_asset_id:
                asset = media_assets_by_id.get(annotation.media_asset_id)
                related_title = asset.original_file_name if asset is not None else _t(request, "media_annotations_title")
                related_url = f"/portal/media/{annotation.media_asset_id}"
        elif job.task_type == COPILOT_CHAT_TASK:
            task_category = "copilot"
            related_title = _t(request, "page_copilot")
            related_url = "/portal/copilot"

        items.append(
            {
                "task_id": job.public_id,
                "task_type": job.task_type,
                "task_type_label": _t(
                    request,
                    (
                        "label_ai_task_type_report"
                        if job.task_type in {REPORT_TASK, PROGRESS_REPORT_TASK}
                        else "label_ai_task_type_camera"
                        if task_category == "camera"
                        else "label_ai_task_type_copilot"
                        if task_category == "copilot"
                        else "label_ai_task_type_annotation"
                        if task_category == "annotation"
                        else "label_ai_task_type_photo"
                    ),
                ),
                "status": job.status.value,
                "status_label": _t(request, f"status_{job.status.value}"),
                "progress_percent": progress_percent,
                "eta_label": _format_eta_label(request, eta_seconds),
                "related_title": related_title or job.related_id or job.public_id,
                "related_url": related_url,
                "related_type": job.related_type or "",
                "task_category": task_category,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "last_error": job.last_error,
                "created_at": to_local_display(job.created_at, timezone_name),
                "source_label": source_label or (
                    _t(request, "label_mobile_upload") if (
                        job.task_type == PHOTO_AI_TASK
                        and isinstance(job.payload_json, dict)
                        and str(job.payload_json.get("trigger_source") or "").strip().lower() == "upload"
                    ) else None
                ),
                "camera_status": camera_status,
                "camera_status_label": camera_status_label,
                "camera_attention": camera_attention,
                "can_retry": is_manager(current_user) and job.status in {TaskStatus.failed, TaskStatus.dead_letter},
            }
        )

    summary = {
        "total_tasks": len(jobs),
        "completed_tasks": sum(1 for job in jobs if job.status == TaskStatus.completed),
        "queued_tasks": sum(1 for job in jobs if job.status == TaskStatus.queued),
        "running_tasks": sum(1 for job in jobs if job.status == TaskStatus.running),
        "failed_tasks": sum(1 for job in jobs if job.status in {TaskStatus.failed, TaskStatus.dead_letter}),
        "dead_letter_tasks": sum(1 for job in jobs if job.status == TaskStatus.dead_letter),
        "status_counts": {
            "completed": sum(1 for job in jobs if job.status == TaskStatus.completed),
            "queued": sum(1 for job in jobs if job.status == TaskStatus.queued),
            "running": sum(1 for job in jobs if job.status == TaskStatus.running),
            "failed": sum(1 for job in jobs if job.status == TaskStatus.failed),
            "dead_letter": sum(1 for job in jobs if job.status == TaskStatus.dead_letter),
        },
        "category_counts": {
            "photo": sum(1 for item in items if item["task_category"] == "photo"),
            "report": sum(1 for item in items if item["task_category"] == "report"),
            "camera": sum(1 for item in items if item["task_category"] == "camera"),
            "copilot": sum(1 for item in items if item["task_category"] == "copilot"),
            "annotation": sum(1 for item in items if item["task_category"] == "annotation"),
        },
    }
    return {
        "summary": summary,
        "system_health": system_health,
        "gpu_monitor": gpu_monitor,
        "camera_health": camera_health,
        "storage_monitor": storage_monitor,
        "items": items,
        "updated_at": to_utc_iso(utc_now()),
    }


def _report_history_payload(db: Session, request: Request, current_user: User) -> dict[str, object]:
    timezone_name = get_system_settings(db, request.app.state.settings).get(
        "timezone",
        request.app.state.settings.default_timezone,
    )
    generated_reports = list(
        db.scalars(
            select(GeneratedReport)
            .where(GeneratedReport.company_id == current_user.company_id)
            .order_by(GeneratedReport.created_at.desc())
            .limit(100)
        )
    )
    progress_reports = list(
        db.scalars(
            select(ProgressReport)
            .where(ProgressReport.company_id == current_user.company_id)
            .order_by(ProgressReport.created_at.desc())
            .limit(100)
        )
    )
    progress_photo_ids = sorted(
        {
            int(photo_id)
            for report in progress_reports
            for photo_id in (report.source_photo_ids or [])
            if str(photo_id).isdigit()
        }
    )
    progress_photo_map = (
        {
            photo.id: photo
            for photo in db.scalars(
                apply_photo_scope(
                    db,
                    select(Photo).where(Photo.id.in_(progress_photo_ids)),
                    current_user,
                    photo_type=PhotoType.project,
                )
            )
        }
        if progress_photo_ids
        else {}
    )
    return {
        "generated_items": [
            {
                **serialize_generated_report(report),
                "created_at_local": to_local_display(report.created_at, timezone_name),
                "completed_at_local": to_local_display(report.completed_at, timezone_name) if report.completed_at else None,
                "photo_count": len(report.source_photo_ids or []),
            }
            for report in generated_reports
        ],
        "progress_items": [
            {
                **serialize_progress_report(
                    report,
                    settings=request.app.state.settings,
                    request=request,
                    photos=[
                        progress_photo_map[int(photo_id)]
                        for photo_id in (report.source_photo_ids or [])
                        if str(photo_id).isdigit() and int(photo_id) in progress_photo_map
                    ],
                ),
                "created_at_local": to_local_display(report.created_at, timezone_name),
                "completed_at_local": to_local_display(report.completed_at, timezone_name) if report.completed_at else None,
                "photo_count": len(report.source_photo_ids or []),
            }
            for report in progress_reports
        ],
        "updated_at": to_utc_iso(utc_now()),
    }


def _ai_backend_form_node(
    *,
    backend_id: str,
    backend_type: str,
    url: str,
    model: str,
    api_key: str | None,
    weight: str,
    enabled: str | None,
    existing_node: AIBackendNode | None = None,
) -> AIBackendNode:
    normalized_id = backend_id.strip()
    normalized_type = backend_type.strip().lower()
    normalized_url = url.strip()
    normalized_model = model.strip()
    if not normalized_id or not normalized_url or not normalized_model:
        raise ValueError("Backend id, url, and model are required")
    if normalized_type not in {OLLAMA_TYPE, GEMINI_TYPE}:
        raise ValueError("Backend type must be ollama or gemini")
    try:
        normalized_weight = int(weight)
    except ValueError as exc:
        raise ValueError("Weight must be a positive integer") from exc
    if normalized_weight <= 0:
        raise ValueError("Weight must be a positive integer")

    normalized_api_key = (api_key or "").strip() or (existing_node.api_key if existing_node else None)
    return AIBackendNode(
        id=normalized_id,
        type=normalized_type,
        url=normalized_url,
        model=normalized_model,
        api_key=normalized_api_key,
        weight=normalized_weight,
        enabled=enabled == "on",
    )


def _find_backend_node(nodes: list[AIBackendNode], backend_id: str) -> AIBackendNode | None:
    return next((node for node in nodes if node.id == backend_id), None)


def _append_query_param(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url or "/portal/photos")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_pairs = [(existing_key, existing_value) for existing_key, existing_value in query_pairs if existing_key != key]
    query_pairs.append((key, value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_pairs), parsed.fragment))


def _billing_summary(db: Session, current_user: User, timezone_name: str) -> dict[str, int | str | None]:
    subscription = get_company_subscription(db, current_user.company_id)
    plan = get_plan(db, subscription.plan_id)
    usage = get_company_usage_summary(db, company_id=current_user.company_id, timezone_name=timezone_name)
    return {
        "company_id": current_user.company_id,
        "plan_id": plan.plan_id.value,
        "plan_name": plan.display_name,
        "billing_status": subscription.status.value,
        "daily_upload_limit": plan.daily_upload_limit,
        "monthly_photo_limit": plan.monthly_photo_limit,
        "storage_limit_mb": plan.storage_limit_mb,
        "photos_this_month": usage["photos_this_month"],
        "uploads_today": usage["uploaded_count"],
        "storage_used": usage["storage_used"],
        "provider": subscription.provider or "manual",
        "started_at": to_utc_iso(subscription.started_at),
        "expires_at": to_utc_iso(subscription.expires_at),
    }


def _overview_stats(db: Session, current_user: User, timezone_name: str) -> dict[str, int]:
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    start_dt, end_dt = build_date_range(today, today, timezone_name)
    photos_today_stmt = apply_photo_scope(db, select(Photo.id), current_user)
    if start_dt:
        photos_today_stmt = photos_today_stmt.where(Photo.created_at >= start_dt)
    if end_dt:
        photos_today_stmt = photos_today_stmt.where(Photo.created_at < end_dt)

    if is_tenant_admin(current_user):
        active_projects = db.scalar(
            select(func.count()).select_from(
                select(Project)
                .where(Project.company_id == current_user.company_id, Project.status == ProjectStatus.active)
                .subquery()
            )
        ) or 0
        active_workers = db.scalar(
            select(func.count()).select_from(
                select(Employee)
                .where(Employee.company_id == current_user.company_id, Employee.active.is_(True))
                .subquery()
            )
        ) or 0
    elif is_manager(current_user) or is_client(current_user):
        project_ids = get_user_project_ids(db, current_user)
        active_projects = db.scalar(
            select(func.count()).select_from(
                select(Project)
                .where(
                    Project.company_id == current_user.company_id,
                    Project.project_id.in_(project_ids),
                    Project.status == ProjectStatus.active,
                )
                .subquery()
            )
        ) or 0
        active_workers = db.scalar(
            select(func.count()).select_from(
                select(Employee)
                .where(
                    Employee.company_id == current_user.company_id,
                    Employee.project_id.in_(project_ids),
                    Employee.active.is_(True),
                )
                .subquery()
            )
        ) or 0
    else:
        active_projects = 0
        active_workers = 1 if current_user.employee_id else 0

    pending_stmt = apply_photo_scope(db, select(Photo.id), current_user, photo_type=PhotoType.project).where(
        Photo.approval_status == ApprovalStatus.pending
    )
    return {
        "total_photos": _count_from_statement(db, apply_photo_scope(db, select(Photo.id), current_user)),
        "photos_today": _count_from_statement(db, photos_today_stmt),
        "active_projects": active_projects,
        "active_workers": active_workers,
        "pending_approvals": 0 if is_client(current_user) else _count_from_statement(db, pending_stmt),
    }


def _project_stats(db: Session, current_user: User) -> list[dict[str, str | int | None]]:
    projects = _accessible_projects(db, current_user)
    items: list[dict[str, str | int | None]] = []
    for project in projects:
        stmt = apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(
            Photo.project_id == project.project_id
        )
        photos = list(db.scalars(stmt))
        last_upload = max((photo.created_at for photo in photos), default=None)
        items.append(
            {
                "project_id": project.project_id,
                "photo_count": len(photos),
                "last_upload_time": to_utc_iso(last_upload),
            }
        )
    return items


def _upload_series(db: Session, current_user: User, timezone_name: str, days: int = 7) -> list[dict[str, str | int]]:
    zone = ZoneInfo(timezone_name)
    today = datetime.now(zone).date()
    points: list[dict[str, str | int]] = []
    for offset in range(days - 1, -1, -1):
        target_day = today - timedelta(days=offset)
        start_dt, end_dt = build_date_range(target_day.isoformat(), target_day.isoformat(), timezone_name)
        stmt = apply_photo_scope(db, select(Photo.id), current_user)
        if start_dt:
            stmt = stmt.where(Photo.created_at >= start_dt)
        if end_dt:
            stmt = stmt.where(Photo.created_at < end_dt)
        points.append({"label": target_day.strftime("%m-%d"), "count": _count_from_statement(db, stmt)})
    return points


@router.get("/portal/login", response_class=HTMLResponse)
def portal_login_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    if current_user is not None:
        return _redirect("/portal")
    if settings.is_private_deployment and db.scalar(select(User.id).limit(1)) is None:
        return _redirect("/setup")
    return _render(request, db, settings, current_user, "pages/auth/login.html", page_title=_t(request, "page_login"))


@router.post("/portal/language")
def set_portal_language(
    request: Request,
    language: str = Form(...),
    next_url: str = Form("/portal"),
):
    set_language(request, language)
    return _redirect(next_url or "/portal")


@router.post("/portal/login")
def portal_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    identifier = username.strip().lower()
    check_login_rate_limit(request, identifier)
    user = db.scalar(select(User).where(or_(User.username == identifier, User.email == identifier)))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        record_failed_login(request, identifier)
        log_audit(
            db,
            action="portal_login_failed",
            target_type="user",
            target_id=str(user.id) if user else None,
            detail_json={"username": username},
            ip_address=request.client.host if request.client else None,
        )
        db.commit()
        flash(request, "error", _t(request, "msg_invalid_credentials"))
        return _redirect("/portal/login")

    clear_failed_logins(request, identifier)
    login_user(request, user.id)
    user.last_login_at = utc_now()
    db.add(user)
    log_audit(
        db,
        action="portal_login",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_login_success"))
    return _redirect("/portal")


@router.post("/portal/logout")
def portal_logout(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    validate_csrf(request, csrf_token)
    log_audit(
        db,
        action="portal_logout",
        target_type="user",
        target_id=str(current_user.id),
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    logout_user(request)
    return _redirect("/portal/login")


@router.get("/portal/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/users/profile.html",
        page_title=_t(request, "page_profile"),
    )


@router.post("/portal/user/change-password")
def change_password_action(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    validate_csrf(request, csrf_token)
    if not verify_password(current_password, current_user.password_hash):
        flash(request, "error", _t(request, "msg_current_password_incorrect"))
        return _redirect("/portal/profile")
    if len(new_password) < 10:
        flash(request, "error", _t(request, "msg_password_min_length"))
        return _redirect("/portal/profile")
    current_user.password_hash = hash_password(new_password)
    db.add(current_user)
    log_audit(
        db,
        action="user_changed_own_password",
        target_type="user",
        target_id=str(current_user.id),
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_password_updated"))
    return _redirect("/portal/profile")


@router.get("/portal/today", response_class=HTMLResponse)
def today_workbench_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not is_manager(current_user):
        return _redirect("/portal")
    context = build_today_workbench(
        db,
        current_user,
        accessible_projects=_accessible_projects(db, current_user),
    )
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/dashboard/today.html",
        page_title=_t(request, "page_today"),
        **context,
    )


@router.get("/portal", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    # The today workbench is the manager landing page; the classic dashboard
    # stays reachable at /portal?classic=1.
    if is_manager(current_user) and request.query_params.get("classic") != "1":
        return _redirect("/portal/today")

    system_settings = get_system_settings(db, settings)
    timezone_name = system_settings.get("timezone", settings.default_timezone)
    overview = _overview_stats(db, current_user, timezone_name)
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    start_dt, end_dt = build_date_range(today, today, timezone_name)
    invoices_stmt = apply_photo_scope(db, select(Photo.id), current_user, photo_type=PhotoType.invoice)
    if start_dt:
        invoices_stmt = invoices_stmt.where(Photo.created_at >= start_dt)
    if end_dt:
        invoices_stmt = invoices_stmt.where(Photo.created_at < end_dt)
    recent_stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.project if is_client(current_user) else None,
        gallery_only=is_client(current_user),
    ).order_by(Photo.created_at.desc()).limit(8)
    billing_summary = _billing_summary(db, current_user, timezone_name)
    dashboard_mode = "platform" if can_manage_companies(current_user) else "workspace"
    platform_dashboard = None
    if dashboard_mode == "platform":
        latest_tenants = list(db.scalars(select(Tenant).order_by(Tenant.created_at.desc()).limit(6)))
        latest_applications = list(
            db.scalars(
                select(CompanyApplication)
                .order_by(CompanyApplication.created_at.desc())
                .limit(6)
            )
        )
        platform_dashboard = {
            "tenant_count": db.scalar(select(func.count()).select_from(Tenant)) or 0,
            "active_tenants": db.scalar(
                select(func.count()).select_from(Tenant).where(Tenant.status == TenantStatus.active)
            )
            or 0,
            "suspended_tenants": db.scalar(
                select(func.count()).select_from(Tenant).where(Tenant.status == TenantStatus.suspended)
            )
            or 0,
            "pending_applications": db.scalar(
                select(func.count()).select_from(CompanyApplication).where(
                    CompanyApplication.status == CompanyApplicationStatus.pending
                )
            )
            or 0,
            "failed_jobs": db.scalar(
                select(func.count()).select_from(TaskJob).where(
                    TaskJob.status.in_([TaskStatus.failed, TaskStatus.dead_letter])
                )
            )
            or 0,
            "latest_tenants": latest_tenants,
            "latest_applications": latest_applications,
        }

    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/dashboard/index.html",
        page_title=_t(request, "page_dashboard"),
        photos_today=overview["photos_today"],
        total_photos=overview["total_photos"],
        invoices_today=_count_from_statement(db, invoices_stmt),
        active_projects=overview["active_projects"],
        active_workers=overview["active_workers"],
        pending_approvals=overview["pending_approvals"],
        recent_uploads=list(db.scalars(recent_stmt)),
        uploads_over_time=_upload_series(db, current_user, timezone_name),
        project_comparison=_project_stats(db, current_user),
        billing_summary=billing_summary,
        dashboard_mode=dashboard_mode,
        platform_dashboard=platform_dashboard,
    )


@router.get("/portal/dashboard")
def dashboard_legacy_alias(
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    return _redirect("/portal")


@router.get("/portal/system-health")
def system_health_legacy_alias(
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    return _redirect("/portal/ai-center#system-health")


@router.get("/portal/tenant-applications")
def tenant_applications_legacy_alias(
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    return _redirect("/portal/platform#tenant-applications")


@router.get("/portal/companies")
def companies_legacy_alias(
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    return _redirect("/portal/platform#tenant-list")


@router.get("/portal/photos", response_class=HTMLResponse)
def photos_page(
    request: Request,
    semantic_query: str | None = Query(None),
    ai_keyword: str | None = Query(None),
    report_id: str | None = Query(None),
    progress_report_id: str | None = Query(None),
    focus_photo_id: int | None = Query(None),
    project_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    visibility: str | None = Query(None),
    approval_status: str | None = Query(None),
    include_rejected_ai: bool = Query(False),
    confidence: str | None = Query(None),
    include_deleted: bool = Query(False),
    recycle_bin: bool = Query(False),
    view: str = Query("grid"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=25, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)

    manage_deleted = is_manager(current_user)
    show_deleted = (include_deleted or recycle_bin) and manage_deleted
    stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.project,
        include_deleted=show_deleted,
    )
    if recycle_bin:
        stmt = stmt.where(Photo.deleted.is_(True))
    system_settings = get_system_settings(db, settings)
    timezone_name = system_settings.get("timezone", settings.default_timezone)
    range_start, range_end = build_date_range(start_date, end_date, timezone_name)
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)
    if employee_id:
        stmt = stmt.where(Photo.employee_id == employee_id)
    if visibility:
        stmt = stmt.where(Photo.visibility == PhotoVisibility(visibility))
    if approval_status:
        stmt = stmt.where(Photo.approval_status == ApprovalStatus(approval_status))
    if range_start:
        stmt = stmt.where(Photo.captured_at_utc >= range_start)
    if range_end:
        stmt = stmt.where(Photo.captured_at_utc < range_end)
    search_scores: dict[int, float] = {}
    normalized_semantic_query = (semantic_query or "").strip()
    normalized_ai_keyword = (ai_keyword or "").strip()
    normalized_confidence = (confidence or "").strip().lower()
    cache_key = _portal_photos_cache_key(
        current_user=current_user,
        semantic_query=normalized_semantic_query,
        ai_keyword=normalized_ai_keyword,
        report_id=report_id or "",
        project_id=project_id or "",
        employee_id=employee_id or "",
        start_date=start_date or "",
        end_date=end_date or "",
        visibility=visibility or "",
        approval_status=approval_status or "",
        include_rejected_ai=include_rejected_ai,
        include_deleted=show_deleted,
        recycle_bin=recycle_bin,
        confidence=normalized_confidence,
        view=view,
    )
    sql_paginated = False
    cached_photo_listing = PORTAL_PHOTOS_CACHE.get(cache_key)
    if cached_photo_listing is not None:
        cached_photo_ids = [int(photo_id) for photo_id in cached_photo_listing.get("photo_ids", [])]
        photos_by_id = {
            photo.id: photo
            for photo in db.scalars(
                apply_photo_scope(
                    db,
                    select(Photo).where(Photo.id.in_(cached_photo_ids)),
                    current_user,
                    photo_type=PhotoType.project,
                    include_deleted=show_deleted,
                )
            )
        } if cached_photo_ids else {}
        photos = [photos_by_id[photo_id] for photo_id in cached_photo_ids if photo_id in photos_by_id]
        search_scores = {
            int(photo_id): float(score)
            for photo_id, score in (cached_photo_listing.get("search_scores") or {}).items()
        }
    elif not normalized_ai_keyword and not normalized_confidence and not normalized_semantic_query:
        # Fast path for the common case with no Python-side filters: count and
        # page in SQL instead of materializing every matching photo in memory.
        sql_paginated = True
        total_filtered_photo_count = _count_from_statement(db, stmt)
        total_pages = max(1, (total_filtered_photo_count + per_page - 1) // per_page)
        if focus_photo_id:
            focus_photo = db.get(Photo, focus_photo_id)
            if focus_photo is not None and focus_photo.captured_at_utc is not None:
                focus_rank = _count_from_statement(
                    db,
                    stmt.where(
                        or_(
                            Photo.captured_at_utc > focus_photo.captured_at_utc,
                            and_(
                                Photo.captured_at_utc == focus_photo.captured_at_utc,
                                Photo.id > focus_photo.id,
                            ),
                        )
                    ),
                )
                page = (focus_rank // per_page) + 1
        page = min(max(page, 1), total_pages)
        page_start = (page - 1) * per_page
        page_end = min(page_start + per_page, total_filtered_photo_count)
        photos = list(
            db.scalars(
                stmt.order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
                .offset(page_start)
                .limit(per_page)
            )
        )
        page_photos = photos
    else:
        candidate_photos = list(db.scalars(stmt.order_by(Photo.captured_at_utc.desc())))
        filtered_photos = _filter_photos_by_ai_history(
            db,
            candidate_photos,
            ai_keyword=normalized_ai_keyword,
            include_rejected_ai=include_rejected_ai,
            confidence=normalized_confidence,
        )
        if normalized_semantic_query:
            candidate_photo_ids = [photo.id for photo in filtered_photos]
            try:
                ranked_results = semantic_search_photos(
                    db,
                    settings,
                    tenant_slug=current_user.company_id,
                    query=normalized_semantic_query,
                    candidate_photo_ids=candidate_photo_ids,
                    limit=50,
                )
                photos = [photo for photo, _ in ranked_results]
                search_scores = {photo.id: score for photo, score in ranked_results}
            except RuntimeError as exc:
                flash(request, "error", str(exc))
                photos = []
        else:
            photos = filtered_photos
        PORTAL_PHOTOS_CACHE.set(
            cache_key,
            {
                "photo_ids": [photo.id for photo in photos],
                "search_scores": search_scores,
            },
            ttl_seconds=settings.portal_photos_cache_ttl_seconds,
        )
    if not sql_paginated:
        total_filtered_photo_count = len(photos)
        total_pages = max(1, (total_filtered_photo_count + per_page - 1) // per_page)
        if focus_photo_id:
            for index, photo in enumerate(photos):
                if photo.id == focus_photo_id:
                    page = (index // per_page) + 1
                    break
        page = min(max(page, 1), total_pages)
        page_start = (page - 1) * per_page
        page_end = min(page_start + per_page, total_filtered_photo_count)
        page_photos = photos[page_start:page_end]

    def _photos_page_url(target_page: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(target_page)
        params["per_page"] = str(per_page)
        return f"{request.url.path}?{urlencode(params, doseq=True)}"

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total_filtered_photo_count,
        "total_pages": total_pages,
        "start": page_start + 1 if total_filtered_photo_count else 0,
        "end": page_end,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_url": _photos_page_url(page - 1) if page > 1 else "",
        "next_url": _photos_page_url(page + 1) if page < total_pages else "",
    }
    photos = page_photos
    current_language = get_language(request)
    comments_by_photo = get_comments_by_photo_ids(db, [photo.id for photo in photos])
    comment_users = _build_comment_users(db, comments_by_photo)
    photo_activity_timeline_by_photo = _build_photo_activity_timeline(
        photos,
        comments_by_photo,
        comment_users,
        language=current_language,
    )
    active_log_counts = active_ai_log_counts_by_photo(db, [photo.id for photo in photos])
    selected_report = None
    if report_id and is_manager(current_user):
        report = _company_report(db, current_user, report_id)
        if report is not None:
            selected_report = serialize_generated_report(report)
    selected_progress_report = None
    if progress_report_id and is_manager(current_user):
        progress_report = db.get(ProgressReport, progress_report_id)
        if progress_report is not None and progress_report.company_id == current_user.company_id:
            progress_photo_ids = [
                int(photo_id)
                for photo_id in (progress_report.source_photo_ids or [])
                if str(photo_id).isdigit()
            ]
            progress_photos = list(
                db.scalars(
                    apply_photo_scope(
                        db,
                        select(Photo),
                        current_user,
                        photo_type=PhotoType.project,
                    ).where(Photo.id.in_(progress_photo_ids))
                )
            ) if progress_photo_ids else []
            selected_progress_report = serialize_progress_report(
                progress_report,
                settings=settings,
                request=request,
                photos=progress_photos,
            )

    deleted_photo_count = 0
    if manage_deleted:
        deleted_photo_count = _count_from_statement(
            db,
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
                include_deleted=True,
            ).where(Photo.deleted.is_(True))
        )
    accessible_projects = _accessible_projects(db, current_user)
    storage_base_url = system_settings.get("storage_base_url")
    photo_media_by_id = {
        photo.id: serialize_photo(photo, request, settings, storage_base_url=storage_base_url)
        for photo in photos
    }
    video_asset_ids = {
        media.media_asset_id
        for media in photo_media_by_id.values()
        if media.media_kind == "video" and media.media_asset_id
    }
    media_assets_by_id = {
        asset.asset_id: asset
        for asset in db.scalars(select(MediaAsset).where(MediaAsset.asset_id.in_(video_asset_ids)))
    } if video_asset_ids else {}
    video_logs_by_asset = list_media_asset_ai_logs_by_asset_ids(db, sorted(video_asset_ids))
    video_logs_by_photo: dict[int, list[AIAnalysisLog]] = {}
    for photo in photos:
        media = photo_media_by_id.get(photo.id)
        if media is None or media.media_kind != "video" or not media.media_asset_id:
            continue
        video_logs_by_photo[photo.id] = video_logs_by_asset.get(media.media_asset_id, [])
    video_active_log_counts_by_photo = {
        photo_id: len([item for item in logs if item.status == AIAnalysisStatus.active])
        for photo_id, logs in video_logs_by_photo.items()
    }

    def _photo_card_ai_summary(photo: Photo) -> str:
        summary = localized_ai_summary(photo.tag_json, current_language) or ""
        if summary:
            return summary
        media = photo_media_by_id.get(photo.id)
        if media is None or media.media_kind != "video" or not media.media_asset_id:
            return ""
        asset = media_assets_by_id.get(media.media_asset_id)
        metadata_json = dict(asset.metadata_json) if asset is not None and isinstance(asset.metadata_json, dict) else {}
        ai_snapshot = metadata_json.get("ai_snapshot")
        if isinstance(ai_snapshot, dict):
            return localized_ai_summary(ai_snapshot, current_language) or str(ai_snapshot.get("ai_summary") or "")
        return ""

    photo_ai_confidence = {}
    for photo in photos:
        active_count = active_log_counts.get(photo.id, 0) + video_active_log_counts_by_photo.get(photo.id, 0)
        if active_count <= 0:
            continue
        confidence_level = ai_confidence_level(active_count)
        photo_ai_confidence[photo.id] = {
            "level": confidence_level,
            "label": _t(request, f"label_ai_confidence_{confidence_level}"),
            "count": active_count,
        }
    photo_ai_summaries = {photo.id: _photo_card_ai_summary(photo) for photo in photos}
    ai_logs_by_photo = list_photo_ai_logs_by_photo_ids(db, [photo.id for photo in photos])
    photo_ai_timeline_by_photo: dict[int, list[dict[str, object]]] = {}
    for photo in photos:
        timeline_items: list[dict[str, object]] = []
        for log in ai_logs_by_photo.get(photo.id, []):
            localized_payload = _localized_ai_log_payload(log, current_language)
            result_data = localized_payload.get("result_data")
            if not isinstance(result_data, dict):
                continue
            summary_text = str(result_data.get("ai_summary") or "").strip()
            if not summary_text:
                continue
            timeline_items.append(
                {
                    "created_at": log.created_at,
                    "summary": summary_text,
                    "analysis_type": localized_payload.get("analysis_type"),
                }
            )
        for log in video_logs_by_photo.get(photo.id, []):
            localized_payload = _localized_ai_log_payload(log, current_language)
            result_data = localized_payload.get("result_data")
            if not isinstance(result_data, dict):
                continue
            summary_text = str(result_data.get("ai_summary") or "").strip()
            if not summary_text:
                continue
            timeline_items.append(
                {
                    "created_at": log.created_at,
                    "summary": summary_text,
                    "analysis_type": localized_payload.get("analysis_type"),
                }
            )
        photo_ai_timeline_by_photo[photo.id] = timeline_items

    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/photos/index.html",
        page_title=_t(request, "page_photos"),
        photos=photos,
        comments_by_photo=comments_by_photo,
        comment_users=comment_users,
        photo_activity_timeline_by_photo=photo_activity_timeline_by_photo,
        selected_report=selected_report,
        selected_progress_report=selected_progress_report,
        projects=accessible_projects,
        employees=_accessible_employees(db, current_user),
        photo_media_by_id=photo_media_by_id,
        filters={
            "report_id": report_id or "",
            "progress_report_id": progress_report_id or "",
            "focus_photo_id": focus_photo_id or "",
            "semantic_query": normalized_semantic_query,
            "ai_keyword": normalized_ai_keyword,
            "project_id": project_id or "",
            "employee_id": employee_id or "",
            "start_date": start_date or "",
            "end_date": end_date or "",
            "visibility": visibility or "",
            "approval_status": approval_status or "",
            "include_rejected_ai": include_rejected_ai,
            "confidence": normalized_confidence,
            "include_deleted": show_deleted,
            "recycle_bin": recycle_bin,
            "view": view,
            "page": page,
            "per_page": per_page,
        },
        pagination=pagination,
        photo_search_scores=search_scores,
        photo_ai_confidence=photo_ai_confidence,
        photo_ai_summaries=photo_ai_summaries,
        photo_ai_timeline_by_photo=photo_ai_timeline_by_photo,
        focused_photo_id=focus_photo_id,
        deleted_photo_count=deleted_photo_count,
        show_project_assignment_hint=(
            current_user.role == UserRole.project_manager
            and not accessible_projects
            and not normalized_semantic_query
            and not normalized_ai_keyword
            and not project_id
            and not employee_id
            and not start_date
            and not end_date
            and not visibility
            and not approval_status
            and not normalized_confidence
            and not include_rejected_ai
            and not show_deleted
            and not recycle_bin
        ),
        can_manage=is_manager(current_user),
        can_reprocess=is_manager(current_user),
        can_generate_reports=is_manager(current_user),
        can_review_ai_history=is_manager(current_user),
        can_comment=is_manager(current_user),
    )


@router.get("/portal/ai-center", response_class=HTMLResponse)
def ai_center_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)
    center_payload = _ai_center_payload(db, request, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/ai_center/index.html",
        page_title=_t(request, "page_ai_center"),
        center_payload=center_payload,
        workspace_focus=_workspace_focus_payload(request, current_user, "ai_center"),
    )


@router.get("/portal/ai-center/data")
def ai_center_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)
    return _ai_center_payload(db, request, current_user)


@router.post("/portal/ai-center/tasks/{task_public_id}/retry")
def ai_center_retry_task(
    task_public_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not is_manager(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)

    job = db.scalar(
        select(TaskJob).where(
            TaskJob.public_id == task_public_id,
            TaskJob.company_id == current_user.company_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    try:
        requeue_task_job(
            db,
            app_settings=settings,
            job=job,
            actor_user_id=current_user.id,
            reason="portal_ai_center",
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect("/portal/ai-center#ai-center-list")

    db.commit()
    flash(request, "success", _t(request, "msg_ai_task_retry_requested"))
    return _redirect("/portal/ai-center#ai-center-list")


@router.get("/portal/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    tab: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    reports_payload = _report_history_payload(db, request, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/reports/index.html",
        page_title=_t(request, "page_reports"),
        reports_payload=reports_payload,
        active_report_tab="progress" if str(tab or "").strip().lower() == "progress" else "generated",
    )


@router.get("/portal/copilot", response_class=HTMLResponse)
def copilot_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    accessible_projects = _accessible_projects(db, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/copilot/index.html",
        page_title=_t(request, "page_copilot"),
        copilot_backend_options=list_copilot_backend_options(db, settings, current_user.company_id),
        copilot_conversations=list_copilot_conversations(
            db,
            company_id=current_user.company_id,
            project_ids=[project.project_id for project in accessible_projects],
        ),
        accessible_projects=accessible_projects,
        workspace_focus=_workspace_focus_payload(request, current_user, "copilot"),
        copilot_starter_questions=_copilot_starter_questions(request, current_user),
    )


@router.get("/portal/reports/data")
def reports_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    return _report_history_payload(db, request, current_user)


@router.get("/portal/reports/progress/{report_id}", response_class=HTMLResponse)
def progress_report_page(
    report_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    report = db.get(ProgressReport, report_id)
    if report is None or report.company_id != current_user.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Progress report not found")

    photo_ids = [int(photo_id) for photo_id in (report.source_photo_ids or []) if str(photo_id).isdigit()]
    photos = list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
            ).where(Photo.id.in_(photo_ids))
        )
    ) if photo_ids else []
    photo_map = {photo.id: photo for photo in photos}
    ordered_photos = [photo_map[photo_id] for photo_id in photo_ids if photo_id in photo_map]

    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/reports/progress_detail.html",
        page_title=_t(request, "page_progress_report"),
        progress_report=serialize_progress_report(
            report,
            settings=settings,
            request=request,
            photos=ordered_photos,
        ),
    )


@router.get("/portal/media/{asset_id}", response_class=HTMLResponse)
def media_asset_page(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)
    asset = _company_media_asset(db, current_user, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/media/detail.html",
        page_title=_t(request, "page_media_asset"),
        media_asset=_media_asset_payload(db, request, asset),
    )


@router.get("/portal/media/{asset_id}/stream")
def media_asset_stream(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)
    asset = _company_media_asset(db, current_user, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
    file_path = Path(asset.file_path) if asset.file_path else None
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file is missing")
    return FileResponse(path=str(file_path), media_type=asset.mime_type or "video/mp4", filename=file_path.name)


@router.get("/portal/media/{asset_id}/preview/{frame_name}")
def media_asset_preview(
    asset_id: str,
    frame_name: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.worker)
    asset = _company_media_asset(db, current_user, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media asset not found")
    preview_path = resolve_media_preview_frame_path(request.app.state.settings, asset, frame_name)
    if preview_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview frame not found")
    return FileResponse(path=str(preview_path), media_type="image/jpeg", filename=preview_path.name)


@router.get("/portal/ip-cameras/health", response_class=HTMLResponse)
def ip_cameras_health_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    health_summary = _camera_health_payload(db, request, current_user.company_id)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/ip_cameras/health.html",
        page_title=_t(request, "page_ip_camera_health"),
        camera_health_summary=health_summary,
    )


@router.get("/portal/ip-cameras", response_class=HTMLResponse)
def ip_cameras_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    cameras = list(
        db.scalars(select(IPCamera).where(IPCamera.company_id == current_user.company_id).order_by(IPCamera.id.desc()))
    )
    health_summary = _camera_health_payload(db, request, current_user.company_id)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/ip_cameras/index.html",
        page_title=_t(request, "page_ip_cameras"),
        cameras=cameras,
        camera_protocols=[CameraProtocol.rtsp.value, CameraProtocol.rtmp.value],
        camera_health_summary=health_summary,
        camera_approval_statuses=[
            CameraApprovalStatus.pending.value,
            CameraApprovalStatus.approved.value,
            CameraApprovalStatus.rejected.value,
        ],
    )


@router.get("/portal/ip-cameras/health-summary")
def camera_health_summary_portal(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    return _camera_health_payload(db, request, current_user.company_id)


@router.post("/portal/ip-cameras")
def create_ip_camera_action(
    request: Request,
    name: str = Form(...),
    stream_url: str = Form(...),
    protocol: str = Form(...),
    is_enabled: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    validate_csrf(request, csrf_token)
    try:
        resolved_protocol = CameraProtocol(protocol)
    except ValueError as exc:
        flash(request, "error", _t(request, "msg_camera_protocol_invalid"))
        return _redirect("/portal/ip-cameras")

    camera = IPCamera(
        company_id=current_user.company_id,
        name=name.strip(),
        stream_url=stream_url.strip(),
        protocol=resolved_protocol,
        is_enabled=is_enabled == "on",
        status=CameraStatus.offline,
        approval_status=CameraApprovalStatus.pending,
    )
    db.add(camera)
    db.flush()
    log_audit(
        db,
        action="ip_camera_created",
        target_type="ip_camera",
        target_id=str(camera.id),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        ip_address=request.client.host if request.client else None,
        detail_json={
            "name": camera.name,
            "protocol": camera.protocol.value,
            "approval_status": camera.approval_status.value,
        },
    )
    db.commit()
    flash(request, "success", _t(request, "msg_camera_created_pending_review", name=camera.name))
    return _redirect("/portal/ip-cameras")


@router.post("/portal/ip-cameras/{camera_id}/update")
def update_ip_camera_action(
    camera_id: int,
    request: Request,
    name: str = Form(...),
    stream_url: str = Form(...),
    protocol: str = Form(...),
    is_enabled: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    validate_csrf(request, csrf_token)
    camera = _company_camera(db, current_user, camera_id)
    if camera is None:
        flash(request, "error", _t(request, "msg_camera_missing"))
        return _redirect("/portal/ip-cameras")
    try:
        resolved_protocol = CameraProtocol(protocol)
    except ValueError:
        flash(request, "error", _t(request, "msg_camera_protocol_invalid"))
        return _redirect("/portal/ip-cameras")

    normalized_name = name.strip()
    normalized_stream_url = stream_url.strip()
    review_reset = (
        camera.stream_url.strip() != normalized_stream_url
        or camera.protocol != resolved_protocol
    )
    camera.name = normalized_name
    camera.stream_url = normalized_stream_url
    camera.protocol = resolved_protocol
    camera.is_enabled = is_enabled == "on"
    if review_reset:
        _reset_camera_review(camera)
    db.add(camera)
    log_audit(
        db,
        action="ip_camera_updated",
        target_type="ip_camera",
        target_id=str(camera.id),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        ip_address=request.client.host if request.client else None,
        detail_json={
            "protocol": camera.protocol.value,
            "is_enabled": camera.is_enabled,
            "approval_status": camera.approval_status.value,
            "review_reset": review_reset,
        },
    )
    db.commit()
    flash(
        request,
        "success",
        _t(request, "msg_camera_review_reset", name=camera.name)
        if review_reset
        else _t(request, "msg_camera_updated", name=camera.name),
    )
    return _redirect("/portal/ip-cameras")


@router.post("/portal/ip-cameras/{camera_id}/delete")
def delete_ip_camera_action(
    camera_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    validate_csrf(request, csrf_token)
    camera = _company_camera(db, current_user, camera_id)
    if camera is None:
        flash(request, "error", _t(request, "msg_camera_missing"))
        return _redirect("/portal/ip-cameras")
    name = camera.name
    db.delete(camera)
    log_audit(
        db,
        action="ip_camera_deleted",
        target_type="ip_camera",
        target_id=str(camera_id),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_camera_deleted", name=name))
    return _redirect("/portal/ip-cameras")


@router.post("/portal/ip-cameras/{camera_id}/test")
def test_ip_camera_action(
    camera_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    validate_csrf(request, csrf_token)
    camera = _company_camera(db, current_user, camera_id)
    if camera is None:
        flash(request, "error", _t(request, "msg_camera_missing"))
        return _redirect("/portal/ip-cameras")
    try:
        result = test_ip_camera_connection(db, camera, settings)
    except Exception as exc:
        log_audit(
            db,
            action="ip_camera_test_failed",
            target_type="ip_camera",
            target_id=str(camera.id),
            actor_user_id=current_user.id,
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={"error": str(exc)},
        )
        db.commit()
        flash(request, "error", _t(request, "msg_camera_test_failed", error=str(exc)))
        return _redirect("/portal/ip-cameras")

    log_audit(
        db,
        action="ip_camera_test_succeeded",
        target_type="ip_camera",
        target_id=str(camera.id),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        ip_address=request.client.host if request.client else None,
        detail_json=result,
    )
    if camera.approval_status != CameraApprovalStatus.approved:
        camera.approval_status = CameraApprovalStatus.pending
        camera.review_notes = None
        camera.reviewed_by_user_id = None
        camera.reviewed_at = None
        db.add(camera)
    db.commit()
    flash(
        request,
        "success",
        _t(request, "msg_camera_test_ready_for_review", name=camera.name)
        if camera.approval_status == CameraApprovalStatus.pending
        else _t(request, "msg_camera_test_success", name=camera.name),
    )
    return _redirect("/portal/ip-cameras")


@router.post("/portal/ip-cameras/bulk")
def bulk_ip_camera_action(
    request: Request,
    bulk_action: str = Form(...),
    camera_ids: list[int] = Form([]),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_company_admin_workspace(current_user)
    validate_csrf(request, csrf_token)
    cameras = _company_cameras(db, current_user, camera_ids)
    if not cameras:
        flash(request, "error", _t(request, "msg_camera_bulk_selection_required"))
        return _redirect("/portal/ip-cameras")

    action = (bulk_action or "").strip().lower()
    if action == "enable":
        for camera in cameras:
            camera.is_enabled = True
            db.add(camera)
        log_audit(
            db,
            action="ip_camera_bulk_enabled",
            target_type="ip_camera",
            target_id="bulk",
            actor_user_id=current_user.id,
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={"camera_ids": [camera.id for camera in cameras]},
        )
        db.commit()
        pending_count = sum(1 for camera in cameras if camera.approval_status != CameraApprovalStatus.approved)
        flash(
            request,
            "success",
            _t(request, "msg_camera_bulk_enabled_pending_review", count=len(cameras), pending=pending_count)
            if pending_count
            else _t(request, "msg_camera_bulk_enabled", count=len(cameras)),
        )
        return _redirect("/portal/ip-cameras")

    if action == "disable":
        for camera in cameras:
            camera.is_enabled = False
            db.add(camera)
        log_audit(
            db,
            action="ip_camera_bulk_disabled",
            target_type="ip_camera",
            target_id="bulk",
            actor_user_id=current_user.id,
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={"camera_ids": [camera.id for camera in cameras]},
        )
        db.commit()
        flash(request, "success", _t(request, "msg_camera_bulk_disabled", count=len(cameras)))
        return _redirect("/portal/ip-cameras")

    if action == "delete":
        camera_ids_to_delete = [camera.id for camera in cameras]
        for camera in cameras:
            db.delete(camera)
        log_audit(
            db,
            action="ip_camera_bulk_deleted",
            target_type="ip_camera",
            target_id="bulk",
            actor_user_id=current_user.id,
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={"camera_ids": camera_ids_to_delete},
        )
        db.commit()
        flash(request, "success", _t(request, "msg_camera_bulk_deleted", count=len(camera_ids_to_delete)))
        return _redirect("/portal/ip-cameras")

    if action == "test":
        success_count = 0
        failed_count = 0
        for camera in cameras:
            try:
                test_ip_camera_connection(db, camera, settings)
                if camera.approval_status != CameraApprovalStatus.approved:
                    camera.approval_status = CameraApprovalStatus.pending
                    camera.review_notes = None
                    camera.reviewed_by_user_id = None
                    camera.reviewed_at = None
                    db.add(camera)
                success_count += 1
            except Exception:
                failed_count += 1
        log_audit(
            db,
            action="ip_camera_bulk_tested",
            target_type="ip_camera",
            target_id="bulk",
            actor_user_id=current_user.id,
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            ip_address=request.client.host if request.client else None,
            detail_json={
                "camera_ids": [camera.id for camera in cameras],
                "success_count": success_count,
                "failed_count": failed_count,
            },
        )
        db.commit()
        flash(
            request,
            "success" if failed_count == 0 else "warning",
            _t(request, "msg_camera_bulk_tested", count=len(cameras), success=success_count, failed=failed_count),
        )
        return _redirect("/portal/ip-cameras")

    flash(request, "error", _t(request, "msg_camera_bulk_action_invalid"))
    return _redirect("/portal/ip-cameras")


@router.get("/portal/photos/{photo_id}/ai_logs")
def photo_ai_logs_portal(
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    photo = _accessible_portal_photo(db, current_user, photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    return _photo_ai_history_payload(db, request, photo)


@router.patch("/portal/ai_logs/{log_id}/status")
def update_ai_log_status_portal(
    log_id: str,
    payload: AIAnalysisLogStatusRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, request.headers.get("x-csrf-token") or "")
    analysis_log = db.get(AIAnalysisLog, log_id)
    if analysis_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI log not found")
    photo = _accessible_portal_photo(db, current_user, analysis_log.photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    updated_log, snapshot = set_ai_analysis_log_status(
        db,
        app_settings=settings,
        analysis_log=analysis_log,
        status=payload.status,
        actor_user_id=current_user.id,
        source="portal_ai_log_status",
    )
    db.commit()
    PORTAL_PHOTOS_CACHE.clear()
    db.refresh(updated_log)
    db.refresh(photo)
    updated_payload = _photo_ai_history_payload(db, request, photo)
    return {
        "photo_id": photo.id,
        "snapshot": _localized_ai_snapshot(snapshot, get_language(request)),
        "log": _localized_ai_log_payload(updated_log, get_language(request)),
        "active_log_count": updated_payload["active_log_count"],
        "confidence_level": updated_payload["confidence_level"],
        "confidence_label": updated_payload["confidence_label"],
        "primary_log_id": updated_payload["primary_log_id"],
    }


@router.post("/portal/ai_logs/{log_id}/promote")
def promote_ai_log_portal(
    log_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, request.headers.get("x-csrf-token") or "")
    analysis_log = db.get(AIAnalysisLog, log_id)
    if analysis_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI log not found")
    photo = _accessible_portal_photo(db, current_user, analysis_log.photo_id)
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    promoted_log, snapshot = promote_ai_analysis_log_to_primary(
        db,
        app_settings=settings,
        analysis_log=analysis_log,
        actor_user_id=current_user.id,
        source="portal_ai_log_promote",
    )
    db.commit()
    PORTAL_PHOTOS_CACHE.clear()
    db.refresh(promoted_log)
    db.refresh(photo)
    updated_payload = _photo_ai_history_payload(db, request, photo)
    return {
        "photo_id": photo.id,
        "snapshot": _localized_ai_snapshot(snapshot, get_language(request)),
        "log": _localized_ai_log_payload(promoted_log, get_language(request)),
        "active_log_count": updated_payload["active_log_count"],
        "confidence_level": updated_payload["confidence_level"],
        "confidence_label": updated_payload["confidence_label"],
        "primary_log_id": updated_payload["primary_log_id"],
    }


@router.post("/portal/ai_logs/batch/{batch_id}/rollback")
def rollback_ai_batch_portal(
    batch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, request.headers.get("x-csrf-token") or "")
    candidate_logs = list(db.scalars(select(AIAnalysisLog).where(AIAnalysisLog.batch_id == batch_id)))
    if not candidate_logs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI batch not found")
    allowed_photo_ids = [
        photo_id
        for photo_id in {log.photo_id for log in candidate_logs}
        if _accessible_portal_photo(db, current_user, photo_id) is not None
    ]
    if not allowed_photo_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI batch not found")
    rolled_back_count, affected_photo_ids = rollback_ai_analysis_batch(
        db,
        app_settings=settings,
        batch_id=batch_id,
        actor_user_id=current_user.id,
        source="portal_ai_batch_rollback",
        allowed_photo_ids=allowed_photo_ids,
    )
    db.commit()
    return {
        "batch_id": batch_id,
        "rolled_back_count": rolled_back_count,
        "affected_photo_ids": affected_photo_ids,
    }


@router.get("/portal/ai-backends", response_class=HTMLResponse)
def ai_backends_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    ai_max_concurrent_requests = get_ai_max_concurrent_requests(db, settings)
    configure_ai_concurrency_limit(ai_max_concurrent_requests)
    ai_enable_dynamic_fallback = get_ai_dynamic_fallback_enabled(db, settings)
    configure_ai_dynamic_fallback_enabled(ai_enable_dynamic_fallback)
    storage_monitor = _storage_monitor_payload(db, request, current_user)
    system_health = _system_health_payload(db, request, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/ai_backends/index.html",
        page_title=_t(request, "page_ai_backends"),
        tenant_backends=get_tenant_ai_backends(db, current_user.company_id),
        system_backends=get_system_ai_backends(db, settings),
        backend_types=[OLLAMA_TYPE, GEMINI_TYPE],
        ai_max_concurrent_requests=ai_max_concurrent_requests,
        ai_max_concurrent_requests_min=MIN_AI_MAX_CONCURRENT_REQUESTS,
        ai_max_concurrent_requests_max=MAX_AI_MAX_CONCURRENT_REQUESTS,
        ai_enable_dynamic_fallback=ai_enable_dynamic_fallback,
        storage_monitor=storage_monitor,
        system_health=system_health,
        workspace_focus=_workspace_focus_payload(request, current_user, "ai_backends"),
    )


@router.post("/portal/ai-backends/settings")
def update_ai_backend_settings_action(
    request: Request,
    ai_max_concurrent_requests: str = Form(...),
    ai_enable_dynamic_fallback: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    configured_limit = configure_ai_concurrency_limit(ai_max_concurrent_requests)
    dynamic_fallback_enabled = configure_ai_dynamic_fallback_enabled(ai_enable_dynamic_fallback == "on")
    update_system_settings(
        db,
        {
            "ai_max_concurrent_requests": str(configured_limit),
            "ai_enable_dynamic_fallback": "true" if dynamic_fallback_enabled else "false",
        },
    )
    log_audit(
        db,
        action="ai_concurrency_setting_updated",
        target_type="system_setting",
        target_id="ai_max_concurrent_requests",
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={
            "ai_max_concurrent_requests": configured_limit,
            "ai_enable_dynamic_fallback": dynamic_fallback_enabled,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(
        request,
        "success",
        _t(
            request,
            "msg_ai_concurrency_saved",
            value=configured_limit,
            fallback_state=_t(
                request,
                "label_enabled" if dynamic_fallback_enabled else "label_disabled",
            ),
        ),
    )
    return _redirect("/portal/ai-backends")


@router.post("/portal/ai-backends")
def create_ai_backend_action(
    request: Request,
    backend_id: str = Form(...),
    backend_type: str = Form(...),
    url: str = Form(...),
    model: str = Form(...),
    api_key: str | None = Form(None),
    weight: str = Form("1"),
    enabled: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    nodes = get_tenant_ai_backends(db, current_user.company_id)
    try:
        node = _ai_backend_form_node(
            backend_id=backend_id,
            backend_type=backend_type,
            url=url,
            model=model,
            api_key=api_key,
            weight=weight,
            enabled=enabled,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect("/portal/ai-backends")
    if _find_backend_node(nodes, node.id) is not None:
        flash(request, "error", _t(request, "msg_ai_backend_duplicate"))
        return _redirect("/portal/ai-backends")

    save_tenant_ai_backends(db, current_user.company_id, nodes + [node])
    log_audit(
        db,
        action="ai_backend_created",
        target_type="ai_backend",
        target_id=node.id,
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={"type": node.type, "url": node.url, "model": node.model, "enabled": node.enabled, "weight": node.weight},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_ai_backend_saved", backend_id=node.id))
    return _redirect("/portal/ai-backends")


@router.post("/portal/ai-backends/{backend_id}/update")
def update_ai_backend_action(
    backend_id: str,
    request: Request,
    new_backend_id: str = Form(..., alias="backend_id"),
    backend_type: str = Form(...),
    url: str = Form(...),
    model: str = Form(...),
    api_key: str | None = Form(None),
    weight: str = Form("1"),
    enabled: str | None = Form(None),
    existing_backend_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    nodes = get_tenant_ai_backends(db, current_user.company_id)
    current_backend = _find_backend_node(nodes, existing_backend_id or backend_id)
    if current_backend is None:
        flash(request, "error", _t(request, "msg_ai_backend_missing"))
        return _redirect("/portal/ai-backends")
    try:
        updated_backend = _ai_backend_form_node(
            backend_id=new_backend_id,
            backend_type=backend_type,
            url=url,
            model=model,
            api_key=api_key,
            weight=weight,
            enabled=enabled,
            existing_node=current_backend,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect("/portal/ai-backends")
    if updated_backend.id != current_backend.id and _find_backend_node(nodes, updated_backend.id) is not None:
        flash(request, "error", _t(request, "msg_ai_backend_duplicate"))
        return _redirect("/portal/ai-backends")

    updated_nodes = [updated_backend if node.id == current_backend.id else node for node in nodes]
    save_tenant_ai_backends(db, current_user.company_id, updated_nodes)
    log_audit(
        db,
        action="ai_backend_updated",
        target_type="ai_backend",
        target_id=updated_backend.id,
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={"type": updated_backend.type, "url": updated_backend.url, "model": updated_backend.model, "enabled": updated_backend.enabled, "weight": updated_backend.weight},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_ai_backend_saved", backend_id=updated_backend.id))
    return _redirect("/portal/ai-backends")


@router.post("/portal/ai-backends/{backend_id}/delete")
def delete_ai_backend_action(
    backend_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    nodes = get_tenant_ai_backends(db, current_user.company_id)
    if _find_backend_node(nodes, backend_id) is None:
        flash(request, "error", _t(request, "msg_ai_backend_missing"))
        return _redirect("/portal/ai-backends")
    updated_nodes = [node for node in nodes if node.id != backend_id]
    save_tenant_ai_backends(db, current_user.company_id, updated_nodes)
    log_audit(
        db,
        action="ai_backend_deleted",
        target_type="ai_backend",
        target_id=backend_id,
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_ai_backend_deleted", backend_id=backend_id))
    return _redirect("/portal/ai-backends")


@router.post("/portal/ai-backends/test")
def test_ai_backend_action(
    request: Request,
    backend_id: str = Form(...),
    backend_type: str = Form(...),
    url: str = Form(...),
    model: str = Form(...),
    api_key: str | None = Form(None),
    weight: str = Form("1"),
    enabled: str | None = Form(None),
    existing_backend_id: str | None = Form(None),
    custom_prompt: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    sync_ai_runtime_settings(db, settings)
    nodes = get_tenant_ai_backends(db, current_user.company_id)
    existing_backend = _find_backend_node(nodes, existing_backend_id or backend_id) if existing_backend_id or backend_id else None
    try:
        node = _ai_backend_form_node(
            backend_id=backend_id,
            backend_type=backend_type,
            url=url,
            model=model,
            api_key=api_key,
            weight=weight,
            enabled=enabled,
            existing_node=existing_backend,
        )
        result = test_ai_backend_connection(node, custom_prompt=custom_prompt)
    except (AIBackendError, ValueError) as exc:
        flash(request, "error", _t(request, "msg_ai_backend_test_failed", error=str(exc)))
        return _redirect("/portal/ai-backends")
    flash(request, "success", _t(request, "msg_ai_backend_test_success", backend_id=node.id, summary=result["ai_summary"]))
    return _redirect("/portal/ai-backends")


@router.get("/portal/invoices", response_class=HTMLResponse)
def invoices_page(
    request: Request,
    project_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    include_deleted: bool = Query(False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)

    stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.invoice,
        include_deleted=include_deleted and is_tenant_admin(current_user),
    )
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    range_start, range_end = build_date_range(start_date, end_date, timezone_name)
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)
    if employee_id:
        stmt = stmt.where(Photo.employee_id == employee_id)
    if range_start:
        stmt = stmt.where(Photo.captured_at_utc >= range_start)
    if range_end:
        stmt = stmt.where(Photo.captured_at_utc < range_end)

    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/invoices/index.html",
        page_title=_t(request, "page_invoices"),
        invoices=list(db.scalars(stmt.order_by(Photo.captured_at_utc.desc()))),
        projects=_accessible_projects(db, current_user),
        employees=_accessible_employees(db, current_user),
        filters={
            "project_id": project_id or "",
            "employee_id": employee_id or "",
            "start_date": start_date or "",
            "end_date": end_date or "",
            "include_deleted": include_deleted,
        },
    )


@router.post("/portal/photos/{photo_id}/approve")
def approve_photo_action(
    photo_id: int,
    request: Request,
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or photo.photo_type != PhotoType.project or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    approve_photo(db, photo=photo, actor_user=current_user, request=request)
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_approved"))
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/visibility")
def visibility_action(
    photo_id: int,
    request: Request,
    visibility: PhotoVisibility = Form(...),
    featured: str | None = Form(None),
    note: str | None = Form(None),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or photo.photo_type != PhotoType.project or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    update_photo_visibility(
        db,
        photo=photo,
        actor_user=current_user,
        visibility=visibility,
        featured=featured == "on",
        note=note,
        request=request,
    )
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_visibility_updated"))
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/approval")
def approval_action(
    photo_id: int,
    request: Request,
    approval_status: ApprovalStatus = Form(...),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or photo.photo_type != PhotoType.project or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    set_photo_approval_status(
        db,
        photo=photo,
        actor_user=current_user,
        approval_status=approval_status,
        request=request,
    )
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_marked", status=_t(request, f"status_{approval_status.value}")))
    return _redirect(next_url)


@router.post("/portal/photos/bulk/review")
def bulk_review_action(
    request: Request,
    photo_ids: list[int] = Form([]),
    bulk_visibility: str | None = Form(None),
    bulk_approval_status: str | None = Form(None),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)

    photos = _selected_project_photos(db, current_user, photo_ids)
    if not photos:
        flash(request, "error", _t(request, "msg_bulk_photo_selection_required"))
        return _redirect(next_url)

    resolved_visibility = None
    normalized_visibility = str(bulk_visibility or "").strip()
    if normalized_visibility:
        try:
            resolved_visibility = PhotoVisibility(normalized_visibility)
        except ValueError:
            flash(request, "error", _t(request, "msg_photo_bulk_visibility_invalid"))
            return _redirect(next_url)

    resolved_approval_status = None
    normalized_approval_status = str(bulk_approval_status or "").strip()
    if normalized_approval_status:
        try:
            resolved_approval_status = ApprovalStatus(normalized_approval_status)
        except ValueError:
            flash(request, "error", _t(request, "msg_photo_bulk_approval_invalid"))
            return _redirect(next_url)

    if resolved_visibility is None and resolved_approval_status is None:
        flash(request, "error", _t(request, "msg_photo_bulk_review_action_required"))
        return _redirect(next_url)

    for photo in photos:
        if resolved_visibility is not None:
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                visibility=resolved_visibility,
                request=request,
                action_name="photo_visibility_changed",
            )
        if resolved_approval_status is not None:
            stage_photo_approval_status(
                db,
                photo=photo,
                actor_user=current_user,
                approval_status=resolved_approval_status,
                request=request,
            )

    log_audit(
        db,
        action="bulk_photo_review_updated",
        target_type="photo_batch",
        target_id=str(len(photos)),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={
            "photo_ids": [photo.id for photo in photos],
            "visibility": resolved_visibility.value if resolved_visibility else None,
            "approval_status": resolved_approval_status.value if resolved_approval_status else None,
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_bulk_review_updated", count=len(photos)))
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/comments")
def add_comment_action(
    photo_id: int,
    request: Request,
    comment: str = Form(...),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    if not comment.strip():
        flash(request, "error", _t(request, "msg_comment_empty"))
        return _redirect(next_url)
    add_photo_comment(
        db,
        photo_id=photo.id,
        actor_user=current_user,
        comment=comment,
        project_id=photo.project_id,
        ip_address=request.client.host if request.client else None,
    )
    flash(request, "success", _t(request, "msg_comment_added"))
    return _redirect(next_url)


@router.post("/portal/photos/bulk/annotate")
def bulk_annotate_action(
    request: Request,
    photo_ids: list[int] = Form([]),
    bulk_note: str | None = Form(None),
    bulk_comment: str | None = Form(None),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client)
    validate_csrf(request, csrf_token)

    photos = _selected_project_photos(db, current_user, photo_ids)
    if not photos:
        flash(request, "error", _t(request, "msg_bulk_photo_selection_required"))
        return _redirect(next_url)

    normalized_note = (bulk_note or "").strip()
    normalized_comment = (bulk_comment or "").strip()
    if not normalized_note and not normalized_comment:
        flash(request, "error", _t(request, "msg_photo_bulk_annotation_required"))
        return _redirect(next_url)

    updated_count = 0
    comment_count = 0
    for photo in photos:
        if normalized_note:
            stage_photo_display_update(
                db,
                photo=photo,
                actor_user=current_user,
                note=normalized_note,
                request=request,
                action_name="photo_note_updated",
            )
            updated_count += 1
        if normalized_comment:
            stage_photo_comment(
                db,
                photo_id=photo.id,
                actor_user=current_user,
                comment=normalized_comment,
                project_id=photo.project_id,
                ip_address=request.client.host if request.client else None,
            )
            comment_count += 1

    log_audit(
        db,
        action="bulk_photo_annotations_updated",
        target_type="photo_batch",
        target_id=str(len(photos)),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={
            "photo_ids": [photo.id for photo in photos],
            "note_updated": bool(normalized_note),
            "comment_added": bool(normalized_comment),
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    PORTAL_PHOTOS_CACHE.clear()
    flash(
        request,
        "success",
        _t(
            request,
            "msg_photo_bulk_annotation_updated",
            count=len(photos),
            comments=comment_count,
        ),
    )
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/reprocess")
def reprocess_photo_action(
    photo_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    prompt: str | None = Form(None),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or photo.photo_type != PhotoType.project or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    try:
        queued_ids = queue_photo_reprocess(
            db,
            photos=[photo],
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=request.app.state.settings,
            actor_user_id=current_user.id,
            source="portal_single",
            custom_prompt=prompt,
            ip_address=request.client.host if request.client else None,
        )
    except RuntimeError as exc:
        flash(request, "error", str(exc))
        return _redirect(next_url)
    db.commit()
    flash(request, "success", _t(request, "msg_photo_reprocess_requested", count=len(queued_ids)))
    return _redirect(next_url)


@router.post("/portal/photos/reprocess")
def reprocess_photos_action(
    request: Request,
    background_tasks: BackgroundTasks,
    photo_ids: list[int] = Form([]),
    prompt: str | None = Form(None),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    requested_ids = sorted({photo_id for photo_id in photo_ids})
    if not requested_ids:
        flash(request, "error", _t(request, "msg_bulk_reprocess_selection_required"))
        return _redirect(next_url)
    photos = list(
        db.scalars(
            apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(Photo.id.in_(requested_ids))
        )
    )
    if not photos:
        flash(request, "error", _t(request, "msg_bulk_reprocess_selection_required"))
        return _redirect(next_url)
    try:
        queued_ids = queue_photo_reprocess(
            db,
            photos=photos,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=request.app.state.settings,
            actor_user_id=current_user.id,
            source="portal_bulk",
            custom_prompt=prompt,
            ip_address=request.client.host if request.client else None,
        )
    except RuntimeError as exc:
        flash(request, "error", str(exc))
        return _redirect(next_url)
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
            "custom_prompt_supplied": normalize_operator_prompt(prompt) is not None,
            "source": "portal_bulk",
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_photo_reprocess_requested", count=len(queued_ids)))
    return _redirect(next_url)


@router.post("/portal/photos/bulk/delete")
def bulk_delete_photos_action(
    request: Request,
    photo_ids: list[int] = Form([]),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)

    photos = [
        photo
        for photo in _selected_project_photos(db, current_user, photo_ids, include_deleted=True)
        if not photo.deleted
    ]
    if not photos:
        flash(request, "error", _t(request, "msg_bulk_photo_selection_required"))
        return _redirect(next_url)

    for photo in photos:
        stage_photo_soft_delete(
            db,
            photo=photo,
            actor_user=current_user,
            request=request,
            detail={"source": "portal_bulk"},
        )

    log_audit(
        db,
        action="bulk_photo_deleted",
        target_type="photo_batch",
        target_id=str(len(photos)),
        actor_user_id=current_user.id,
        tenant_id=current_user.company_id,
        company_id=current_user.company_id,
        detail_json={"photo_ids": [photo.id for photo in photos], "source": "portal_bulk"},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_bulk_deleted", count=len(photos)))
    return _redirect(next_url)


@router.post("/portal/reports/generate")
def generate_report_action(
    request: Request,
    background_tasks: BackgroundTasks,
    photo_ids: list[int] = Form([]),
    prompt: str = Form(...),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)

    requested_ids = sorted({photo_id for photo_id in photo_ids})
    if not requested_ids:
        flash(request, "error", _t(request, "msg_bulk_report_selection_required"))
        return _redirect(next_url)
    if not prompt.strip():
        flash(request, "error", _t(request, "msg_report_prompt_required"))
        return _redirect(next_url)

    accessible_photos = list(
        db.scalars(
            apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(Photo.id.in_(requested_ids))
        )
    )
    photo_map = {photo.id: photo for photo in accessible_photos}
    photos = [photo_map[photo_id] for photo_id in requested_ids if photo_id in photo_map]
    if not photos:
        flash(request, "error", _t(request, "msg_bulk_report_selection_required"))
        return _redirect(next_url)

    try:
        report = queue_report_generation(
            db,
            photos=photos,
            prompt=prompt,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=request.app.state.settings,
            actor_user_id=current_user.id,
            company_id=current_user.company_id,
            tenant_id=current_user.company_id,
            source="portal_reports",
            ip_address=request.client.host if request.client else None,
        )
    except (ValueError, RuntimeError) as exc:
        flash(request, "error", str(exc))
        return _redirect(next_url)
    db.commit()
    flash(request, "success", _t(request, "msg_report_generation_requested", count=len(photos)))
    flash(request, "info", _t(request, "msg_report_generation_followup"))
    return _redirect(_append_query_param(next_url, "report_id", report.public_id))


@router.post("/portal/reports/compare-progress")
def compare_progress_report_action(
    request: Request,
    background_tasks: BackgroundTasks,
    photo_ids: list[int] = Form([]),
    prompt: str = Form(""),
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)

    requested_ids = sorted({photo_id for photo_id in photo_ids})
    if len(requested_ids) < 2:
        flash(request, "error", _t(request, "msg_progress_report_selection_required"))
        return _redirect(next_url)

    accessible_photos = list(
        db.scalars(
            apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(Photo.id.in_(requested_ids))
        )
    )
    photo_map = {photo.id: photo for photo in accessible_photos}
    photos = [photo_map[photo_id] for photo_id in requested_ids if photo_id in photo_map]
    if len(photos) < 2:
        flash(request, "error", _t(request, "msg_progress_report_selection_required"))
        return _redirect(next_url)

    try:
        report = queue_progress_report_generation(
            db,
            photos=photos,
            custom_prompt=prompt,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=request.app.state.settings,
            actor_user_id=current_user.id,
            company_id=current_user.company_id,
            tenant_id=current_user.company_id,
            source="portal_progress_compare",
            ip_address=request.client.host if request.client else None,
        )
    except (ValueError, RuntimeError) as exc:
        flash(request, "error", str(exc))
        return _redirect(next_url)

    db.commit()
    flash(request, "success", _t(request, "msg_progress_report_requested", count=len(photos)))
    flash(request, "info", _t(request, "msg_progress_report_followup"))
    return _redirect(f"/portal/reports/progress/{report.id}")


@router.post("/portal/reports/{report_public_id}/retry")
def retry_report_action(
    report_public_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    report = _company_report(db, current_user, report_public_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    try:
        retry_report_generation(
            db,
            report=report,
            schedule_task=background_tasks.add_task,
            session_maker=request.app.state.session_maker,
            app_settings=request.app.state.settings,
            actor_user_id=current_user.id,
            source="portal_report_retry",
            ip_address=request.client.host if request.client else None,
        )
    except RuntimeError as exc:
        flash(request, "error", str(exc))
        return _redirect(next_url)
    db.commit()
    flash(request, "success", _t(request, "msg_report_retry_requested"))
    return _redirect(_append_query_param(next_url, "report_id", report.public_id))


@router.get("/portal/reports/{report_public_id}/download")
def download_report_action(
    report_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    report = _company_report(db, current_user, report_public_id)
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


@router.post("/portal/photos/{photo_id}/delete")
def delete_photo_action(
    photo_id: int,
    request: Request,
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    soft_delete_photo(db, photo=photo, actor_user=current_user, request=request, detail={"source": "portal"})
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_deleted"))
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/restore")
def restore_photo_action(
    photo_id: int,
    request: Request,
    next_url: str = Form("/portal/photos"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    restore_photo(db, photo=photo, actor_user=current_user, request=request)
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_restored"))
    return _redirect(next_url)


@router.post("/portal/photos/{photo_id}/destroy")
def destroy_photo_action(
    photo_id: int,
    request: Request,
    next_url: str = Form("/portal/photos?recycle_bin=true"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    photo = _company_photo(db, current_user, photo_id)
    if photo is None or not can_access_photo(db, current_user, photo):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    hard_delete_photo(
        db,
        app_settings=settings,
        photo=photo,
        actor_user=current_user,
        request=request,
        detail={"source": "portal_recycle_bin"},
    )
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_photo_permanently_deleted"))
    return _redirect(next_url)


@router.post("/portal/photos/recycle-bin/empty")
def empty_recycle_bin_action(
    request: Request,
    next_url: str = Form("/portal/photos?recycle_bin=true"),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    deleted_photos = list(
        db.scalars(
            apply_photo_scope(
                db,
                select(Photo),
                current_user,
                photo_type=PhotoType.project,
                include_deleted=True,
            )
            .where(Photo.deleted.is_(True))
            .order_by(Photo.soft_deleted_at.asc(), Photo.id.asc())
        )
    )
    purged_count = 0
    for photo in deleted_photos:
        hard_delete_photo(
            db,
            app_settings=settings,
            photo=photo,
            actor_user=current_user,
            request=request,
            detail={"source": "portal_recycle_bin_empty"},
        )
        purged_count += 1
    PORTAL_PHOTOS_CACHE.clear()
    flash(request, "success", _t(request, "msg_recycle_bin_emptied", count=purged_count))
    return _redirect(next_url)


@router.get("/portal/projects", response_class=HTMLResponse)
def projects_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/projects/index.html",
        page_title=_t(request, "page_projects"),
        projects=_accessible_projects(db, current_user),
        can_create_projects=_can_create_projects(current_user),
        can_manage_project_settings=_can_manage_project_settings(current_user),
    )


@router.post("/portal/projects")
def create_project_action(
    request: Request,
    project_id: str = Form(...),
    project_name: str = Form(...),
    client_name: str = Form(...),
    location: str = Form(...),
    image_video_ai_prompt: str | None = Form(None),
    billing_receipt_ai_prompt: str | None = Form(None),
    status_value: ProjectStatus = Form(ProjectStatus.active),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    try:
        normalized_project_id = scoped_identifier_for_company(
            db,
            company_id=current_user.company_id,
            raw_value=project_id,
            kind="project",
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect("/portal/projects")
    if _company_project(db, current_user, normalized_project_id):
        flash(request, "error", _t(request, "msg_project_exists_scoped", project_id=normalized_project_id))
        return _redirect("/portal/projects")
    db.add(
        Project(
            tenant_id=current_user.company_id,
            company_id=current_user.company_id,
            project_id=normalized_project_id,
            project_name=project_name,
            client_name=client_name,
            location=location,
            image_video_ai_prompt=(image_video_ai_prompt or "").strip() or None,
            billing_receipt_ai_prompt=(billing_receipt_ai_prompt or "").strip() or None,
            status=status_value,
            created_by=current_user.id,
        )
    )
    log_audit(
        db,
        action="project_created",
        target_type="project",
        target_id=normalized_project_id,
        actor_user_id=current_user.id,
        project_id=normalized_project_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_project_created_with_id", project_id=normalized_project_id))
    return _redirect("/portal/projects")


@router.post("/portal/projects/{project_id}/update")
def update_project_action(
    project_id: str,
    request: Request,
    project_name: str = Form(...),
    client_name: str = Form(...),
    location: str = Form(...),
    image_video_ai_prompt: str | None = Form(None),
    billing_receipt_ai_prompt: str | None = Form(None),
    status_value: ProjectStatus = Form(ProjectStatus.active),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    project = _company_project(db, current_user, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project.project_name = project_name
    project.client_name = client_name
    project.location = location
    project.image_video_ai_prompt = (image_video_ai_prompt or "").strip() or None
    project.billing_receipt_ai_prompt = (billing_receipt_ai_prompt or "").strip() or None
    project.status = status_value
    db.add(project)
    log_audit(
        db,
        action="project_updated",
        target_type="project",
        target_id=project.project_id,
        actor_user_id=current_user.id,
        project_id=project.project_id,
        ip_address=request.client.host if request.client else None,
        company_id=current_user.company_id,
        detail_json={
            "image_video_ai_prompt_present": bool(project.image_video_ai_prompt),
            "billing_receipt_ai_prompt_present": bool(project.billing_receipt_ai_prompt),
        },
    )
    db.commit()
    flash(request, "success", _t(request, "msg_project_updated"))
    return _redirect(f"/portal/projects/{project.project_id}")


@router.get("/portal/projects/{project_id}", response_class=HTMLResponse)
def project_detail_page(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    project = _company_project(db, current_user, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    members = list(
        db.scalars(
            select(User)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id, User.company_id == current_user.company_id)
        )
    )
    recent_stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.project,
        gallery_only=is_client(current_user),
    ).where(Photo.project_id == project_id)
    recent_photos = list(db.scalars(recent_stmt.order_by(Photo.captured_at_utc.desc()).limit(12)))
    project_photo_count = _count_from_statement(db, recent_stmt)
    visible_count = db.scalar(
        select(func.count()).select_from(
            apply_photo_scope(db, select(Photo.id), current_user, photo_type=PhotoType.project, gallery_only=True)
            .where(Photo.project_id == project_id)
            .subquery()
        )
    ) or 0
    invoice_count = 0
    if not is_client(current_user):
        invoice_count = db.scalar(
            select(func.count()).select_from(
                apply_photo_scope(db, select(Photo.id), current_user, photo_type=PhotoType.invoice)
                .where(Photo.project_id == project_id)
                .subquery()
            )
        ) or 0
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/projects/detail.html",
        page_title=_t(request, "page_project", project_id=project.project_id),
        project=project,
        members=members,
        recent_photos=recent_photos,
        can_manage_project_settings=_can_manage_project_settings(current_user),
        stats={
            "project_photos": project_photo_count,
            "visible_photos": visible_count,
            "invoice_photos": invoice_count,
            "members": len(members),
        },
    )


@router.get("/portal/employees", response_class=HTMLResponse)
def employees_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    projects = _accessible_projects(db, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/users/employees.html",
        page_title=_t(request, "page_employees"),
        employees=_accessible_employees(db, current_user),
        projects=projects,
        has_accessible_projects=bool(projects),
        can_create_employees=is_tenant_admin(current_user) or current_user.role in {UserRole.project_manager, UserRole.manager},
        can_assign_projects=False,
        can_administer_employees=is_tenant_admin(current_user),
    )


@router.post("/portal/employees")
def create_employee_action(
    request: Request,
    employee_id: str = Form(...),
    name: str = Form(...),
    role_name: str = Form("worker"),
    project_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    try:
        employee = create_employee_for_user(
            db,
            user=current_user,
            employee_id=employee_id,
            name=name,
            role_name=role_name,
            project_id=project_id,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            normalized_employee_id = employee_id.strip()
            try:
                normalized_employee_id = scoped_identifier_for_company(
                    db,
                    company_id=current_user.company_id,
                    raw_value=employee_id,
                    kind="employee",
                )
            except ValueError:
                pass
            flash(request, "error", _t(request, "msg_employee_exists_scoped", employee_id=normalized_employee_id))
            return _redirect("/portal/employees")
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            flash(request, "error", str(exc.detail))
            return _redirect("/portal/employees")
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            flash(request, "error", _t(request, "msg_project_access_denied"))
            return _redirect("/portal/employees")
        raise
    log_audit(
        db,
        action="employee_created",
        target_type="employee",
        target_id=employee.employee_id,
        actor_user_id=current_user.id,
        project_id=employee.project_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_employee_created_with_id", employee_id=employee.employee_id))
    return _redirect("/portal/employees")


@router.post("/portal/employees/{employee_id}/assign-project")
def assign_employee_project_action(
    employee_id: str,
    request: Request,
    project_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    validate_csrf(request, csrf_token)
    employee = get_manageable_employee(db, current_user, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    try:
        updated_employee = assign_employee_project_for_user(
            db,
            user=current_user,
            employee=employee,
            project_id=project_id,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            flash(request, "error", _t(request, "msg_employee_project_required"))
            return _redirect("/portal/employees")
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            flash(request, "error", _t(request, "msg_project_access_denied"))
            return _redirect("/portal/employees")
        raise
    log_audit(
        db,
        action="employee_project_assigned",
        target_type="employee",
        target_id=updated_employee.employee_id,
        actor_user_id=current_user.id,
        project_id=updated_employee.project_id,
        detail_json={"project_id": updated_employee.project_id},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_project_access_updated"))
    return _redirect("/portal/employees")


@router.post("/portal/employees/{employee_id}/toggle-active")
def toggle_employee_action(
    employee_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    employee = _company_employee(db, current_user, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    employee.active = not employee.active
    db.add(employee)
    log_audit(
        db,
        action="employee_status_changed",
        target_type="employee",
        target_id=employee_id,
        actor_user_id=current_user.id,
        detail_json={"active": employee.active},
        project_id=employee.project_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_employee_status_updated"))
    return _redirect("/portal/employees")


@router.post("/portal/employees/{employee_id}/regenerate-key")
def regenerate_employee_key(
    employee_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    employee = _company_employee(db, current_user, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    employee.api_key = generate_api_key()
    db.add(employee)
    log_audit(
        db,
        action="employee_api_key_rotated",
        target_type="employee",
        target_id=employee_id,
        actor_user_id=current_user.id,
        project_id=employee.project_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_employee_key_rotated"))
    return _redirect("/portal/employees")


@router.post("/portal/employees/{employee_id}/update-key")
def update_employee_key(
    employee_id: str,
    request: Request,
    api_key: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    employee = _company_employee(db, current_user, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    try:
        normalized_key = normalize_api_key(api_key)
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect("/portal/employees")
    existing = db.scalar(
        select(Employee).where(Employee.api_key == normalized_key, Employee.employee_id != employee.employee_id)
    )
    if existing is not None:
        flash(request, "error", _t(request, "msg_employee_key_exists"))
        return _redirect("/portal/employees")
    employee.api_key = normalized_key
    db.add(employee)
    log_audit(
        db,
        action="employee_api_key_updated",
        target_type="employee",
        target_id=employee_id,
        actor_user_id=current_user.id,
        detail_json={"mode": "manual", "length": len(normalized_key)},
        project_id=employee.project_id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_employee_key_updated"))
    return _redirect("/portal/employees")


@router.get("/portal/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    users = list(
        db.scalars(select(User).where(User.company_id == current_user.company_id).order_by(User.created_at.desc()))
    )
    available_roles = _creatable_user_roles(current_user)
    projects = _accessible_projects(db, current_user)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/users/index.html",
        page_title=_t(request, "page_users"),
        users=users,
        user_projects={user.id: _selected_projects_for_user(db, user.id) for user in users},
        projects=projects,
        available_roles=available_roles,
        can_create_owner_role=UserRole.owner in available_roles,
        can_create_platform_role=UserRole.super_admin in available_roles,
        project_scoped_role_values={role.value for role in available_roles if _role_requires_project_scope(role)},
        has_accessible_projects=bool(projects),
        employees=list(
            db.scalars(
                select(Employee).where(Employee.company_id == current_user.company_id).order_by(Employee.employee_id)
            )
        ),
    )


@router.post("/portal/users")
def create_user_action(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    role: UserRole = Form(...),
    password: str = Form(...),
    email: str | None = Form(None),
    employee_id: str | None = Form(None),
    project_ids: list[str] = Form([]),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    if role not in _creatable_user_roles(current_user):
        flash(request, "error", _t(request, "msg_role_not_allowed"))
        return _redirect("/portal/users")
    if db.scalar(select(User).where(User.username == username.strip().lower())):
        flash(request, "error", _t(request, "msg_username_exists"))
        return _redirect("/portal/users")
    if email and db.scalar(select(User).where(User.email == email)):
        flash(request, "error", _t(request, "msg_email_exists"))
        return _redirect("/portal/users")
    if employee_id and _company_employee(db, current_user, employee_id) is None:
        flash(request, "error", _t(request, "msg_linked_employee_missing"))
        return _redirect("/portal/users")
    normalized_project_ids = [item.strip() for item in project_ids if item and item.strip()]
    if _role_requires_project_scope(role) and not normalized_project_ids:
        flash(request, "error", _t(request, "msg_user_project_access_required"))
        return _redirect("/portal/users")
    user = User(
        company_id=current_user.company_id,
        username=username.strip().lower(),
        password_hash=hash_password(password),
        role=role,
        display_name=display_name,
        email=email or None,
        employee_id=employee_id or None,
        active=True,
    )
    db.add(user)
    db.flush()
    db.add(
        Membership(
            tenant_id=current_user.company_id,
            user_id=user.id,
            role=role.value,
            status=MembershipStatus.active,
        )
    )
    for item in normalized_project_ids:
        if not can_access_project(db, current_user, item):
            continue
        db.add(ProjectMember(project_id=item, user_id=user.id, role_in_project=role.value))
    log_audit(
        db,
        action="user_created",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        detail_json={"role": role.value, "username": username},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_user_created"))
    return _redirect("/portal/users")


@router.post("/portal/users/{user_id}/toggle-active")
def toggle_user_action(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    user = _company_user(db, current_user, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.active = not user.active
    db.add(user)
    log_audit(
        db,
        action="user_status_changed",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        detail_json={"active": user.active},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_user_status_updated"))
    return _redirect("/portal/users")


@router.post("/portal/users/{user_id}/reset-password")
def reset_password_action(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    user = _company_user(db, current_user, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if len(new_password) < 10:
        flash(request, "error", _t(request, "msg_password_min_length"))
        return _redirect("/portal/users")
    user.password_hash = hash_password(new_password)
    db.add(user)
    log_audit(
        db,
        action="user_password_reset",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_user_password_updated", username=user.username))
    return _redirect("/portal/users")


@router.post("/portal/users/{user_id}/update-role")
def update_user_role_action(
    user_id: int,
    request: Request,
    role: UserRole = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    user = _company_user(db, current_user, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_user.id:
        flash(request, "error", _t(request, "msg_user_cannot_modify_self"))
        return _redirect("/portal/users")
    if role not in _creatable_user_roles(current_user):
        flash(request, "error", _t(request, "msg_role_not_allowed"))
        return _redirect("/portal/users")
    selected_project_ids = _selected_projects_for_user(db, user.id)
    if _role_requires_project_scope(role) and not selected_project_ids:
        flash(request, "error", _t(request, "msg_user_project_access_required"))
        return _redirect("/portal/users")
    previous_role = user.role.value
    user.role = role
    db.add(user)
    membership = db.scalar(
        select(Membership).where(Membership.tenant_id == current_user.company_id, Membership.user_id == user.id)
    )
    if membership is None:
        db.add(
            Membership(
                tenant_id=current_user.company_id,
                user_id=user.id,
                role=role.value,
                status=MembershipStatus.active,
            )
        )
    else:
        membership.role = role.value
        membership.status = MembershipStatus.active
        db.add(membership)
    db.query(ProjectMember).filter(ProjectMember.user_id == user.id).update({"role_in_project": role.value})
    log_audit(
        db,
        action="user_role_updated",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        detail_json={"previous_role": previous_role, "new_role": role.value},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_user_role_updated", username=user.username))
    return _redirect("/portal/users")


@router.post("/portal/users/{user_id}/sync-projects")
def sync_user_projects_action(
    user_id: int,
    request: Request,
    project_ids: list[str] = Form([]),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    user = _company_user(db, current_user, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    normalized_project_ids = [item.strip() for item in project_ids if item and item.strip()]
    if _role_requires_project_scope(user.role) and not normalized_project_ids:
        flash(request, "error", _t(request, "msg_user_project_access_required"))
        return _redirect("/portal/users")
    db.query(ProjectMember).filter(ProjectMember.user_id == user_id).delete()
    for item in normalized_project_ids:
        if not can_access_project(db, current_user, item):
            continue
        db.add(ProjectMember(project_id=item, user_id=user_id, role_in_project=user.role.value))
    log_audit(
        db,
        action="user_projects_synced",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        detail_json={"project_ids": normalized_project_ids},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_project_access_updated"))
    return _redirect("/portal/users")


@router.post("/portal/users/{user_id}/delete")
def delete_user_action(
    user_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    user = _company_user(db, current_user, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_user.id:
        flash(request, "error", _t(request, "msg_user_cannot_delete_self"))
        return _redirect("/portal/users")
    previous_username = user.username
    db.query(ProjectMember).filter(ProjectMember.user_id == user.id).delete()
    db.query(Membership).filter(Membership.tenant_id == current_user.company_id, Membership.user_id == user.id).delete()
    user.active = False
    user.employee_id = None
    user.email = None
    user.username = f"deleted_user_{user.id}"
    user.display_name = f"Deleted User {user.id}"
    user.password_hash = hash_password(generate_api_key())
    db.add(user)
    log_audit(
        db,
        action="user_deleted",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=current_user.id,
        detail_json={"previous_username": previous_username},
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_user_deleted", username=previous_username))
    return _redirect("/portal/users")


@router.get("/portal/projects/{project_id}/export")
def export_project_photos(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    import io
    import zipfile

    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager)
    if not can_access_project(db, current_user, project_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    photos = list(
        db.scalars(
            apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project).where(Photo.project_id == project_id)
        )
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for photo in photos:
            file_path = Path(photo.file_path)
            if file_path.exists():
                archive.write(file_path, arcname=f"{photo.employee_id}/{file_path.name}")
    archive_buffer.seek(0)
    return StreamingResponse(
        archive_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}_photo_pack.zip"'},
    )


@router.get("/portal/map", response_class=HTMLResponse)
def map_page(
    request: Request,
    project_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client, UserRole.worker)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/map/index.html",
        page_title=_t(request, "page_map"),
        projects=_accessible_projects(db, current_user),
        filters={"project_id": project_id or "", "start_date": start_date or "", "end_date": end_date or ""},
    )


@router.get("/portal/map/photos")
def map_photos_api(
    request: Request,
    project_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client, UserRole.worker)
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    stmt = apply_photo_scope(
        db,
        select(Photo),
        current_user,
        photo_type=PhotoType.project,
        gallery_only=is_client(current_user),
    ).where(Photo.gps_lat.is_not(None), Photo.gps_lon.is_not(None))
    if project_id:
        if not is_tenant_admin(current_user) and project_id and not can_access_project(db, current_user, project_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        stmt = stmt.where(Photo.project_id == project_id)
    range_start, range_end = build_date_range(start_date, end_date, timezone_name)
    if range_start:
        stmt = stmt.where(Photo.captured_at_utc >= range_start)
    if range_end:
        stmt = stmt.where(Photo.captured_at_utc < range_end)
    photos = list(db.scalars(stmt.order_by(Photo.captured_at_utc.desc())))
    storage_base_url = get_system_settings(db, settings).get("storage_base_url")
    return [
        {
            "photo_id": photo.id,
            "project_id": photo.project_id,
            "original_file_name": photo.original_file_name,
            "image_url": serialized.image_url,
            "thumb_url": serialized.thumb_url,
            "preview_url": serialized.thumb_url or serialized.image_url,
            "media_kind": serialized.media_kind,
            "duration_seconds": serialized.duration_seconds or 0.0,
            "gps_lat": photo.gps_lat,
            "gps_lon": photo.gps_lon,
            "captured_at": to_utc_iso(photo.captured_at_utc),
        }
        for photo in photos
        for serialized in [serialize_photo(photo, request, settings, storage_base_url=storage_base_url)]
    ]


@router.get("/portal/stats/overview")
def overview_stats_api(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    overview = _overview_stats(db, current_user, timezone_name)
    return {
        "total_photos": overview["total_photos"],
        "photos_today": overview["photos_today"],
        "active_projects": overview["active_projects"],
        "active_workers": overview["active_workers"],
    }


@router.get("/portal/stats/projects")
def project_stats_api(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    return _project_stats(db, current_user)


@router.get("/portal/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    plans = list(db.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.monthly_price_cents.asc())))
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/billing/index.html",
        page_title=_t(request, "page_billing"),
        billing_summary=_billing_summary(db, current_user, timezone_name),
        plans=plans,
        can_manage=can_manage_companies(current_user),
        workspace_focus=_workspace_focus_payload(request, current_user, "billing"),
    )


@router.post("/portal/billing/subscription")
def update_billing_action(
    request: Request,
    plan_id: PlanCode = Form(...),
    billing_status: SubscriptionStatus = Form(...),
    notes: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    subscription = ensure_subscription(
        db,
        company_id=current_user.company_id,
        plan_id=plan_id,
        status=billing_status,
    )
    subscription.plan_id = plan_id
    subscription.status = billing_status
    subscription.provider = "manual"
    subscription.notes = (notes or "").strip() or None
    db.add(subscription)
    log_audit(
        db,
        action="subscription_updated",
        target_type="subscription",
        target_id=str(subscription.id),
        actor_user_id=current_user.id,
        detail_json={"plan_id": subscription.plan_id.value, "status": subscription.status.value},
        ip_address=request.client.host if request.client else None,
        company_id=current_user.company_id,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_subscription_updated"))
    return _redirect("/portal/billing")


@router.get("/portal/platform", response_class=HTMLResponse)
def platform_admin_page(
    request: Request,
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    tenant_metrics = _platform_company_metrics(db, request)
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.created_at.desc())))
    applications = list(
        db.scalars(
            select(CompanyApplication)
            .order_by(
                CompanyApplication.status.asc(),
                CompanyApplication.created_at.desc(),
            )
        )
    )
    companies = {company.company_id: company for company in db.scalars(select(Company)).all()}
    subscriptions = {
        subscription.company_id: subscription for subscription in db.scalars(select(Subscription)).all()
    }
    plans = {plan.plan_id: plan for plan in db.scalars(select(Plan)).all()}
    owners = {
        user.company_id: user
        for user in db.scalars(
            select(User).where(User.role.in_([UserRole.owner, UserRole.admin])).order_by(User.created_at.asc())
        )
    }
    pending_applications = [item for item in applications if item.status == CompanyApplicationStatus.pending]
    reviewed_application_count = max(0, len(applications) - len(pending_applications))
    pending_camera_reviews = _platform_camera_review_rows(db, request)
    tenant_rows = []
    for tenant in tenants:
        subscription = subscriptions.get(tenant.slug)
        company = companies.get(tenant.slug)
        plan = plans.get(subscription.plan_id) if subscription else None
        owner = owners.get(tenant.slug)
        metrics = tenant_metrics.get(tenant.slug, {})
        tenant_rows.append(
            {
                "slug": tenant.slug,
                "name": tenant.name,
                "company_code": company.company_code if company else "",
                "status": tenant.status.value,
                "company_active": company.active if company else False,
                "plan_name": plan.display_name if plan else "-",
                "billing_status": subscription.status.value if subscription else "-",
                "owner_email": owner.email if owner else "-",
                "owner_username": owner.username if owner else "-",
                "employee_count": metrics.get("employee_count", 0),
                "photo_count": metrics.get("photo_count", 0),
                "video_count": metrics.get("video_count", 0),
                "activity": metrics.get(
                    "activity",
                    _platform_activity_profile(request, uploads_last_14d=0, active_uploaders_last_14d=0),
                ),
                "created_at": company.created_at if company is not None else tenant.created_at,
                "can_delete": tenant.slug != "default" and tenant.status == TenantStatus.suspended,
            }
        )
    normalized_query = (q or "").strip()
    filtered_tenants = [
        row for row in tenant_rows if _platform_tenant_row_matches(row, normalized_query)
    ]
    page_size = 100
    total_tenants = len(filtered_tenants)
    total_pages = max(1, (total_tenants + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start_index = (current_page - 1) * page_size
    end_index = start_index + page_size
    paged_tenants = filtered_tenants[start_index:end_index]
    tenant_range_start = start_index + 1 if total_tenants else 0
    tenant_range_end = min(end_index, total_tenants) if total_tenants else 0
    tenant_prev_url = (
        _append_query_param(_append_query_param(str(request.url), "q", normalized_query), "page", str(current_page - 1))
        if current_page > 1
        else None
    )
    tenant_next_url = (
        _append_query_param(_append_query_param(str(request.url), "q", normalized_query), "page", str(current_page + 1))
        if current_page < total_pages
        else None
    )
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/platform/index.html",
        page_title=_t(request, "page_platform"),
        applications=pending_applications,
        reviewed_application_count=reviewed_application_count,
        pending_camera_reviews=pending_camera_reviews,
        tenants=paged_tenants,
        tenant_query=normalized_query,
        tenant_page=current_page,
        tenant_page_size=page_size,
        tenant_total_count=total_tenants,
        tenant_total_pages=total_pages,
        tenant_range_start=tenant_range_start,
        tenant_range_end=tenant_range_end,
        tenant_prev_url=tenant_prev_url,
        tenant_next_url=tenant_next_url,
        platform_current_url=str(request.url),
        plans=list(plans.values()),
    )


@router.post("/portal/platform/companies")
def create_company_workspace_action(
    request: Request,
    company_name: str = Form(...),
    display_name: str = Form(...),
    email: str = Form(...),
    company_id: str | None = Form(None),
    company_code: str | None = Form(None),
    username: str | None = Form(None),
    password: str | None = Form(None),
    plan_id: PlanCode = Form(PlanCode.free),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    try:
        company, _, owner_user, generated_password = provision_company_workspace(
            db,
            company_name=company_name,
            display_name=display_name,
            email=email,
            password=password,
            company_id=company_id,
            username=username,
            plan_id=plan_id,
            company_code=company_code,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect(_platform_redirect_target(next_url))
    log_audit(
        db,
        action="platform_company_created",
        target_type="company",
        target_id=company.company_id,
        actor_user_id=current_user.id,
        detail_json={
            "company_code": company.company_code,
            "owner_username": owner_user.username,
            "plan_id": plan_id.value,
        },
        ip_address=request.client.host if request.client else None,
        company_id=company.company_id,
    )
    db.commit()
    flash(
        request,
        "success",
        _t(
            request,
            "msg_platform_company_created",
            company=company.company_name,
            company_code=company.company_code,
            username=owner_user.username,
            password=generated_password,
        ),
    )
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/applications/{application_id}/approve")
def approve_company_application_action(
    application_id: int,
    request: Request,
    plan_id: PlanCode = Form(PlanCode.free),
    review_notes: str | None = Form(None),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    application = db.get(CompanyApplication, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status != CompanyApplicationStatus.pending:
        flash(request, "error", _t(request, "msg_company_application_not_pending"))
        return _redirect(_platform_redirect_target(next_url))
    tenant_settings = {
        "company_intro": application.company_intro,
        "contact_phone": application.contact_phone,
        "contact_title": application.contact_title,
        "website_url": application.website_url,
        "application_public_id": application.public_id,
    }
    try:
        company, _, owner_user, _ = provision_company_workspace(
            db,
            company_name=application.company_name,
            display_name=application.contact_name,
            email=application.contact_email,
            password_hash=application.password_hash,
            company_id=application.requested_company_id,
            username=application.requested_username,
            plan_id=plan_id,
            company_code=application.requested_company_code,
            tenant_settings=tenant_settings,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect(_platform_redirect_target(next_url))
    application.status = CompanyApplicationStatus.approved
    application.review_notes = (review_notes or "").strip() or None
    application.reviewed_by_user_id = current_user.id
    application.reviewed_at = utc_now()
    application.approved_company_id = company.company_id
    db.add(application)
    send_email(
        db,
        settings=settings,
        to_email=application.contact_email,
        subject="KK Field Logger workspace approved",
        html_content=(
            f"<p>Your company workspace for <strong>{company.company_name}</strong> has been approved.</p>"
            f"<p>Company code: <strong>{company.company_code}</strong></p>"
            f"<p>Admin username: <strong>{owner_user.username}</strong></p>"
            f"<p>Sign in at <a href=\"{settings.public_base_url.rstrip('/')}/login\">KK Field Logger</a>.</p>"
        ),
        action="company_application_approved_email_sent",
        company_id=company.company_id,
    )
    log_audit(
        db,
        action="company_application_approved",
        target_type="company_application",
        target_id=str(application.id),
        actor_user_id=current_user.id,
        detail_json={"company_id": company.company_id, "plan_id": plan_id.value},
        ip_address=request.client.host if request.client else None,
        company_id=company.company_id,
    )
    db.commit()
    flash(
        request,
        "success",
        _t(
            request,
            "msg_company_application_approved",
            company=company.company_name,
            username=owner_user.username,
            company_code=company.company_code,
        ),
    )
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/applications/{application_id}/reject")
def reject_company_application_action(
    application_id: int,
    request: Request,
    review_notes: str | None = Form(None),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    application = db.get(CompanyApplication, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status != CompanyApplicationStatus.pending:
        flash(request, "error", _t(request, "msg_company_application_not_pending"))
        return _redirect(_platform_redirect_target(next_url))
    application.status = CompanyApplicationStatus.rejected
    application.review_notes = (review_notes or "").strip() or None
    application.reviewed_by_user_id = current_user.id
    application.reviewed_at = utc_now()
    db.add(application)
    # No rejection email: most rejected applications are bot submissions with
    # arbitrary (possibly third-party) addresses, and emailing them burns mail
    # reputation. Only approval sends mail.
    log_audit(
        db,
        action="company_application_rejected",
        target_type="company_application",
        target_id=str(application.id),
        actor_user_id=current_user.id,
        detail_json={"contact_email": application.contact_email},
        ip_address=request.client.host if request.client else None,
        company_id=None,
    )
    db.commit()
    flash(
        request,
        "success",
        _t(request, "msg_company_application_rejected", company=application.company_name),
    )
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/ip-cameras/{camera_id}/approve")
def approve_ip_camera_for_platform_action(
    camera_id: int,
    request: Request,
    review_notes: str | None = Form(None),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    camera = db.get(IPCamera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    camera.approval_status = CameraApprovalStatus.approved
    camera.review_notes = (review_notes or "").strip() or None
    camera.reviewed_by_user_id = current_user.id
    camera.reviewed_at = utc_now()
    db.add(camera)
    log_audit(
        db,
        action="ip_camera_platform_approved",
        target_type="ip_camera",
        target_id=str(camera.id),
        actor_user_id=current_user.id,
        detail_json={"company_id": camera.company_id, "is_enabled": camera.is_enabled},
        ip_address=request.client.host if request.client else None,
        company_id=camera.company_id,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_camera_review_approved", name=camera.name))
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/ip-cameras/{camera_id}/reject")
def reject_ip_camera_for_platform_action(
    camera_id: int,
    request: Request,
    review_notes: str | None = Form(None),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_platform_control(current_user)
    validate_csrf(request, csrf_token)
    camera = db.get(IPCamera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    camera.approval_status = CameraApprovalStatus.rejected
    camera.review_notes = (review_notes or "").strip() or None
    camera.reviewed_by_user_id = current_user.id
    camera.reviewed_at = utc_now()
    camera.is_enabled = False
    db.add(camera)
    log_audit(
        db,
        action="ip_camera_platform_rejected",
        target_type="ip_camera",
        target_id=str(camera.id),
        actor_user_id=current_user.id,
        detail_json={"company_id": camera.company_id},
        ip_address=request.client.host if request.client else None,
        company_id=camera.company_id,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_camera_review_rejected", name=camera.name))
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/tenants/{tenant_slug}/status")
def update_tenant_status_action(
    tenant_slug: str,
    request: Request,
    tenant_status: str = Form(...),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    company = db.scalar(select(Company).where(Company.company_id == tenant_slug))
    if tenant is None or company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    try:
        normalized_status = TenantStatus(str(tenant_status or "").strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tenant status") from exc
    tenant.status = normalized_status
    company.active = normalized_status == TenantStatus.active
    db.add(tenant)
    db.add(company)
    log_audit(
        db,
        action="tenant_status_updated",
        target_type="tenant",
        target_id=tenant.slug,
        actor_user_id=current_user.id,
        detail_json={"status": normalized_status.value},
        ip_address=request.client.host if request.client else None,
        company_id=company.company_id,
    )
    db.commit()
    flash(
        request,
        "success",
        _t(request, "msg_tenant_status_updated", tenant=tenant.name, status=tenant.status.value),
    )
    return _redirect(_platform_redirect_target(next_url))


@router.post("/portal/platform/tenants/{tenant_slug}/delete")
def delete_tenant_action(
    tenant_slug: str,
    request: Request,
    confirm_slug: str = Form(...),
    next_url: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    if not can_manage_companies(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    validate_csrf(request, csrf_token)
    if str(confirm_slug or "").strip() != tenant_slug:
        flash(request, "error", _t(request, "msg_tenant_delete_confirm_mismatch"))
        return _redirect(_platform_redirect_target(next_url))
    try:
        deletion_summary = delete_company_workspace(
            db,
            app_settings=settings,
            tenant_slug=tenant_slug,
            actor_user=current_user,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        flash(request, "error", str(exc))
        return _redirect(_platform_redirect_target(next_url))
    flash(
        request,
        "success",
        _t(
            request,
            "msg_tenant_deleted",
            tenant=deletion_summary["company_name"],
            photos=deletion_summary["photo_count"],
            videos=deletion_summary["video_count"],
            employees=deletion_summary["employee_count"],
        ),
    )
    return _redirect(_platform_redirect_target(next_url))


@router.get("/portal/gallery", response_class=HTMLResponse)
def gallery_page(
    request: Request,
    project_id: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin, UserRole.project_manager, UserRole.client)
    projects = _accessible_projects(db, current_user)
    stmt = apply_photo_scope(db, select(Photo), current_user, photo_type=PhotoType.project, gallery_only=True)
    if project_id:
        if not can_access_project(db, current_user, project_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        stmt = stmt.where(Photo.project_id == project_id)
    photos = list(db.scalars(stmt.order_by(Photo.featured.desc(), Photo.captured_at_utc.desc())))
    comments_by_photo = get_comments_by_photo_ids(db, [photo.id for photo in photos])
    comment_users = _build_comment_users(db, comments_by_photo)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/gallery/index.html",
        page_title=_t(request, "page_gallery"),
        projects=projects,
        selected_project=project_id or "",
        photos=photos,
        comments_by_photo=comments_by_photo,
        comment_users=comment_users,
        can_comment=is_manager(current_user) or is_client(current_user),
    )


@router.get("/portal/audit-logs", response_class=HTMLResponse)
def audit_logs_page(
    request: Request,
    actor: str | None = Query(None),
    action: str | None = Query(None),
    project_id: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=25, le=200),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    stmt = select(AuditLog).where(AuditLog.company_id == current_user.company_id).order_by(AuditLog.created_at.desc())
    timezone_name = get_system_settings(db, settings).get("timezone", settings.default_timezone)
    range_start, range_end = build_date_range(start_date, end_date, timezone_name)
    if actor:
        actor_user = db.scalar(
            select(User).where(
                User.company_id == current_user.company_id,
                or_(User.username == actor, User.email == actor),
            )
        )
        stmt = stmt.where(AuditLog.actor_user_id == (actor_user.id if actor_user else -1))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if project_id:
        stmt = stmt.where(AuditLog.project_id == project_id)
    if range_start:
        stmt = stmt.where(AuditLog.created_at >= range_start)
    if range_end:
        stmt = stmt.where(AuditLog.created_at < range_end)
    total_logs = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    total_pages = max(1, (total_logs + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    page_start = (page - 1) * per_page
    page_end = min(page_start + per_page, total_logs)
    logs = list(db.scalars(stmt.offset(page_start).limit(per_page)))
    users = {
        user.id: user
        for user in db.scalars(select(User).where(User.company_id == current_user.company_id)).all()
    }
    audit_rows = [
        {
            "created_at": log.created_at,
            "actor": users[log.actor_user_id].username if log.actor_user_id in users else _t(request, "meta_none"),
            "action": log.action,
            "target": f"{log.target_type}:{log.target_id or _t(request, 'meta_none')}",
            "project_id": log.project_id or _t(request, "meta_none"),
            "detail_summary": _compact_audit_detail(log.detail_json),
            "detail_json": _audit_detail_json(log.detail_json),
            "ip_address": log.ip_address or _t(request, "meta_none"),
        }
        for log in logs
    ]

    def _audit_page_url(target_page: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(target_page)
        params["per_page"] = str(per_page)
        return f"{request.url.path}?{urlencode(params, doseq=True)}"

    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/audit/index.html",
        page_title=_t(request, "page_audit_logs"),
        audit_rows=audit_rows,
        projects=_accessible_projects(db, current_user),
        pagination={
            "page": page,
            "per_page": per_page,
            "total": total_logs,
            "total_pages": total_pages,
            "start": page_start + 1 if total_logs else 0,
            "end": page_end,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_url": _audit_page_url(page - 1) if page > 1 else "",
            "next_url": _audit_page_url(page + 1) if page < total_pages else "",
        },
        filters={
            "actor": actor or "",
            "action": action or "",
            "project_id": project_id or "",
            "start_date": start_date or "",
            "end_date": end_date or "",
            "per_page": per_page,
        },
    )


@router.get("/portal/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    return _render(
        request,
        db,
        settings,
        current_user,
        "pages/settings/index.html",
        page_title=_t(request, "page_settings"),
        form_settings=get_system_settings(db, settings),
    )


@router.post("/portal/settings")
def update_settings_action(
    request: Request,
    system_title: str = Form(...),
    portal_title: str = Form(...),
    company_name: str = Form(...),
    timezone: str = Form(...),
    logo_path: str = Form(...),
    storage_base_url: str = Form(""),
    upload_limit_mb: str = Form(...),
    default_visibility: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    redirect = _require_portal_user(current_user)
    if redirect is not None:
        return redirect
    _require_role(current_user, UserRole.super_admin)
    validate_csrf(request, csrf_token)
    update_system_settings(
        db,
        {
            "system_title": system_title,
            "portal_title": portal_title,
            "company_name": company_name,
            "timezone": timezone,
            "logo_path": logo_path,
            "storage_base_url": storage_base_url.rstrip("/"),
            "upload_limit_mb": upload_limit_mb,
            "default_visibility": default_visibility,
        },
    )
    log_audit(
        db,
        action="settings_updated",
        target_type="system",
        target_id="settings",
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    flash(request, "success", _t(request, "msg_settings_updated"))
    return _redirect("/portal/settings")
