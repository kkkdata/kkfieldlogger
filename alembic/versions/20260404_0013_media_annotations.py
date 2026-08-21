"""media annotations

Revision ID: 20260404_0013
Revises: 20260403_0012
Create Date: 2026-04-04 10:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260404_0013"
down_revision = "20260403_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_annotations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("media_asset_id", sa.String(length=36), sa.ForeignKey("media_assets.asset_id"), nullable=True),
        sa.Column("parent_id", sa.String(length=36), sa.ForeignKey("media_annotations.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "role_at_time",
            sa.Enum("employee", "manager", name="annotation_role", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "annotation_type",
            sa.Enum("voice", "text", name="annotation_type", native_enum=False),
            nullable=False,
        ),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("audio_file_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "visibility",
            sa.Enum("public", "manager_only", name="annotation_visibility", native_enum=False),
            nullable=False,
            server_default="public",
        ),
        sa.Column(
            "status",
            sa.Enum("processing", "completed", "failed", name="annotation_status", native_enum=False),
            nullable=False,
            server_default="completed",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_annotations_photo_id", "media_annotations", ["photo_id"], unique=False)
    op.create_index("ix_media_annotations_media_asset_id", "media_annotations", ["media_asset_id"], unique=False)
    op.create_index("ix_media_annotations_parent_id", "media_annotations", ["parent_id"], unique=False)
    op.create_index("ix_media_annotations_user_id", "media_annotations", ["user_id"], unique=False)
    op.create_index("ix_media_annotations_role_at_time", "media_annotations", ["role_at_time"], unique=False)
    op.create_index("ix_media_annotations_annotation_type", "media_annotations", ["annotation_type"], unique=False)
    op.create_index("ix_media_annotations_visibility", "media_annotations", ["visibility"], unique=False)
    op.create_index("ix_media_annotations_status", "media_annotations", ["status"], unique=False)
    op.create_index("ix_media_annotations_photo_created_at", "media_annotations", ["photo_id", "created_at"], unique=False)
    op.create_index(
        "ix_media_annotations_media_asset_created_at",
        "media_annotations",
        ["media_asset_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_annotations_parent_created_at",
        "media_annotations",
        ["parent_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
