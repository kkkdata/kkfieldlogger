"""progress reports and project ai prompts

Revision ID: 20260404_0015
Revises: 20260404_0014
Create Date: 2026-04-04 22:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260404_0015"
down_revision = "20260404_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("image_video_ai_prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("billing_receipt_ai_prompt", sa.Text(), nullable=True))

    op.create_table(
        "progress_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "completed", "failed", name="progress_report_status", native_enum=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("source_photo_ids", sa.JSON(), nullable=True),
        sa.Column("report_content", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_progress_reports_tenant_id", "progress_reports", ["tenant_id"], unique=False)
    op.create_index("ix_progress_reports_company_id", "progress_reports", ["company_id"], unique=False)
    op.create_index("ix_progress_reports_created_by_user_id", "progress_reports", ["created_by_user_id"], unique=False)
    op.create_index("ix_progress_reports_project_id", "progress_reports", ["project_id"], unique=False)
    op.create_index("ix_progress_reports_status", "progress_reports", ["status"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
