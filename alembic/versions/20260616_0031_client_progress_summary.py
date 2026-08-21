"""client progress summary expression

Revision ID: 20260616_0031
Revises: 20260616_0030
Create Date: 2026-06-16 06:05:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0031"
down_revision = "20260616_0030"
branch_labels = None
depends_on = None


TEMPLATE_ID = "client_progress_summary"
PROMPT_VERSION_ID = "client_progress_summary:v1"
BINDING_ID = "global:client_progress_summary:v1"
CONTRACT_ID = "client_progress_summary:client:v1"
DISCLAIMER = "This summary is based only on approved client-visible records and is not a contract commitment or final acceptance decision."


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


SUMMARY_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "category", "deterministic_summary", "fact_refs", "metrics"],
    "properties": {
        "item_id": {"type": "string", "maxLength": 80},
        "category": {
            "type": "string",
            "enum": ["progress_status", "work_completed", "recent_activity", "evidence_confidence", "limitations"],
        },
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
        "summary_surface",
        "body",
        "disclaimer",
        "provenance",
        "validation",
    ],
    "properties": {
        "artifact_type": {"type": "string", "const": TEMPLATE_ID},
        "artifact_version": {"type": "string", "const": "1.0.0"},
        "audience": {"type": "string", "const": "client"},
        "visibility": {"type": "string", "const": "shadow"},
        "project_id": {"type": "string", "maxLength": 64},
        "as_of": {"type": "string", "maxLength": 64},
        "fact_snapshot_ids": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}},
        "summary_surface": {
            "type": "object",
            "additionalProperties": False,
            "required": ["computed_at", "items"],
            "properties": {
                "computed_at": {"type": "string", "maxLength": 64},
                "items": {"type": "array", "minItems": 1, "maxItems": 10, "items": SUMMARY_ITEM_SCHEMA},
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
        "disclaimer": {"type": "string", "const": DISCLAIMER},
        "provenance": {"type": "object"},
        "validation": {"type": "object"},
    },
}


SYSTEM_PROMPT = """
You may polish a client-safe project progress summary, but the runtime owns the summary_surface.

Rules:
1. Use only facts_json fields that are already client-safe.
2. Do not mention employees, internal notes, safety risks, quality risks, finances, costs, or recommended actions.
3. Do not add contract promises, acceptance decisions, deadlines, penalties, or blame.
4. Do not use advice language such as should, must, recommend, priority, urgent, or escalate.
5. Return strict JSON only.
""".strip()


USER_PROMPT_TEMPLATE = """
This artifact is normally assembled by the server runtime from redacted database facts.

Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


PHRASES = [
    ("client_progress_en_v1:employee", "en", r"(?i)\bemployee_id\b", "regex"),
    ("client_progress_en_v1:internal", "en", r"(?i)\binternal\b", "regex"),
    ("client_progress_en_v1:cost", "en", r"(?i)\bcost\b", "regex"),
    ("client_progress_en_v1:penalty", "en", r"(?i)\bpenalty\b", "regex"),
    ("client_progress_en_v1:breach", "en", r"(?i)\bbreach\b", "regex"),
    ("client_progress_en_v1:should", "en", r"(?i)\bshould\b", "regex"),
    ("client_progress_en_v1:priority", "en", r"(?i)\bpriority\b", "regex"),
    ("client_progress_zh_v1:performance", "zh", "绩效", "literal"),
    ("client_progress_zh_v1:internal", "zh", "内部", "literal"),
    ("client_progress_zh_v1:cost", "zh", "成本", "literal"),
    ("client_progress_zh_v1:penalty", "zh", "罚款", "literal"),
    ("client_progress_zh_v1:breach", "zh", "违约", "literal"),
    ("client_progress_zh_v1:safety", "zh", "安全隐患", "literal"),
    ("client_progress_zh_v1:quality", "zh", "质量问题", "literal"),
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
                "title": "Client progress summary",
                "artifact_type": TEMPLATE_ID,
                "stage": "expression",
                "default_audience_id": "client",
                "description": "Shadow-only client-safe progress summary assembled from redacted database facts.",
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
                "notes": "Phase 1 uses deterministic fallback only; AI polish is disabled until summary-surface immutability validation exists.",
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
                "audience_id": "client",
                "version": "v1",
                "json_schema": CONTRACT_SCHEMA,
                "required_fact_paths_json": [
                    "scope.company_id",
                    "scope.project_id",
                    "client_visible_photo_metrics.photos_current",
                    "latest_progress_report.has_completed_report",
                ],
                "forbidden_claims_json": [
                    {"pattern": "\\bemployee_id\\b"},
                    {"pattern": "\\binternal\\b"},
                    {"pattern": "\\bcost\\b"},
                    {"pattern": "\\bpenalty\\b"},
                    {"pattern": "\\bbreach\\b"},
                    {"pattern": "\\bshould\\b"},
                    {"pattern": "\\bmust\\b"},
                    {"pattern": "\\bpriority\\b"},
                    {"pattern": "\\brecommend\\b"},
                    {"pattern": "\\burgent\\b"},
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
                "set_id": "client_progress_zh_v1",
                "audience_id": "client",
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
