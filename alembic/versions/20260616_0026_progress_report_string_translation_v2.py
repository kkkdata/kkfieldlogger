"""progress report string translation prompt v2

Revision ID: 20260616_0026
Revises: 20260616_0025
Create Date: 2026-06-16 03:10:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0026"
down_revision = "20260616_0025"
branch_labels = None
depends_on = None


TEMPLATE_ID = "progress_report_string_translation"
PROMPT_VERSION_ID = "progress_report_string_translation:v2"
BINDING_ID = "global:progress_report_string_translation:v2"


SYSTEM_PROMPT = """
You translate one construction progress report text field at a time.

Rules:
1. Translate only facts_json.source_text.
2. Return strict JSON only with keys zh, en, and es.
3. zh must be natural Simplified Chinese. Do not copy English into zh.
4. en must be natural English. If the source is already English, en may match the source.
5. es must be natural Spanish. Do not copy English into es.
6. Do not add new facts, numbers, dates, photo IDs, risks, or decisions.
7. Preserve the meaning and specificity of the source text.
8. For short construction phrases, translate the phrase directly; do not leave it untranslated.
9. Use correct Spanish gender and articles. For example, "rock" is "la roca", not "el roca".
10. If the source is empty, return empty strings.
11. Do not wrap the answer in markdown.
""".strip()


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
    now = datetime.now(timezone.utc)
    versions = sa.table(
        "expression_prompt_versions",
        sa.column("id", sa.String),
        sa.column("template_id", sa.String),
        sa.column("version", sa.String),
        sa.column("system_prompt", sa.Text),
        sa.column("user_prompt_template", sa.Text),
        sa.column("few_shot_json", sa.JSON),
        sa.column("json_schema_override", sa.JSON),
        sa.column("status", sa.String),
        sa.column("created_by_user_id", sa.Integer),
        sa.column("notes", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    bindings = sa.table(
        "expression_prompt_bindings",
        sa.column("id", sa.String),
        sa.column("scope_type", sa.String),
        sa.column("scope_id", sa.String),
        sa.column("template_id", sa.String),
        sa.column("prompt_version_id", sa.String),
        sa.column("priority", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
        sa.column("effective_until", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": PROMPT_VERSION_ID,
                "template_id": TEMPLATE_ID,
                "version": "v2",
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Adds explicit multilingual rules and examples for short construction phrases.",
                "created_at": now,
            }
        ],
    )
    op.bulk_insert(
        bindings,
        [
            {
                "id": BINDING_ID,
                "scope_type": "global",
                "scope_id": None,
                "template_id": TEMPLATE_ID,
                "prompt_version_id": PROMPT_VERSION_ID,
                "priority": 110,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            }
        ],
    )


def downgrade() -> None:
    bindings = sa.table("expression_prompt_bindings", sa.column("id", sa.String))
    versions = sa.table("expression_prompt_versions", sa.column("id", sa.String))
    op.execute(bindings.delete().where(bindings.c.id == BINDING_ID))
    op.execute(versions.delete().where(versions.c.id == PROMPT_VERSION_ID))
