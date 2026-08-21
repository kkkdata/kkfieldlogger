from __future__ import annotations

import io
import re
import secrets
import hashlib
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request, UploadFile
from PIL import Image, ImageDraw, UnidentifiedImageError
import requests
from requests import RequestException
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import MobileApiError
from app.core.time import parse_utc_timestamp, utc_now
from app.models import (
    AIAnalysisLog,
    ApprovalStatus,
    Employee,
    EvidenceObservation,
    MediaAnnotation,
    MediaAsset,
    MediaAssetStatus,
    MediaType,
    Photo,
    PhotoComment,
    PhotoLensObservation,
    PhotoLensObservationRun,
    PhotoObservationPromotion,
    PhotoType,
    PhotoVisibility,
    Project,
    ReceiptFact,
    ReviewTask,
    TaskJob,
    User,
)
from app.schemas.mobile import PhotoOut
from app.services.audit import log_audit
from app.services.billing import enforce_upload_limit, record_upload_usage
from app.services.media_access import sign_media_url
from app.services.settings import get_system_settings

SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
VIDEO_MIME_PREFIX = "video/"
UNSET = object()
UNAVAILABLE_LOCATION_VALUES = {
    "",
    "-",
    "location unavailable",
    "unavailable",
    "unknown",
    "none",
    "null",
}
REVERSE_GEOCODE_URL = "https://nominatim.openstreetmap.org/reverse"
REVERSE_GEOCODE_TIMEOUT_SECONDS = (1.0, 1.5)
REVERSE_GEOCODE_CACHE: dict[tuple[float, float], str | None] = {}
REVERSE_GEOCODE_CACHE_LOCK = threading.Lock()
US_STATE_ABBREVIATIONS = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


def sanitize_component(value: str) -> str:
    cleaned = SAFE_FILENAME.sub("_", value.strip())
    return cleaned or "file"


def normalize_location_text(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    if cleaned.lower() in UNAVAILABLE_LOCATION_VALUES:
        return None
    return cleaned


def _format_reverse_geocode_location(address: dict[str, object]) -> str | None:
    city = next(
        (
            str(address.get(key) or "").strip()
            for key in ("city", "town", "village", "municipality", "hamlet", "suburb", "county")
            if str(address.get(key) or "").strip()
        ),
        "",
    )
    if not city:
        return None
    state = str(address.get("state") or address.get("region") or "").strip()
    country_code = str(address.get("country_code") or "").strip().upper()
    country = str(address.get("country") or "").strip()
    if country_code == "US":
        state = US_STATE_ABBREVIATIONS.get(state, state)
    parts = [city]
    if state and state.lower() != city.lower():
        parts.append(state)
    if country_code and country_code != "US":
        parts.append(country_code)
    elif country and not country_code:
        parts.append(country)
    return ", ".join(part for part in parts if part)[:255]


def reverse_geocode_city(gps_lat: float | None, gps_lon: float | None) -> str | None:
    if gps_lat is None or gps_lon is None:
        return None
    cache_key = (round(float(gps_lat), 4), round(float(gps_lon), 4))
    with REVERSE_GEOCODE_CACHE_LOCK:
        if cache_key in REVERSE_GEOCODE_CACHE:
            return REVERSE_GEOCODE_CACHE[cache_key]
    try:
        response = requests.get(
            REVERSE_GEOCODE_URL,
            params={
                "format": "jsonv2",
                "lat": f"{float(gps_lat):.6f}",
                "lon": f"{float(gps_lon):.6f}",
                "zoom": "10",
                "addressdetails": "1",
            },
            headers={"User-Agent": "KKFieldLogger/1.0 server-side-location"},
            timeout=REVERSE_GEOCODE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        location = _format_reverse_geocode_location(payload.get("address") or {})
    except (RequestException, ValueError, TypeError):
        location = None
    with REVERSE_GEOCODE_CACHE_LOCK:
        REVERSE_GEOCODE_CACHE[cache_key] = location
    return location


def resolve_upload_location(
    location: str | None,
    gps: str | None,
    gps_lat: float | None,
    gps_lon: float | None,
) -> str | None:
    if gps_lat is not None and gps_lon is not None:
        return reverse_geocode_city(gps_lat, gps_lon) or gps_location_fallback(gps, gps_lat, gps_lon)
    return normalize_location_text(location) or gps_location_fallback(gps, gps_lat, gps_lon)


def gps_location_fallback(gps: str | None, gps_lat: float | None, gps_lon: float | None) -> str | None:
    cleaned_gps = str(gps or "").strip()
    if cleaned_gps:
        return cleaned_gps
    if gps_lat is not None and gps_lon is not None:
        return f"{gps_lat:.6f},{gps_lon:.6f}"
    return None


def build_relative_image_url(
    photo_type: PhotoType,
    company_id: str,
    project_id: str | None,
    employee_id: str,
    filename: str,
) -> str:
    if photo_type == PhotoType.invoice:
        return f"/media/{company_id}/invoice/{employee_id}/{filename}"
    return f"/media/{company_id}/{project_id}/{employee_id}/{filename}"


def build_thumb_filename(filename: str, extension_override: str | None = None) -> str:
    if not extension_override:
        return f"thumb_{filename}"
    extension = extension_override if extension_override.startswith(".") else f".{extension_override}"
    return f"thumb_{Path(filename).stem}{extension}"


def is_video_media(mime_type: str | None, filename: str | None = None) -> bool:
    normalized_mime_type = str(mime_type or "").strip().lower()
    if normalized_mime_type.startswith(VIDEO_MIME_PREFIX):
        return True
    extension = Path(filename or "").suffix.lower()
    return extension in VIDEO_EXTENSIONS


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


def _write_video_placeholder_thumbnail(destination: Path) -> None:
    image = Image.new("RGB", (640, 480), "#d9e2ef")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((72, 72, 568, 408), radius=32, fill="#173a63")
    draw.polygon([(276, 186), (276, 294), (384, 240)], fill="#ffffff")
    image.save(destination, format="JPEG", quality=82)


def create_video_thumbnail_file(video_path: Path, destination: Path) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is not None:
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(destination),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode == 0 and destination.is_file():
            return
    _write_video_placeholder_thumbnail(destination)


def photo_media_kind(photo: Photo) -> str:
    return "video" if is_video_media(photo.mime_type, photo.original_file_name or photo.file_path) else "photo"


def photo_media_asset_id(photo: Photo) -> str:
    metadata_json = photo.metadata_json if isinstance(photo.metadata_json, dict) else {}
    media_asset_id = str(metadata_json.get("media_asset_id") or "").strip()
    return media_asset_id or str(photo.id)


def photo_duration_seconds(photo: Photo) -> float:
    metadata_json = photo.metadata_json if isinstance(photo.metadata_json, dict) else {}
    try:
        return float(metadata_json.get("duration_seconds") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def ensure_legacy_video_media_asset(db: Session, app_settings: Settings, photo: Photo) -> MediaAsset | None:
    if photo_media_kind(photo) != "video":
        return None
    metadata_json = dict(photo.metadata_json) if isinstance(photo.metadata_json, dict) else {}
    existing_asset_id = str(metadata_json.get("media_asset_id") or "").strip()
    if existing_asset_id:
        asset = db.get(MediaAsset, existing_asset_id)
        if asset is not None:
            return asset

    resolved_thumb_url = resolve_photo_thumb_relative_url(photo, app_settings) or photo.thumb_url
    asset_metadata = {
        "legacy_photo_id": photo.id,
        "employee_id": photo.employee_id,
        "authorized_employee_ids": [photo.employee_id] if photo.employee_id else [],
        "project_id": photo.project_id,
        "photo_type": photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type),
        "note": photo.note,
        "location": photo.location,
        "image_url": photo.image_url,
        "thumb_url": resolved_thumb_url,
    }
    asset = MediaAsset(
        asset_id=str(uuid4()),
        tenant_id=photo.tenant_id or photo.company_id,
        company_id=photo.company_id,
        uploaded_by_user_id=None,
        media_type=MediaType.video,
        source="legacy_mobile_upload",
        file_path=photo.file_path,
        original_file_name=photo.original_file_name,
        mime_type=photo.mime_type,
        file_size=photo.file_size,
        checksum=photo.checksum,
        duration_seconds=photo_duration_seconds(photo),
        status=MediaAssetStatus.processing,
        metadata_json=asset_metadata,
    )
    db.add(asset)
    db.flush()
    metadata_json["media_asset_id"] = asset.asset_id
    metadata_json["media_kind"] = "video"
    if asset.duration_seconds is not None:
        metadata_json["duration_seconds"] = asset.duration_seconds
    photo.metadata_json = metadata_json
    db.add(photo)
    db.flush()
    return asset


def resolve_photo_thumb_relative_url(photo: Photo, app_settings: Settings) -> str | None:
    if photo_media_kind(photo) != "video":
        return photo.thumb_url
    file_path = Path(photo.file_path)
    if not file_path.is_file():
        return photo.thumb_url
    thumb_filename = build_thumb_filename(file_path.name, extension_override=".jpg")
    thumb_path = file_path.with_name(thumb_filename)
    if not thumb_path.is_file():
        create_video_thumbnail_file(file_path, thumb_path)
    return build_relative_image_url(photo.photo_type, photo.company_id, photo.project_id, photo.employee_id, thumb_filename)


def create_thumbnail_file(original_bytes: bytes, destination: Path, extension: str) -> None:
    try:
        image = Image.open(io.BytesIO(original_bytes))
        image.thumbnail((640, 640))
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        format_name = "PNG" if extension.lower() == ".png" else "JPEG"
        save_image = image
        if format_name == "JPEG" and image.mode == "RGBA":
            save_image = image.convert("RGB")
        save_image.save(destination, format=format_name, quality=82)
    except (UnidentifiedImageError, OSError):
        destination.write_bytes(original_bytes)


def resolve_public_url(
    request: Request | None,
    app_settings: Settings,
    relative_url: str,
    storage_base_url: str | None = None,
) -> str:
    media_prefix = app_settings.media_url_prefix.rstrip("/")
    if relative_url.startswith(f"{media_prefix}/"):
        relative_url = sign_media_url(relative_url, app_settings)
    if request is not None:
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        host = forwarded_host or str(request.headers.get("host") or "").strip()
        scheme = forwarded_proto or request.url.scheme
        if host:
            return f"{scheme}://{host}{relative_url}"
        return str(request.base_url).rstrip("/") + relative_url
    base_url = (storage_base_url or app_settings.public_base_url).rstrip("/")
    return f"{base_url}{relative_url}"


def _relative_media_path_to_filesystem(app_settings: Settings, relative_url: str | None) -> Path | None:
    normalized_relative_url = str(relative_url or "").strip()
    if not normalized_relative_url:
        return None
    media_prefix = app_settings.media_url_prefix.rstrip("/")
    if not normalized_relative_url.startswith(f"{media_prefix}/"):
        return None
    relative_fragment = normalized_relative_url[len(media_prefix) + 1 :].strip("/")
    if not relative_fragment:
        return None
    candidate = (app_settings.photos_root / relative_fragment).resolve()
    photos_root = app_settings.photos_root.resolve()
    try:
        candidate.relative_to(photos_root)
    except ValueError:
        return None
    return candidate


def _safe_remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
    except OSError:
        return


def _delete_linked_media_asset_rows(db: Session, asset: MediaAsset) -> None:
    db.execute(delete(AIAnalysisLog).where(AIAnalysisLog.media_asset_id == asset.asset_id))
    db.execute(delete(MediaAnnotation).where(MediaAnnotation.media_asset_id == asset.asset_id))
    db.execute(delete(EvidenceObservation).where(EvidenceObservation.media_asset_id == asset.asset_id))
    db.delete(asset)


def hard_delete_photo(
    db: Session,
    *,
    app_settings: Settings,
    photo: Photo,
    actor_user: User | None,
    request: Request | None,
    detail: dict | None = None,
) -> None:
    if not photo.deleted:
        raise RuntimeError("Photo must be moved to the recycle bin before permanent deletion")

    metadata_json = dict(photo.metadata_json) if isinstance(photo.metadata_json, dict) else {}
    linked_media_asset_id = str(metadata_json.get("media_asset_id") or "").strip()
    linked_media_asset = db.get(MediaAsset, linked_media_asset_id) if linked_media_asset_id else None

    file_candidates = {
        Path(photo.file_path).resolve() if photo.file_path else None,
        Path(photo.storage_path).resolve() if photo.storage_path else None,
        _relative_media_path_to_filesystem(app_settings, photo.image_url),
        _relative_media_path_to_filesystem(app_settings, photo.thumb_url),
        _relative_media_path_to_filesystem(app_settings, resolve_photo_thumb_relative_url(photo, app_settings)),
    }
    linked_asset_file = None
    linked_asset_frames_dir = None
    if linked_media_asset is not None:
        linked_asset_file = Path(linked_media_asset.file_path) if linked_media_asset.file_path else None
        linked_asset_frames_dir = app_settings.media_frames_root / linked_media_asset.asset_id

    db.execute(delete(PhotoComment).where(PhotoComment.photo_id == photo.id))
    db.execute(delete(MediaAnnotation).where(MediaAnnotation.photo_id == photo.id))
    db.execute(delete(AIAnalysisLog).where(AIAnalysisLog.photo_id == photo.id))
    db.execute(delete(EvidenceObservation).where(EvidenceObservation.photo_id == photo.id))
    db.execute(delete(ReceiptFact).where(ReceiptFact.photo_id == photo.id))
    db.execute(delete(PhotoObservationPromotion).where(PhotoObservationPromotion.photo_id == photo.id))
    db.execute(delete(PhotoLensObservation).where(PhotoLensObservation.photo_id == photo.id))
    db.execute(delete(PhotoLensObservationRun).where(PhotoLensObservationRun.photo_id == photo.id))
    # Review tasks are workflow history; detach them from the photo instead of deleting.
    db.execute(update(ReviewTask).where(ReviewTask.related_photo_id == photo.id).values(related_photo_id=None))
    db.execute(update(ReviewTask).where(ReviewTask.completion_photo_id == photo.id).values(completion_photo_id=None))
    db.execute(
        delete(TaskJob).where(
            TaskJob.company_id == photo.company_id,
            TaskJob.related_type == "photo",
            TaskJob.related_id == str(photo.id),
        )
    )

    if linked_media_asset is not None:
        _delete_linked_media_asset_rows(db, linked_media_asset)

    log_audit(
        db,
        action="photo_permanently_deleted",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user.id if actor_user else None,
        detail_json=detail,
        ip_address=request.client.host if request and request.client else None,
        project_id=photo.project_id,
        company_id=photo.company_id,
    )
    db.delete(photo)
    # Commit the database deletion before touching disk so a failure cannot
    # leave rows pointing at files that were already removed.
    db.commit()

    for file_path in file_candidates:
        _safe_remove_file(file_path)
    _safe_remove_file(linked_asset_file)
    if linked_asset_frames_dir is not None:
        shutil.rmtree(linked_asset_frames_dir, ignore_errors=True)


async def save_mobile_upload(
    db: Session,
    *,
    app_settings: Settings,
    request: Request,
    upload: UploadFile,
    employee: Employee,
    employee_id: str,
    project_id: str,
    photo_type: PhotoType,
    gps: str | None,
    gps_lat: float | None,
    gps_lon: float | None,
    heading: float | None,
    pitch: float | None,
    roll: float | None,
    location: str | None,
    timestamp: str,
    device_model: str | None = None,
    os_version: str | None = None,
    app_version: str | None = None,
    note: str | None = None,
    media_kind: str | None = None,
) -> Photo:
    if employee.employee_id != employee_id:
        raise MobileApiError(
            status_code=403,
            code="AUTH_EMPLOYEE_MISMATCH",
            message="employee_id does not match API key",
            message_zh="工号与登录密钥不匹配，请在设置中检查工号",
            message_es="El ID de empleado no coincide con la clave de acceso",
            retryable=False,
        )

    system_settings = get_system_settings(db, app_settings)
    company_id = employee.company_id
    timezone_name = system_settings.get("timezone", app_settings.default_timezone)
    normalized_location = resolve_upload_location(location, gps, gps_lat, gps_lon)
    if not app_settings.is_private_deployment:
        # Private/NAS deployments have no plans or quotas; the NAS disk is
        # the limit and retention policies manage it.
        enforce_upload_limit(db, company_id=company_id, timezone_name=timezone_name)
    original_project_id = project_id
    project_id_recovered = False
    if photo_type == PhotoType.project:
        project = db.scalar(select(Project).where(Project.project_id == project_id, Project.company_id == company_id))
        if project is None and employee.project_id and employee.project_id != project_id:
            # Compatibility fallback: apps with a mistyped local project id (or an
            # old offline queue) would otherwise lose photos to a hard 404. When
            # the employee has an assigned project in this company, accept the
            # upload into it and keep the original id for audit/reconciliation.
            fallback_project = db.scalar(
                select(Project).where(
                    Project.project_id == employee.project_id,
                    Project.company_id == company_id,
                )
            )
            if fallback_project is not None:
                project = fallback_project
                project_id = fallback_project.project_id
                project_id_recovered = True
        if project is None:
            raise MobileApiError(
                status_code=404,
                code="PROJECT_NOT_FOUND",
                message="Unknown project_id",
                message_zh="项目编号不存在，请联系管理员确认你的项目号",
                message_es="El número de proyecto no existe, contacte a su administrador",
                retryable=False,
            )
        storage_dir = app_settings.photos_root / company_id / project_id / employee_id
        visibility = system_settings.get("default_visibility", "internal")
        visibility_value = (
            PhotoVisibility.client_visible if visibility == PhotoVisibility.client_visible.value else PhotoVisibility.internal
        )
        stored_project_id = project_id
    else:
        storage_dir = app_settings.photos_root / company_id / "invoice" / employee_id
        visibility_value = PhotoVisibility.internal
        stored_project_id = project_id

    try:
        captured_at = parse_utc_timestamp(timestamp)
    except ValueError:
        raise MobileApiError(
            status_code=400,
            code="TIMESTAMP_INVALID",
            message="Invalid timestamp format",
            message_zh="照片时间格式错误，请检查手机时间设置",
            message_es="Formato de fecha no válido",
            retryable=False,
        )

    body = await upload.read()
    if not body:
        raise MobileApiError(
            status_code=400,
            code="FILE_EMPTY",
            message="Uploaded file is empty",
            message_zh="上传的文件是空的，请重新拍摄后上传",
            message_es="El archivo subido está vacío",
            retryable=False,
        )

    size_limit = int(system_settings.get("upload_limit_mb", str(app_settings.upload_max_mb)))
    if len(body) > size_limit * 1024 * 1024:
        raise MobileApiError(
            status_code=413,
            code="FILE_TOO_LARGE",
            message="Uploaded file exceeds configured upload limit",
            message_zh=f"文件超过 {size_limit}MB 上限，请压缩后上传",
            message_es=f"El archivo supera el límite de {size_limit}MB",
            retryable=False,
        )

    extension = Path(upload.filename or "").suffix.lower() or ".jpg"
    filename = sanitize_component(f"{utc_now().strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(6)}{extension}")
    normalized_media_kind = str(media_kind or "").strip().lower()
    video_upload = normalized_media_kind == "video" or is_video_media(upload.content_type, upload.filename or filename)

    if not video_upload and extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        # A flaky connection can deliver a truncated multipart part; without this
        # check the corrupt file would be stored, returned 200, and the app would
        # delete its only good local copy.
        try:
            Image.open(io.BytesIO(body)).verify()
        except (UnidentifiedImageError, OSError):
            raise MobileApiError(
                status_code=400,
                code="FILE_CORRUPT",
                message="Uploaded file is not a valid image",
                message_zh="图片文件损坏（可能是网络中断导致），请重新上传",
                message_es="El archivo de imagen está dañado, vuelva a subirlo",
                retryable=True,
            )

    checksum = hashlib.sha256(body).hexdigest()
    duplicate = db.scalar(
        select(Photo).where(
            Photo.company_id == company_id,
            Photo.employee_id == employee_id,
            Photo.checksum == checksum,
            Photo.deleted.is_(False),
        )
    )
    if duplicate is not None:
        # Offline-queue retries after an ambiguous timeout re-send the same
        # bytes; return the already-stored photo instead of duplicating it.
        return duplicate

    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / filename
    file_path.write_bytes(body)
    thumb_filename = build_thumb_filename(filename, extension_override=".jpg" if video_upload else None)
    thumb_path = storage_dir / thumb_filename
    if video_upload:
        create_video_thumbnail_file(file_path, thumb_path)
    else:
        create_thumbnail_file(body, thumb_path, extension)

    relative_url = build_relative_image_url(photo_type, company_id, project_id, employee_id, filename)
    thumb_relative_url = build_relative_image_url(photo_type, company_id, project_id, employee_id, thumb_filename)
    metadata_json = {
        "gps": gps,
        "device_model": device_model,
        "os_version": os_version,
        "app_version": app_version,
        "media_kind": "video" if video_upload else "photo",
    }
    if project_id_recovered:
        metadata_json["original_project_id"] = original_project_id
    if video_upload:
        duration_seconds = _read_video_duration_seconds(file_path)
        if duration_seconds is not None:
            metadata_json["duration_seconds"] = duration_seconds

    photo = Photo(
        company_id=company_id,
        tenant_id=company_id,
        employee_id=employee_id,
        project_id=stored_project_id,
        photo_type=photo_type,
        file_path=str(file_path),
        storage_path=str(file_path),
        image_url=relative_url,
        thumb_url=thumb_relative_url,
        original_file_name=upload.filename or filename,
        mime_type=upload.content_type,
        file_size=len(body),
        checksum=checksum,
        gps=gps,
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        gps_lng=gps_lon,
        location=normalized_location,
        heading=heading,
        pitch=pitch,
        roll=roll,
        captured_at_utc=captured_at,
        visibility=visibility_value,
        approval_status=ApprovalStatus.pending,
        approved_by_manager=False,
        note=note,
        # Uploads always start pending so the background labeling job can transition them to completed.
        labeling_status="pending",
        device_model=device_model,
        os_version=os_version,
        app_version=app_version,
        metadata_json=metadata_json,
    )
    db.add(photo)
    db.flush()
    if video_upload:
        ensure_legacy_video_media_asset(db, app_settings, photo)
    record_upload_usage(db, company_id=company_id, timezone_name=timezone_name, file_size=len(body))

    audit_detail = {"employee_id": employee_id, "photo_type": photo_type.value, "filename": filename}
    if project_id_recovered:
        audit_detail["original_project_id"] = original_project_id
        audit_detail["resolved_project_id"] = project_id
        audit_detail["project_id_recovered"] = True
    log_audit(
        db,
        action="mobile_upload_project_recovered" if project_id_recovered else "mobile_upload",
        target_type="photo",
        target_id=str(photo.id),
        detail_json=audit_detail,
        ip_address=request.client.host if request.client else None,
        project_id=project_id if photo_type == PhotoType.project else employee.project_id or project_id,
        company_id=company_id,
    )
    db.commit()
    db.refresh(photo)
    return photo


def serialize_photo(
    photo: Photo,
    request: Request | None,
    app_settings: Settings,
    storage_base_url: str | None = None,
    queue_info=None,
) -> PhotoOut:
    image_url = resolve_public_url(request, app_settings, photo.image_url, storage_base_url=storage_base_url)
    resolved_thumb_relative_url = resolve_photo_thumb_relative_url(photo, app_settings)
    thumb_url = (
        resolve_public_url(request, app_settings, resolved_thumb_relative_url, storage_base_url=storage_base_url)
        if resolved_thumb_relative_url
        else None
    )
    return PhotoOut.from_photo(
        photo,
        image_url,
        thumb_url=thumb_url,
        media_url=image_url,
        media_kind=photo_media_kind(photo),
        media_asset_id=photo_media_asset_id(photo),
        duration_seconds=photo_duration_seconds(photo),
        queue_info=queue_info,
    )


def soft_delete_photo(
    db: Session,
    *,
    photo: Photo,
    actor_user: User | None,
    request: Request | None,
    detail: dict | None = None,
) -> Photo:
    stage_photo_soft_delete(
        db,
        photo=photo,
        actor_user=actor_user,
        request=request,
        detail=detail,
    )
    db.commit()
    db.refresh(photo)
    return photo


def stage_photo_soft_delete(
    db: Session,
    *,
    photo: Photo,
    actor_user: User | None,
    request: Request | None,
    detail: dict | None = None,
) -> Photo:
    photo.deleted = True
    photo.deleted_at = utc_now()
    photo.soft_deleted_at = photo.deleted_at
    db.add(photo)
    log_audit(
        db,
        action="photo_deleted",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user.id if actor_user else None,
        detail_json=detail,
        ip_address=request.client.host if request and request.client else None,
        project_id=photo.project_id,
        company_id=photo.company_id,
    )
    return photo


def restore_photo(db: Session, *, photo: Photo, actor_user: User, request: Request | None) -> Photo:
    stage_photo_restore(
        db,
        photo=photo,
        actor_user=actor_user,
        request=request,
    )
    db.commit()
    db.refresh(photo)
    return photo


def stage_photo_restore(
    db: Session,
    *,
    photo: Photo,
    actor_user: User,
    request: Request | None,
) -> Photo:
    photo.deleted = False
    photo.deleted_at = None
    photo.soft_deleted_at = None
    db.add(photo)
    log_audit(
        db,
        action="photo_restored",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user.id,
        ip_address=request.client.host if request and request.client else None,
        project_id=photo.project_id,
        company_id=photo.company_id,
    )
    return photo


def approve_photo(db: Session, *, photo: Photo, actor_user: User, request: Request | None) -> Photo:
    return set_photo_approval_status(
        db,
        photo=photo,
        actor_user=actor_user,
        approval_status=ApprovalStatus.approved,
        request=request,
    )


def set_photo_approval_status(
    db: Session,
    *,
    photo: Photo,
    actor_user: User,
    approval_status: ApprovalStatus,
    request: Request | None,
) -> Photo:
    stage_photo_approval_status(
        db,
        photo=photo,
        actor_user=actor_user,
        approval_status=approval_status,
        request=request,
    )
    db.commit()
    db.refresh(photo)
    return photo


def stage_photo_approval_status(
    db: Session,
    *,
    photo: Photo,
    actor_user: User,
    approval_status: ApprovalStatus,
    request: Request | None,
) -> Photo:
    photo.approval_status = approval_status
    photo.approved_by_manager = approval_status == ApprovalStatus.approved
    if approval_status == ApprovalStatus.pending:
        photo.approved_by_user_id = None
        photo.approved_at = None
    else:
        photo.approved_by_user_id = actor_user.id
        photo.approved_at = utc_now()
    db.add(photo)
    log_audit(
        db,
        action=f"photo_{approval_status.value}",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user.id,
        detail_json={"approval_status": approval_status.value},
        ip_address=request.client.host if request and request.client else None,
        project_id=photo.project_id,
        company_id=photo.company_id,
    )
    return photo


def update_photo_visibility(
    db: Session,
    *,
    photo: Photo,
    actor_user: User,
    visibility: PhotoVisibility,
    featured: bool | None = None,
    note: str | None = None,
    request: Request | None,
) -> Photo:
    stage_photo_display_update(
        db,
        photo=photo,
        actor_user=actor_user,
        visibility=visibility,
        featured=featured if featured is not None else UNSET,
        note=note if note is not None else UNSET,
        request=request,
        action_name="photo_visibility_changed",
    )
    db.commit()
    db.refresh(photo)
    return photo


def stage_photo_display_update(
    db: Session,
    *,
    photo: Photo,
    actor_user: User,
    visibility: PhotoVisibility | None = None,
    featured: object = UNSET,
    note: object = UNSET,
    request: Request | None,
    action_name: str = "photo_display_updated",
) -> Photo:
    detail_json: dict[str, object] = {}
    if visibility is not None:
        photo.visibility = visibility
        detail_json["visibility"] = visibility.value
    if featured is not UNSET:
        photo.featured = bool(featured)
        detail_json["featured"] = photo.featured
    if note is not UNSET:
        photo.note = note if isinstance(note, str) and note else None
        detail_json["note_present"] = bool(photo.note)
    if not detail_json:
        return photo
    db.add(photo)
    log_audit(
        db,
        action=action_name,
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user.id,
        detail_json=detail_json,
        ip_address=request.client.host if request and request.client else None,
        project_id=photo.project_id,
        company_id=photo.company_id,
    )
    return photo
