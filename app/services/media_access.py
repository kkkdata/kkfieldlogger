from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import urlencode

from app.core.config import Settings


MEDIA_EXPIRES_PARAM = "media_expires"
MEDIA_SIGNATURE_PARAM = "media_sig"


def _media_signature(settings: Settings, relative_url: str, expires_at: int) -> str:
    payload = f"{relative_url}\n{expires_at}".encode("utf-8")
    secret = settings.session_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def sign_media_url(relative_url: str, settings: Settings, *, now: int | None = None) -> str:
    expires_at = int(now if now is not None else time.time()) + int(settings.media_signed_url_ttl_seconds)
    signature = _media_signature(settings, relative_url, expires_at)
    separator = "&" if "?" in relative_url else "?"
    return f"{relative_url}{separator}{urlencode({MEDIA_EXPIRES_PARAM: expires_at, MEDIA_SIGNATURE_PARAM: signature})}"


def verify_media_url_signature(relative_url: str, expires_at: str | None, signature: str | None, settings: Settings) -> bool:
    if not expires_at or not signature:
        return False
    try:
        expires_at_int = int(expires_at)
    except ValueError:
        return False
    if expires_at_int < int(time.time()):
        return False
    expected = _media_signature(settings, relative_url, expires_at_int)
    return hmac.compare_digest(expected, signature)


def resolve_media_relative_path(settings: Settings, media_path: str) -> Path | None:
    cleaned = str(media_path or "").replace("\\", "/").strip("/")
    if not cleaned or any(part in {"", ".", ".."} for part in cleaned.split("/")):
        return None
    root = settings.photos_root.resolve()
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate
