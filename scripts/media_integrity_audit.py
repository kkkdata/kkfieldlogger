from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import load_settings
from app.db.session import create_engine_from_settings, create_session_maker
from app.models.entities import (
    MediaAnnotation,
    MediaAsset,
    MediaAssetStatus,
    Photo,
    ProgressReport,
    ProgressReportStatus,
    TaskJob,
    TaskStatus,
)
from app.services.photos import build_thumb_filename, photo_media_kind


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


def _path_from_value(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        return Path(value)
    except (OSError, ValueError):
        return None


def _media_url_to_path(url_value: str | None, *, media_prefix: str, photos_root: Path) -> Path | None:
    normalized = str(url_value or "").strip()
    if not normalized:
        return None
    if normalized.startswith("http://") or normalized.startswith("https://"):
        normalized = urlparse(normalized).path
    prefix = media_prefix.rstrip("/")
    if not normalized.startswith(f"{prefix}/"):
        return None
    relative = normalized[len(prefix) + 1 :].strip("/")
    if not relative:
        return None
    candidate = (photos_root / relative).resolve()
    try:
        candidate.relative_to(photos_root.resolve())
    except ValueError:
        return None
    return candidate


def _file_exists(path: Path | None) -> bool:
    try:
        return bool(path and path.is_file())
    except OSError:
        return False


def _photo_file_candidates(photo: Photo) -> list[Path]:
    candidates: list[Path] = []
    for raw_path in (photo.file_path, photo.storage_path):
        path = _path_from_value(raw_path)
        if path is not None:
            candidates.append(path)
    return candidates


def _photo_has_file(photo: Photo) -> bool:
    return any(_file_exists(path) for path in _photo_file_candidates(photo))


def _thumb_candidates(photo: Photo, *, media_prefix: str, photos_root: Path) -> list[Path]:
    candidates: list[Path] = []
    url_path = _media_url_to_path(photo.thumb_url, media_prefix=media_prefix, photos_root=photos_root)
    if url_path is not None:
        candidates.append(url_path)
    for file_path in _photo_file_candidates(photo):
        if file_path.name:
            candidates.append(file_path.with_name(build_thumb_filename(file_path.name)))
    return candidates


def _photo_has_thumb(photo: Photo, *, media_prefix: str, photos_root: Path) -> bool:
    return any(_file_exists(path) for path in _thumb_candidates(photo, media_prefix=media_prefix, photos_root=photos_root))


def _sample_photo(photo: Photo) -> dict[str, object]:
    return {
        "id": photo.id,
        "company_id": photo.company_id,
        "tenant_id": photo.tenant_id,
        "project_id": photo.project_id,
        "employee_id": photo.employee_id,
        "deleted": bool(photo.deleted),
        "media_kind": photo_media_kind(photo),
        "file_path": photo.file_path,
        "thumb_url": photo.thumb_url,
    }


def _sample_job(job: TaskJob) -> dict[str, object]:
    return {
        "id": job.id,
        "public_id": job.public_id,
        "company_id": job.company_id,
        "task_type": job.task_type,
        "status": _enum_value(job.status),
        "related_type": job.related_type,
        "related_id": job.related_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "last_error": (job.last_error or "")[:240],
    }


def _count_scalar(db, statement) -> int:
    return int(db.execute(statement).scalar_one() or 0)


def run_audit(sample_limit: int) -> dict[str, object]:
    with contextlib.redirect_stdout(io.StringIO()):
        settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    now = datetime.now(timezone.utc)
    stale_processing_cutoff = now - timedelta(hours=2)
    stale_queue_cutoff = now - timedelta(hours=24)

    with session_maker() as db:
        photos = db.execute(select(Photo)).scalars().all()
        active_photos = [photo for photo in photos if not photo.deleted]
        deleted_photos = [photo for photo in photos if photo.deleted]
        missing_active_files = [photo for photo in active_photos if not _photo_has_file(photo)]
        missing_active_thumbs = [
            photo
            for photo in active_photos
            if not _photo_has_thumb(photo, media_prefix=settings.media_url_prefix, photos_root=settings.photos_root)
        ]
        deleted_photo_ids = {str(photo.id) for photo in deleted_photos}
        active_job_statuses = {TaskStatus.queued, TaskStatus.running, TaskStatus.failed}
        jobs_for_deleted_photos = []
        if deleted_photo_ids:
            jobs_for_deleted_photos = (
                db.execute(
                    select(TaskJob).where(
                        TaskJob.related_type == "photo",
                        TaskJob.related_id.in_(deleted_photo_ids),
                        TaskJob.status.in_(active_job_statuses),
                    )
                )
                .scalars()
                .all()
            )

        media_assets = db.execute(select(MediaAsset)).scalars().all()
        live_asset_statuses = {
            MediaAssetStatus.uploading,
            MediaAssetStatus.processing,
            MediaAssetStatus.completed,
            MediaAssetStatus.failed,
        }
        missing_asset_files = [
            asset
            for asset in media_assets
            if asset.status in live_asset_statuses and not _file_exists(_path_from_value(asset.file_path))
        ]

        voice_annotations = db.execute(select(MediaAnnotation).where(MediaAnnotation.audio_file_path.is_not(None))).scalars().all()
        missing_audio_files = [
            annotation
            for annotation in voice_annotations
            if not _file_exists(_path_from_value(annotation.audio_file_path))
        ]

        stale_progress_reports = (
            db.execute(
                select(ProgressReport).where(
                    ProgressReport.status.in_([ProgressReportStatus.pending, ProgressReportStatus.processing]),
                    ProgressReport.updated_at < stale_processing_cutoff,
                )
            )
            .scalars()
            .all()
        )
        stale_jobs = (
            db.execute(
                select(TaskJob).where(
                    TaskJob.status.in_([TaskStatus.queued, TaskStatus.running, TaskStatus.failed]),
                    TaskJob.updated_at < stale_queue_cutoff,
                )
            )
            .scalars()
            .all()
        )

        pending_by_company: dict[str, int] = defaultdict(int)
        status_counts: Counter[str] = Counter()
        task_type_counts: Counter[str] = Counter()
        for company_id, status, task_type, count in db.execute(
            select(TaskJob.company_id, TaskJob.status, TaskJob.task_type, func.count())
            .group_by(TaskJob.company_id, TaskJob.status, TaskJob.task_type)
        ):
            count_value = int(count or 0)
            status_counts[_enum_value(status)] += count_value
            task_type_counts[str(task_type)] += count_value
            if status in {TaskStatus.queued, TaskStatus.running, TaskStatus.failed}:
                pending_by_company[str(company_id)] += count_value

        tenant_capacity_warnings = [
            {"company_id": company_id, "pending_jobs": count, "capacity": settings.queue_max_pending_jobs_per_tenant}
            for company_id, count in sorted(pending_by_company.items(), key=lambda item: item[1], reverse=True)
            if count >= int(settings.queue_max_pending_jobs_per_tenant * 0.8)
        ]

        payload: dict[str, object] = {
            "generated_at": now.isoformat(),
            "database_url_driver": settings.database_url.split(":", 1)[0],
            "media_root": str(settings.media_root_path),
            "summary": {
                "photos_total": len(photos),
                "photos_active": len(active_photos),
                "photos_in_recycle_bin": len(deleted_photos),
                "photos_video": sum(1 for photo in photos if photo_media_kind(photo) == "video"),
                "media_assets_total": len(media_assets),
                "voice_annotations_with_audio": len(voice_annotations),
                "task_jobs_by_status": dict(sorted(status_counts.items())),
                "task_jobs_by_type": dict(sorted(task_type_counts.items())),
            },
            "findings": {
                "missing_active_photo_files": {
                    "count": len(missing_active_files),
                    "samples": [_sample_photo(photo) for photo in missing_active_files[:sample_limit]],
                },
                "missing_active_thumbnails": {
                    "count": len(missing_active_thumbs),
                    "samples": [_sample_photo(photo) for photo in missing_active_thumbs[:sample_limit]],
                },
                "active_jobs_for_recycle_bin_photos": {
                    "count": len(jobs_for_deleted_photos),
                    "samples": [_sample_job(job) for job in jobs_for_deleted_photos[:sample_limit]],
                },
                "missing_media_asset_files": {
                    "count": len(missing_asset_files),
                    "samples": [
                        {
                            "asset_id": asset.asset_id,
                            "company_id": asset.company_id,
                            "media_type": _enum_value(asset.media_type),
                            "status": _enum_value(asset.status),
                            "file_path": asset.file_path,
                        }
                        for asset in missing_asset_files[:sample_limit]
                    ],
                },
                "missing_voice_audio_files": {
                    "count": len(missing_audio_files),
                    "samples": [
                        {
                            "id": annotation.id,
                            "photo_id": annotation.photo_id,
                            "media_asset_id": annotation.media_asset_id,
                            "status": _enum_value(annotation.status),
                            "audio_file_path": annotation.audio_file_path,
                        }
                        for annotation in missing_audio_files[:sample_limit]
                    ],
                },
                "stale_progress_reports": {
                    "count": len(stale_progress_reports),
                    "samples": [
                        {
                            "id": report.id,
                            "company_id": report.company_id,
                            "project_id": report.project_id,
                            "status": _enum_value(report.status),
                            "updated_at": report.updated_at.isoformat() if report.updated_at else None,
                        }
                        for report in stale_progress_reports[:sample_limit]
                    ],
                },
                "stale_jobs_over_24h": {
                    "count": len(stale_jobs),
                    "samples": [_sample_job(job) for job in stale_jobs[:sample_limit]],
                },
                "tenant_capacity_warnings": tenant_capacity_warnings[:sample_limit],
            },
            "notes": [
                "This script is read-only and does not delete or repair records.",
                "Recycle-bin photos may keep files and AI logs until permanent deletion, but queued/running jobs for them should be investigated.",
                "Missing active thumbnails are often repairable by reprocessing or thumbnail regeneration.",
            ],
        }
    engine.dispose()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for media files, thumbnails, recycle-bin jobs, and queue drift.")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    payload = run_audit(sample_limit=max(1, args.sample_limit))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
