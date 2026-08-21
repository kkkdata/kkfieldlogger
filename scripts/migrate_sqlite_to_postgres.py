#!/usr/bin/env python
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.core.config import Settings
from app.db.session import create_engine_from_settings, create_session_maker
from app.models import (
    ApprovalStatus,
    AuditLog,
    Company,
    Employee,
    Membership,
    MembershipStatus,
    Photo,
    PhotoComment,
    PhotoType,
    PhotoVisibility,
    PlanCode,
    Project,
    ProjectMember,
    ProjectStatus,
    SubscriptionStatus,
    SystemSetting,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.services.billing import ensure_plans, ensure_subscription
from app.services.bootstrap import bootstrap_platform_data


TABLES = ("users", "employees", "projects", "project_members", "photos", "photo_comments", "audit_logs", "system_settings")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate a legacy KK Field Logger SQLite database into PostgreSQL.")
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--default-tenant-id", default="default")
    parser.add_argument("--default-tenant-name", default="Default Company")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def fetch_rows(conn: sqlite3.Connection, table_name: str) -> list[dict]:
    if not table_exists(conn, table_name):
        return []
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table_name}").fetchall()]


def parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text_value = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_enum(enum_cls, value, default):
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default


def ensure_default_scope(db, tenant_id: str, tenant_name: str) -> None:
    company = db.query(Company).filter_by(company_id=tenant_id).one_or_none()
    if company is None:
        db.add(Company(company_id=tenant_id, company_name=tenant_name, active=True))
        db.flush()
    tenant = db.query(Tenant).filter_by(slug=tenant_id).one_or_none()
    if tenant is None:
        db.add(
            Tenant(
                id=str(uuid4()),
                slug=tenant_id,
                name=tenant_name,
                status=TenantStatus.active,
                current_plan_id=PlanCode.business,
            )
        )
        db.flush()
    ensure_subscription(db, company_id=tenant_id, plan_id=PlanCode.business, status=SubscriptionStatus.active)


def migrate_users(db, rows: list[dict], tenant_id: str) -> int:
    imported = 0
    for row in rows:
        user = db.get(User, row.get("id")) if row.get("id") else None
        if user is None and row.get("username"):
            user = db.query(User).filter_by(username=str(row["username"]).strip().lower()).one_or_none()
        if user is None and row.get("email"):
            user = db.query(User).filter_by(email=str(row["email"]).strip().lower()).one_or_none()
        if user is None:
            user = User(id=row.get("id")) if row.get("id") else User()
        user.public_id = row.get("public_id") or user.public_id or str(uuid4())
        user.company_id = tenant_id
        user.username = str(row.get("username") or row.get("email") or f"legacy-{uuid4().hex[:8]}").strip().lower()
        user.password_hash = row.get("password_hash") or user.password_hash or ""
        user.role = parse_enum(UserRole, row.get("role"), UserRole.super_admin)
        user.display_name = row.get("display_name") or user.display_name or user.username
        user.email = str(row["email"]).strip().lower() if row.get("email") else user.email
        user.employee_id = row.get("employee_id")
        user.active = parse_bool(row.get("active"), True)
        user.is_verified = parse_bool(row.get("is_verified"), False)
        user.created_at = parse_dt(row.get("created_at")) or user.created_at or now_utc()
        user.updated_at = parse_dt(row.get("updated_at")) or user.updated_at or user.created_at
        user.last_login_at = parse_dt(row.get("last_login_at")) or user.last_login_at
        db.add(user)
        db.flush()
        membership = db.query(Membership).filter_by(tenant_id=tenant_id, user_id=user.id).one_or_none()
        if membership is None:
            db.add(
                Membership(
                    tenant_id=tenant_id,
                    user_id=user.id,
                    role=user.role.value,
                    status=MembershipStatus.active,
                    created_at=user.created_at,
                )
            )
        imported += 1
    return imported


def migrate_projects(db, rows: list[dict], tenant_id: str) -> int:
    imported = 0
    for row in rows:
        project = db.get(Project, row.get("id")) if row.get("id") else None
        if project is None and row.get("project_id"):
            project = db.query(Project).filter_by(project_id=row["project_id"]).one_or_none()
        if project is None:
            project = Project(id=row.get("id")) if row.get("id") else Project()
        project.tenant_id = tenant_id
        project.company_id = tenant_id
        project.project_id = row.get("project_id") or project.project_id or f"legacy-project-{uuid4().hex[:8]}"
        project.project_name = row.get("project_name") or project.project_id
        project.client_name = row.get("client_name") or ""
        project.location = row.get("location") or ""
        project.status = parse_enum(ProjectStatus, row.get("status"), ProjectStatus.active)
        project.created_at = parse_dt(row.get("created_at")) or project.created_at or now_utc()
        project.created_by = row.get("created_by")
        db.add(project)
        imported += 1
    return imported


def migrate_employees(db, rows: list[dict], tenant_id: str) -> int:
    imported = 0
    for row in rows:
        employee = db.get(Employee, row.get("id")) if row.get("id") else None
        if employee is None and row.get("employee_id"):
            employee = db.query(Employee).filter_by(employee_id=row["employee_id"]).one_or_none()
        if employee is None:
            employee = Employee(id=row.get("id")) if row.get("id") else Employee()
        employee.tenant_id = tenant_id
        employee.company_id = tenant_id
        employee.employee_id = row.get("employee_id") or employee.employee_id or f"legacy-employee-{uuid4().hex[:8]}"
        employee.name = row.get("name") or employee.employee_id
        employee.api_key = row.get("api_key") or employee.api_key or f"legacy-{uuid4().hex}"
        employee.role = row.get("role") or employee.role or "worker"
        employee.active = parse_bool(row.get("active"), True)
        employee.project_id = row.get("project_id")
        employee.created_at = parse_dt(row.get("created_at")) or employee.created_at or now_utc()
        db.add(employee)
        imported += 1
    return imported


def migrate_project_members(db, rows: list[dict]) -> int:
    imported = 0
    for row in rows:
        project_id = row.get("project_id")
        user_id = row.get("user_id")
        if not project_id or not user_id:
            continue
        existing = db.query(ProjectMember).filter_by(project_id=project_id, user_id=user_id).one_or_none()
        if existing is not None:
            continue
        db.add(
            ProjectMember(
                id=row.get("id"),
                project_id=project_id,
                user_id=user_id,
                role_in_project=row.get("role_in_project") or "member",
                created_at=parse_dt(row.get("created_at")) or now_utc(),
            )
        )
        imported += 1
    return imported


def migrate_photos(db, rows: list[dict], tenant_id: str) -> int:
    imported = 0
    for row in rows:
        employee_id = row.get("employee_id")
        if not employee_id:
            continue
        photo = db.get(Photo, row.get("id")) if row.get("id") else None
        if photo is None and row.get("file_path"):
            photo = db.query(Photo).filter_by(file_path=row["file_path"]).one_or_none()
        if photo is None:
            photo = Photo(id=row.get("id")) if row.get("id") else Photo()

        approval_status = row.get("approval_status")
        if approval_status is None and parse_bool(row.get("approved_by_manager"), False):
            approval_status = ApprovalStatus.approved.value

        photo.tenant_id = tenant_id
        photo.company_id = tenant_id
        photo.uploaded_by_user_id = row.get("uploaded_by_user_id")
        photo.employee_id = employee_id
        photo.project_id = row.get("project_id")
        photo.photo_type = parse_enum(PhotoType, row.get("photo_type"), PhotoType.project)
        photo.file_path = row.get("file_path") or row.get("storage_path") or ""
        photo.storage_path = row.get("storage_path") or photo.file_path
        photo.image_url = row.get("image_url") or photo.image_url or ""
        photo.thumb_url = row.get("thumb_url")
        photo.original_file_name = row.get("original_file_name") or row.get("original_filename") or Path(photo.file_path or "legacy.jpg").name
        photo.mime_type = row.get("mime_type")
        photo.file_size = row.get("file_size")
        photo.checksum = row.get("checksum")
        photo.gps = row.get("gps")
        photo.gps_lat = row.get("gps_lat")
        photo.gps_lon = row.get("gps_lon")
        photo.gps_lng = row.get("gps_lng") if row.get("gps_lng") is not None else row.get("gps_lon")
        photo.location = row.get("location")
        photo.heading = row.get("heading")
        photo.pitch = row.get("pitch")
        photo.roll = row.get("roll")
        photo.captured_at_utc = parse_dt(row.get("captured_at_utc")) or parse_dt(row.get("timestamp")) or parse_dt(row.get("created_at")) or now_utc()
        photo.created_at = parse_dt(row.get("created_at")) or photo.captured_at_utc
        photo.deleted = parse_bool(row.get("deleted"), False)
        photo.deleted_at = parse_dt(row.get("deleted_at"))
        photo.soft_deleted_at = parse_dt(row.get("soft_deleted_at")) or photo.deleted_at
        photo.visibility = parse_enum(PhotoVisibility, row.get("visibility"), PhotoVisibility.internal)
        photo.approval_status = parse_enum(ApprovalStatus, approval_status, ApprovalStatus.pending)
        photo.approved_by_manager = parse_bool(row.get("approved_by_manager"), photo.approval_status == ApprovalStatus.approved)
        photo.approved_by_user_id = row.get("approved_by_user_id")
        photo.approved_at = parse_dt(row.get("approved_at"))
        photo.featured = parse_bool(row.get("featured"), False)
        photo.note = row.get("note")
        photo.tag_json = row.get("tag_json")
        photo.metadata_json = row.get("metadata_json")
        photo.device_model = row.get("device_model")
        photo.os_version = row.get("os_version")
        photo.app_version = row.get("app_version")
        photo.weather_json = row.get("weather_json")
        photo.ocr_status = row.get("ocr_status")
        photo.labeling_status = row.get("labeling_status")
        photo.defect_status = row.get("defect_status")
        photo.scene_status = row.get("scene_status")
        photo.duplicate_hash = row.get("duplicate_hash")
        photo.embedding_ref = row.get("embedding_ref")
        photo.reconstruction_batch_id = row.get("reconstruction_batch_id")
        db.add(photo)
        imported += 1
    return imported


def migrate_comments(db, rows: list[dict]) -> int:
    imported = 0
    for row in rows:
        if not row.get("photo_id") or not row.get("user_id") or not row.get("comment"):
            continue
        comment = db.get(PhotoComment, row.get("id")) if row.get("id") else None
        if comment is None:
            comment = PhotoComment(id=row.get("id")) if row.get("id") else PhotoComment()
        comment.photo_id = row["photo_id"]
        comment.user_id = row["user_id"]
        comment.comment = row["comment"]
        comment.created_at = parse_dt(row.get("created_at")) or now_utc()
        db.add(comment)
        imported += 1
    return imported


def migrate_audit_logs(db, rows: list[dict], tenant_id: str) -> int:
    imported = 0
    for row in rows:
        log = db.get(AuditLog, row.get("id")) if row.get("id") else None
        if log is None:
            log = AuditLog(id=row.get("id")) if row.get("id") else AuditLog()
        log.tenant_id = tenant_id
        log.company_id = tenant_id
        log.actor_user_id = row.get("actor_user_id")
        log.action = row.get("action") or "legacy_import"
        log.target_type = row.get("target_type") or "legacy"
        log.target_id = row.get("target_id")
        log.project_id = row.get("project_id")
        log.detail_json = row.get("detail_json")
        log.created_at = parse_dt(row.get("created_at")) or now_utc()
        log.ip_address = row.get("ip_address")
        db.add(log)
        imported += 1
    return imported


def migrate_system_settings(db, rows: list[dict]) -> int:
    imported = 0
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        setting = db.get(SystemSetting, key)
        if setting is None:
            setting = SystemSetting(key=key, value=str(row.get("value") or ""))
        else:
            setting.value = str(row.get("value") or setting.value)
        db.add(setting)
        imported += 1
    return imported


def sync_sequences(db) -> None:
    if db.bind.dialect.name != "postgresql":
        return
    for table_name in ("users", "employees", "projects", "project_members", "photos", "photo_comments", "audit_logs", "plans", "subscriptions"):
        db.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), COALESCE((SELECT MAX(id) FROM {table_name}), 1), true)"
            )
        )


def main() -> int:
    args = parse_args()
    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    settings = Settings(
        database_url=args.postgres_url,
        default_company_id=args.default_tenant_id,
        default_company_name=args.default_tenant_name,
    )
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)

    summary: dict[str, int] = {}
    with session_maker() as db:
        bootstrap_platform_data(db, settings)
        ensure_plans(db)
        ensure_default_scope(db, args.default_tenant_id, args.default_tenant_name)

        summary["users"] = migrate_users(db, fetch_rows(sqlite_conn, "users"), args.default_tenant_id)
        summary["projects"] = migrate_projects(db, fetch_rows(sqlite_conn, "projects"), args.default_tenant_id)
        summary["employees"] = migrate_employees(db, fetch_rows(sqlite_conn, "employees"), args.default_tenant_id)
        summary["project_members"] = migrate_project_members(db, fetch_rows(sqlite_conn, "project_members"))
        summary["photos"] = migrate_photos(db, fetch_rows(sqlite_conn, "photos"), args.default_tenant_id)
        summary["photo_comments"] = migrate_comments(db, fetch_rows(sqlite_conn, "photo_comments"))
        summary["audit_logs"] = migrate_audit_logs(db, fetch_rows(sqlite_conn, "audit_logs"), args.default_tenant_id)
        summary["system_settings"] = migrate_system_settings(db, fetch_rows(sqlite_conn, "system_settings"))
        sync_sequences(db)

        if args.dry_run:
            db.rollback()
            print("Dry run completed. No PostgreSQL changes were committed.")
        else:
            db.commit()
            print("Migration committed successfully.")

    print("Migration summary:")
    for table_name in TABLES:
        print(f"  {table_name}: {summary.get(table_name, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
