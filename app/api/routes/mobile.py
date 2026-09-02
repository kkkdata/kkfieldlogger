from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_mobile_employee, get_settings
from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import to_utc_iso, utc_now
from app.models import Employee, ExpressionArtifact, FactSnapshot, MediaAsset, MediaAssetStatus, Photo, PhotoType, Project, ReviewTask, ReviewTaskStatus, ReviewTaskType
from app.schemas.mobile import HealthResponse, PhotoOut, QueueInfo, UploadResponse
from app.services.job_queue import PHOTO_AI_TASK, enqueue_photo_ai_task, get_task_queue_snapshot, schedule_job_worker
from app.services.media_pipeline import queue_media_asset_processing
from app.services.photos import photo_media_asset_id, photo_media_kind, save_mobile_upload, serialize_photo, soft_delete_photo
from app.services.expression_display import build_mobile_contribution_dashboard, build_project_manager_status_card
from app.services.settings import get_system_settings
from app.services.audit import log_audit
from app.services.request_rate_limit import check_rate_limit
from app.services.review_workflow import (
    apply_employee_review_task_action,
    get_review_task_for_employee,
    serialize_review_task,
)

router = APIRouter()
PUBLIC_DIR = Path(__file__).resolve().parents[2] / "static" / "public"
logger = get_logger("kkfieldlogger.mobile")

EMPLOYEE_CONTRIBUTION_ARTIFACT_TYPE = "employee_contribution_narrative"
EMPLOYEE_CONTRIBUTION_SCOPE_TYPE = "employee_project_window"
EMPLOYEE_CONTRIBUTION_VISIBLE_STATUS = "promoted_valid"


class MobileReviewTaskActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    message: str | None = Field(default=None, max_length=2000)
    completion_photo_id: int | None = None


def _dict_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_items(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(text[:180])
        if len(items) >= limit:
            break
    return items


def _top_items(counter: Counter[str], *, limit: int = 8) -> list[dict[str, object]]:
    return [{"value": value, "count": count} for value, count in counter.most_common(limit)]


def _latest_employee_contribution_artifact(
    db: Session,
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int,
) -> tuple[ExpressionArtifact, FactSnapshot] | None:
    rows = (
        db.execute(
            select(ExpressionArtifact, FactSnapshot)
            .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id)
            .where(
                ExpressionArtifact.company_id == company_id,
                ExpressionArtifact.artifact_type == EMPLOYEE_CONTRIBUTION_ARTIFACT_TYPE,
                ExpressionArtifact.audience_id == "employee",
                ExpressionArtifact.promoted.is_(True),
                ExpressionArtifact.validation_status == EMPLOYEE_CONTRIBUTION_VISIBLE_STATUS,
                FactSnapshot.company_id == company_id,
                FactSnapshot.employee_id == employee_id,
                FactSnapshot.project_id == project_id,
                FactSnapshot.scope_type == EMPLOYEE_CONTRIBUTION_SCOPE_TYPE,
            )
            .order_by(ExpressionArtifact.created_at.desc())
            .limit(20)
        )
        .tuples()
        .all()
    )
    for artifact, snapshot in rows:
        scope_key = snapshot.scope_key_json if isinstance(snapshot.scope_key_json, dict) else {}
        if int(scope_key.get("window_days") or 0) == window_days:
            return artifact, snapshot
    return None


def _fact_count_summary(snapshot: FactSnapshot) -> dict[str, object]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    counts = facts.get("counts") if isinstance(facts.get("counts"), dict) else {}
    trend = facts.get("trend") if isinstance(facts.get("trend"), dict) else {}
    coverage = facts.get("coverage") if isinstance(facts.get("coverage"), dict) else {}
    return {
        "photos_current": int(counts.get("photos_current") or 0),
        "photos_previous": int(counts.get("photos_previous") or 0),
        "active_days_current": int(counts.get("active_days_current") or 0),
        "active_days_previous": int(counts.get("active_days_previous") or 0),
        "completed_ai_current": int(counts.get("completed_ai_current") or 0),
        "completed_ai_previous": int(counts.get("completed_ai_previous") or 0),
        "photo_delta": int(trend.get("photo_delta") or 0),
        "active_day_delta": int(trend.get("active_day_delta") or 0),
        "completed_ai_delta": int(trend.get("completed_ai_delta") or 0),
        "has_current_photos": bool(coverage.get("has_current_photos")),
        "has_completed_ai": bool(coverage.get("has_completed_ai")),
        "has_recent_highlights": bool(coverage.get("has_recent_highlights")),
    }


def _employee_contribution_payload(artifact: ExpressionArtifact) -> dict[str, object]:
    payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
    contribution_explanation = payload.get("contribution_explanation")
    strengths = payload.get("strengths")
    suggestions = payload.get("suggestions")
    recent_highlights = payload.get("recent_highlights")
    response = dict(payload)
    response.setdefault("summary", payload.get("summary_line"))
    response.setdefault("highlights", recent_highlights if isinstance(recent_highlights, list) else [])
    response.setdefault(
        "sections",
        {
            "contribution_explanation": contribution_explanation if isinstance(contribution_explanation, list) else [],
            "strengths": strengths if isinstance(strengths, list) else [],
            "suggestions": suggestions if isinstance(suggestions, list) else [],
        },
    )
    response.setdefault("comparison", payload.get("comparison_text"))
    return response


def _localized_summary(tag_json: dict[str, Any], language: str | None) -> str | None:
    normalized_language = (language or "").strip().lower()
    translations = tag_json.get("ai_summary_translations")
    if normalized_language and isinstance(translations, dict):
        translated = translations.get(normalized_language)
        if translated:
            return str(translated)
    summary = tag_json.get("ai_summary")
    return str(summary) if summary else None


def _extend_counts(counter: Counter[str], values: list[str]) -> None:
    for value in values:
        counter[value] += 1


def _work_evidence_item(photo: Photo, serialized: PhotoOut, *, language: str | None) -> dict[str, object]:
    tag_json = _dict_payload(photo.tag_json)
    manager_evidence = _dict_payload(tag_json.get("manager_evidence_summary"))
    receipt_facts = _dict_payload(tag_json.get("receipt_facts"))
    photo_type = photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type)
    approval_status = (
        photo.approval_status.value if hasattr(photo.approval_status, "value") else str(photo.approval_status)
    )
    return {
        "photo_id": photo.id,
        "project_id": photo.project_id,
        "employee_id": photo.employee_id,
        "photo_type": photo_type,
        "media_kind": serialized.media_kind,
        "media_url": serialized.media_url,
        "thumb_url": serialized.thumb_url,
        "captured_at_utc": to_utc_iso(photo.captured_at_utc),
        "created_at": to_utc_iso(photo.created_at),
        "approval_status": approval_status,
        "labeling_status": photo.labeling_status,
        "note": photo.note,
        "ai_summary": _localized_summary(tag_json, language),
        "base_ai_summary": tag_json.get("base_ai_summary"),
        "confidence_level": tag_json.get("confidence_level"),
        "labels": _string_items(tag_json.get("labels")),
        "defects": _string_items(tag_json.get("defects")),
        "visible_objects": _string_items(tag_json.get("visible_objects")),
        "materials": _string_items(tag_json.get("materials")),
        "equipment": _string_items(tag_json.get("equipment")),
        "people_ppe": _string_items(tag_json.get("people_ppe")),
        "recommended_actions": _string_items(tag_json.get("recommended_actions")),
        "evidence_limitations": _string_items(tag_json.get("evidence_limitations")),
        "manager_value": manager_evidence.get("manager_value"),
        "what_this_photo_proves": _string_items(manager_evidence.get("what_this_photo_proves"), limit=4),
        "manager_next_actions": _string_items(manager_evidence.get("manager_next_actions"), limit=4),
        "receipt_facts": receipt_facts,
    }


class MobileDiagnosticLogIn(BaseModel):
    source: str = Field(default="mobile", max_length=80)
    created_at: str | None = Field(default=None, max_length=80)
    log_text: str = Field(min_length=1, max_length=200_000)


def _mobile_queue_info(db: Session, company_id: str, *, queue_position: int | None = None) -> QueueInfo:
    snapshot = get_task_queue_snapshot(db, company_id, PHOTO_AI_TASK)
    active_count = int(snapshot["active_count"])
    # Keep this field non-null for shipped app compatibility: 1 when the queue
    # is clear (work starts immediately), otherwise the real backlog depth so
    # it no longer contradicts queue_message.
    resolved_position = queue_position if queue_position is not None else max(1, active_count)
    estimated_wait_seconds = int(snapshot["estimated_wait_seconds"])
    estimated_completion_time = (
        to_utc_iso(utc_now() + timedelta(seconds=estimated_wait_seconds))
        if estimated_wait_seconds > 0
        else to_utc_iso(utc_now())
    )
    if active_count <= 0:
        queue_message = "AI analysis is available immediately."
    else:
        minutes = max(1, round(estimated_wait_seconds / 60))
        queue_message = (
            f"{active_count} AI photo task(s) are currently queued or running. "
            f"Estimated completion in about {minutes} minute(s)."
        )
    return QueueInfo(
        queued_count=int(snapshot["queued_count"]),
        running_count=int(snapshot["running_count"]),
        retry_pending_count=int(snapshot["retry_pending_count"]),
        active_count=active_count,
        average_duration_seconds=float(snapshot["average_duration_seconds"]),
        estimated_wait_seconds=estimated_wait_seconds,
        estimated_completion_time=estimated_completion_time,
        position_in_queue=resolved_position,
        queue_position=resolved_position,
        queue_message=queue_message,
    )


@router.get("/")
def root(request: Request, settings: Settings = Depends(get_settings)):
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept and "application/json" not in accept and "X-API-Key" not in request.headers:
        return FileResponse(PUBLIC_DIR / "index.html")
    return JSONResponse(
        HealthResponse(
            status="ok",
            name=settings.app_name,
            portal=settings.portal_title,
            company=settings.company_name,
            time=to_utc_iso(utc_now()) or "",
        ).model_dump()
    )


@router.get("/health", response_model=HealthResponse)
def health(request: Request, settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        name=settings.app_name,
        portal=settings.portal_title,
        company=settings.company_name,
        time=to_utc_iso(utc_now()) or "",
    )


@router.get("/api/key/validate")
def validate_api_key(employee: Employee = Depends(get_mobile_employee)) -> dict[str, object]:
    return {"ok": True, "employee_id": employee.employee_id}


@router.head("/api/key/validate", status_code=status.HTTP_204_NO_CONTENT)
def validate_api_key_head(employee: Employee = Depends(get_mobile_employee)) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"X-Employee-ID": employee.employee_id})


@router.post("/api/mobile/diagnostics/logs", status_code=status.HTTP_201_CREATED)
def upload_mobile_diagnostic_logs(
    payload: MobileDiagnosticLogIn,
    request: Request,
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    retry_after = check_rate_limit(
        f"mobile_diagnostics:{employee.company_id}:{employee.employee_id}",
        limit_per_window=settings.mobile_diagnostic_log_rate_limit_per_hour,
        window_seconds=3600,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many diagnostic log uploads",
            headers={"Retry-After": str(max(1, int(round(retry_after))))},
        )
    diagnostics_dir = settings.logs_root / "mobile_diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    file_name = f"{timestamp}-{employee.company_id}-{employee.employee_id}-{uuid4().hex[:8]}.log"
    log_path = diagnostics_dir / file_name
    content = (
        f"source={payload.source}\n"
        f"client_created_at={payload.created_at or ''}\n"
        f"server_received_at={to_utc_iso(now)}\n"
        f"company_id={employee.company_id}\n"
        f"employee_id={employee.employee_id}\n"
        f"remote_addr={request.client.host if request.client else ''}\n"
        "--- log_text ---\n"
        f"{payload.log_text}\n"
    )
    log_path.write_text(content, encoding="utf-8")
    log_audit(
        db,
        action="mobile_diagnostic_log_uploaded",
        target_type="employee",
        target_id=employee.employee_id,
        company_id=employee.company_id,
        detail_json={
            "source": payload.source,
            "file_name": file_name,
            "log_chars": len(payload.log_text),
        },
        ip_address=request.client.host if request.client else None,
    )
    db.commit()
    logger.info(
        "mobile_diagnostic_log_uploaded",
        company_id=employee.company_id,
        employee_id=employee.employee_id,
        file_name=file_name,
        log_chars=len(payload.log_text),
    )
    return {"ok": True, "file_name": file_name}


@router.options("/api/key/validate", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/diagnostics/logs", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/projects/{project_id}/contribution", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/projects/{project_id}/contribution-dashboard", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/projects/{project_id}/project-manager-status-card", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/tasks", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/api/mobile/tasks/{public_id}/actions", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/photos/me", status_code=status.HTTP_204_NO_CONTENT)
@router.options("/upload", status_code=status.HTTP_204_NO_CONTENT)
def mobile_options() -> Response:
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "Allow": "GET,POST,HEAD,OPTIONS,DELETE",
            "Access-Control-Allow-Headers": "X-API-Key, Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET,POST,HEAD,OPTIONS,DELETE",
        },
    )


@router.get("/api/mobile/tasks")
def mobile_review_tasks(
    status_filter: str = Query("active", pattern="^(active|open|acknowledged|completed|cancelled|all)$"),
    project_id: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=300),
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_mobile_employee),
):
    stmt = select(ReviewTask).where(
        ReviewTask.company_id == employee.company_id,
        ReviewTask.assigned_employee_id == employee.employee_id,
        ReviewTask.task_type != ReviewTaskType.internal_note,
    )
    if project_id:
        stmt = stmt.where(ReviewTask.project_id == project_id)
    if status_filter == "active":
        stmt = stmt.where(ReviewTask.status.in_([ReviewTaskStatus.open, ReviewTaskStatus.acknowledged]))
    elif status_filter != "all":
        stmt = stmt.where(ReviewTask.status == ReviewTaskStatus(status_filter))
    tasks = list(db.scalars(stmt.order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc()).limit(limit)))
    return {
        "contract_version": "mobile_review_tasks:v1",
        "employee_id": employee.employee_id,
        "status_filter": status_filter,
        "items": [serialize_review_task(task) for task in tasks],
    }


@router.post("/api/mobile/tasks/{public_id}/actions")
def mobile_review_task_action(
    public_id: str,
    request: Request,
    payload: MobileReviewTaskActionRequest,
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_mobile_employee),
):
    task = get_review_task_for_employee(db, employee=employee, public_id=public_id)
    task = apply_employee_review_task_action(
        db,
        employee=employee,
        task=task,
        action=payload.action.strip(),
        message=payload.message,
        completion_photo_id=payload.completion_photo_id,
        request=request,
    )
    db.commit()
    db.refresh(task)
    return {
        "message": "ok",
        "contract_version": "mobile_review_tasks:v1",
        "task": serialize_review_task(task),
    }


@router.post("/upload", response_model=UploadResponse)
async def upload_photo(
    request: Request,
    background_tasks: BackgroundTasks,
    photo: UploadFile = File(...),
    employee_id: str = Form(...),
    project_id: str = Form(...),
    photo_type: PhotoType = Form(...),
    gps: str | None = Form(None),
    gps_lat: float | None = Form(None),
    gps_lon: float | None = Form(None),
    heading: float | None = Form(None),
    pitch: float | None = Form(None),
    roll: float | None = Form(None),
    location: str | None = Form(None),
    timestamp: str = Form(...),
    device_model: str | None = Form(None),
    os_version: str | None = Form(None),
    app_version: str | None = Form(None),
    note: str | None = Form(None),
    media_kind: str | None = Form(None),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    try:
        uploaded = await save_mobile_upload(
            db,
            app_settings=settings,
            request=request,
            upload=photo,
            employee=employee,
            employee_id=employee_id,
            project_id=project_id,
            photo_type=photo_type,
            gps=gps,
            gps_lat=gps_lat,
            gps_lon=gps_lon,
            heading=heading,
            pitch=pitch,
            roll=roll,
            location=location,
            timestamp=timestamp,
            device_model=device_model,
            os_version=os_version,
            app_version=app_version,
            note=note,
            media_kind=media_kind,
        )
    except HTTPException as exc:
        logger.warning(
            "mobile_upload_rejected",
            status_code=exc.status_code,
            detail=str(exc.detail),
            employee_id=employee_id,
            employee_company_id=employee.company_id,
            project_id=project_id,
            employee_assigned_project_id=employee.project_id,
            photo_type=str(photo_type.value if hasattr(photo_type, "value") else photo_type),
            file_name=photo.filename,
            content_type=photo.content_type,
            content_length=request.headers.get("content-length"),
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        raise
    try:
        if photo_media_kind(uploaded) == "video":
            asset = db.get(MediaAsset, photo_media_asset_id(uploaded))
            if asset is not None:
                queue_media_asset_processing(
                    db,
                    app_settings=request.app.state.settings,
                    asset=asset,
                    schedule_task=background_tasks.add_task,
                    session_maker=request.app.state.session_maker,
                )
        else:
            # AI is optional and off by default: with no enabled backend,
            # skip the pipeline job entirely instead of enqueueing work that
            # can only retry and fail (a fresh NAS install would otherwise
            # show failed-job alerts after its very first uploads).
            from app.services.ai_pipeline import resolve_ai_backends

            backends, _ = resolve_ai_backends(db, request.app.state.settings, uploaded)
            if backends:
                enqueue_photo_ai_task(
                    db,
                    app_settings=request.app.state.settings,
                    photo=uploaded,
                    actor_user_id=None,
                    custom_prompt=None,
                    trigger_source="upload",
                    priority="normal",
                )
            else:
                logger.info(
                    "photo_ai_skipped_no_backend",
                    photo_id=uploaded.id,
                    company_id=uploaded.company_id,
                )
        db.commit()
        if photo_media_kind(uploaded) != "video":
            schedule_job_worker(background_tasks.add_task, request.app.state.session_maker, request.app.state.settings)
    except RuntimeError as exc:
        uploaded.labeling_status = "failed"
        uploaded.tag_json = {
            "ai_summary": None,
            "labels": [],
            "defects": [],
            "error": {
                "code": "queue_capacity_exceeded",
                "message": str(exc),
                "attempts": [],
            },
        }
        db.add(uploaded)
        linked_asset = db.get(MediaAsset, photo_media_asset_id(uploaded))
        if linked_asset is not None:
            linked_asset.status = MediaAssetStatus.failed
            linked_asset.error_message = str(exc)
            db.add(linked_asset)
        db.commit()
    system_settings = get_system_settings(db, settings)
    queue_info = _mobile_queue_info(db, employee.company_id)
    return UploadResponse(
        message="Upload successful",
        photo=serialize_photo(
            uploaded,
            request,
            settings,
            storage_base_url=system_settings.get("storage_base_url"),
            queue_info=queue_info,
        ),
        queue=queue_info,
        queue_message=queue_info.queue_message,
    )


@router.get("/photos/me", response_model=list[PhotoOut])
def my_photos(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=200),
    page_size: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[PhotoOut]:
    resolved_limit = page_size or limit or 50
    photos = db.scalars(
        select(Photo)
        .where(
            Photo.employee_id == employee.employee_id,
            Photo.company_id == employee.company_id,
            Photo.deleted.is_(False),
        )
        .order_by(Photo.created_at.desc())
        .offset(offset)
        .limit(resolved_limit)
    ).all()
    system_settings = get_system_settings(db, settings)
    queue_info = _mobile_queue_info(db, employee.company_id)
    return [
        serialize_photo(
            photo,
            request,
            settings,
            storage_base_url=system_settings.get("storage_base_url"),
            queue_info=queue_info,
        )
        for photo in photos
    ]


@router.get("/api/mobile/projects/{project_id}/work-evidence")
def project_work_evidence(
    project_id: str,
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    days: int | None = Query(default=None, ge=1, le=365),
    language: str | None = Query(default=None, max_length=8),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    project = db.scalar(
        select(Project).where(Project.project_id == project_id, Project.company_id == employee.company_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    filters = [
        Photo.employee_id == employee.employee_id,
        Photo.company_id == employee.company_id,
        Photo.project_id == project_id,
        Photo.deleted.is_(False),
    ]
    if days is not None:
        filters.append(Photo.captured_at_utc >= utc_now() - timedelta(days=days))

    photos = list(
        db.scalars(
            select(Photo)
            .where(*filters)
            .order_by(Photo.captured_at_utc.desc(), Photo.created_at.desc(), Photo.id.desc())
            .limit(limit + 1)
        )
    )
    has_more = len(photos) > limit
    visible_photos = photos[:limit]
    system_settings = get_system_settings(db, settings)

    label_counts: Counter[str] = Counter()
    material_counts: Counter[str] = Counter()
    equipment_counts: Counter[str] = Counter()
    defect_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    approval_counts: Counter[str] = Counter()
    labeling_counts: Counter[str] = Counter()
    media_counts: Counter[str] = Counter()
    items: list[dict[str, object]] = []
    latest_captured_at: str | None = None

    for photo in visible_photos:
        serialized = serialize_photo(
            photo,
            request,
            settings,
            storage_base_url=system_settings.get("storage_base_url"),
            queue_info=None,
        )
        item = _work_evidence_item(photo, serialized, language=language)
        items.append(item)
        tag_json = _dict_payload(photo.tag_json)
        _extend_counts(label_counts, _string_items(tag_json.get("labels"), limit=20))
        _extend_counts(material_counts, _string_items(tag_json.get("materials"), limit=20))
        _extend_counts(equipment_counts, _string_items(tag_json.get("equipment"), limit=20))
        _extend_counts(defect_counts, _string_items(tag_json.get("defects"), limit=20))
        _extend_counts(action_counts, _string_items(tag_json.get("recommended_actions"), limit=20))
        approval_counts[str(item["approval_status"])] += 1
        labeling_counts[str(photo.labeling_status or "unknown")] += 1
        media_counts[str(item["media_kind"])] += 1
        if latest_captured_at is None and isinstance(item["captured_at_utc"], str):
            latest_captured_at = item["captured_at_utc"]

    return {
        "employee": {
            "employee_id": employee.employee_id,
            "name": employee.name,
        },
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
        },
        "window": {
            "days": days,
            "limit": limit,
            "has_more": has_more,
        },
        "summary": {
            "photo_count": len(visible_photos),
            "latest_captured_at": latest_captured_at,
            "approval_counts": dict(approval_counts),
            "labeling_counts": dict(labeling_counts),
            "media_counts": dict(media_counts),
            "top_labels": _top_items(label_counts),
            "top_materials": _top_items(material_counts),
            "top_equipment": _top_items(equipment_counts),
            "top_defects": _top_items(defect_counts),
            "top_recommended_actions": _top_items(action_counts),
        },
        "items": items,
    }


@router.get("/api/mobile/projects/{project_id}/contribution")
def project_employee_contribution(
    project_id: str,
    window_days: int = Query(default=30, ge=1, le=365),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    project = db.scalar(
        select(Project).where(Project.project_id == project_id, Project.company_id == employee.company_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    resolved = _latest_employee_contribution_artifact(
        db,
        company_id=employee.company_id,
        employee_id=employee.employee_id,
        project_id=project_id,
        window_days=window_days,
    )
    base_payload: dict[str, object] = {
        "employee": {
            "employee_id": employee.employee_id,
            "name": employee.name,
        },
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
        },
        "window": {
            "days": window_days,
        },
    }
    if resolved is None:
        return {
            **base_payload,
            "status": "not_ready",
            "contribution": None,
            "rendered_markdown": None,
            "facts_summary": None,
            "artifact": None,
        }

    artifact, snapshot = resolved
    return {
        **base_payload,
        "status": "available",
        "contribution": _employee_contribution_payload(artifact),
        "rendered_markdown": artifact.rendered_markdown,
        "facts_summary": _fact_count_summary(snapshot),
        "artifact": {
            "artifact_id": artifact.id,
            "fact_snapshot_id": snapshot.id,
            "prompt_version_id": artifact.prompt_version_id,
            "contract_id": artifact.contract_id,
            "validation_status": artifact.validation_status,
            "promoted": artifact.promoted,
            "model_used": artifact.model_used,
            "created_at": to_utc_iso(artifact.created_at),
        },
    }


@router.get("/api/mobile/projects/{project_id}/contribution-dashboard")
def project_employee_contribution_dashboard(
    project_id: str,
    window_days: int = Query(default=30, ge=1, le=365),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    project = db.scalar(
        select(Project).where(Project.project_id == project_id, Project.company_id == employee.company_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return build_mobile_contribution_dashboard(
        db,
        employee=employee,
        project=project,
        window_days=window_days,
    )


@router.get("/api/mobile/projects/{project_id}/project-manager-status-card")
def project_manager_status_card(
    project_id: str,
    window_days: int = Query(default=30, ge=1, le=365),
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    project = db.scalar(
        select(Project).where(Project.project_id == project_id, Project.company_id == employee.company_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return build_project_manager_status_card(
        db,
        project=project,
        window_days=window_days,
    )


@router.head("/photos/me", status_code=status.HTTP_204_NO_CONTENT)
def my_photos_head(employee: Employee = Depends(get_mobile_employee)) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"X-Employee-ID": employee.employee_id})


@router.delete("/photo/{photo_id}")
def delete_photo(
    photo_id: int,
    request: Request,
    employee: Employee = Depends(get_mobile_employee),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    photo = db.scalar(
        select(Photo).where(
            Photo.id == photo_id,
            Photo.employee_id == employee.employee_id,
            Photo.company_id == employee.company_id,
        )
    )
    if photo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
    soft_delete_photo(
        db,
        photo=photo,
        actor_user=None,
        request=request,
        detail={"source": "mobile", "employee_id": employee.employee_id},
    )
    return {"message": "Photo deleted"}
