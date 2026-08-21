from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import SystemSetting

DEFAULT_SETTING_KEYS = {
    "system_title": "KK Field Logger",
    "portal_title": "KK Field Logger Portal",
    "company_name": "K&K Data Service Inc.",
    "timezone": "America/Chicago",
    "logo_path": "/static/img/kk-logo.png",
    "storage_base_url": "",
    "upload_limit_mb": "50",
    "default_visibility": "internal",
    "ai_backends": "[]",
    "ai_max_concurrent_requests": "3",
    "ai_enable_dynamic_fallback": "true",
    "ai_backend_failure_cooldown_seconds": "120",
    "voice_transcription_model": "small",
    "voice_translation_ollama_model": "qwen2.5:7b-instruct",
    "video_retention_days": "30",
    "hot_storage_days": "7",
    "video_delete_files_only": "true",
    "camera_failure_alert_threshold": "3",
    "storage_cost_per_gb_month": "0.12",
    "storage_warning_threshold_percent": "80",
    "system_health_storage_alert_threshold_percent": "85",
    "system_health_alert_cooldown_seconds": "1800",
}

LEGACY_LOGO_PATHS = {"/static/img/logo.svg"}


def _normalize_logo_path(value: str | None, app_settings: Settings) -> str:
    normalized = str(value or "").strip() or app_settings.logo_path
    if normalized in LEGACY_LOGO_PATHS:
        return app_settings.logo_path
    return normalized


def bootstrap_system_settings(db: Session, app_settings: Settings) -> None:
    defaults = DEFAULT_SETTING_KEYS | {
        "system_title": app_settings.app_name,
        "portal_title": app_settings.portal_title,
        "company_name": app_settings.company_name,
        "timezone": app_settings.default_timezone,
        "logo_path": app_settings.logo_path,
        "storage_base_url": app_settings.public_base_url.rstrip("/"),
        "upload_limit_mb": str(app_settings.upload_max_mb),
        "default_visibility": app_settings.default_visibility,
    }
    existing = {item.key for item in db.scalars(select(SystemSetting)).all()}
    dirty = False
    for key, value in defaults.items():
        if key in existing:
            continue
        db.add(SystemSetting(key=key, value=value))
        dirty = True
    if dirty:
        db.commit()
    logo_setting = db.get(SystemSetting, "logo_path")
    if logo_setting is not None:
        normalized_logo = _normalize_logo_path(logo_setting.value, app_settings)
        if logo_setting.value != normalized_logo:
            logo_setting.value = normalized_logo
            db.commit()


def get_system_settings(db: Session, app_settings: Settings) -> dict[str, str]:
    bootstrap_system_settings(db, app_settings)
    saved = {item.key: item.value for item in db.scalars(select(SystemSetting)).all()}
    settings = DEFAULT_SETTING_KEYS | {
        "system_title": app_settings.app_name,
        "portal_title": app_settings.portal_title,
        "company_name": app_settings.company_name,
        "timezone": app_settings.default_timezone,
        "logo_path": app_settings.logo_path,
        "storage_base_url": app_settings.public_base_url.rstrip("/"),
        "upload_limit_mb": str(app_settings.upload_max_mb),
        "default_visibility": app_settings.default_visibility,
    } | saved
    settings["logo_path"] = _normalize_logo_path(settings.get("logo_path"), app_settings)
    return settings


def update_system_settings(db: Session, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        setting = db.get(SystemSetting, key)
        if setting is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            setting.value = value
    db.commit()
