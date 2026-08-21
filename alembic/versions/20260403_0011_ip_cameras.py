"""ip cameras

Revision ID: 20260403_0011
Revises: 20260403_0010
Create Date: 2026-04-03 18:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260403_0011"
down_revision = "20260403_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ip_cameras",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("stream_url", sa.String(length=1024), nullable=False),
        sa.Column(
            "protocol",
            sa.Enum("rtsp", "rtmp", name="camera_protocol", native_enum=False),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "status",
            sa.Enum("online", "offline", "error", name="camera_status", native_enum=False),
            nullable=False,
            server_default="offline",
        ),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ip_cameras_company_id", "ip_cameras", ["company_id"], unique=False)
    op.create_index("ix_ip_cameras_protocol", "ip_cameras", ["protocol"], unique=False)
    op.create_index("ix_ip_cameras_is_enabled", "ip_cameras", ["is_enabled"], unique=False)
    op.create_index("ix_ip_cameras_status", "ip_cameras", ["status"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
