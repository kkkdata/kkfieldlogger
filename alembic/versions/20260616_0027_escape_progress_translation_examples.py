"""escape progress translation prompt examples

Revision ID: 20260616_0027
Revises: 20260616_0026
Create Date: 2026-06-16 03:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260616_0027"
down_revision = "20260616_0026"
branch_labels = None
depends_on = None


PROMPT_VERSION_ID = "progress_report_string_translation:v2"


USER_PROMPT_TEMPLATE = """
Translate this single progress report text field.

Field label: {field_label}
Source text:
{source_text}

Output examples:
- Source: "Rough-in progressed."
  JSON: {{"zh":"粗装施工已有进展。","en":"Rough-in progressed.","es":"La instalación preliminar avanzó."}}
- Source: "The rock blocks the work area."
  JSON: {{"zh":"这块岩石阻挡了作业区域。","en":"The rock blocks the work area.","es":"La roca bloquea el área de trabajo."}}

Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


def upgrade() -> None:
    versions = sa.table(
        "expression_prompt_versions",
        sa.column("id", sa.String),
        sa.column("user_prompt_template", sa.Text),
    )
    op.execute(
        versions.update()
        .where(versions.c.id == PROMPT_VERSION_ID)
        .values(user_prompt_template=USER_PROMPT_TEMPLATE)
    )


def downgrade() -> None:
    # Keep the escaped prompt on downgrade; reverting would reintroduce a runtime formatting error.
    pass
