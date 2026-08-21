"""generated reports

Revision ID: 20260402_0007
Revises: 20260402_0006
Create Date: 2026-04-02 16:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260402_0007"
down_revision = "20260402_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("source_photo_ids", sa.JSON(), nullable=True),
        sa.Column("markdown_content", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_generated_reports_public_id", "generated_reports", ["public_id"], unique=True)
    op.create_index("ix_generated_reports_tenant_id", "generated_reports", ["tenant_id"], unique=False)
    op.create_index("ix_generated_reports_company_id", "generated_reports", ["company_id"], unique=False)
    op.create_index("ix_generated_reports_created_by_user_id", "generated_reports", ["created_by_user_id"], unique=False)
    op.create_index("ix_generated_reports_status", "generated_reports", ["status"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
