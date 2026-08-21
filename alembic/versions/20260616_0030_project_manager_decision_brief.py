"""project manager decision brief expression

Revision ID: 20260616_0030
Revises: 20260616_0029
Create Date: 2026-06-16 05:20:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0030"
down_revision = "20260616_0029"
branch_labels = None
depends_on = None


TEMPLATE_ID = "project_manager_decision_brief"
PROMPT_VERSION_ID = "project_manager_decision_brief:v1"
BINDING_ID = "global:project_manager_decision_brief:v1"
CONTRACT_ID = "project_manager_decision_brief:project_manager:v1"


FACT_REF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["snapshot_id", "field_path", "observed_value"],
    "properties": {
        "snapshot_id": {"type": "string", "maxLength": 64},
        "field_path": {"type": "string", "maxLength": 200},
        "observed_value": {},
    },
}


DECISION_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "category", "severity", "deterministic_summary", "fact_refs", "metrics"],
    "properties": {
        "item_id": {"type": "string", "maxLength": 80},
        "category": {
            "type": "string",
            "enum": [
                "schedule_risk",
                "resource_gap",
                "quality_gate",
                "dependency_blocker",
                "scope_drift",
                "contribution_gap",
                "evidence_coverage",
                "evidence_processing",
                "reporting_coverage",
                "reporting_signal",
                "expression_readiness",
            ],
        },
        "severity": {"type": "string", "enum": ["info", "watch", "action_required"]},
        "deterministic_summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "fact_refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": FACT_REF_SCHEMA},
        "metrics": {"type": "object"},
    },
}


CONTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "artifact_type",
        "artifact_version",
        "audience",
        "visibility",
        "project_id",
        "as_of",
        "fact_snapshot_ids",
        "decision_surface",
        "body",
        "provenance",
        "validation",
    ],
    "properties": {
        "artifact_type": {"type": "string", "const": TEMPLATE_ID},
        "artifact_version": {"type": "string", "const": "1.0.0"},
        "audience": {"type": "string", "const": "project_manager"},
        "visibility": {"type": "string", "const": "shadow"},
        "project_id": {"type": "string", "maxLength": 64},
        "as_of": {"type": "string", "maxLength": 64},
        "fact_snapshot_ids": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}},
        "decision_surface": {
            "type": "object",
            "additionalProperties": False,
            "required": ["computed_at", "items"],
            "properties": {
                "computed_at": {"type": "string", "maxLength": 64},
                "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": DECISION_ITEM_SCHEMA},
            },
        },
        "body": {
            "type": "object",
            "additionalProperties": False,
            "required": ["format", "paragraphs", "word_count", "generation_path"],
            "properties": {
                "format": {"type": "string", "const": "markdown"},
                "paragraphs": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 600}},
                "word_count": {"type": "integer"},
                "generation_path": {"type": "string", "enum": ["ai_polish", "deterministic_fallback"]},
            },
        },
        "provenance": {"type": "object"},
        "validation": {"type": "object"},
    },
}


SYSTEM_PROMPT = """
You may polish a project manager brief, but the runtime owns the decision_surface.

Rules:
1. Use only facts_json and the supplied decision_surface.
2. Do not change, add, remove, or re-rank decision_surface items.
3. Do not invent people, dates, quantities, causes, priorities, risks, or recommendations.
4. Do not use advice language such as should, must, recommend, priority, urgent, or escalate.
5. Return strict JSON only.
""".strip()


USER_PROMPT_TEMPLATE = """
This artifact is normally assembled by the server runtime from database facts.

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
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Project manager decision brief",
                "artifact_type": TEMPLATE_ID,
                "stage": "expression",
                "default_audience_id": "project_manager",
                "description": "Shadow-only project-manager brief assembled from database-backed project evidence.",
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
                "notes": "Phase 1 uses deterministic fallback only; AI polish is gated by validators.",
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
                "json_schema": CONTRACT_SCHEMA,
                "required_fact_paths_json": [
                    "scope.company_id",
                    "scope.project_id",
                    "photo_metrics.photos_current",
                    "progress_report_metrics.completed_with_content",
                ],
                "forbidden_claims_json": [
                    {"pattern": "\\bshould\\b"},
                    {"pattern": "\\bmust\\b"},
                    {"pattern": "\\bpriority\\b"},
                    {"pattern": "\\brecommend\\b"},
                    {"pattern": "\\bescalate\\b"},
                    {"pattern": "\\burgent\\b"},
                    {"pattern": "\\bguaranteed\\b"},
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
