from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PhotoComment, User
from app.services.audit import log_audit


def get_comments_by_photo_ids(db: Session, photo_ids: list[int]) -> dict[int, list[PhotoComment]]:
    if not photo_ids:
        return {}
    comments = list(
        db.scalars(
            select(PhotoComment).where(PhotoComment.photo_id.in_(photo_ids)).order_by(PhotoComment.created_at.asc())
        )
    )
    grouped: dict[int, list[PhotoComment]] = defaultdict(list)
    for comment in comments:
        grouped[comment.photo_id].append(comment)
    return dict(grouped)


def add_photo_comment(
    db: Session,
    *,
    photo_id: int,
    actor_user: User,
    comment: str,
    project_id: str | None,
    ip_address: str | None,
) -> PhotoComment:
    entry = stage_photo_comment(
        db,
        photo_id=photo_id,
        actor_user=actor_user,
        comment=comment,
        project_id=project_id,
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(entry)
    return entry


def stage_photo_comment(
    db: Session,
    *,
    photo_id: int,
    actor_user: User,
    comment: str,
    project_id: str | None,
    ip_address: str | None,
) -> PhotoComment:
    entry = PhotoComment(photo_id=photo_id, user_id=actor_user.id, comment=comment.strip())
    db.add(entry)
    db.flush()
    log_audit(
        db,
        action="photo_comment_added",
        target_type="photo_comment",
        target_id=str(entry.id),
        actor_user_id=actor_user.id,
        project_id=project_id,
        detail_json={"photo_id": photo_id},
        ip_address=ip_address,
        company_id=actor_user.company_id,
    )
    return entry
