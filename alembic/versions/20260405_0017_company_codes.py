"""company codes for scoped numeric ids

Revision ID: 20260405_0017
Revises: 20260405_0016
Create Date: 2026-04-05 17:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260405_0017"
down_revision = "20260405_0016"
branch_labels = None
depends_on = None


DEFAULT_COMPANY_CODE = "10000"


def upgrade() -> None:
    op.add_column("companies", sa.Column("company_code", sa.String(length=5), nullable=True))

    connection = op.get_bind()
    companies = sa.table(
        "companies",
        sa.column("id", sa.Integer()),
        sa.column("company_id", sa.String(length=64)),
        sa.column("company_code", sa.String(length=5)),
    )
    rows = list(connection.execute(sa.select(companies.c.id, companies.c.company_id).order_by(companies.c.id.asc())))
    assigned_codes: set[str] = set()
    next_code = int(DEFAULT_COMPANY_CODE)

    for row in rows:
        if row.company_id == "default":
            code = DEFAULT_COMPANY_CODE
        else:
            while True:
                candidate = f"{next_code:05d}"
                next_code += 1
                if candidate == DEFAULT_COMPANY_CODE or candidate in assigned_codes:
                    continue
                code = candidate
                break
        connection.execute(
            sa.update(companies).where(companies.c.id == row.id).values(company_code=code)
        )
        assigned_codes.add(code)

    op.alter_column("companies", "company_code", existing_type=sa.String(length=5), nullable=False)
    op.create_index("ix_companies_company_code", "companies", ["company_code"], unique=True)


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
