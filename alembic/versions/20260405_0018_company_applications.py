"""company applications and tenant review controls

Revision ID: 20260405_0018
Revises: 20260405_0017
Create Date: 2026-04-05 21:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260405_0018"
down_revision = "20260405_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("company_name", sa.String(length=160), nullable=False),
        sa.Column("requested_company_id", sa.String(length=64), nullable=True),
        sa.Column("requested_company_code", sa.String(length=5), nullable=True),
        sa.Column("contact_name", sa.String(length=120), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=False),
        sa.Column("contact_phone", sa.String(length=64), nullable=True),
        sa.Column("contact_title", sa.String(length=120), nullable=True),
        sa.Column("website_url", sa.String(length=255), nullable=True),
        sa.Column("company_intro", sa.Text(), nullable=False),
        sa.Column("requested_username", sa.String(length=64), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="company_application_status", native_enum=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_applications_public_id", "company_applications", ["public_id"], unique=True)
    op.create_index("ix_company_applications_requested_company_id", "company_applications", ["requested_company_id"], unique=False)
    op.create_index("ix_company_applications_requested_company_code", "company_applications", ["requested_company_code"], unique=False)
    op.create_index("ix_company_applications_contact_email", "company_applications", ["contact_email"], unique=False)
    op.create_index("ix_company_applications_status", "company_applications", ["status"], unique=False)
    op.create_index("ix_company_applications_reviewed_by_user_id", "company_applications", ["reviewed_by_user_id"], unique=False)
    op.create_index("ix_company_applications_approved_company_id", "company_applications", ["approved_company_id"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
