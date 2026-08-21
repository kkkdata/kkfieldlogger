from __future__ import annotations

from datetime import datetime, timezone
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.security import hash_password
from app.models import (
    AnnotationRole,
    AnnotationStatus,
    AnnotationType,
    AnnotationVisibility,
    MediaAnnotation,
    MediaAsset,
    Photo,
    TaskJob,
    User,
    UserRole,
)
from app.services.audit import log_audit
from app.services.photos import sanitize_component

logger = get_logger("kkfieldlogger.annotations")

ANNOTATION_TRANSCRIPTION_TASK = "annotation_transcription"


@dataclass(frozen=True)
class AnnotationTargetRef:
    photo_id: int | None = None
    media_asset_id: str | None = None

    @property
    def target_type(self) -> str:
        return "photo" if self.photo_id is not None else "media_asset"

    @property
    def target_id(self) -> str:
        return str(self.photo_id if self.photo_id is not None else self.media_asset_id or "")


def annotation_role_for_user(user: User) -> AnnotationRole:
    manager_roles = {
        UserRole.platform_super_admin,
        UserRole.owner,
        UserRole.admin,
        UserRole.manager,
        UserRole.super_admin,
        UserRole.project_manager,
    }
    return AnnotationRole.manager if user.role in manager_roles else AnnotationRole.employee


def ensure_employee_annotation_user(db: Session, employee: Any) -> User:
    existing_user = db.scalar(
        select(User).where(
            User.company_id == employee.company_id,
            User.employee_id == employee.employee_id,
        )
    )
    if existing_user is not None:
        return existing_user

    username_base = sanitize_component(f"employee-{employee.company_id}-{employee.employee_id}")[:48] or f"employee-{employee.employee_id}"
    candidate_username = username_base
    suffix = 1
    while db.scalar(select(User).where(User.username == candidate_username)) is not None:
        candidate_username = f"{username_base[:40]}-{suffix}"
        suffix += 1

    user = User(
        company_id=employee.company_id,
        username=candidate_username,
        password_hash=hash_password(uuid4().hex),
        role=UserRole.employee,
        display_name=employee.name,
        employee_id=employee.employee_id,
        active=True,
        is_verified=True,
    )
    db.add(user)
    db.flush()
    return user


def annotation_storage_root(app_settings: Settings) -> Path:
    return app_settings.media_assets_root / "_annotations"


def save_annotation_audio_file(
    app_settings: Settings,
    *,
    company_id: str,
    original_file_name: str,
    body: bytes,
) -> str:
    extension = Path(original_file_name).suffix.lower() or ".webm"
    safe_name = sanitize_component(f"{uuid4().hex}{extension}")
    destination_dir = annotation_storage_root(app_settings) / sanitize_component(company_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / safe_name
    destination_path.write_bytes(body)
    return str(destination_path)


def list_target_annotations(
    db: Session,
    *,
    photo_id: int | None = None,
    media_asset_id: str | None = None,
) -> list[MediaAnnotation]:
    if photo_id is None and media_asset_id is None:
        return []
    stmt = select(MediaAnnotation)
    if photo_id is not None:
        stmt = stmt.where(MediaAnnotation.photo_id == photo_id)
    else:
        stmt = stmt.where(MediaAnnotation.media_asset_id == media_asset_id)
    return list(db.scalars(stmt.order_by(MediaAnnotation.created_at.asc(), MediaAnnotation.id.asc())))


def list_public_completed_annotation_texts(
    db: Session,
    *,
    photo_id: int | None = None,
    media_asset_id: str | None = None,
) -> list[str]:
    annotations = list_target_annotations(db, photo_id=photo_id, media_asset_id=media_asset_id)
    texts: list[str] = []
    for annotation in annotations:
        if annotation.visibility != AnnotationVisibility.public:
            continue
        if annotation.status != AnnotationStatus.completed:
            continue
        content_text = annotation_context_text(annotation)
        if not content_text:
            continue
        texts.append(content_text)
    return texts


def annotation_translations(annotation: MediaAnnotation) -> dict[str, str]:
    raw_translations = annotation.translations_json if isinstance(annotation.translations_json, dict) else {}
    translations: dict[str, str] = {}
    for language in ("zh", "en", "es"):
        translated_text = str(raw_translations.get(language) or "").strip()
        if translated_text:
            translations[language] = translated_text
    return translations


def annotation_context_text(annotation: MediaAnnotation) -> str:
    content_text = str(annotation.content_text or "").strip()
    source_language = str(annotation.source_language or "").strip().lower()
    translations = annotation_translations(annotation)
    if not translations:
        return content_text
    parts: list[str] = []
    if content_text:
        parts.append(f"{source_language or 'source'}: {content_text}")
    for language in ("zh", "en", "es"):
        translated_text = translations.get(language)
        if not translated_text:
            continue
        if content_text and translated_text == content_text and language == source_language:
            continue
        parts.append(f"{language}: {translated_text}")
    return " | ".join(parts)


def filter_visible_annotations(
    annotations: list[MediaAnnotation],
    *,
    include_manager_only: bool,
) -> list[MediaAnnotation]:
    if include_manager_only:
        return annotations
    visible_annotations = [
        annotation
        for annotation in annotations
        if annotation.visibility == AnnotationVisibility.public
    ]
    visible_ids = {annotation.id for annotation in visible_annotations}
    return [
        annotation
        for annotation in visible_annotations
        if annotation.parent_id is None or annotation.parent_id in visible_ids
    ]


def _normalize_annotation_created_at(created_at: datetime | None) -> datetime | None:
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


def create_text_annotations(
    db: Session,
    *,
    targets: list[AnnotationTargetRef],
    user: User,
    content_text: str,
    visibility: AnnotationVisibility,
    parent_id: str | None = None,
    created_at: datetime | None = None,
) -> list[MediaAnnotation]:
    role_at_time = annotation_role_for_user(user)
    normalized_created_at = _normalize_annotation_created_at(created_at)
    annotations: list[MediaAnnotation] = []
    for target in targets:
        annotation_kwargs: dict[str, Any] = {
            "id": str(uuid4()),
            "photo_id": target.photo_id,
            "media_asset_id": target.media_asset_id,
            "parent_id": parent_id,
            "user_id": user.id,
            "role_at_time": role_at_time,
            "annotation_type": AnnotationType.text,
            "content_text": content_text,
            "audio_file_path": None,
            "visibility": visibility,
            "status": AnnotationStatus.completed,
        }
        if normalized_created_at is not None:
            annotation_kwargs["created_at"] = normalized_created_at
            annotation_kwargs["updated_at"] = normalized_created_at
        annotation = MediaAnnotation(
            **annotation_kwargs,
        )
        db.add(annotation)
        annotations.append(annotation)
    db.flush()
    return annotations


def create_voice_annotations(
    db: Session,
    *,
    targets: list[AnnotationTargetRef],
    user: User,
    audio_file_path: str,
    visibility: AnnotationVisibility,
    parent_id: str | None = None,
    created_at: datetime | None = None,
) -> list[MediaAnnotation]:
    role_at_time = annotation_role_for_user(user)
    normalized_created_at = _normalize_annotation_created_at(created_at)
    annotations: list[MediaAnnotation] = []
    for target in targets:
        annotation_kwargs: dict[str, Any] = {
            "id": str(uuid4()),
            "photo_id": target.photo_id,
            "media_asset_id": target.media_asset_id,
            "parent_id": parent_id,
            "user_id": user.id,
            "role_at_time": role_at_time,
            "annotation_type": AnnotationType.voice,
            "content_text": None,
            "audio_file_path": audio_file_path,
            "visibility": visibility,
            "status": AnnotationStatus.processing,
        }
        if normalized_created_at is not None:
            annotation_kwargs["created_at"] = normalized_created_at
            annotation_kwargs["updated_at"] = normalized_created_at
        annotation = MediaAnnotation(
            **annotation_kwargs,
        )
        db.add(annotation)
        annotations.append(annotation)
    db.flush()
    return annotations


def serialize_annotation_tree(
    db: Session,
    *,
    annotations: list[MediaAnnotation],
) -> list[dict[str, Any]]:
    if not annotations:
        return []
    user_ids = sorted({annotation.user_id for annotation in annotations})
    users = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(user_ids)))
    }
    annotation_ids = [annotation.id for annotation in annotations]
    task_jobs_by_annotation_id: dict[str, TaskJob] = {}
    if annotation_ids:
        task_jobs = list(
            db.scalars(
                select(TaskJob)
                .where(
                    TaskJob.related_type == "media_annotation",
                    TaskJob.related_id.in_(annotation_ids),
                )
                .order_by(TaskJob.created_at.desc(), TaskJob.id.desc())
            )
        )
        for task_job in task_jobs:
            if task_job.related_id and task_job.related_id not in task_jobs_by_annotation_id:
                task_jobs_by_annotation_id[task_job.related_id] = task_job

    nodes: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for annotation in annotations:
        author = users.get(annotation.user_id)
        translations = annotation_translations(annotation)
        task_job = task_jobs_by_annotation_id.get(annotation.id)
        error_message = task_job.last_error if task_job is not None and task_job.last_error else None
        node = {
            "id": annotation.id,
            "photo_id": annotation.photo_id,
            "media_asset_id": annotation.media_asset_id,
            "parent_id": annotation.parent_id,
            "user_id": annotation.user_id,
            "author_name": author.display_name if author is not None else None,
            "author_employee_id": author.employee_id if author is not None else None,
            "role_at_time": annotation.role_at_time.value,
            "author_role": annotation.role_at_time.value,
            "annotation_type": annotation.annotation_type.value,
            "content_text": annotation.content_text,
            "body_text": annotation.content_text,
            "source_language": annotation.source_language,
            "translations": translations,
            "content_text_zh": translations.get("zh") or "",
            "content_text_en": translations.get("en") or "",
            "content_text_es": translations.get("es") or "",
            "audio_file_path": annotation.audio_file_path,
            "audio_url": f"/api/v2/annotations/{annotation.id}/audio" if annotation.audio_file_path else None,
            "transcript_text": annotation.content_text if annotation.annotation_type == AnnotationType.voice else "",
            "transcript_language": annotation.source_language or "",
            "transcript_status": annotation.status.value if annotation.annotation_type == AnnotationType.voice else annotation.status.value,
            "processing_status": annotation.status.value,
            "worker_status": task_job.status.value if task_job is not None else None,
            "error_message": error_message,
            "visibility": annotation.visibility.value,
            "status": annotation.status.value,
            "ai_conclusion": "",
            "created_at": annotation.created_at.isoformat() if annotation.created_at else None,
            "updated_at": annotation.updated_at.isoformat() if annotation.updated_at else None,
            "children": [],
        }
        nodes[annotation.id] = node

    for annotation in annotations:
        node = nodes[annotation.id]
        if annotation.parent_id and annotation.parent_id in nodes:
            nodes[annotation.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


def process_voice_annotation_batch(
    annotation_ids: list[str],
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    *,
    company_id: str,
    raise_on_failure: bool = False,
) -> None:
    if not annotation_ids:
        return
    with session_maker() as db:
        annotations = [
            annotation
            for annotation in db.scalars(
                select(MediaAnnotation).where(MediaAnnotation.id.in_(annotation_ids)).order_by(MediaAnnotation.created_at.asc())
            )
        ]
        if not annotations:
            logger.warning("annotation_transcription_skipped", reason="missing_annotations")
            return
        audio_file_path = str(annotations[0].audio_file_path or "").strip()
        if not audio_file_path:
            raise RuntimeError("Voice annotation audio file is missing")
        audio_path = Path(audio_file_path)
        if not audio_path.is_file():
            raise RuntimeError("Voice annotation audio file not found on disk")
        mime_type = mimetypes.guess_type(audio_path.name)[0] or "audio/webm"

        try:
            from app.services.ai_pipeline import transcribe_audio_with_ai_backends

            transcript_payload, backend_ref, attempts = transcribe_audio_with_ai_backends(
                db,
                app_settings,
                tenant_slug=company_id,
                audio_path=audio_path,
                mime_type=mime_type,
            )
            source_text = str(transcript_payload.get("source_text") or "").strip()
            source_language = str(transcript_payload.get("source_language") or "").strip().lower() or None
            translations = transcript_payload.get("translations") if isinstance(transcript_payload, dict) else None
            for annotation in annotations:
                annotation.content_text = source_text
                annotation.source_language = source_language
                annotation.translations_json = translations if isinstance(translations, dict) else None
                annotation.status = AnnotationStatus.completed
                db.add(annotation)
                log_audit(
                    db,
                    action="media_annotation_transcribed",
                    target_type="media_annotation",
                    target_id=annotation.id,
                    actor_user_id=annotation.user_id,
                    company_id=company_id,
                    detail_json={
                        "backend": backend_ref,
                        "attempts": attempts,
                        "transcript_length": len(source_text),
                        "source_language": source_language,
                        "translations": translations,
                    },
                )
            db.commit()
            logger.info(
                "annotation_transcription_completed",
                company_id=company_id,
                annotation_count=len(annotations),
                backend=backend_ref,
            )
        except Exception as exc:
            db.rollback()
            for annotation in annotations:
                annotation.status = AnnotationStatus.failed
                annotation.source_language = None
                annotation.translations_json = None
                db.add(annotation)
            db.commit()
            logger.warning(
                "annotation_transcription_failed",
                company_id=company_id,
                annotation_count=len(annotations),
                error=str(exc),
            )
            if raise_on_failure:
                raise
