"""progress report translation expression prompt

Revision ID: 20260616_0024
Revises: 20260616_0023
Create Date: 2026-06-16 01:45:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0024"
down_revision = "20260616_0023"
branch_labels = None
depends_on = None


TEMPLATE_ID = "progress_report_translation"
PROMPT_VERSION_ID = "progress_report_translation:v1"
BINDING_ID = "global:progress_report_translation:v1"
CONTRACT_ID = "progress_report_translation:project_manager:v1"


TIMELINE_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["photo_id", "captured_at", "observation", "progress_signal", "risk_signal"],
    "properties": {
        "photo_id": {"type": ["integer", "null"]},
        "captured_at": {"type": "string", "maxLength": 80},
        "observation": {"type": "string", "maxLength": 1200},
        "progress_signal": {"type": "string", "maxLength": 400},
        "risk_signal": {"type": "string", "maxLength": 400},
    },
}


REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "executive_summary",
        "overall_progress_percent",
        "overall_status",
        "confidence_level",
        "manager_brief",
        "angle_bias_notes",
        "key_changes",
        "work_completed",
        "work_remaining",
        "safety_risks",
        "quality_risks",
        "material_inventory_signals",
        "water_housekeeping_signals",
        "uncertain_items",
        "evidence_limitations",
        "immediate_decisions",
        "recommended_actions",
        "timeline_observations",
    ],
    "properties": {
        "executive_summary": {"type": "string", "maxLength": 2400},
        "overall_progress_percent": {"type": ["integer", "null"]},
        "overall_status": {"type": "string", "enum": ["on_track", "at_risk", "blocked", "unknown"]},
        "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
        "manager_brief": {"type": "string", "maxLength": 1600},
        "angle_bias_notes": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}},
        "key_changes": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}},
        "work_completed": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}},
        "work_remaining": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}},
        "safety_risks": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "quality_risks": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "material_inventory_signals": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 500},
        },
        "water_housekeeping_signals": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 500},
        },
        "uncertain_items": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "evidence_limitations": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "immediate_decisions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "recommended_actions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 500}},
        "timeline_observations": {"type": "array", "maxItems": 60, "items": TIMELINE_ITEM_SCHEMA},
    },
}


TRANSLATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["zh", "en", "es"],
    "properties": {
        "zh": REPORT_SCHEMA,
        "en": REPORT_SCHEMA,
        "es": REPORT_SCHEMA,
    },
}


SYSTEM_PROMPT = """
You are the AI expression layer for construction progress reports.

Rules:
1. Translate only string values in facts_json.structured_report.
2. Return one strict JSON object only, with top-level keys zh, en, and es.
3. Preserve every JSON key, array shape, integer, null, timestamp, and photo_id value exactly.
4. Do not translate enum values. overall_status and confidence_level must remain unchanged.
5. Do not add new observations, risks, decisions, progress percentages, or photo references.
6. Do not wrap the answer in markdown.
""".strip()


USER_PROMPT_TEMPLATE = """
Translate the structured progress report below into Simplified Chinese, English, and Spanish.

Return shape:
{{
  "zh": <translated report object>,
  "en": <translated report object>,
  "es": <translated report object>
}}

Input facts JSON:
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
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Progress report translation",
                "artifact_type": TEMPLATE_ID,
                "stage": "translate",
                "default_audience_id": "project_manager",
                "description": "Translates a normalized progress report structure into supported UI languages.",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        versions,
        [
            {
                "id": PROMPT_VERSION_ID,
                "template_id": TEMPLATE_ID,
                "version": "v1",
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Initial registry-backed shadow prompt for progress report localization.",
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
                "priority": 100,
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
                "id": CONTRACT_ID,
                "artifact_type": TEMPLATE_ID,
                "audience_id": "project_manager",
                "version": "v1",
                "json_schema": TRANSLATION_SCHEMA,
                "required_fact_paths_json": [
                    "scope.report_id",
                    "scope.project_id",
                    "structured_report.executive_summary",
                    "structured_report.manager_brief",
                    "structured_report.timeline_observations",
                ],
                "forbidden_claims_json": [
                    {"pattern": "overall_status\\s*[:=]\\s*(complete|completed|done)"},
                    {"pattern": "confidence_level\\s*[:=]\\s*(certain|guaranteed)"},
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
    templates = sa.table("expression_prompt_templates", sa.column("id", sa.String))
    op.execute(contracts.delete().where(contracts.c.id == CONTRACT_ID))
    op.execute(bindings.delete().where(bindings.c.id == BINDING_ID))
    op.execute(versions.delete().where(versions.c.id == PROMPT_VERSION_ID))
    op.execute(templates.delete().where(templates.c.id == TEMPLATE_ID))
