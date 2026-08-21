"""executive company health summary expression

Revision ID: 20260616_0034
Revises: 20260616_0033
Create Date: 2026-06-16 07:15:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0034"
down_revision = "20260616_0033"
branch_labels = None
depends_on = None


TEMPLATE_ID = "executive_company_health_summary"
PROMPT_VERSION_ID = "executive_company_health_summary:v1"
BINDING_ID = "global:executive_company_health_summary:v1"
CONTRACT_ID = "executive_company_health_summary:executive:v1"


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


CARD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "card_id", "status", "title", "lines", "fact_refs", "metrics", "rule_flags"],
    "properties": {
        "item_id": {"type": "string", "maxLength": 80},
        "card_id": {
            "type": "string",
            "enum": [
                "project_portfolio",
                "photo_activity",
                "progress_reports",
                "expression_readiness",
                "finance_receipt_coverage",
            ],
        },
        "status": {"type": "string", "enum": ["green", "amber", "red"]},
        "title": {"type": "string", "maxLength": 120},
        "lines": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 600}},
        "fact_refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": FACT_REF_SCHEMA},
        "metrics": {"type": "object"},
        "rule_flags": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
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
        "headline_metrics",
        "fact_snapshot_ids",
        "company_health_surface",
        "body",
        "provenance",
        "validation",
    ],
    "properties": {
        "artifact_type": {"type": "string", "const": TEMPLATE_ID},
        "artifact_version": {"type": "string", "const": "1.0.0"},
        "audience": {"type": "string", "const": "executive"},
        "visibility": {"type": "string", "const": "shadow"},
        "company_id": {"type": "string", "maxLength": 64},
        "scope_type": {"type": "string", "const": "company_health_period"},
        "as_of": {"type": "string", "maxLength": 64},
        "overall_status": {"type": "string", "enum": ["green", "amber", "red"]},
        "headline_metrics": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "projects_total",
                "photos_current",
                "completed_reports_current",
                "shadow_invalid_current",
                "receipt_count",
            ],
            "properties": {
                "projects_total": {"type": "integer"},
                "photos_current": {"type": "integer"},
                "completed_reports_current": {"type": "integer"},
                "shadow_invalid_current": {"type": "integer"},
                "receipt_count": {"type": "integer"},
            },
        },
        "fact_snapshot_ids": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}},
        "company_health_surface": {
            "type": "object",
            "additionalProperties": False,
            "required": ["computed_at", "cards"],
            "properties": {
                "computed_at": {"type": "string", "maxLength": 64},
                "cards": {"type": "array", "minItems": 5, "maxItems": 8, "items": CARD_SCHEMA},
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
This executive company health artifact is assembled by the server runtime from locked database facts.

Rules:
1. Use only facts_json metrics already present in the fact snapshot.
2. Do not mention employee identifiers, file paths, image URLs, prompts, raw model outputs, API keys, secrets, tokens, or passwords.
3. Do not invent risks, forecasts, rankings, responsibility attribution, performance scores, or action language.
4. Return strict JSON only.
""".strip()


USER_PROMPT_TEMPLATE = """
Facts JSON:
{facts_json}

Output contract JSON Schema:
{contract_schema_json}
""".strip()


PHRASES = [
    ("executive_company_health_v1:employee_id", "en", r"(?i)\bemployee_id\b", "regex"),
    ("executive_company_health_v1:file_path", "en", r"(?i)\bfile_path\b", "regex"),
    ("executive_company_health_v1:image_url", "en", r"(?i)\bimage_url\b", "regex"),
    ("executive_company_health_v1:raw", "en", r"(?i)\braw_model_output\b", "regex"),
    ("executive_company_health_v1:secret", "en", r"(?i)\bsecret\b", "regex"),
    ("executive_company_health_v1:password", "en", r"(?i)\bpassword\b", "regex"),
    ("executive_company_health_v1:token", "en", r"(?i)\btoken\b", "regex"),
    ("executive_company_health_v1:should", "en", r"(?i)\bshould\b", "regex"),
    ("executive_company_health_v1:must", "en", r"(?i)\bmust\b", "regex"),
    ("executive_company_health_v1:priority", "en", r"(?i)\bpriority\b", "regex"),
    ("executive_company_health_v1:recommend", "en", r"(?i)\brecommend\b", "regex"),
    ("executive_company_health_v1:urgent", "en", r"(?i)\burgent\b", "regex"),
    ("executive_company_health_v1:rank", "en", r"(?i)\brank(?:ing)?\b", "regex"),
    ("executive_company_health_v1:performance", "en", r"(?i)\bperformance score\b", "regex"),
    ("executive_company_health_v1:employee_zh", "zh", "员工号", "literal"),
    ("executive_company_health_v1:rank_zh", "zh", "排名", "literal"),
    ("executive_company_health_v1:score_zh", "zh", "绩效评分", "literal"),
    ("executive_company_health_v1:responsibility_zh", "zh", "责任归因", "literal"),
]


def _bulk_insert_ignore(table: sa.TableClause, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        stmt = insert(table).values(rows).on_conflict_do_nothing(index_elements=["id"])
        op.execute(stmt)
        return
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        stmt = insert(table).values(rows).prefix_with("OR IGNORE")
        op.execute(stmt)
        return
    for row in rows:
        row_id = row.get("id")
        exists = bind.execute(sa.select(sa.literal(1)).select_from(table).where(table.c.id == row_id)).first()
        if exists is None:
            op.bulk_insert(table, [row])


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
    _bulk_insert_ignore(
        templates,
        [
            {
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Executive company health summary",
                "artifact_type": TEMPLATE_ID,
                "stage": "expression",
                "default_audience_id": "executive",
                "description": "Shadow-only company health summary assembled from database facts.",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    _bulk_insert_ignore(
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
                "notes": "Phase 1 uses deterministic fallback only; AI polish is disabled until company-health surface validation exists.",
                "created_at": now,
            }
        ],
    )
    _bulk_insert_ignore(
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
    _bulk_insert_ignore(
        contracts,
        [
            {
                "id": CONTRACT_ID,
                "artifact_type": TEMPLATE_ID,
                "audience_id": "executive",
                "version": "v1",
                "json_schema": CONTRACT_SCHEMA,
                "required_fact_paths_json": [
                    "scope.company_id",
                    "project_portfolio.projects_total",
                    "photo_activity.photos_current",
                    "progress_reports.completed_current",
                    "expression_readiness.shadow_invalid_current",
                    "finance_receipt_coverage.receipt_count",
                ],
                "forbidden_claims_json": [
                    {"pattern": "\\bemployee_id\\b"},
                    {"pattern": "https?://"},
                    {"pattern": "\\bfile_path\\b"},
                    {"pattern": "\\bimage_url\\b"},
                    {"pattern": "\\braw_model_output\\b"},
                    {"pattern": "\\bshould\\b"},
                    {"pattern": "\\bmust\\b"},
                    {"pattern": "\\bpriority\\b"},
                    {"pattern": "\\brecommend\\b"},
                    {"pattern": "\\burgent\\b"},
                    {"pattern": "\\brank(?:ing)?\\b"},
                    {"pattern": "\\bperformance score\\b"},
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    _bulk_insert_ignore(
        phrases,
        [
            {
                "id": row_id,
                "set_id": "executive_overview_zh_v1",
                "audience_id": "executive",
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
