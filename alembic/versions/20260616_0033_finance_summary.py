"""finance summary expression

Revision ID: 20260616_0033
Revises: 20260616_0032
Create Date: 2026-06-16 06:45:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0033"
down_revision = "20260616_0032"
branch_labels = None
depends_on = None


TEMPLATE_ID = "finance_summary"
PROMPT_VERSION_ID = "finance_summary:v1"
BINDING_ID = "global:finance_summary:v1"
CONTRACT_ID = "finance_summary:finance:v1"


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


SECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "section_id", "title", "lines", "fact_refs", "metrics", "flags"],
    "properties": {
        "item_id": {"type": "string", "maxLength": 80},
        "section_id": {"type": "string", "enum": ["receipt_amounts", "fuel_evidence", "vendor_distribution"]},
        "title": {"type": "string", "maxLength": 120},
        "lines": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 600}},
        "fact_refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": FACT_REF_SCHEMA},
        "metrics": {"type": "object"},
        "flags": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
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
        "company_id",
        "scope_type",
        "as_of",
        "headline_metrics",
        "status_flags",
        "data_gaps",
        "fact_snapshot_ids",
        "finance_surface",
        "body",
        "provenance",
        "validation",
    ],
    "properties": {
        "artifact_type": {"type": "string", "const": TEMPLATE_ID},
        "artifact_version": {"type": "string", "const": "1.0.0"},
        "audience": {"type": "string", "const": "finance"},
        "visibility": {"type": "string", "const": "shadow"},
        "project_id": {"type": "string", "maxLength": 64},
        "company_id": {"type": "string", "maxLength": 64},
        "scope_type": {"type": "string", "const": "project_finance_period"},
        "as_of": {"type": "string", "maxLength": 64},
        "headline_metrics": {
            "type": "object",
            "additionalProperties": False,
            "required": ["receipt_count", "total_amount", "receipts_missing_amount", "fuel_gallons_total"],
            "properties": {
                "receipt_count": {"type": "integer"},
                "total_amount": {"type": "number"},
                "receipts_missing_amount": {"type": "integer"},
                "fuel_gallons_total": {"type": "number"},
            },
        },
        "status_flags": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "data_gaps": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
        "fact_snapshot_ids": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}},
        "finance_surface": {
            "type": "object",
            "additionalProperties": False,
            "required": ["computed_at", "sections"],
            "properties": {
                "computed_at": {"type": "string", "maxLength": 64},
                "sections": {"type": "array", "minItems": 3, "maxItems": 6, "items": SECTION_SCHEMA},
            },
        },
        "body": {
            "type": "object",
            "additionalProperties": False,
            "required": ["format", "paragraphs", "word_count", "generation_path"],
            "properties": {
                "format": {"type": "string", "const": "markdown"},
                "paragraphs": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 600}},
                "word_count": {"type": "integer"},
                "generation_path": {"type": "string", "enum": ["ai_polish", "deterministic_fallback"]},
            },
        },
        "provenance": {"type": "object"},
        "validation": {"type": "object"},
    },
}


SYSTEM_PROMPT = """
You may polish a finance receipt summary, but the runtime owns finance_surface and headline_metrics.

Rules:
1. Use only receipt_facts metrics already present in facts_json.
2. Do not mention employee IDs, purchaser names, file paths, image URLs, prompts, secrets, tokens, or raw model output.
3. Do not invent amounts, approvals, budgets, forecasts, reimbursements, or recommendations.
4. Return strict JSON only.
""".strip()


USER_PROMPT_TEMPLATE = """
Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


PHRASES = [
    ("finance_summary_v1:employee", "en", r"(?i)\bemployee_id\b", "regex"),
    ("finance_summary_v1:purchaser", "en", r"(?i)\bpurchaser_name\b", "regex"),
    ("finance_summary_v1:file_path", "en", r"(?i)\bfile_path\b", "regex"),
    ("finance_summary_v1:image_url", "en", r"(?i)\bimage_url\b", "regex"),
    ("finance_summary_v1:approval", "en", r"(?i)\bapproved\b", "regex"),
    ("finance_summary_v1:reimburse", "en", r"(?i)\breimburse", "regex"),
    ("finance_summary_v1:employee_zh", "zh", "员工号", "literal"),
    ("finance_summary_v1:purchaser_zh", "zh", "购买人", "literal"),
]


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
    phrases = sa.table(
        "expression_forbidden_phrases",
        sa.column("id", sa.String),
        sa.column("set_id", sa.String),
        sa.column("audience_id", sa.String),
        sa.column("language", sa.String),
        sa.column("phrase", sa.String),
        sa.column("match_type", sa.String),
        sa.column("severity", sa.String),
        sa.column("replacement_hint", sa.Text),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        templates,
        [
            {
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Finance summary",
                "artifact_type": TEMPLATE_ID,
                "stage": "expression",
                "default_audience_id": "finance",
                "description": "Shadow-only project finance receipt summary from receipt_facts.",
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
                "notes": "Phase 1 uses deterministic fallback only; AI polish is disabled until numeric-surface immutability validation exists.",
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
                "audience_id": "finance",
                "version": "v1",
                "json_schema": CONTRACT_SCHEMA,
                "required_fact_paths_json": [
                    "scope.company_id",
                    "scope.project_id",
                    "receipt_metrics.receipt_count",
                    "receipt_metrics.total_amount",
                    "receipt_metrics.receipts_missing_amount",
                    "receipt_metrics.fuel_gallons_total",
                ],
                "forbidden_claims_json": [
                    {"pattern": "\\bemployee_id\\b"},
                    {"pattern": "\\bpurchaser_name\\b"},
                    {"pattern": "https?://"},
                    {"pattern": "\\bfile_path\\b"},
                    {"pattern": "\\bimage_url\\b"},
                    {"pattern": "\\bapproved\\b"},
                    {"pattern": "\\breimburse"},
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        phrases,
        [
            {
                "id": row_id,
                "set_id": "finance_evidence_zh_v1",
                "audience_id": "finance",
                "language": language,
                "phrase": phrase,
                "match_type": match_type,
                "severity": "error",
                "replacement_hint": None,
                "is_active": True,
                "created_at": now,
            }
            for row_id, language, phrase, match_type in PHRASES
        ],
    )


def downgrade() -> None:
    phrase_ids = [row_id for row_id, _, _, _ in PHRASES]
    phrases = sa.table("expression_forbidden_phrases", sa.column("id", sa.String))
    contracts = sa.table("expression_output_contracts", sa.column("id", sa.String))
    bindings = sa.table("expression_prompt_bindings", sa.column("id", sa.String))
    versions = sa.table("expression_prompt_versions", sa.column("id", sa.String))
    templates = sa.table("expression_prompt_templates", sa.column("id", sa.String))
    op.execute(phrases.delete().where(phrases.c.id.in_(phrase_ids)))
    op.execute(contracts.delete().where(contracts.c.id == CONTRACT_ID))
    op.execute(bindings.delete().where(bindings.c.id == BINDING_ID))
    op.execute(versions.delete().where(versions.c.id == PROMPT_VERSION_ID))
    op.execute(templates.delete().where(templates.c.id == TEMPLATE_ID))
