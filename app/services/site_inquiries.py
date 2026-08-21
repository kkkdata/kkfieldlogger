from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.email import send_email


def _record_path(settings: Settings, inquiry_id: str) -> Path:
    return Path(settings.log_dir) / "site-inquiries" / f"{inquiry_id}.json"


def _write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_site_inquiry(
    *,
    settings: Settings,
    payload: dict[str, Any],
    source_ip: str | None,
    host: str | None,
    origin: str | None,
    user_agent: str | None,
) -> tuple[str, Path]:
    created_at = datetime.now(timezone.utc)
    inquiry_id = f"KKH-{created_at:%Y%m%d}-{uuid4().hex[:10].upper()}"
    record = {
        "inquiry_id": inquiry_id,
        "created_at": created_at.isoformat(),
        "delivery_status": "recorded",
        "delivery_updated_at": created_at.isoformat(),
        "payload": payload,
        "request": {
            "source_ip": source_ip,
            "host": host,
            "origin": origin,
            "user_agent": user_agent,
        },
    }
    path = _record_path(settings, inquiry_id)
    _write_record(path, record)
    return inquiry_id, path


def update_site_inquiry_delivery(path: Path, delivery_status: str) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    record["delivery_status"] = delivery_status
    record["delivery_updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_record(path, record)


def _display_rows(payload: dict[str, Any], inquiry_id: str) -> list[tuple[str, str]]:
    labels = {
        "name": "Name",
        "email": "Email",
        "phone": "Phone",
        "location": "County and state",
        "use": "Intended use",
        "preferred_plan": "Preferred plan",
        "home_count": "Number of homes",
        "target_living_area_sq_ft": "Target living area",
        "bedrooms": "Bedrooms",
        "bathrooms": "Bathrooms",
        "land": "Land status",
        "message": "Plans, target date, or questions",
        "utm_source": "UTM source",
        "utm_medium": "UTM medium",
        "utm_campaign": "UTM campaign",
        "utm_content": "UTM content",
        "entry_path": "Entry path",
    }
    rows = [("Inquiry ID", inquiry_id)]
    for key, label in labels.items():
        value = payload.get(key)
        if value is None or str(value).strip() == "":
            continue
        if key == "target_living_area_sq_ft":
            value = f"{value} sq. ft."
        rows.append((label, str(value).strip()))
    return rows


def _email_content(payload: dict[str, Any], inquiry_id: str) -> tuple[str, str]:
    rows = _display_rows(payload, inquiry_id)
    html_rows = "".join(
        "<tr>"
        f"<th style=\"padding:8px 12px;text-align:left;vertical-align:top;border:1px solid #d8e1ea\">{escape(label)}</th>"
        f"<td style=\"padding:8px 12px;border:1px solid #d8e1ea\">{escape(value).replace(chr(10), '<br>')}</td>"
        "</tr>"
        for label, value in rows
    )
    html_content = (
        "<h1>New permanent steel home inquiry</h1>"
        "<p>A prospective customer submitted the project form on kkdatasvc.com.</p>"
        f"<table style=\"border-collapse:collapse\">{html_rows}</table>"
        "<p>Reply to this message to contact the customer directly.</p>"
    )
    text_content = "New permanent steel home inquiry\n\n" + "\n".join(
        f"{label}: {value}" for label, value in rows
    )
    return text_content, html_content


def send_site_inquiry_notification(
    db: Session,
    *,
    settings: Settings,
    payload: dict[str, Any],
    inquiry_id: str,
) -> bool:
    text_content, html_content = _email_content(payload, inquiry_id)
    location = str(payload.get("location") or "location pending").replace("\r", " ").replace("\n", " ")
    primary_email = settings.site_inquiry_email.strip().lower()
    copy_email = (settings.site_inquiry_copy_email or "").strip().lower()
    cc_emails = [copy_email] if copy_email and copy_email != primary_email else []
    return send_email(
        db,
        settings=settings,
        to_email=primary_email,
        cc_emails=cc_emails,
        subject=f"New home inquiry {inquiry_id}: {location[:100]}",
        html_content=html_content,
        text_content=text_content,
        reply_to=str(payload["email"]).lower(),
        action="site_inquiry_notification_sent",
    )
