from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from sqlalchemy import func, select

from app.core.config import load_settings
from app.core.security import hash_password
from app.db.session import create_engine_from_settings, create_session_maker
from app.models import (
    Company,
    Employee,
    GeneratedReport,
    MediaAsset,
    Photo,
    PlanCode,
    Project,
    ProjectStatus,
    Subscription,
    SubscriptionStatus,
    SystemSetting,
    TaskJob,
    User,
    UserRole,
)
from app.services.bootstrap import bootstrap_platform_data
from app.services.settings import bootstrap_system_settings


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xd9\xa4\x1e\x00\x00\x00\x00IEND\xaeB`\x82"
)


def ensure_fixture_data() -> None:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        bootstrap_platform_data(db, settings)
        bootstrap_system_settings(db, settings)

        company = db.scalar(select(Company).where(Company.company_id == settings.default_company_id))
        if company is None:
            company = Company(
                company_id=settings.default_company_id,
                company_code="10000",
                company_name=settings.default_company_name,
                active=True,
            )
            db.add(company)
            db.flush()

        subscription = db.scalar(select(Subscription).where(Subscription.company_id == settings.default_company_id))
        if subscription is None:
            db.add(
                Subscription(
                    company_id=settings.default_company_id,
                    plan_id=PlanCode.business,
                    status=SubscriptionStatus.active,
                )
            )

        admin = db.scalar(select(User).where(User.email == settings.default_admin_email))
        if admin is None:
            db.add(
                User(
                    company_id=settings.default_company_id,
                    username=settings.default_admin_username,
                    password_hash=hash_password(settings.default_admin_password),
                    role=UserRole.super_admin,
                    display_name=settings.default_admin_display_name,
                    email=settings.default_admin_email,
                    active=True,
                )
            )

        project = db.scalar(select(Project).where(Project.project_id == "P100"))
        if project is None:
            project = Project(
                tenant_id=settings.default_company_id,
                company_id=settings.default_company_id,
                project_id="P100",
                project_name="Smoke Test Project",
                client_name="Smoke Client",
                location="Local Test Site",
                status=ProjectStatus.active,
            )
            db.add(project)
            db.flush()

        employee = db.scalar(select(Employee).where(Employee.employee_id == "E100"))
        if employee is None:
            db.add(
                Employee(
                    tenant_id=settings.default_company_id,
                    company_id=settings.default_company_id,
                    employee_id="E100",
                    name="Smoke Employee",
                    api_key="api-key-e100",
                    role="worker",
                    active=True,
                    project_id="P100",
                )
            )

        ai_backends = [
            {
                "id": "mock-ollama",
                "type": "ollama",
                "url": "http://127.0.0.1:8010",
                "model": "mock-vision:latest",
                "embedding_model": "mock-embed:latest",
                "weight": 1,
                "enabled": True,
            }
        ]
        setting = db.get(SystemSetting, "ai_backends")
        if setting is None:
            db.add(SystemSetting(key="ai_backends", value=json.dumps(ai_backends)))
        else:
            setting.value = json.dumps(ai_backends)
            db.add(setting)

        storage_setting = db.get(SystemSetting, "storage_base_url")
        if storage_setting is None:
            db.add(SystemSetting(key="storage_base_url", value=settings.public_base_url.rstrip("/")))
        else:
            storage_setting.value = settings.public_base_url.rstrip("/")
            db.add(storage_setting)

        db.commit()


def require_ok(response: requests.Response, label: str) -> None:
    if response.status_code != 200 and response.status_code != 202:
        raise RuntimeError(f"{label} failed: {response.status_code} {response.text[:400]}")


def poll_report(session: requests.Session, base_url: str, report_id: str, timeout_seconds: int = 45) -> dict:
    deadline = time.time() + timeout_seconds
    last_payload: dict | None = None
    while time.time() < deadline:
        response = session.get(f"{base_url}/api/v2/reports/{report_id}", timeout=10)
        require_ok(response, "report status")
        payload = response.json()
        last_payload = payload
        if payload.get("status") == "completed":
            return payload
        if payload.get("status") == "failed":
            raise RuntimeError(f"Report generation failed: {payload}")
        time.sleep(1.0)
    raise RuntimeError(f"Timed out waiting for report completion. Last payload: {last_payload}")


def verify_database_side_effects(photo_id: int, report_public_id: str) -> dict[str, int]:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        if photo is None:
            raise RuntimeError("Uploaded photo record was not created")
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == report_public_id))
        if report is None:
            raise RuntimeError("Report record was not created")
        task_count = db.scalar(select(func.count()).select_from(select(TaskJob.id).subquery())) or 0
        return {
            "photo_id": photo.id,
            "report_id": report.id,
            "task_count": int(task_count),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end smoke validation for the local app")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    ensure_fixture_data()

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    base_url = args.base_url.rstrip("/")

    healthz = session.get(f"{base_url}/api/v2/healthz", timeout=10)
    require_ok(healthz, "healthz")

    readyz = session.get(f"{base_url}/api/v2/readyz", timeout=10)
    require_ok(readyz, "readyz")

    root = session.get(f"{base_url}/", headers={"Accept": "text/html"}, timeout=10)
    if root.status_code != 200 or "text/html" not in root.headers.get("content-type", ""):
        raise RuntimeError(f"root route failed: {root.status_code} {root.headers.get('content-type')}")

    portal_login = session.get(f"{base_url}/portal/login", headers={"Accept": "text/html"}, timeout=10)
    if portal_login.status_code != 200 or "text/html" not in portal_login.headers.get("content-type", ""):
        raise RuntimeError(f"portal login failed: {portal_login.status_code} {portal_login.text[:200]}")

    legacy_health = session.get(f"{base_url}/health", timeout=10)
    require_ok(legacy_health, "legacy health")

    cors_preflight = session.options(
        f"{base_url}/api/v2/healthz",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
        timeout=10,
    )
    if cors_preflight.status_code not in {200, 204}:
        raise RuntimeError(f"CORS preflight failed: {cors_preflight.status_code}")
    allow_origin = cors_preflight.headers.get("access-control-allow-origin")
    if allow_origin not in {"http://localhost:5173", "*"}:
        raise RuntimeError(f"CORS allow-origin missing or wrong: {allow_origin}")

    settings = load_settings()
    login_response = session.post(
        f"{base_url}/api/v2/auth/login",
        json={"email": settings.default_admin_email, "password": settings.default_admin_password},
        timeout=10,
    )
    require_ok(login_response, "api login")

    upload_response = session.post(
        f"{base_url}/upload",
        headers={"X-API-Key": "api-key-e100"},
        files={"photo": ("smoke.png", PNG_BYTES, "image/png")},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-04-04T20:00:00Z",
            "location": "Local validation upload",
            "note": "Smoke upload from automated validation",
        },
        timeout=20,
    )
    require_ok(upload_response, "legacy upload")
    upload_payload = upload_response.json()
    photo_id = int(upload_payload["photo"]["id"])
    image_url = upload_payload["photo"]["image_url"]

    image_response = session.get(image_url, timeout=10)
    if image_response.status_code != 200:
        raise RuntimeError(f"Uploaded image fetch failed: {image_response.status_code}")

    media_upload_response = session.post(
        f"{base_url}/api/v2/media/upload",
        files={"upload": ("smoke-media.png", PNG_BYTES, "image/png")},
        data={
            "media_type": "image",
            "source": "manual_upload",
            "filename": "smoke-media.png",
            "metadata_json": json.dumps({"smoke_test": True}),
        },
        timeout=20,
    )
    require_ok(media_upload_response, "media upload")
    media_upload_payload = media_upload_response.json()
    media_asset_id = str(media_upload_payload["asset_id"])

    media_detail_response = session.get(f"{base_url}/api/v2/media/{media_asset_id}/detail", timeout=10)
    require_ok(media_detail_response, "media detail")
    media_detail_payload = media_detail_response.json()
    media_stream_url = media_detail_payload["media"]["stream_url"]
    if not media_stream_url:
        raise RuntimeError(f"Media detail missing stream_url: {media_detail_payload}")
    media_stream_response = session.get(f"{base_url}{media_stream_url}", timeout=10)
    if media_stream_response.status_code != 200:
        raise RuntimeError(f"Media stream fetch failed: {media_stream_response.status_code}")

    report_response = session.post(
        f"{base_url}/api/v2/reports/generate",
        json={"photo_ids": [photo_id], "prompt": "Generate a concise smoke-test inspection report."},
        timeout=20,
    )
    if report_response.status_code != 202:
        raise RuntimeError(f"Report generation request failed: {report_response.status_code} {report_response.text[:400]}")
    report_payload = report_response.json()
    report_public_id = str(report_payload["report_id"])
    completed_report = poll_report(session, base_url, report_public_id)

    download_url = completed_report.get("download_url")
    if not download_url:
        raise RuntimeError(f"Completed report missing download_url: {completed_report}")
    report_download = session.get(f"{base_url}{download_url}", timeout=20)
    if report_download.status_code != 200:
        raise RuntimeError(f"Report download failed: {report_download.status_code}")

    db_checks = verify_database_side_effects(photo_id, report_public_id)
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        media_asset = db.get(MediaAsset, media_asset_id)
        if media_asset is None:
            raise RuntimeError("Media asset record was not created")
    result = {
        "healthz": healthz.json(),
        "readyz": readyz.json(),
        "legacy_health": legacy_health.json(),
        "uploaded_photo_id": photo_id,
        "uploaded_image_url": image_url,
        "media_asset_id": media_asset_id,
        "media_stream_url": media_stream_url,
        "report_public_id": report_public_id,
        "report_status": completed_report["status"],
        "db_checks": db_checks,
        "cors_allow_origin": allow_origin,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI smoke surface
        print(f"SMOKE_VALIDATION_FAILED: {exc}", file=sys.stderr)
        raise
