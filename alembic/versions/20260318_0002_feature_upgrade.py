"""feature upgrade for portal ux and metadata

Revision ID: 20260318_0002
Revises: 20260318_0001
Create Date: 2026-03-18 03:20:00

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0002"
down_revision = "20260318_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("photos") as batch_op:
        batch_op.add_column(sa.Column("approval_status", sa.String(length=32), nullable=False, server_default="pending"))
        batch_op.add_column(sa.Column("device_model", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("os_version", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("app_version", sa.String(length=120), nullable=True))
        batch_op.create_index("ix_photos_approval_status", ["approval_status"], unique=False)

    op.execute(
        """
        UPDATE photos
        SET approval_status = CASE
            WHEN approved_by_manager THEN 'approved'
            ELSE 'pending'
        END
        """
    )

    op.create_table(
        "photo_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("photo_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_photo_comments_photo_id", "photo_comments", ["photo_id"])
    op.create_index("ix_photo_comments_user_id", "photo_comments", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_photo_comments_user_id", table_name="photo_comments")
    op.drop_index("ix_photo_comments_photo_id", table_name="photo_comments")
    op.drop_table("photo_comments")

    with op.batch_alter_table("photos") as batch_op:
        batch_op.drop_index("ix_photos_approval_status")
        batch_op.drop_column("app_version")
        batch_op.drop_column("os_version")
        batch_op.drop_column("device_model")
        batch_op.drop_column("approval_status")
