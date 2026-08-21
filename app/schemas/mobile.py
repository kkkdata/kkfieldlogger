from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.time import to_utc_iso
from app.models import ApprovalStatus, Photo, PhotoType, PhotoVisibility


class HealthResponse(BaseModel):
    status: str
    name: str
    portal: str
    company: str
    time: str


class QueueInfo(BaseModel):
    queued_count: int
    running_count: int
    retry_pending_count: int
    active_count: int
    average_duration_seconds: float
    estimated_wait_seconds: int
    estimated_completion_time: str | None = None
    position_in_queue: int | None = None
    queue_position: int | None = None
    queue_message: str | None = None


class PhotoOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: int
    employee_id: str
    project_id: str | None
    photo_type: PhotoType
    file_path: str
    image_url: str
    thumb_url: str | None
    media_url: str | None = None
    media_kind: str = "photo"
    media_asset_id: str | None = None
    duration_seconds: float | None = None
    original_file_name: str
    gps: str | None
    gps_lat: float | None
    gps_lon: float | None
    location: str | None
    heading: float | None
    pitch: float | None
    roll: float | None
    captured_at_utc: str
    created_at: str
    deleted: bool
    deleted_at: str | None
    visibility: PhotoVisibility
    approval_status: ApprovalStatus
    approved_by_manager: bool
    approved_by_user_id: int | None
    approved_at: str | None
    featured: bool
    note: str | None
    tag_json: dict[str, Any] | list[Any] | None
    labeling_status: str | None
    device_model: str | None
    os_version: str | None
    app_version: str | None
    queue_info: QueueInfo | None = None

    @classmethod
    def from_photo(
        cls,
        photo: Photo,
        image_url: str,
        thumb_url: str | None = None,
        media_url: str | None = None,
        media_kind: str = "photo",
        media_asset_id: str | None = None,
        duration_seconds: float | None = None,
        queue_info: QueueInfo | None = None,
    ) -> "PhotoOut":
        return cls(
            id=photo.id,
            employee_id=photo.employee_id,
            project_id=photo.project_id,
            photo_type=photo.photo_type,
            file_path=photo.file_path,
            image_url=image_url,
            thumb_url=thumb_url or photo.thumb_url,
            media_url=media_url or image_url,
            media_kind=media_kind,
            media_asset_id=media_asset_id,
            duration_seconds=duration_seconds,
            original_file_name=photo.original_file_name,
            gps=photo.gps,
            gps_lat=photo.gps_lat,
            gps_lon=photo.gps_lon,
            location=photo.location,
            heading=photo.heading,
            pitch=photo.pitch,
            roll=photo.roll,
            captured_at_utc=to_utc_iso(photo.captured_at_utc) or "",
            created_at=to_utc_iso(photo.created_at) or "",
            deleted=photo.deleted,
            deleted_at=to_utc_iso(photo.deleted_at),
            visibility=photo.visibility,
            approval_status=photo.approval_status,
            approved_by_manager=photo.approval_status == ApprovalStatus.approved,
            approved_by_user_id=photo.approved_by_user_id,
            approved_at=to_utc_iso(photo.approved_at),
            featured=photo.featured,
            note=photo.note,
            tag_json=photo.tag_json,
            labeling_status=photo.labeling_status,
            device_model=photo.device_model,
            os_version=photo.os_version,
            app_version=photo.app_version,
            queue_info=queue_info,
        )


class UploadResponse(BaseModel):
    message: str
    photo: PhotoOut
    queue: QueueInfo
    queue_message: str | None = None
