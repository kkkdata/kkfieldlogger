"""photo embeddings with pgvector

Revision ID: 20260402_0006
Revises: 20260320_0005
Create Date: 2026-04-02 14:30:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260402_0006"
down_revision = "20260320_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    from pgvector.sqlalchemy import Vector

    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.add_column("photos", sa.Column("embedding", Vector(768), nullable=True))


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
