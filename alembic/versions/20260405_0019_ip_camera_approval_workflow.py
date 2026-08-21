"""ip camera approval workflow

Revision ID: 20260405_0019
Revises: 20260405_0018
Create Date: 2026-04-05 22:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260405_0019"
down_revision = "20260405_0018"
branch_labels = None
depends_on = None


camera_approval_status_enum = sa.Enum(
    "pending",
    "approved",
    "rejected",
    name="camera_approval_status",
    native_enum=False,
)


def upgrade() -> None:
    op.add_column(
        "ip_cameras",
        sa.Column("approval_status", camera_approval_status_enum, nullable=True),
    )
    op.add_column("ip_cameras", sa.Column("review_notes", sa.Text(), nullable=True))
    op.add_column("ip_cameras", sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True))
    op.add_column("ip_cameras", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_ip_cameras_reviewed_by_user_id_users",
        "ip_cameras",
        "users",
        ["reviewed_by_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_ip_cameras_approval_status",
        "ip_cameras",
        ["approval_status"],
        unique=False,
    )
    op.create_index(
        "ix_ip_cameras_reviewed_by_user_id",
        "ip_cameras",
        ["reviewed_by_user_id"],
        unique=False,
    )
    op.execute("UPDATE ip_cameras SET approval_status = 'approved' WHERE approval_status IS NULL")
    op.alter_column("ip_cameras", "approval_status", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_ip_cameras_reviewed_by_user_id", table_name="ip_cameras")
    op.drop_index("ix_ip_cameras_approval_status", table_name="ip_cameras")
    op.drop_constraint("fk_ip_cameras_reviewed_by_user_id_users", "ip_cameras", type_="foreignkey")
    op.drop_column("ip_cameras", "reviewed_at")
    op.drop_column("ip_cameras", "reviewed_by_user_id")
    op.drop_column("ip_cameras", "review_notes")
    op.drop_column("ip_cameras", "approval_status")
    camera_approval_status_enum.drop(op.get_bind(), checkfirst=False)
