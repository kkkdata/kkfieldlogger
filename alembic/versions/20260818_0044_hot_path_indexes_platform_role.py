"""Add hot-path indexes and split the platform admin role.

Revision ID: 20260818_0044
Revises: 20260708_0043
Create Date: 2026-08-18 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260818_0044"
down_revision = "20260708_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nearly every photo listing filters by company and sorts by capture time;
    # no index covered captured_at_utc at all before this revision.
    op.create_index(
        "ix_photos_company_deleted_captured",
        "photos",
        ["company_id", "deleted", "captured_at_utc"],
    )
    op.create_index(
        "ix_photos_company_project_captured",
        "photos",
        ["company_id", "project_id", "captured_at_utc"],
    )
    # Serves the duplicate-upload guard in save_mobile_upload.
    op.create_index(
        "ix_photos_company_employee_checksum",
        "photos",
        ["company_id", "employee_id", "checksum"],
    )
    # Matches the worker dequeue predicate and sort.
    op.create_index(
        "ix_task_jobs_claim",
        "task_jobs",
        ["status", "available_at", "priority", "created_at"],
    )

    # Platform powers moved from role super_admin to platform_super_admin
    # (see app/services/rbac.py). Existing platform operators live in the
    # default company; tenant-created super_admin accounts elsewhere must
    # stay tenant-scoped and are deliberately not migrated.
    op.execute(
        sa.text(
            "UPDATE users SET role = 'platform_super_admin' "
            "WHERE role = 'super_admin' AND company_id = 'default'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET role = 'super_admin' "
            "WHERE role = 'platform_super_admin' AND company_id = 'default'"
        )
    )
    op.drop_index("ix_task_jobs_claim", table_name="task_jobs")
    op.drop_index("ix_photos_company_employee_checksum", table_name="photos")
    op.drop_index("ix_photos_company_project_captured", table_name="photos")
    op.drop_index("ix_photos_company_deleted_captured", table_name="photos")
