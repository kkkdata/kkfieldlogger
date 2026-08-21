"""ai analysis event sourcing logs

Revision ID: 20260403_0009
Revises: 20260402_0008
Create Date: 2026-04-03 10:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

try:
    from sqlalchemy.dialects import postgresql
except ImportError:  # pragma: no cover - sqlite-only environments may not load postgres dialects
    postgresql = None


revision = "20260403_0009"
down_revision = "20260402_0008"
branch_labels = None
depends_on = None


def _result_data_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql" and postgresql is not None:
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "ai_analysis_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column(
            "analysis_type",
            sa.Enum(
                "fast_screen",
                "deep_analysis",
                "sticker_trigger",
                "inventory_scan",
                name="ai_analysis_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("prompt_used", sa.Text(), nullable=False),
        sa.Column("model_used", sa.String(length=255), nullable=False),
        sa.Column("result_data", _result_data_type(bind), nullable=True),
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
    op.create_index("ix_ai_analysis_logs_photo_id", "ai_analysis_logs", ["photo_id"], unique=False)
    op.create_index("ix_ai_analysis_logs_batch_id", "ai_analysis_logs", ["batch_id"], unique=False)
    op.create_index("ix_ai_analysis_logs_analysis_type", "ai_analysis_logs", ["analysis_type"], unique=False)
    op.create_index("ix_ai_analysis_logs_status", "ai_analysis_logs", ["status"], unique=False)
    op.create_index(
        "ix_ai_analysis_logs_photo_created_at",
        "ai_analysis_logs",
        ["photo_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
