"""media assets and video ai logs

Revision ID: 20260403_0010
Revises: 20260403_0009
Create Date: 2026-04-03 16:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

try:
    from sqlalchemy.dialects import postgresql
except ImportError:  # pragma: no cover - sqlite-only environments may not load postgres dialects
    postgresql = None


revision = "20260403_0010"
down_revision = "20260403_0009"
branch_labels = None
depends_on = None


def _json_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql" and postgresql is not None:
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "media_assets",
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "media_type",
            sa.Enum("image", "video", name="media_type", native_enum=False),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="manual_upload"),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("original_file_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("uploading", "processing", "completed", "failed", name="media_asset_status", native_enum=False),
            nullable=False,
            server_default="uploading",
        ),
        sa.Column("metadata_json", _json_type(bind), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    op.create_index("ix_media_assets_company_id", "media_assets", ["company_id"], unique=False)
    op.create_index("ix_media_assets_tenant_id", "media_assets", ["tenant_id"], unique=False)
    op.create_index("ix_media_assets_uploaded_by_user_id", "media_assets", ["uploaded_by_user_id"], unique=False)
    op.create_index("ix_media_assets_media_type", "media_assets", ["media_type"], unique=False)
    op.create_index("ix_media_assets_source", "media_assets", ["source"], unique=False)
    op.create_index("ix_media_assets_status", "media_assets", ["status"], unique=False)

    op.create_table(
        "ai_analysis_logs_v2",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("media_asset_id", sa.String(length=36), sa.ForeignKey("media_assets.asset_id"), nullable=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column(
            "analysis_type",
            sa.Enum(
                "fast_screen",
                "deep_analysis",
                "sticker_trigger",
                "inventory_scan",
                "video_insight",
                name="ai_analysis_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("prompt_used", sa.Text(), nullable=False),
        sa.Column("model_used", sa.String(length=255), nullable=False),
        sa.Column("result_data", _json_type(bind), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "rejected", name="ai_analysis_status", native_enum=False),
            nullable=False,
            server_default="active",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO ai_analysis_logs_v2 (
                id,
                photo_id,
                media_asset_id,
                batch_id,
                analysis_type,
                prompt_used,
                model_used,
                result_data,
                status,
                created_at,
                created_by
            )
            SELECT
                id,
                photo_id,
                NULL,
                batch_id,
                analysis_type,
                prompt_used,
                model_used,
                result_data,
                status,
                created_at,
                created_by
            FROM ai_analysis_logs
            """
        )
    )
    op.drop_table("ai_analysis_logs")
    op.rename_table("ai_analysis_logs_v2", "ai_analysis_logs")
    op.create_index("ix_ai_analysis_logs_photo_id", "ai_analysis_logs", ["photo_id"], unique=False)
    op.create_index("ix_ai_analysis_logs_media_asset_id", "ai_analysis_logs", ["media_asset_id"], unique=False)
    op.create_index("ix_ai_analysis_logs_batch_id", "ai_analysis_logs", ["batch_id"], unique=False)
    op.create_index("ix_ai_analysis_logs_analysis_type", "ai_analysis_logs", ["analysis_type"], unique=False)
    op.create_index("ix_ai_analysis_logs_status", "ai_analysis_logs", ["status"], unique=False)
    op.create_index("ix_ai_analysis_logs_photo_created_at", "ai_analysis_logs", ["photo_id", "created_at"], unique=False)
    op.create_index(
        "ix_ai_analysis_logs_media_asset_created_at",
        "ai_analysis_logs",
        ["media_asset_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
