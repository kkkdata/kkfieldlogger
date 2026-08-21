"""operations health summary expression

Revision ID: 20260616_0032
Revises: 20260616_0031
Create Date: 2026-06-16 06:25:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0032"
down_revision = "20260616_0031"
branch_labels = None
depends_on = None


TEMPLATE_ID = "operations_health_summary"
PROMPT_VERSION_ID = "operations_health_summary:v1"
BINDING_ID = "global:operations_health_summary:v1"
CONTRACT_ID = "operations_health_summary:operations:v1"


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


DIMENSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "item_id",
        "dimension_id",
        "status",
        "deterministic_summary",
        "fact_refs",
        "metrics",
        "threshold_breaches",
    ],
    "properties": {
        "item_id": {"type": "string", "maxLength": 80},
        "dimension_id": {
            "type": "string",
            "enum": ["task_jobs", "photo_processing", "ai_analysis", "expression_artifacts"],
        },
        "status": {"type": "string", "enum": ["green", "amber", "red"]},
        "deterministic_summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "fact_refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": FACT_REF_SCHEMA},
        "metrics": {"type": "object"},
        "threshold_breaches": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
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
        "company_id",
        "scope_type",
        "as_of",
        "overall_status",
        "fact_snapshot_ids",
        "health_surface",
        "body",
        "provenance",
        "validation",
    ],
    "properties": {
        "artifact_type": {"type": "string", "const": TEMPLATE_ID},
        "artifact_version": {"type": "string", "const": "1.0.0"},
        "audience": {"type": "string", "const": "operations"},
        "visibility": {"type": "string", "const": "shadow"},
        "company_id": {"type": "string", "maxLength": 64},
        "scope_type": {"type": "string", "const": "company_operations"},
        "as_of": {"type": "string", "maxLength": 64},
        "overall_status": {"type": "string", "enum": ["green", "amber", "red"]},
        "fact_snapshot_ids": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}},
        "health_surface": {
            "type": "object",
            "additionalProperties": False,
            "required": ["computed_at", "dimensions"],
            "properties": {
                "computed_at": {"type": "string", "maxLength": 64},
                "dimensions": {"type": "array", "minItems": 4, "maxItems": 8, "items": DIMENSION_SCHEMA},
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
You may polish a short operations health summary, but the runtime owns health_surface and overall_status.

Rules:
1. Use only facts_json metrics already present in the operations health fact snapshot.
2. Do not mention file paths, image URLs, prompts, raw model outputs, API keys, secrets, tokens, or passwords.
3. Do not invent metrics, statuses, outages, financial claims, employee performance claims, or customer commitments.
4. Keep notes factual and cite metric keys only.
5. Return strict JSON only.
""".strip()


USER_PROMPT_TEMPLATE = """
This artifact is normally assembled by the server runtime from locked database metrics.

Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


PHRASES = [
    ("operations_health_v1:file_path", "en", r"(?i)\bfile_path\b", "regex"),
    ("operations_health_v1:image_url", "en", r"(?i)\bimage_url\b", "regex"),
    ("operations_health_v1:api_key", "en", r"(?i)\bapi_key\b", "regex"),
    ("operations_health_v1:secret", "en", r"(?i)\bsecret\b", "regex"),
    ("operations_health_v1:password", "en", r"(?i)\bpassword\b", "regex"),
    ("operations_health_v1:token", "en", r"(?i)\btoken\b", "regex"),
    ("operations_health_v1:prompt", "en", r"(?i)\bprompt_used\b", "regex"),
    ("operations_health_v1:raw", "en", r"(?i)\braw_model_output\b", "regex"),
    ("operations_health_v1:secret_zh", "zh", "密钥", "literal"),
    ("operations_health_v1:password_zh", "zh", "密码", "literal"),
    ("operations_health_v1:path_zh", "zh", "文件路径", "literal"),
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
                "title": "Operations health summary",
                "artifact_type": TEMPLATE_ID,
                "stage": "expression",
                "default_audience_id": "operations",
                "description": "Shadow-only operations health summary assembled from database metrics.",
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
                "notes": "Phase 1 uses deterministic fallback only; AI polish is disabled until health-surface immutability validation exists.",
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
                "audience_id": "operations",
                "version": "v1",
                "json_schema": CONTRACT_SCHEMA,
                "required_fact_paths_json": [
                    "scope.company_id",
                    "task_jobs.queued_backlog",
                    "photo_processing.project_photos_current",
                    "ai_analysis.logs_current",
                    "expression_artifacts.artifacts_current",
                ],
                "forbidden_claims_json": [
                    {"pattern": "https?://"},
                    {"pattern": "\\bfile_path\\b"},
                    {"pattern": "\\bimage_url\\b"},
                    {"pattern": "\\bapi_key\\b"},
                    {"pattern": "\\bsecret\\b"},
                    {"pattern": "\\bpassword\\b"},
                    {"pattern": "\\btoken\\b"},
                    {"pattern": "\\bprompt_used\\b"},
                    {"pattern": "\\braw_model_output\\b"},
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
                "set_id": "operations_health_zh_v1",
                "audience_id": "operations",
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
