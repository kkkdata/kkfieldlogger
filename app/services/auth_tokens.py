from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import utc_now
from app.models import PasswordResetToken, User


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_password_reset_token(db: Session, *, user: User, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utc_now() + timedelta(hours=settings.password_reset_hours),
        )
    )
    db.flush()
    return token


def consume_password_reset_token(db: Session, token: str) -> User | None:
    token_hash = hash_token(token)
    entry = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash))
    expires_at = entry.expires_at.replace(tzinfo=timezone.utc) if entry and entry.expires_at.tzinfo is None else (
        entry.expires_at if entry else None
    )
    if entry is None or entry.used_at is not None or expires_at <= utc_now():
        return None
    user = db.get(User, entry.user_id)
    if user is None or not user.active:
        return None
    entry.used_at = utc_now()
    db.add(entry)
    return user
