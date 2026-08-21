"""Shared brute-force throttle for every password login endpoint.

All three login routes (/auth/login, /portal/login, /api/v2/auth/login) must
use this module; a limiter on only one endpoint is bypassed by posting to the
others. State is in-process, so it resets on restart and is per worker - a
deliberate trade-off to avoid a datastore dependency on the login path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

FAILED_LOGIN_BUCKETS: dict[str, list[datetime]] = {}
FAILED_LOGIN_WINDOW = timedelta(minutes=10)
FAILED_LOGIN_LIMIT = 6


def _failed_key(request: Request, identifier: str) -> str:
    # Behind nginx every socket peer is the proxy container, so prefer the
    # X-Real-IP header nginx sets; fall back to the socket address.
    proxy_ip = request.client.host if request.client else "unknown"
    ip = request.headers.get("x-real-ip") or proxy_ip
    return f"{ip}:{identifier.strip().lower()}"


def check_login_rate_limit(request: Request, identifier: str) -> None:
    now = datetime.now(timezone.utc)
    key = _failed_key(request, identifier)
    attempts = [item for item in FAILED_LOGIN_BUCKETS.get(key, []) if item > now - FAILED_LOGIN_WINDOW]
    FAILED_LOGIN_BUCKETS[key] = attempts
    if len(attempts) >= FAILED_LOGIN_LIMIT:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")


def record_failed_login(request: Request, identifier: str) -> None:
    now = datetime.now(timezone.utc)
    key = _failed_key(request, identifier)
    attempts = [item for item in FAILED_LOGIN_BUCKETS.get(key, []) if item > now - FAILED_LOGIN_WINDOW]
    attempts.append(now)
    FAILED_LOGIN_BUCKETS[key] = attempts


def clear_failed_logins(request: Request, identifier: str) -> None:
    FAILED_LOGIN_BUCKETS.pop(_failed_key(request, identifier), None)
