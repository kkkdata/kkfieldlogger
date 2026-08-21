"""task jobs queue

Revision ID: 20260402_0008
Revises: 20260402_0007
Create Date: 2026-04-02 19:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260402_0008"
down_revision = "20260402_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("related_type", sa.String(length=64), nullable=True),
        sa.Column("related_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_jobs_public_id", "task_jobs", ["public_id"], unique=True)
    op.create_index("ix_task_jobs_tenant_id", "task_jobs", ["tenant_id"], unique=False)
    op.create_index("ix_task_jobs_company_id", "task_jobs", ["company_id"], unique=False)
    op.create_index("ix_task_jobs_created_by_user_id", "task_jobs", ["created_by_user_id"], unique=False)
    op.create_index("ix_task_jobs_task_type", "task_jobs", ["task_type"], unique=False)
    op.create_index("ix_task_jobs_status", "task_jobs", ["status"], unique=False)
    op.create_index("ix_task_jobs_priority", "task_jobs", ["priority"], unique=False)
    op.create_index("ix_task_jobs_available_at", "task_jobs", ["available_at"], unique=False)
    op.create_index("ix_task_jobs_related_type", "task_jobs", ["related_type"], unique=False)
    op.create_index("ix_task_jobs_related_id", "task_jobs", ["related_id"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
