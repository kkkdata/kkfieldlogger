"""saas multi-tenant upgrade

Revision ID: 20260319_0003
Revises: 20260318_0002
Create Date: 2026-03-19 09:10:00

"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260319_0003"
down_revision = "20260318_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    now = datetime.now(timezone.utc)

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("company_name", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("company_id"),
    )
    op.create_index("ix_companies_company_id", "companies", ["company_id"])

    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("monthly_price_cents", sa.Integer(), nullable=False),
        sa.Column("monthly_photo_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("plan_id"),
    )
    op.create_index("ix_plans_plan_id", "plans", ["plan_id"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.plan_id"]),
    )
    op.create_index("ix_subscriptions_company_id", "subscriptions", ["company_id"])
    op.create_index("ix_subscriptions_plan_id", "subscriptions", ["plan_id"])

    op.create_table(
        "usage_counters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("month_key", sa.String(length=16), nullable=False),
        sa.Column("photos_this_month", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"]),
        sa.UniqueConstraint("company_id", "month_key", name="uq_usage_counters_company_month"),
    )
    op.create_index("ix_usage_counters_company_id", "usage_counters", ["company_id"])
    op.create_index("ix_usage_counters_month_key", "usage_counters", ["month_key"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"])

    op.bulk_insert(
        sa.table(
            "plans",
            sa.column("plan_id", sa.String()),
            sa.column("display_name", sa.String()),
            sa.column("monthly_price_cents", sa.Integer()),
            sa.column("monthly_photo_limit", sa.Integer()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("active", sa.Boolean()),
        ),
        [
            {
                "plan_id": "free",
                "display_name": "Free",
                "monthly_price_cents": 0,
                "monthly_photo_limit": 250,
                "created_at": now,
                "active": True,
            },
            {
                "plan_id": "basic",
                "display_name": "Basic",
                "monthly_price_cents": 900,
                "monthly_photo_limit": 5000,
                "created_at": now,
                "active": True,
            },
            {
                "plan_id": "pro",
                "display_name": "Pro",
                "monthly_price_cents": 2900,
                "monthly_photo_limit": 50000,
                "created_at": now,
                "active": True,
            },
        ],
    )

    op.bulk_insert(
        sa.table(
            "companies",
            sa.column("company_id", sa.String()),
            sa.column("company_name", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("active", sa.Boolean()),
        ),
        [
            {
                "company_id": "default",
                "company_name": "Default Company",
                "created_at": now,
                "active": True,
            }
        ],
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.String(length=64), nullable=True, server_default="default"))
        batch_op.create_index("ix_users_company_id", ["company_id"], unique=False)

    with op.batch_alter_table("employees") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.String(length=64), nullable=True, server_default="default"))
        batch_op.create_index("ix_employees_company_id", ["company_id"], unique=False)

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.String(length=64), nullable=True, server_default="default"))
        batch_op.create_index("ix_projects_company_id", ["company_id"], unique=False)

    with op.batch_alter_table("photos") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.String(length=64), nullable=True, server_default="default"))
        batch_op.create_index("ix_photos_company_id", ["company_id"], unique=False)

    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_audit_logs_company_id", ["company_id"], unique=False)

    op.execute("UPDATE users SET company_id = 'default' WHERE company_id IS NULL")
    op.execute("UPDATE employees SET company_id = 'default' WHERE company_id IS NULL")
    op.execute("UPDATE projects SET company_id = 'default' WHERE company_id IS NULL")
    op.execute("UPDATE photos SET company_id = 'default' WHERE company_id IS NULL")
    op.execute("UPDATE audit_logs SET company_id = 'default' WHERE company_id IS NULL")

    if dialect_name != "sqlite":
        op.create_foreign_key("fk_users_company_id_companies", "users", "companies", ["company_id"], ["company_id"])
        op.create_foreign_key(
            "fk_employees_company_id_companies",
            "employees",
            "companies",
            ["company_id"],
            ["company_id"],
        )
        op.create_foreign_key("fk_projects_company_id_companies", "projects", "companies", ["company_id"], ["company_id"])
        op.create_foreign_key("fk_photos_company_id_companies", "photos", "companies", ["company_id"], ["company_id"])
        op.create_foreign_key(
            "fk_audit_logs_company_id_companies",
            "audit_logs",
            "companies",
            ["company_id"],
            ["company_id"],
        )

    bind.execute(
        sa.text(
            """
            INSERT INTO subscriptions (company_id, plan_id, status, created_at, updated_at)
            VALUES (:company_id, :plan_id, :status, :created_at, :updated_at)
            """
        ),
        {
            "company_id": "default",
            "plan_id": "pro",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
