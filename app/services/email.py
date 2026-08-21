from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from urllib import error, request as urlrequest

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.audit import log_audit


def _sender(settings: Settings) -> str:
    address = settings.email_sender_address or settings.sendgrid_from_email or "noreply@example.com"
    return f"{settings.email_sender_name} <{address}>"


def _send_via_sendgrid(
    settings: Settings,
    *,
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str | None = None,
    reply_to: str | None = None,
    cc_emails: list[str] | None = None,
) -> None:
    personalization = {"to": [{"email": to_email}]}
    if cc_emails:
        personalization["cc"] = [{"email": email} for email in cc_emails]
    payload = {
        "personalizations": [personalization],
        "from": {"email": settings.sendgrid_from_email or settings.email_sender_address or "noreply@example.com"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_content or "K&K website notification"},
            {"type": "text/html", "value": html_content},
        ],
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    req = urlrequest.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=10):
        return None


def _send_via_smtp(
    settings: Settings,
    *,
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str | None = None,
    reply_to: str | None = None,
    cc_emails: list[str] | None = None,
) -> None:
    if not settings.smtp_host or not settings.email_sender_address:
        raise RuntimeError("SMTP is not fully configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _sender(settings)
    message["To"] = to_email
    if cc_emails:
        message["Cc"] = ", ".join(cc_emails)
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text_content or "K&K website notification")
    message.add_alternative(html_content, subtype="html")

    if settings.smtp_use_ssl:
        client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with client as smtp:
        if settings.smtp_use_starttls and not settings.smtp_use_ssl:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        refused_recipients = smtp.send_message(message)
        if refused_recipients:
            raise smtplib.SMTPRecipientsRefused(refused_recipients)


def send_email(
    db: Session,
    *,
    settings: Settings,
    to_email: str,
    subject: str,
    html_content: str,
    action: str,
    company_id: str | None = None,
    text_content: str | None = None,
    reply_to: str | None = None,
    cc_emails: list[str] | None = None,
) -> bool:
    try:
        backend = settings.email_backend.lower()
        if backend == "console":
            log_audit(
                db,
                action=f"{action}_skipped",
                target_type="email",
                target_id=to_email,
                detail_json={"reason": "console_backend", "subject": subject, "cc": cc_emails or []},
                company_id=company_id,
            )
            return False
        if backend == "sendgrid":
            if not settings.sendgrid_api_key:
                raise RuntimeError("SendGrid API key is missing")
            _send_via_sendgrid(
                settings,
                to_email=to_email,
                subject=subject,
                html_content=html_content,
                text_content=text_content,
                reply_to=reply_to,
                cc_emails=cc_emails,
            )
        else:
            _send_via_smtp(
                settings,
                to_email=to_email,
                subject=subject,
                html_content=html_content,
                text_content=text_content,
                reply_to=reply_to,
                cc_emails=cc_emails,
            )
        log_audit(
            db,
            action=action,
            target_type="email",
            target_id=to_email,
            detail_json={"subject": subject, "backend": backend, "cc": cc_emails or []},
            company_id=company_id,
        )
        return True
    except (RuntimeError, ValueError, error.URLError, OSError, smtplib.SMTPException) as exc:
        log_audit(
            db,
            action=f"{action}_failed",
            target_type="email",
            target_id=to_email,
            detail_json={
                "subject": subject,
                "error": str(exc),
                "backend": settings.email_backend,
                "cc": cc_emails or [],
            },
            company_id=company_id,
        )
        return False
