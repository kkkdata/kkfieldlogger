"""generated report markdown expression prompts

Revision ID: 20260616_0028
Revises: 20260616_0027
Create Date: 2026-06-16 04:05:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0028"
down_revision = "20260616_0027"
branch_labels = None
depends_on = None


MARKDOWN_TEMPLATE_ID = "generated_report_markdown"
MARKDOWN_VERSION_ID = "generated_report_markdown:v1"
MARKDOWN_BINDING_ID = "global:generated_report_markdown:v1"
MARKDOWN_CONTRACT_ID = "generated_report_markdown:project_manager:v1"

SECTION_TEMPLATE_ID = "generated_report_markdown_section"
SECTION_VERSION_ID = "generated_report_markdown_section:v1"
SECTION_BINDING_ID = "global:generated_report_markdown_section:v1"
SECTION_CONTRACT_ID = "generated_report_markdown_section:project_manager:v1"


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


MARKDOWN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "metrics", "sections", "source_artifacts", "disclaimer"],
    "properties": {
        "title": {"type": "string", "maxLength": 200},
        "summary": {"type": "string", "maxLength": 1000},
        "metrics": {
            "type": "object",
            "additionalProperties": False,
            "required": ["overall_progress_percent", "overall_status", "confidence_level"],
            "properties": {
                "overall_progress_percent": {"type": ["integer", "null"]},
                "overall_status": {"type": "string", "enum": ["on_track", "at_risk", "blocked", "unknown"]},
                "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
            },
        },
        "sections": {"type": "array", "minItems": 5, "maxItems": 5, "items": SECTION_SCHEMA},
        "source_artifacts": {"type": "object"},
        "disclaimer": {"type": "string", "maxLength": 160},
    },
}


SECTION_SYSTEM_PROMPT = """
You write one short construction progress report markdown section.

Rules:
1. Use only facts_json.source_values.
2. Return strict JSON only.
3. Do not invent people, dates, quantities, project IDs, photos, risks, or decisions.
4. markdown must use plain markdown bullets or short paragraphs. Do not use raw HTML.
5. source_refs must contain only refs from facts_json.allowed_source_refs.
6. Keep the section concise and useful for a project manager.
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


MARKDOWN_SYSTEM_PROMPT = """
This prompt version represents the final generated_report_markdown artifact.
The runtime assembles it from validated generated_report_markdown_section chunks and deterministic database fields.
Do not call this prompt directly for long-form synthesis.
""".strip()


MARKDOWN_USER_PROMPT_TEMPLATE = """
The generated_report_markdown artifact is assembled by the server runtime.

Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    templates = sa.table(
        "expression_prompt_templates",
        sa.column("id", sa.String),
        sa.column("slug", sa.String),
        sa.column("title", sa.String),
        sa.column("artifact_type", sa.String),
        sa.column("stage", sa.String),
        sa.column("default_audience_id", sa.String),
        sa.column("description", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
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
        templates,
        [
            {
                "id": MARKDOWN_TEMPLATE_ID,
                "slug": MARKDOWN_TEMPLATE_ID,
                "title": "Generated report markdown",
                "artifact_type": MARKDOWN_TEMPLATE_ID,
                "stage": "render",
                "default_audience_id": "project_manager",
                "description": "Assembles a validated markdown progress report from database-backed report facts.",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": SECTION_TEMPLATE_ID,
                "slug": SECTION_TEMPLATE_ID,
                "title": "Generated report markdown section",
                "artifact_type": SECTION_TEMPLATE_ID,
                "stage": "render",
                "default_audience_id": "project_manager",
                "description": "Writes one bounded markdown section using allowed source refs.",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": MARKDOWN_VERSION_ID,
                "template_id": MARKDOWN_TEMPLATE_ID,
                "version": "v1",
                "system_prompt": MARKDOWN_SYSTEM_PROMPT,
                "user_prompt_template": MARKDOWN_USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Final artifact is assembled by code from section chunks and deterministic fields.",
                "created_at": now,
            },
            {
                "id": SECTION_VERSION_ID,
                "template_id": SECTION_TEMPLATE_ID,
                "version": "v1",
                "system_prompt": SECTION_SYSTEM_PROMPT,
                "user_prompt_template": SECTION_USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Small-output markdown section prompt for qwen/Ollama shadow experiments.",
                "created_at": now,
            },
        ],
    )
    op.bulk_insert(
        bindings,
        [
            {
                "id": MARKDOWN_BINDING_ID,
                "scope_type": "global",
                "scope_id": None,
                "template_id": MARKDOWN_TEMPLATE_ID,
                "prompt_version_id": MARKDOWN_VERSION_ID,
                "priority": 100,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            },
            {
                "id": SECTION_BINDING_ID,
                "scope_type": "global",
                "scope_id": None,
                "template_id": SECTION_TEMPLATE_ID,
                "prompt_version_id": SECTION_VERSION_ID,
                "priority": 100,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            },
        ],
    )
    op.bulk_insert(
        contracts,
        [
            {
                "id": MARKDOWN_CONTRACT_ID,
                "artifact_type": MARKDOWN_TEMPLATE_ID,
                "audience_id": "project_manager",
                "version": "v1",
                "json_schema": MARKDOWN_SCHEMA,
                "required_fact_paths_json": [
                    "scope.report_id",
                    "scope.project_id",
                    "structured_report.executive_summary",
                    "structured_report.manager_brief",
                ],
                "forbidden_claims_json": [
                    {"pattern": "\\bguaranteed\\b"},
                    {"pattern": "\\bmust\\s+(fire|hire|discipline)\\b"},
                    {"pattern": "\\bunderperforming\\b"},
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": SECTION_CONTRACT_ID,
                "artifact_type": SECTION_TEMPLATE_ID,
                "audience_id": "project_manager",
                "version": "v1",
                "json_schema": SECTION_SCHEMA,
                "required_fact_paths_json": ["section_key", "heading", "allowed_source_refs"],
                "forbidden_claims_json": [
                    {"pattern": "\\bguaranteed\\b"},
                    {"pattern": "\\bmust\\s+(fire|hire|discipline)\\b"},
                    {"pattern": "\\bunderperforming\\b"},
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    contracts = sa.table("expression_output_contracts", sa.column("id", sa.String))
    bindings = sa.table("expression_prompt_bindings", sa.column("id", sa.String))
    versions = sa.table("expression_prompt_versions", sa.column("id", sa.String))
    templates = sa.table("expression_prompt_templates", sa.column("id", sa.String))
    for contract_id in (MARKDOWN_CONTRACT_ID, SECTION_CONTRACT_ID):
        op.execute(contracts.delete().where(contracts.c.id == contract_id))
    for binding_id in (MARKDOWN_BINDING_ID, SECTION_BINDING_ID):
        op.execute(bindings.delete().where(bindings.c.id == binding_id))
    for version_id in (MARKDOWN_VERSION_ID, SECTION_VERSION_ID):
        op.execute(versions.delete().where(versions.c.id == version_id))
    for template_id in (MARKDOWN_TEMPLATE_ID, SECTION_TEMPLATE_ID):
        op.execute(templates.delete().where(templates.c.id == template_id))
