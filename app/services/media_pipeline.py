from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import shutil
import subprocess
import threading
import re
from datetime import timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import utc_now
from app.models import AIAnalysisStatus, AIAnalysisType, MediaAsset, MediaAssetStatus, MediaType, Photo, PhotoType
from app.services.ai_pipeline import (
    AIBackendError,
    _analysis_created_by,
    _build_backend_order,
    _create_ai_analysis_log,
    _normalize_analysis_snapshot,
    _reprioritize_backends_for_dynamic_fallback,
    build_media_asset_ai_prompt,
    ensure_ai_summary_translations,
    list_media_asset_ai_logs,
    merge_active_ai_analysis_logs,
    resolve_ai_backends_for_tenant,
    serialize_ai_analysis_log,
    sync_ai_runtime_settings,
    call_ai_backend,
)
from app.services.audit import log_audit
from app.services.job_queue import enqueue_video_asset_task, schedule_job_worker
from app.services.photos import sanitize_component
from app.services.settings import get_system_settings

try:  # pragma: no cover - optional dependency during local test runs
    import cv2
except ImportError:  # pragma: no cover - optional dependency during local test runs
    cv2 = None


logger = get_logger("kkfieldlogger.media_pipeline")

VIDEO_FRAME_FPS = 1
MIN_VIDEO_KEYFRAMES = 4
MAX_VIDEO_KEYFRAMES = 8
DEFAULT_VIDEO_RETENTION_DAYS = 30
DEFAULT_HOT_STORAGE_DAYS = 7
DEFAULT_STORAGE_COST_PER_GB_MONTH = 0.12
DEFAULT_STORAGE_WARNING_THRESHOLD_PERCENT = 80.0
DEFAULT_EXPIRING_SOON_WINDOW_DAYS = 7
STORAGE_WARNING_LOG_INTERVAL_SECONDS = 300.0

_STORAGE_WARNING_LOCK = threading.Lock()
_LAST_STORAGE_WARNING_BY_COMPANY: dict[str, float] = {}


def _coerce_media_type(value: str) -> MediaType:
    try:
        return MediaType(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError("Unsupported media_type. Use image or video.") from exc


def _asset_storage_dir(app_settings: Settings, media_type: MediaType, company_id: str, asset_id: str) -> Path:
    return app_settings.media_assets_root / media_type.value / company_id / asset_id


def _chunk_dir(app_settings: Settings, asset_id: str) -> Path:
    return app_settings.media_upload_chunks_root / asset_id


def _manifest_path(chunk_dir: Path) -> Path:
    return chunk_dir / "manifest.json"


def _load_manifest(chunk_dir: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(chunk_dir)
    if not manifest_path.is_file():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_manifest(chunk_dir: Path, payload: dict[str, Any]) -> None:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    _manifest_path(chunk_dir).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_metadata_json(raw_value: str | None) -> dict[str, Any]:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return {}
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("metadata_json must be a JSON object")
    return parsed


def _received_chunk_indexes(chunk_dir: Path) -> list[int]:
    indexes: list[int] = []
    for path in chunk_dir.glob("*.part"):
        stem = path.stem
        if stem.isdigit():
            indexes.append(int(stem))
    return sorted(indexes)


def save_media_upload_chunk(
    app_settings: Settings,
    *,
    asset_id: str,
    chunk_index: int,
    total_chunks: int,
    original_file_name: str,
    media_type: MediaType,
    source: str,
    mime_type: str | None,
    metadata_json: dict[str, Any],
    payload: bytes,
) -> dict[str, Any]:
    if total_chunks <= 0:
        raise ValueError("total_chunks must be greater than zero")
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise ValueError("chunk_index is out of range")
    if not payload:
        raise ValueError("Chunk payload is empty")

    chunk_dir = _chunk_dir(app_settings, asset_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunk_dir / f"{chunk_index:08d}.part"
    chunk_path.write_bytes(payload)

    manifest = _load_manifest(chunk_dir)
    manifest.update(
        {
            "asset_id": asset_id,
            "total_chunks": total_chunks,
            "original_file_name": original_file_name,
            "media_type": media_type.value,
            "source": source,
            "mime_type": mime_type,
            "metadata_json": metadata_json,
        }
    )
    _write_manifest(chunk_dir, manifest)

    received_indexes = _received_chunk_indexes(chunk_dir)
    return {
        "asset_id": asset_id,
        "received_chunks": len(received_indexes),
        "received_indexes": received_indexes,
        "total_chunks": total_chunks,
        "complete": len(received_indexes) >= total_chunks,
    }


def _assemble_media_chunks(
    app_settings: Settings,
    *,
    asset_id: str,
    media_type: MediaType,
    company_id: str,
    original_file_name: str,
) -> tuple[Path, bytes]:
    chunk_dir = _chunk_dir(app_settings, asset_id)
    manifest = _load_manifest(chunk_dir)
    total_chunks = int(manifest.get("total_chunks") or 0)
    received_indexes = _received_chunk_indexes(chunk_dir)
    if total_chunks <= 0 or len(received_indexes) < total_chunks:
        raise RuntimeError("Upload is incomplete")

    safe_name = sanitize_component(original_file_name)
    destination_dir = _asset_storage_dir(app_settings, media_type, company_id, asset_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / safe_name

    with destination_path.open("wb") as output:
        for chunk_index in range(total_chunks):
            chunk_path = chunk_dir / f"{chunk_index:08d}.part"
            if not chunk_path.is_file():
                raise RuntimeError(f"Missing chunk {chunk_index}")
            output.write(chunk_path.read_bytes())

    body = destination_path.read_bytes()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    return destination_path, body


def _read_video_duration_seconds(video_path: Path) -> float | None:
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None:
        return None
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "").strip()
    try:
        return round(float(output), 2) if output else None
    except ValueError:
        return None


def extract_video_frames(video_path: Path, output_dir: Path, *, fps: int = VIDEO_FRAME_FPS) -> list[Path]:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH")

    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = output_dir / "frame_%05d.jpg"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps}",
        str(output_pattern),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg failed").strip())
    return sorted(output_dir.glob("frame_*.jpg"))


def _evenly_sample_paths(paths: list[Path], max_items: int) -> list[Path]:
    if len(paths) <= max_items:
        return paths
    if max_items <= 1:
        return [paths[0]]
    indexes = {round(index * (len(paths) - 1) / (max_items - 1)) for index in range(max_items)}
    return [path for index, path in enumerate(paths) if index in indexes]


def select_representative_video_frames(
    frame_paths: list[Path],
    *,
    min_frames: int = MIN_VIDEO_KEYFRAMES,
    max_frames: int = MAX_VIDEO_KEYFRAMES,
) -> list[Path]:
    ordered_paths = sorted(frame_paths)
    if not ordered_paths:
        return []
    if cv2 is None:
        return _evenly_sample_paths(ordered_paths, min(max_frames, len(ordered_paths)))

    candidates: list[tuple[Path, float]] = []
    reserve: list[tuple[Path, float]] = []
    seen_signatures: list[Any] = []
    for path in ordered_paths:
        frame = cv2.imread(str(path))
        if frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        thumbnail = cv2.resize(gray, (24, 24), interpolation=cv2.INTER_AREA)
        is_duplicate = False
        for previous in seen_signatures:
            difference = float(cv2.norm(thumbnail, previous, cv2.NORM_L1)) / float(thumbnail.size)
            if difference < 2.0:
                is_duplicate = True
                break
        reserve.append((path, blur_score))
        if blur_score < 20.0 or is_duplicate:
            continue
        seen_signatures.append(thumbnail)
        candidates.append((path, blur_score))

    if len(candidates) < min_frames:
        ranked_reserve = sorted(reserve, key=lambda item: item[1], reverse=True)
        selected_lookup = {path for path, _ in candidates}
        for path, blur_score in ranked_reserve:
            if path in selected_lookup:
                continue
            candidates.append((path, blur_score))
            selected_lookup.add(path)
            if len(candidates) >= min(min_frames, len(ordered_paths)):
                break

    candidates.sort(key=lambda item: ordered_paths.index(item[0]))
    selected_paths = [path for path, _ in candidates]
    return _evenly_sample_paths(selected_paths, min(max_frames, len(selected_paths)))


def _frame_time_seconds(frame_path: Path) -> float:
    stem = frame_path.stem.split("_")[-1]
    if stem.isdigit():
        return float(max(0, int(stem) - 1))
    return 0.0


def _image_mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/jpeg"


def _guess_mime_type(path: Path, explicit_mime_type: str | None = None) -> str | None:
    return explicit_mime_type or mimetypes.guess_type(path.name)[0]


def _coerce_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _coerce_non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _human_size_label(byte_count: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, int(byte_count)))
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def _preview_root(asset_id: str, app_settings: Settings) -> Path:
    return app_settings.media_frames_root / asset_id / "preview"


def _working_frame_root(asset_id: str, app_settings: Settings) -> Path:
    return app_settings.media_frames_root / asset_id / "working"


def _linked_legacy_photo(db: Session, asset: MediaAsset) -> Photo | None:
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    legacy_photo_id = metadata_json.get("legacy_photo_id")
    try:
        resolved_photo_id = int(legacy_photo_id)
    except (TypeError, ValueError):
        return None
    return db.get(Photo, resolved_photo_id)


def _sync_legacy_photo_with_media_asset(
    db: Session,
    asset: MediaAsset,
    *,
    completed: bool,
    error_message: str | None = None,
) -> None:
    photo = _linked_legacy_photo(db, asset)
    if photo is None:
        return
    metadata_json = dict(photo.metadata_json) if isinstance(photo.metadata_json, dict) else {}
    metadata_json["media_asset_id"] = asset.asset_id
    metadata_json["media_kind"] = "video"
    if asset.duration_seconds is not None:
        metadata_json["duration_seconds"] = asset.duration_seconds
    photo.metadata_json = metadata_json
    if completed:
        asset_metadata = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
        ai_snapshot = asset_metadata.get("ai_snapshot")
        photo.tag_json = ai_snapshot if isinstance(ai_snapshot, dict) else {"ai_summary": None, "labels": [], "defects": []}
        photo.labeling_status = "completed"
    else:
        photo.tag_json = {
            "ai_summary": None,
            "labels": [],
            "defects": [],
            "error": {
                "code": "ai_processing_failed",
                "message": error_message or "Video AI processing failed",
                "attempts": [],
            },
        }
        photo.labeling_status = "failed"
    db.add(photo)


def _format_timestamp_label(total_seconds: float | int) -> str:
    whole_seconds = max(0, int(round(float(total_seconds or 0.0))))
    minutes, seconds = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def get_video_storage_policy(db: Session, app_settings: Settings) -> dict[str, Any]:
    system_settings = get_system_settings(db, app_settings)
    retention_days = _coerce_non_negative_int(
        system_settings.get("video_retention_days"),
        DEFAULT_VIDEO_RETENTION_DAYS,
    )
    hot_storage_days = _coerce_non_negative_int(
        system_settings.get("hot_storage_days"),
        DEFAULT_HOT_STORAGE_DAYS,
    )
    if retention_days > 0 and hot_storage_days > retention_days:
        hot_storage_days = retention_days
    return {
        "video_retention_days": retention_days,
        "hot_storage_days": hot_storage_days,
        "video_delete_files_only": _coerce_bool(system_settings.get("video_delete_files_only"), True),
    }


def get_media_storage_monitor(
    db: Session,
    app_settings: Settings,
    *,
    company_id: str,
    storage_limit_mb: int | None = None,
) -> dict[str, Any]:
    system_settings = get_system_settings(db, app_settings)
    policy = get_video_storage_policy(db, app_settings)
    cost_per_gb_month = _coerce_non_negative_float(
        system_settings.get("storage_cost_per_gb_month"),
        DEFAULT_STORAGE_COST_PER_GB_MONTH,
    )
    warning_threshold_percent = _coerce_non_negative_float(
        system_settings.get("storage_warning_threshold_percent"),
        DEFAULT_STORAGE_WARNING_THRESHOLD_PERCENT,
    ) or DEFAULT_STORAGE_WARNING_THRESHOLD_PERCENT
    expiring_window_days = max(1, min(DEFAULT_EXPIRING_SOON_WINDOW_DAYS, policy["video_retention_days"] or DEFAULT_EXPIRING_SOON_WINDOW_DAYS))
    assets = list(
        db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.company_id == company_id,
                MediaAsset.status != MediaAssetStatus.deleted,
            )
            .order_by(MediaAsset.created_at.desc())
        )
    )
    now = utc_now()
    thirty_days_ago = now - timedelta(days=30)

    total_media_bytes = 0
    total_video_bytes = 0
    expiring_soon_count = 0
    recent_video_bytes = 0
    for asset in assets:
        file_size = _asset_size_bytes(asset)
        total_media_bytes += file_size
        if asset.media_type != MediaType.video:
            continue
        total_video_bytes += file_size
        reference_time = _ensure_utc_datetime(_asset_reference_time(asset))
        if reference_time >= thirty_days_ago:
            recent_video_bytes += file_size
        if (
            policy["video_retention_days"] > 0
            and asset.status not in {MediaAssetStatus.processing, MediaAssetStatus.uploading}
        ):
            days_remaining = float(policy["video_retention_days"]) - _asset_age_days(asset, now=now)
            if 0.0 <= days_remaining <= float(expiring_window_days):
                expiring_soon_count += 1

    average_daily_video_bytes = recent_video_bytes / 30.0 if recent_video_bytes > 0 else float(total_video_bytes) / max(1.0, float(policy["video_retention_days"] or 30))
    projected_next_month_video_bytes = max(
        float(total_video_bytes),
        average_daily_video_bytes * max(1, min(policy["video_retention_days"] or 30, 30)),
    )
    estimated_next_month_cost = round((projected_next_month_video_bytes / float(1024 ** 3)) * cost_per_gb_month, 2)

    storage_limit_bytes = max(0, int(storage_limit_mb or 0)) * 1024 * 1024
    usage_rate_percent = round((total_media_bytes / storage_limit_bytes) * 100, 2) if storage_limit_bytes > 0 else 0.0
    warning = storage_limit_bytes > 0 and usage_rate_percent >= warning_threshold_percent
    days_until_full: int | None = None
    if storage_limit_bytes > 0 and average_daily_video_bytes > 0 and total_media_bytes < storage_limit_bytes:
        days_until_full = max(0, int((storage_limit_bytes - total_media_bytes) / average_daily_video_bytes))

    summary = {
        "company_id": company_id,
        "total_media_bytes": total_media_bytes,
        "total_media_label": _human_size_label(total_media_bytes),
        "total_video_bytes": total_video_bytes,
        "total_video_label": _human_size_label(total_video_bytes),
        "storage_limit_mb": int(storage_limit_mb or 0),
        "storage_limit_label": _human_size_label(storage_limit_bytes) if storage_limit_bytes > 0 else None,
        "usage_rate_percent": usage_rate_percent,
        "warning_threshold_percent": warning_threshold_percent,
        "warning": warning,
        "estimated_next_month_cost": estimated_next_month_cost,
        "estimated_next_month_cost_label": f"${estimated_next_month_cost:.2f}",
        "cost_per_gb_month": cost_per_gb_month,
        "cost_per_gb_month_label": f"${cost_per_gb_month:.2f}/GB-month",
        "expiring_soon_count": expiring_soon_count,
        "expiring_soon_window_days": expiring_window_days,
        "video_retention_days": policy["video_retention_days"],
        "hot_storage_days": policy["hot_storage_days"],
        "days_until_full": days_until_full,
    }
    if warning:
        _emit_storage_warning(summary)
    return summary


def media_asset_preview_filenames(asset: MediaAsset) -> list[str]:
    metadata_json = _metadata_dict(asset)
    raw_frames = metadata_json.get("preview_frames")
    if not isinstance(raw_frames, list):
        return []
    preview_names: list[str] = []
    for item in raw_frames:
        candidate = Path(str(item or "")).name
        if candidate:
            preview_names.append(candidate)
    return preview_names


def media_asset_thumbnail_filename(asset: MediaAsset) -> str | None:
    metadata_json = _metadata_dict(asset)
    thumbnail_name = Path(str(metadata_json.get("thumbnail_frame") or "")).name
    if thumbnail_name:
        return thumbnail_name
    preview_frames = media_asset_preview_filenames(asset)
    return preview_frames[0] if preview_frames else None


def media_asset_timeline_markers(asset: MediaAsset) -> list[dict[str, Any]]:
    metadata_json = _metadata_dict(asset)
    raw_markers = metadata_json.get("timeline_markers")
    if isinstance(raw_markers, list):
        markers: list[dict[str, Any]] = []
        for marker in raw_markers:
            if not isinstance(marker, dict):
                continue
            markers.append(dict(marker))
        return markers
    return []


def resolve_media_asset_file_path(asset: MediaAsset) -> Path | None:
    file_path = Path(asset.file_path) if asset.file_path else None
    if file_path is None or not file_path.is_file():
        return None
    return file_path


def resolve_media_preview_frame_path(app_settings: Settings, asset: MediaAsset, frame_name: str) -> Path | None:
    safe_name = Path(frame_name).name
    if safe_name != frame_name or not safe_name:
        return None
    preview_path = _preview_root(asset.asset_id, app_settings) / safe_name
    if not preview_path.is_file():
        return None
    return preview_path


def _copy_file_into_asset_storage(
    app_settings: Settings,
    *,
    media_type: MediaType,
    company_id: str,
    asset_id: str,
    source_path: Path,
    original_file_name: str,
) -> Path:
    destination_dir = _asset_storage_dir(app_settings, media_type, company_id, asset_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / sanitize_component(original_file_name)
    shutil.copy2(source_path, destination_path)
    return destination_path


def _cold_storage_path(app_settings: Settings, asset: MediaAsset) -> Path:
    return app_settings.media_cold_storage_root / asset.company_id / asset.asset_id / sanitize_component(asset.original_file_name)


def _asset_reference_time(asset: MediaAsset):
    return asset.completed_at or asset.updated_at or asset.created_at


def _ensure_utc_datetime(value):
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _asset_age_days(asset: MediaAsset, *, now) -> float:
    reference_time = _ensure_utc_datetime(_asset_reference_time(asset))
    return max(0.0, (now - reference_time).total_seconds() / 86400.0)


def _metadata_dict(asset: MediaAsset) -> dict[str, Any]:
    return dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}


def _asset_size_bytes(asset: MediaAsset) -> int:
    if int(asset.file_size or 0) > 0:
        return int(asset.file_size or 0)
    file_path = Path(asset.file_path) if asset.file_path else None
    if file_path is not None and file_path.is_file():
        return int(file_path.stat().st_size)
    return 0


def _emit_storage_warning(summary: dict[str, Any]) -> None:
    company_id = str(summary.get("company_id") or "")
    if not company_id:
        return
    now = monotonic()
    with _STORAGE_WARNING_LOCK:
        last_warning_at = _LAST_STORAGE_WARNING_BY_COMPANY.get(company_id, 0.0)
        if now - last_warning_at < STORAGE_WARNING_LOG_INTERVAL_SECONDS:
            return
        _LAST_STORAGE_WARNING_BY_COMPANY[company_id] = now
    logger.warning(
        "media_storage_usage_warning",
        company_id=company_id,
        usage_rate_percent=summary.get("usage_rate_percent"),
        warning_threshold_percent=summary.get("warning_threshold_percent"),
        total_media_bytes=summary.get("total_media_bytes"),
        storage_limit_mb=summary.get("storage_limit_mb"),
        expiring_soon_count=summary.get("expiring_soon_count"),
    )


def _persist_preview_frames(app_settings: Settings, *, asset_id: str, selected_frames: list[Path]) -> list[str]:
    preview_root = _preview_root(asset_id, app_settings)
    if preview_root.exists():
        shutil.rmtree(preview_root, ignore_errors=True)
    preview_root.mkdir(parents=True, exist_ok=True)
    preview_names: list[str] = []
    for preview_index, source_path in enumerate(selected_frames, start=1):
        preview_name = f"preview_{preview_index:02d}{source_path.suffix.lower() or '.jpg'}"
        destination_path = preview_root / preview_name
        shutil.copy2(source_path, destination_path)
        preview_names.append(preview_name)
    return preview_names


def _timeline_markers_from_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for log in logs:
        result_data = log.get("result_data")
        if not isinstance(result_data, dict):
            continue
        defects = [str(item).strip() for item in (result_data.get("defects") or []) if str(item).strip()]
        time_seconds = float(result_data.get("frame_second") or 0.0)
        summary = str(result_data.get("ai_summary") or "").strip()
        label = ", ".join(defects[:2]) if defects else summary or f"Frame {result_data.get('frame_index') or '?'}"
        markers.append(
            {
                "time_seconds": round(time_seconds, 2),
                "time_label": _format_timestamp_label(time_seconds),
                "label": label,
                "summary": summary,
                "defects": defects,
                "has_defect": bool(defects),
                "frame_index": result_data.get("frame_index"),
            }
        )
    return markers


def cleanup_expired_media_assets(session_maker: sessionmaker[Session], app_settings: Settings) -> dict[str, int]:
    summary = {"checked": 0, "archived": 0, "deleted": 0, "failed": 0}
    with session_maker() as db:
        policy = get_video_storage_policy(db, app_settings)
        asset_ids = [
            asset_id
            for asset_id in db.scalars(
                select(MediaAsset.asset_id).where(
                    MediaAsset.media_type == MediaType.video,
                    MediaAsset.status.notin_([MediaAssetStatus.uploading, MediaAssetStatus.processing]),
                )
            )
        ]

    if not asset_ids:
        logger.info("media_asset_cleanup_skipped", reason="no_video_assets")
        return summary

    now = utc_now()
    for asset_id in asset_ids:
        with session_maker() as db:
            asset = db.get(MediaAsset, asset_id)
            if asset is None or asset.media_type != MediaType.video:
                continue
            summary["checked"] += 1
            metadata_json = _metadata_dict(asset)
            file_path = Path(asset.file_path) if asset.file_path else None
            preview_root = _preview_root(asset.asset_id, app_settings)
            asset_age_days = _asset_age_days(asset, now=now)
            try:
                should_delete = (
                    policy["video_retention_days"] > 0
                    and asset_age_days >= float(policy["video_retention_days"])
                    and asset.status != MediaAssetStatus.deleted
                )
                should_archive = (
                    policy["hot_storage_days"] > 0
                    and asset_age_days >= float(policy["hot_storage_days"])
                    and asset.status not in {MediaAssetStatus.archived, MediaAssetStatus.deleted}
                )

                if should_delete:
                    if file_path is not None and file_path.is_file():
                        file_path.unlink(missing_ok=True)
                    shutil.rmtree(preview_root, ignore_errors=True)
                    metadata_json["storage_tier"] = "deleted"
                    metadata_json["deleted_at"] = now.isoformat()
                    metadata_json["deleted_file_path"] = str(file_path) if file_path is not None else asset.file_path
                    asset.metadata_json = metadata_json
                    asset.status = MediaAssetStatus.deleted
                    if not policy["video_delete_files_only"]:
                        asset.file_path = ""
                    db.add(asset)
                    log_audit(
                        db,
                        action="media_asset_deleted_by_retention",
                        target_type="media_asset",
                        target_id=asset.asset_id,
                        actor_user_id=asset.uploaded_by_user_id,
                        tenant_id=asset.tenant_id or asset.company_id,
                        company_id=asset.company_id,
                        detail_json={
                            "video_retention_days": policy["video_retention_days"],
                            "delete_files_only": policy["video_delete_files_only"],
                            "asset_age_days": round(asset_age_days, 2),
                        },
                    )
                    db.commit()
                    summary["deleted"] += 1
                    logger.info(
                        "media_asset_deleted_by_retention",
                        asset_id=asset.asset_id,
                        company_id=asset.company_id,
                        asset_age_days=round(asset_age_days, 2),
                        delete_files_only=policy["video_delete_files_only"],
                    )
                    continue

                if should_archive:
                    cold_path = _cold_storage_path(app_settings, asset)
                    cold_path.parent.mkdir(parents=True, exist_ok=True)
                    if file_path is not None and file_path.is_file():
                        shutil.move(str(file_path), str(cold_path))
                        asset.file_path = str(cold_path)
                    metadata_json["storage_tier"] = "cold"
                    metadata_json["archived_at"] = now.isoformat()
                    asset.metadata_json = metadata_json
                    asset.status = MediaAssetStatus.archived
                    db.add(asset)
                    log_audit(
                        db,
                        action="media_asset_archived_to_cold_storage",
                        target_type="media_asset",
                        target_id=asset.asset_id,
                        actor_user_id=asset.uploaded_by_user_id,
                        tenant_id=asset.tenant_id or asset.company_id,
                        company_id=asset.company_id,
                        detail_json={
                            "hot_storage_days": policy["hot_storage_days"],
                            "asset_age_days": round(asset_age_days, 2),
                            "cold_storage_path": asset.file_path,
                        },
                    )
                    db.commit()
                    summary["archived"] += 1
                    logger.info(
                        "media_asset_archived_to_cold_storage",
                        asset_id=asset.asset_id,
                        company_id=asset.company_id,
                        asset_age_days=round(asset_age_days, 2),
                        cold_storage_path=asset.file_path,
                    )
            except Exception as exc:
                db.rollback()
                summary["failed"] += 1
                logger.warning(
                    "media_asset_cleanup_failed",
                    asset_id=asset.asset_id,
                    company_id=asset.company_id,
                    error=str(exc),
                )
    logger.info("media_asset_cleanup_completed", **summary)
    return summary


def _rebuild_media_asset_ai_snapshot(db: Session, asset: MediaAsset) -> dict[str, Any]:
    active_logs = [
        log
        for log in list_media_asset_ai_logs(db, asset.asset_id)
        if log.status == AIAnalysisStatus.active
    ]
    merged_snapshot = merge_active_ai_analysis_logs(active_logs)
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    metadata_json["ai_snapshot"] = merged_snapshot
    metadata_json["active_analysis_log_count"] = len(active_logs)
    asset.metadata_json = metadata_json
    db.add(asset)
    db.flush()
    return merged_snapshot


def _analyze_video_frame(
    db: Session,
    *,
    app_settings: Settings,
    asset: MediaAsset,
    frame_path: Path,
    frame_index: int,
    custom_prompt: str | None,
) -> AIAnalysisLog:
    sync_ai_runtime_settings(db, app_settings)
    backends, config_source = resolve_ai_backends_for_tenant(db, app_settings, asset.tenant_id or asset.company_id)
    if not backends:
        raise RuntimeError("No enabled AI backends are configured")

    frame_bytes = frame_path.read_bytes()
    prompt = build_media_asset_ai_prompt(
        db,
        asset,
        photo_type=PhotoType.project,
        custom_prompt=custom_prompt,
    )

    selected_backend = None
    structured_result: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    remaining_backends = list(_build_backend_order(backends))
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            structured_result = call_ai_backend(
                backend,
                prompt=prompt,
                image_base64=base64.b64encode(frame_bytes).decode("ascii"),
                mime_type=_image_mime_type(frame_path),
            )
            selected_backend = backend
            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "success"})
            break
        except AIBackendError as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": exc.message,
                }
            )
            logger.warning(
                "video_ai_backend_failed",
                asset_id=asset.asset_id,
                frame_index=frame_index,
                backend_id=backend.id,
                backend_type=backend.type,
                config_source=config_source,
                error=exc.message,
            )
            remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                remaining_backends,
                failed_backend=backend,
                operation="video_frame",
                error_message=exc.message,
            )

    if structured_result is None or selected_backend is None:
        raise RuntimeError("All configured AI backends failed for video frame")

    normalized_result = ensure_ai_summary_translations(
        db,
        app_settings,
        tenant_slug=asset.tenant_id or asset.company_id,
        payload=structured_result,
    )
    result_payload = {
        **normalized_result,
        "frame_index": frame_index,
        "frame_second": _frame_time_seconds(frame_path),
        "frame_file_name": frame_path.name,
        "attempts": attempts,
    }
    analysis_log = _create_ai_analysis_log(
        db,
        media_asset_id=asset.asset_id,
        batch_id=asset.asset_id,
        analysis_type=AIAnalysisType.video_insight,
        prompt_used=prompt,
        model_used=f"{selected_backend.type}:{selected_backend.model}",
        result_data=result_payload,
        created_by=_analysis_created_by(asset.uploaded_by_user_id),
    )
    logger.info(
        "video_ai_frame_completed",
        asset_id=asset.asset_id,
        frame_index=frame_index,
        frame_second=result_payload["frame_second"],
        backend_id=selected_backend.id,
        backend_type=selected_backend.type,
    )
    return analysis_log


def create_media_asset_from_chunks(
    db: Session,
    *,
    app_settings: Settings,
    asset_id: str,
    company_id: str,
    tenant_id: str | None,
    uploaded_by_user_id: int | None,
    media_type: MediaType,
    source: str,
    original_file_name: str,
    mime_type: str | None,
    metadata_json: dict[str, Any],
) -> MediaAsset:
    destination_path, body = _assemble_media_chunks(
        app_settings,
        asset_id=asset_id,
        media_type=media_type,
        company_id=company_id,
        original_file_name=original_file_name,
    )
    asset = MediaAsset(
        asset_id=asset_id,
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        uploaded_by_user_id=uploaded_by_user_id,
        media_type=media_type,
        source=source,
        file_path=str(destination_path),
        original_file_name=original_file_name,
        mime_type=mime_type or mimetypes.guess_type(destination_path.name)[0],
        file_size=len(body),
        checksum=hashlib.sha256(body).hexdigest(),
        duration_seconds=_read_video_duration_seconds(destination_path) if media_type == MediaType.video else None,
        status=MediaAssetStatus.processing if media_type == MediaType.video else MediaAssetStatus.completed,
        metadata_json=metadata_json,
        completed_at=utc_now() if media_type == MediaType.image else None,
    )
    db.add(asset)
    db.flush()
    return asset


def create_media_asset_from_local_file(
    db: Session,
    *,
    app_settings: Settings,
    source_path: Path,
    company_id: str,
    tenant_id: str | None,
    uploaded_by_user_id: int | None,
    media_type: MediaType,
    source: str,
    original_file_name: str | None = None,
    mime_type: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    asset_id: str | None = None,
) -> MediaAsset:
    resolved_asset_id = next_media_asset_id(asset_id)
    resolved_original_name = original_file_name or source_path.name
    destination_path = _copy_file_into_asset_storage(
        app_settings,
        media_type=media_type,
        company_id=company_id,
        asset_id=resolved_asset_id,
        source_path=source_path,
        original_file_name=resolved_original_name,
    )
    file_size = destination_path.stat().st_size if destination_path.exists() else 0
    checksum = hashlib.sha256(destination_path.read_bytes()).hexdigest()
    asset = MediaAsset(
        asset_id=resolved_asset_id,
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        uploaded_by_user_id=uploaded_by_user_id,
        media_type=media_type,
        source=source,
        file_path=str(destination_path),
        original_file_name=resolved_original_name,
        mime_type=_guess_mime_type(destination_path, mime_type),
        file_size=file_size,
        checksum=checksum,
        duration_seconds=_read_video_duration_seconds(destination_path) if media_type == MediaType.video else None,
        status=MediaAssetStatus.processing if media_type == MediaType.video else MediaAssetStatus.completed,
        metadata_json=metadata_json or {},
        completed_at=utc_now() if media_type == MediaType.image else None,
    )
    db.add(asset)
    db.flush()
    return asset


def queue_media_asset_processing(
    db: Session,
    *,
    app_settings: Settings,
    asset: MediaAsset,
    schedule_task=None,
    session_maker: sessionmaker[Session] | None = None,
) -> None:
    if asset.media_type != MediaType.video:
        return
    enqueue_video_asset_task(
        db,
        app_settings=app_settings,
        company_id=asset.company_id,
        tenant_id=asset.tenant_id,
        asset_id=asset.asset_id,
        actor_user_id=asset.uploaded_by_user_id,
        source=asset.source,
    )
    if schedule_task is not None and session_maker is not None:
        schedule_job_worker(schedule_task, session_maker, app_settings)


def ingest_local_media_file(
    db: Session,
    *,
    app_settings: Settings,
    source_path: Path,
    company_id: str,
    tenant_id: str | None,
    uploaded_by_user_id: int | None,
    media_type: MediaType,
    source: str,
    original_file_name: str | None = None,
    mime_type: str | None = None,
    metadata_json: dict[str, Any] | None = None,
    asset_id: str | None = None,
    queue_processing: bool = True,
) -> MediaAsset:
    asset = create_media_asset_from_local_file(
        db,
        app_settings=app_settings,
        source_path=source_path,
        company_id=company_id,
        tenant_id=tenant_id,
        uploaded_by_user_id=uploaded_by_user_id,
        media_type=media_type,
        source=source,
        original_file_name=original_file_name,
        mime_type=mime_type,
        metadata_json=metadata_json,
        asset_id=asset_id,
    )
    if queue_processing:
        queue_media_asset_processing(db, app_settings=app_settings, asset=asset)
    return asset


def process_media_asset(
    asset_id: str,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    raise_on_failure: bool = False,
) -> None:
    frame_output_dir = _working_frame_root(asset_id, app_settings)
    try:
        with session_maker() as db:
            asset = db.get(MediaAsset, asset_id)
            if asset is None:
                logger.warning("media_asset_processing_skipped", asset_id=asset_id, reason="missing_asset")
                return
            if asset.media_type != MediaType.video:
                asset.status = MediaAssetStatus.completed
                asset.completed_at = utc_now()
                db.add(asset)
                db.commit()
                return
            asset.status = MediaAssetStatus.processing
            asset.error_message = None
            db.add(asset)
            db.commit()
            asset_path = Path(asset.file_path)

        extracted_frames = extract_video_frames(asset_path, frame_output_dir, fps=VIDEO_FRAME_FPS)
        selected_frames = select_representative_video_frames(extracted_frames)
        if not selected_frames:
            raise RuntimeError("No representative video frames were extracted")
        preview_frames = _persist_preview_frames(app_settings, asset_id=asset_id, selected_frames=selected_frames)

        with session_maker() as db:
            asset = db.get(MediaAsset, asset_id)
            if asset is None:
                raise RuntimeError("Media asset not found during processing")

            successful_logs: list[dict[str, Any]] = []
            for frame_index, frame_path in enumerate(selected_frames, start=1):
                analysis_log = _analyze_video_frame(
                    db,
                    app_settings=app_settings,
                    asset=asset,
                    frame_path=frame_path,
                    frame_index=frame_index,
                    custom_prompt=((asset.metadata_json or {}).get("custom_prompt") if isinstance(asset.metadata_json, dict) else None),
                )
                successful_logs.append(serialize_ai_analysis_log(analysis_log))

            merged_snapshot = _rebuild_media_asset_ai_snapshot(db, asset)
            timeline_markers = _timeline_markers_from_logs(successful_logs)
            metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
            metadata_json.update(
                {
                    "duration_seconds": asset.duration_seconds,
                    "extracted_frame_count": len(extracted_frames),
                    "selected_frame_count": len(selected_frames),
                    "selected_frames": [path.name for path in selected_frames],
                    "preview_frames": preview_frames,
                    "thumbnail_frame": preview_frames[0] if preview_frames else None,
                    "timeline_markers": timeline_markers,
                    "ai_logs": successful_logs,
                    "ai_snapshot": merged_snapshot,
                    "notification_message": f"Video analysis completed for asset {asset.asset_id}.",
                }
            )
            asset.metadata_json = metadata_json
            asset.status = MediaAssetStatus.completed
            asset.completed_at = utc_now()
            asset.error_message = None
            db.add(asset)
            _sync_legacy_photo_with_media_asset(db, asset, completed=True)
            log_audit(
                db,
                action="media_asset_processing_completed",
                target_type="media_asset",
                target_id=asset.asset_id,
                actor_user_id=asset.uploaded_by_user_id,
                tenant_id=asset.tenant_id or asset.company_id,
                company_id=asset.company_id,
                detail_json={
                    "source": asset.source,
                    "selected_frame_count": len(selected_frames),
                    "duration_seconds": asset.duration_seconds,
                    "notification_message": metadata_json["notification_message"],
                },
            )
            db.commit()
            logger.info(
                "media_asset_processing_completed",
                asset_id=asset.asset_id,
                selected_frame_count=len(selected_frames),
                extracted_frame_count=len(extracted_frames),
                duration_seconds=asset.duration_seconds,
            )
    except Exception as exc:
        with session_maker() as db:
            asset = db.get(MediaAsset, asset_id)
            if asset is not None:
                asset.status = MediaAssetStatus.failed
                asset.error_message = str(exc)
                db.add(asset)
                _sync_legacy_photo_with_media_asset(db, asset, completed=False, error_message=str(exc))
                log_audit(
                    db,
                    action="media_asset_processing_failed",
                    target_type="media_asset",
                    target_id=asset.asset_id,
                    actor_user_id=asset.uploaded_by_user_id,
                    tenant_id=asset.tenant_id or asset.company_id,
                    company_id=asset.company_id,
                    detail_json={"error": str(exc), "source": asset.source},
                )
                db.commit()
        logger.exception("media_asset_processing_failed", asset_id=asset_id, error=str(exc))
        if raise_on_failure:
            raise
    finally:
        shutil.rmtree(frame_output_dir, ignore_errors=True)


def next_media_asset_id(provided_asset_id: str | None = None) -> str:
    cleaned = (provided_asset_id or "").strip()
    if not cleaned:
        return str(uuid4())
    try:
        return str(UUID(cleaned))
    except ValueError:
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{7,63}", cleaned):
            return cleaned
    return str(uuid4())
