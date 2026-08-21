from __future__ import annotations

import secrets

from fastapi import HTTPException, Request


def login_user(request: Request, user_id: int) -> None:
    language = request.session.get("portal_language")
    request.session.clear()
    if language:
        request.session["portal_language"] = language
    request.session["user_id"] = user_id
    request.session["csrf_token"] = secrets.token_hex(24)


def logout_user(request: Request) -> None:
    language = request.session.get("portal_language")
    request.session.clear()
    if language:
        request.session["portal_language"] = language


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if token:
        return token
    token = secrets.token_hex(24)
    request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, token: str) -> None:
    session_token = request.session.get("csrf_token")
    if not session_token or session_token != token:
        raise HTTPException(status_code=400, detail="Invalid CSRF token")


def flash(request: Request, level: str, message: str) -> None:
    request.session.setdefault("_flashes", []).append({"level": level, "message": message})


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("_flashes", [])
