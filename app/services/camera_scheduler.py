from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import utc_now
from app.models import AuditLog, CameraApprovalStatus, CameraProtocol, CameraStatus, IPCamera, MediaType
from app.services.audit import log_audit
from app.services.email import send_email
from app.services.media_pipeline import ingest_local_media_file
from app.services.photos import sanitize_component
from app.services.settings import get_system_settings


logger = get_logger("kkfieldlogger.camera_scheduler")

DEFAULT_CAMERA_FAILURE_ALERT_THRESHOLD = 3
CAMERA_FAILURE_ACTIONS = {
    "ip_camera_rtsp_capture_failed",
    "ip_camera_test_failed",
    "ip_camera_failure_threshold_reached",
}


def _coerce_positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _camera_ffmpeg_input_args(camera: IPCamera) -> list[str]:
    args: list[str] = []
    if camera.protocol == CameraProtocol.rtsp:
        args.extend(["-rtsp_transport", "tcp"])
    args.extend(["-i", camera.stream_url])
    return args


def _camera_clip_path(app_settings: Settings, camera: IPCamera) -> Path:
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
    safe_name = sanitize_component(camera.name or f"camera-{camera.id}")
    target_dir = app_settings.camera_clips_root / camera.company_id / str(camera.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{safe_name}-{timestamp}.mp4"


def _camera_status_for_error(error: str) -> CameraStatus:
    normalized = str(error or "").strip().lower()
    if any(marker in normalized for marker in ("timed out", "timeout", "connection refused", "404", "offline", "no route")):
        return CameraStatus.offline
    return CameraStatus.error


def get_camera_failure_alert_threshold(db: Session, app_settings: Settings) -> int:
    system_settings = get_system_settings(db, app_settings)
    return _coerce_positive_int(
        system_settings.get("camera_failure_alert_threshold"),
        DEFAULT_CAMERA_FAILURE_ALERT_THRESHOLD,
    )


def _mark_camera_success(camera: IPCamera, *, checked_at) -> None:
    camera.status = CameraStatus.online
    camera.last_check_at = checked_at
    camera.error_log = None
    camera.consecutive_failures = 0
    camera.last_alert_at = None


def _camera_alert_html(camera: IPCamera, *, error: str, threshold: int) -> str:
    return (
        f"<h2>IP Camera Attention Required</h2>"
        f"<p>Camera <strong>{camera.name}</strong> has failed {camera.consecutive_failures} consecutive checks.</p>"
        f"<ul>"
        f"<li>Camera ID: {camera.id}</li>"
        f"<li>Company ID: {camera.company_id}</li>"
        f"<li>Protocol: {camera.protocol.value}</li>"
        f"<li>Status: {camera.status.value}</li>"
        f"<li>Alert threshold: {threshold}</li>"
        f"</ul>"
        f"<p>Most recent error:</p>"
        f"<pre>{error}</pre>"
    )


def _maybe_send_camera_failure_alert(
    db: Session,
    *,
    camera: IPCamera,
    app_settings: Settings,
    error: str,
    checked_at,
) -> bool:
    threshold = get_camera_failure_alert_threshold(db, app_settings)
    if camera.consecutive_failures < threshold:
        return False
    if camera.last_alert_at is not None:
        return False

    sent = send_email(
        db,
        settings=app_settings,
        to_email=app_settings.support_email,
        subject=f"[KK] Camera attention required: {camera.name}",
        html_content=_camera_alert_html(camera, error=error, threshold=threshold),
        action="ip_camera_failure_alert_sent",
        company_id=camera.company_id,
    )
    camera.last_alert_at = checked_at
    db.add(camera)
    log_audit(
        db,
        action="ip_camera_failure_threshold_reached",
        target_type="ip_camera",
        target_id=str(camera.id),
        tenant_id=camera.company_id,
        company_id=camera.company_id,
        detail_json={
            "status": camera.status.value,
            "consecutive_failures": camera.consecutive_failures,
            "alert_threshold": threshold,
            "email_sent": sent,
            "error": error,
        },
    )
    logger.warning(
        "ip_camera_attention_required",
        camera_id=camera.id,
        company_id=camera.company_id,
        status=camera.status.value,
        consecutive_failures=camera.consecutive_failures,
        threshold=threshold,
        email_sent=sent,
    )
    return sent


def _mark_camera_failure(
    db: Session,
    *,
    camera: IPCamera,
    app_settings: Settings,
    error: str,
    checked_at,
) -> None:
    camera.status = _camera_status_for_error(error)
    camera.last_check_at = checked_at
    camera.error_log = error
    camera.consecutive_failures = max(0, int(camera.consecutive_failures or 0)) + 1
    db.add(camera)
    _maybe_send_camera_failure_alert(
        db,
        camera=camera,
        app_settings=app_settings,
        error=error,
        checked_at=checked_at,
    )
    logger.warning(
        "ip_camera_status_updated",
        camera_id=camera.id,
        company_id=camera.company_id,
        status=camera.status.value,
        consecutive_failures=camera.consecutive_failures,
        error=error,
    )


def record_rtsp_camera_clip(camera: IPCamera, app_settings: Settings) -> Path:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH")

    output_path = _camera_clip_path(app_settings, camera)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_camera_ffmpeg_input_args(camera),
        "-t",
        str(app_settings.camera_capture_clip_seconds),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=app_settings.camera_capture_timeout_seconds,
    )
    if completed.returncode != 0 or not output_path.is_file():
        error_message = (completed.stderr or completed.stdout or "camera capture failed").strip()
        raise RuntimeError(error_message)
    return output_path


def probe_ip_camera_stream(camera: IPCamera, app_settings: Settings) -> dict[str, Any]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        *_camera_ffmpeg_input_args(camera),
        "-t",
        "2",
        "-an",
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=min(app_settings.camera_capture_timeout_seconds, 15),
    )
    if completed.returncode != 0:
        error_message = (completed.stderr or completed.stdout or "camera probe failed").strip()
        raise RuntimeError(error_message)
    return {
        "status": CameraStatus.online.value,
        "checked_at": utc_now().isoformat(),
        "message": "Camera stream is reachable.",
    }


def test_ip_camera_connection(db: Session, camera: IPCamera, app_settings: Settings) -> dict[str, Any]:
    checked_at = utc_now()
    try:
        result = probe_ip_camera_stream(camera, app_settings)
        _mark_camera_success(camera, checked_at=checked_at)
        db.add(camera)
        db.flush()
        logger.info(
            "ip_camera_test_succeeded",
            camera_id=camera.id,
            company_id=camera.company_id,
            protocol=camera.protocol.value,
            checked_at=checked_at.isoformat(),
        )
        return result
    except Exception as exc:
        _mark_camera_failure(db, camera=camera, app_settings=app_settings, error=str(exc), checked_at=checked_at)
        db.flush()
        logger.warning(
            "ip_camera_test_failed",
            camera_id=camera.id,
            company_id=camera.company_id,
            protocol=camera.protocol.value,
            checked_at=checked_at.isoformat(),
            error=str(exc),
        )
        raise


def camera_health_summary(
    db: Session,
    company_id: str,
    *,
    failure_threshold: int = DEFAULT_CAMERA_FAILURE_ALERT_THRESHOLD,
) -> dict[str, Any]:
    cameras = list(
        db.scalars(
            select(IPCamera)
            .where(IPCamera.company_id == company_id)
            .order_by(IPCamera.is_enabled.desc(), IPCamera.id.asc())
        )
    )
    attention_cameras = [
        camera
        for camera in cameras
        if camera.is_enabled and (camera.status in {CameraStatus.offline, CameraStatus.error} or camera.consecutive_failures >= failure_threshold)
    ]
    return {
        "total_cameras": len(cameras),
        "enabled_cameras": sum(1 for camera in cameras if camera.is_enabled),
        "pending_review_cameras": sum(1 for camera in cameras if camera.approval_status == CameraApprovalStatus.pending),
        "approved_cameras": sum(1 for camera in cameras if camera.approval_status == CameraApprovalStatus.approved),
        "rejected_cameras": sum(1 for camera in cameras if camera.approval_status == CameraApprovalStatus.rejected),
        "online_cameras": sum(1 for camera in cameras if camera.status == CameraStatus.online),
        "offline_cameras": sum(1 for camera in cameras if camera.status == CameraStatus.offline),
        "error_cameras": sum(1 for camera in cameras if camera.status == CameraStatus.error),
        "attention_count": len(attention_cameras),
        "failure_threshold": failure_threshold,
        "items": [
            {
                "id": camera.id,
                "name": camera.name,
                "protocol": camera.protocol.value,
                "status": camera.status.value,
                "approval_status": camera.approval_status.value,
                "is_enabled": camera.is_enabled,
                "consecutive_failures": camera.consecutive_failures,
                "last_check_at": camera.last_check_at.isoformat() if camera.last_check_at else None,
                "error_log": camera.error_log,
            }
            for camera in attention_cameras
        ],
        "recent_failures": recent_camera_failures(db, company_id, limit=10),
    }


def recent_camera_failures(db: Session, company_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    recent_audits = list(
        db.scalars(
            select(AuditLog)
            .where(
                AuditLog.company_id == company_id,
                AuditLog.target_type == "ip_camera",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(max(limit * 4, limit))
        )
    )
    filtered_audits = [audit for audit in recent_audits if audit.action in CAMERA_FAILURE_ACTIONS][:limit]
    camera_ids = sorted(
        {
            int(audit.target_id)
            for audit in filtered_audits
            if str(audit.target_id or "").isdigit()
        }
    )
    cameras_by_id = {
        camera.id: camera
        for camera in db.scalars(select(IPCamera).where(IPCamera.company_id == company_id, IPCamera.id.in_(camera_ids)))
    } if camera_ids else {}
    return [
        {
            "camera_id": int(audit.target_id) if str(audit.target_id or "").isdigit() else None,
            "camera_name": cameras_by_id.get(int(audit.target_id)).name
            if str(audit.target_id or "").isdigit() and int(audit.target_id) in cameras_by_id
            else f"Camera {audit.target_id}",
            "action": audit.action,
            "created_at": audit.created_at.isoformat() if audit.created_at else None,
            "error": (audit.detail_json or {}).get("error") if isinstance(audit.detail_json, dict) else None,
            "detail": dict(audit.detail_json) if isinstance(audit.detail_json, dict) else {},
        }
        for audit in filtered_audits
    ]


def _enabled_rtsp_cameras(db: Session) -> list[IPCamera]:
    return list(
        db.scalars(
            select(IPCamera)
            .where(
                IPCamera.is_enabled.is_(True),
                IPCamera.approval_status == CameraApprovalStatus.approved,
                IPCamera.protocol == CameraProtocol.rtsp,
            )
            .order_by(IPCamera.id.asc())
        )
    )


def poll_enabled_rtsp_cameras(session_maker: sessionmaker[Session], app_settings: Settings) -> dict[str, int]:
    with session_maker() as db:
        cameras = _enabled_rtsp_cameras(db)

    summary = {"checked": len(cameras), "queued": 0, "failed": 0}
    for camera in cameras:
        clip_path: Path | None = None
        checked_at = utc_now()
        try:
            clip_path = record_rtsp_camera_clip(camera, app_settings)
            with session_maker() as db:
                live_camera = db.get(IPCamera, camera.id)
                if live_camera is None:
                    continue
                asset = ingest_local_media_file(
                    db,
                    app_settings=app_settings,
                    source_path=clip_path,
                    company_id=live_camera.company_id,
                    tenant_id=live_camera.company_id,
                    uploaded_by_user_id=None,
                    media_type=MediaType.video,
                    source="ip_camera",
                    original_file_name=clip_path.name,
                    metadata_json={
                        "camera_id": live_camera.id,
                        "camera_name": live_camera.name,
                        "stream_url": live_camera.stream_url,
                        "protocol": live_camera.protocol.value,
                        "capture_mode": "rtsp_pull",
                    },
                )
                _mark_camera_success(live_camera, checked_at=checked_at)
                db.add(live_camera)
                log_audit(
                    db,
                    action="ip_camera_rtsp_clip_queued",
                    target_type="ip_camera",
                    target_id=str(live_camera.id),
                    tenant_id=live_camera.company_id,
                    company_id=live_camera.company_id,
                    detail_json={"asset_id": asset.asset_id, "source": asset.source},
                )
                db.commit()
                summary["queued"] += 1
            logger.info("ip_camera_rtsp_clip_queued", camera_id=camera.id, company_id=camera.company_id)
        except Exception as exc:
            with session_maker() as db:
                live_camera = db.get(IPCamera, camera.id)
                if live_camera is not None:
                    _mark_camera_failure(
                        db,
                        camera=live_camera,
                        app_settings=app_settings,
                        error=str(exc),
                        checked_at=checked_at,
                    )
                    log_audit(
                        db,
                        action="ip_camera_rtsp_capture_failed",
                        target_type="ip_camera",
                        target_id=str(live_camera.id),
                        tenant_id=live_camera.company_id,
                        company_id=live_camera.company_id,
                        detail_json={"error": str(exc)},
                    )
                    db.commit()
            summary["failed"] += 1
            logger.warning("ip_camera_rtsp_capture_failed", camera_id=camera.id, error=str(exc))
        finally:
            if clip_path is not None:
                clip_path.unlink(missing_ok=True)
    logger.info("ip_camera_poll_completed", **summary)
    return summary
