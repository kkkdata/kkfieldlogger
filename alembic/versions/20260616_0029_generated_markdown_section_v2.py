"""generated markdown section prompt v2

Revision ID: 20260616_0029
Revises: 20260616_0028
Create Date: 2026-06-16 04:40:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0029"
down_revision = "20260616_0028"
branch_labels = None
depends_on = None


SECTION_TEMPLATE_ID = "generated_report_markdown_section"
SECTION_VERSION_ID = "generated_report_markdown_section:v2"
SECTION_BINDING_ID = "global:generated_report_markdown_section:v2"
SECTION_CONTRACT_ID = "generated_report_markdown_section:project_manager:v2"


SECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "heading", "markdown", "source_refs"],
    "properties": {
        "key": {"type": "string", "maxLength": 64},
        "heading": {"type": "string", "maxLength": 120},
        "markdown": {"type": "string", "minLength": 1, "maxLength": 2500},
        "source_refs": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string", "maxLength": 180}},
        "generation_mode": {"type": "string", "enum": ["ai", "deterministic_fallback"]},
    },
}


SECTION_SYSTEM_PROMPT = """
You write one short construction progress report markdown section.

Rules:
1. Use only facts_json.source_values.
2. Return strict JSON only.
3. Do not invent people, dates, quantities, project IDs, photos, risks, decisions, urgency levels, or priorities.
4. Do not use advice language such as "should", "must", "priority", "high priority", or "top priority".
5. markdown must use plain markdown bullets or short paragraphs. Do not use raw HTML.
6. source_refs must contain only refs from facts_json.allowed_source_refs.
7. Keep the section concise and useful for a project manager, but descriptive only.
""".strip()


SECTION_USER_PROMPT_TEMPLATE = """
Write this section.

Section key: {section_key}
Heading: {heading}
Allowed source refs JSON:
{allowed_source_refs_json}

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
    contracts = sa.table(
        "expression_output_contracts",
        sa.column("id", sa.String),
        sa.column("artifact_type", sa.String),
        sa.column("audience_id", sa.String),
        sa.column("version", sa.String),
        sa.column("json_schema", sa.JSON),
        sa.column("required_fact_paths_json", sa.JSON),
        sa.column("forbidden_claims_json", sa.JSON),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": SECTION_VERSION_ID,
                "template_id": SECTION_TEMPLATE_ID,
                "version": "v2",
                "system_prompt": SECTION_SYSTEM_PROMPT,
                "user_prompt_template": SECTION_USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Rejects unsupported recommendation and priority language in bounded markdown sections.",
                "created_at": now,
            }
        ],
    )
    op.bulk_insert(
        bindings,
        [
            {
                "id": SECTION_BINDING_ID,
                "scope_type": "global",
                "scope_id": None,
                "template_id": SECTION_TEMPLATE_ID,
                "prompt_version_id": SECTION_VERSION_ID,
                "priority": 110,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            }
        ],
    )
    op.bulk_insert(
        contracts,
        [
            {
                "id": SECTION_CONTRACT_ID,
                "artifact_type": SECTION_TEMPLATE_ID,
                "audience_id": "project_manager",
                "version": "v2",
                "json_schema": SECTION_SCHEMA,
                "required_fact_paths_json": ["section_key", "heading", "allowed_source_refs"],
                "forbidden_claims_json": [
                    {"pattern": "\\bshould\\b"},
                    {"pattern": "\\bpriority\\b"},
                    {"pattern": "\\bguaranteed\\b"},
                    {"pattern": "\\bmust\\b"},
                    {"pattern": "\\bunderperforming\\b"},
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    contracts = sa.table("expression_output_contracts", sa.column("id", sa.String))
    bindings = sa.table("expression_prompt_bindings", sa.column("id", sa.String))
    versions = sa.table("expression_prompt_versions", sa.column("id", sa.String))
    op.execute(contracts.delete().where(contracts.c.id == SECTION_CONTRACT_ID))
    op.execute(bindings.delete().where(bindings.c.id == SECTION_BINDING_ID))
    op.execute(versions.delete().where(versions.c.id == SECTION_VERSION_ID))
