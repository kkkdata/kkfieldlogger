#!/usr/bin/env python
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.core.config import Settings
from app.db.session import create_engine_from_settings, create_session_maker
from app.models import AuditLog, Employee, Photo, PhotoComment, Project, ProjectMember, SystemSetting, User


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare SQLite counts against PostgreSQL counts after migration.")
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--tenant-id", default="default")
    return parser.parse_args()


def sqlite_count(conn: sqlite3.Connection, table_name: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def main() -> int:
    args = parse_args()
    sqlite_conn = sqlite3.connect(args.sqlite_path)
    settings = Settings(database_url=args.postgres_url)
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)

    with session_maker() as db:
        postgres_counts = {
            "users": db.scalar(select(func.count()).select_from(select(User).where(User.company_id == args.tenant_id).subquery())) or 0,
            "employees": db.scalar(select(func.count()).select_from(select(Employee).where(Employee.company_id == args.tenant_id).subquery())) or 0,
            "projects": db.scalar(select(func.count()).select_from(select(Project).where(Project.company_id == args.tenant_id).subquery())) or 0,
            "project_members": db.scalar(select(func.count()).select_from(ProjectMember)) or 0,
            "photos": db.scalar(select(func.count()).select_from(select(Photo).where(Photo.company_id == args.tenant_id).subquery())) or 0,
            "photo_comments": db.scalar(select(func.count()).select_from(PhotoComment)) or 0,
            "audit_logs": db.scalar(select(func.count()).select_from(select(AuditLog).where(AuditLog.company_id == args.tenant_id).subquery())) or 0,
            "system_settings": db.scalar(select(func.count()).select_from(SystemSetting)) or 0,
        }

    sqlite_counts = {
        table_name: sqlite_count(sqlite_conn, table_name)
        for table_name in ("users", "employees", "projects", "project_members", "photos", "photo_comments", "audit_logs", "system_settings")
    }

    mismatches = 0
    print("Migration verification summary:")
    for table_name, sqlite_value in sqlite_counts.items():
        postgres_value = postgres_counts.get(table_name, 0)
        status = "OK" if postgres_value >= sqlite_value else "MISMATCH"
        if status != "OK":
            mismatches += 1
        print(f"  {table_name}: sqlite={sqlite_value} postgres={postgres_value} status={status}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
