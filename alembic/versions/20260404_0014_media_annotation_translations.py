"""media annotation translations

Revision ID: 20260404_0014
Revises: 20260404_0013
Create Date: 2026-04-04 23:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

try:
    from sqlalchemy.dialects import postgresql
except ImportError:  # pragma: no cover
    postgresql = None


revision = "20260404_0014"
down_revision = "20260404_0013"
branch_labels = None
depends_on = None


def _json_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql" and postgresql is not None:
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("media_annotations") as batch_op:
        batch_op.add_column(sa.Column("source_language", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("translations_json", _json_type(bind), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
