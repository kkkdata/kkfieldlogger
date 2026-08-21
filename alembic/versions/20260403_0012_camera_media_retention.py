"""camera production optimization fields

Revision ID: 20260403_0012
Revises: 20260403_0011
Create Date: 2026-04-03 21:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260403_0012"
down_revision = "20260403_0011"
branch_labels = None
depends_on = None


old_media_asset_status = sa.Enum(
    "uploading",
    "processing",
    "completed",
    "failed",
    name="media_asset_status",
    native_enum=False,
)
new_media_asset_status = sa.Enum(
    "uploading",
    "processing",
    "completed",
    "failed",
    "archived",
    "deleted",
    name="media_asset_status",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=old_media_asset_status,
            type_=new_media_asset_status,
            existing_nullable=False,
            existing_server_default="uploading",
        )

    with op.batch_alter_table("ip_cameras") as batch_op:
        batch_op.add_column(sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
