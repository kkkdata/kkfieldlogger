"""v2 saas foundation

Revision ID: 20260319_0004
Revises: 20260319_0003
Create Date: 2026-03-19 13:00:00

"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "20260319_0004"
down_revision = "20260319_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("current_plan_id", sa.String(length=32), nullable=True),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["current_plan_id"], ["plans.plan_id"]),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
    )
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])
    op.create_index("ix_invitations_email", "invitations", ["email"])
    op.create_index("ix_invitations_token_hash", "invitations", ["token_hash"])

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("public_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_users_public_id", ["public_id"], unique=True)

    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(sa.Column("daily_upload_limit", sa.Integer(), nullable=False, server_default="20"))
        batch_op.add_column(sa.Column("storage_limit_mb", sa.Integer(), nullable=True))

    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("provider", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("external_ref", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))

    with op.batch_alter_table("usage_counters") as batch_op:
        batch_op.add_column(sa.Column("usage_date", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("uploaded_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("storage_used", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_index("ix_usage_counters_usage_date", ["usage_date"], unique=False)

    with op.batch_alter_table("employees") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_employees_tenant_id", ["tenant_id"], unique=False)

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_projects_tenant_id", ["tenant_id"], unique=False)

    with op.batch_alter_table("photos") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("storage_path", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("mime_type", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("file_size", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("checksum", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("gps_lng", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("soft_deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("weather_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("ocr_status", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("labeling_status", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("defect_status", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("scene_status", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("duplicate_hash", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("embedding_ref", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("reconstruction_batch_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_photos_tenant_id", ["tenant_id"], unique=False)

    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_audit_logs_tenant_id", ["tenant_id"], unique=False)

    companies = list(bind.execute(sa.text("SELECT company_id, company_name, active, created_at FROM companies")))
    for company_id, company_name, active, created_at in companies:
        bind.execute(
            sa.text(
                """
                INSERT INTO tenants (id, slug, name, status, created_at, updated_at)
                VALUES (:id, :slug, :name, :status, :created_at, :updated_at)
                """
            ),
            {
                "id": str(uuid4()),
                "slug": company_id,
                "name": company_name,
                "status": "active" if active else "suspended",
                "created_at": created_at or now,
                "updated_at": created_at or now,
            },
        )

    users = list(bind.execute(sa.text("SELECT id, company_id, role, created_at FROM users")))
    for user_id, company_id, role, created_at in users:
        bind.execute(
            sa.text(
                """
                INSERT INTO memberships (tenant_id, user_id, role, status, created_at)
                VALUES (:tenant_id, :user_id, :role, :status, :created_at)
                """
            ),
            {
                "tenant_id": company_id or "default",
                "user_id": user_id,
                "role": role,
                "status": "active",
                "created_at": created_at or now,
            },
        )

    bind.execute(sa.text("UPDATE users SET public_id = :public_id WHERE 1 = 0"), {"public_id": str(uuid4())})
    for (user_id,) in bind.execute(sa.text("SELECT id FROM users")):
        bind.execute(
            sa.text("UPDATE users SET public_id = :public_id, updated_at = COALESCE(updated_at, created_at) WHERE id = :id"),
            {"public_id": str(uuid4()), "id": user_id},
        )

    bind.execute(sa.text("UPDATE employees SET tenant_id = company_id WHERE tenant_id IS NULL"))
    bind.execute(sa.text("UPDATE projects SET tenant_id = company_id WHERE tenant_id IS NULL"))
    bind.execute(sa.text("UPDATE photos SET tenant_id = company_id WHERE tenant_id IS NULL"))
    bind.execute(sa.text("UPDATE audit_logs SET tenant_id = company_id WHERE tenant_id IS NULL"))
    bind.execute(sa.text("UPDATE photos SET storage_path = file_path WHERE storage_path IS NULL"))
    bind.execute(sa.text("UPDATE photos SET gps_lng = gps_lon WHERE gps_lng IS NULL"))
    bind.execute(sa.text("UPDATE photos SET soft_deleted_at = deleted_at WHERE soft_deleted_at IS NULL"))
    bind.execute(sa.text("UPDATE plans SET daily_upload_limit = 200 WHERE plan_id = 'basic'"))
    bind.execute(sa.text("UPDATE plans SET storage_limit_mb = 10240 WHERE plan_id = 'basic'"))
    bind.execute(sa.text("UPDATE plans SET daily_upload_limit = 5000 WHERE plan_id = 'pro'"))
    bind.execute(sa.text("UPDATE plans SET storage_limit_mb = 102400 WHERE plan_id = 'pro'"))
    bind.execute(
        sa.text(
            """
            INSERT INTO plans (plan_id, display_name, monthly_price_cents, monthly_photo_limit, daily_upload_limit, storage_limit_mb, created_at, active)
            VALUES ('starter', 'Starter', 10, 620, 20, 2048, :created_at, true)
            """
        ),
        {"created_at": now},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO plans (plan_id, display_name, monthly_price_cents, monthly_photo_limit, daily_upload_limit, storage_limit_mb, created_at, active)
            VALUES ('business', 'Business', 1000, 1000000, 1000000, 51200, :created_at, true)
            """
        ),
        {"created_at": now},
    )
    bind.execute(sa.text("UPDATE subscriptions SET started_at = COALESCE(started_at, created_at)"))
    bind.execute(sa.text("UPDATE tenants SET current_plan_id = 'business' WHERE slug = 'default'"))


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
