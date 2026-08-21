"""ai expression layer registry

Revision ID: 20260615_0021
Revises: 20260415_0020
Create Date: 2026-06-15 20:20:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260615_0021"
down_revision = "20260415_0020"
branch_labels = None
depends_on = None


EMPLOYEE_CONTRIBUTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary_line",
        "contribution_explanation",
        "strengths",
        "suggestions",
        "comparison_text",
        "recent_highlights",
        "disclaimer",
    ],
    "properties": {
        "summary_line": {"type": "string", "minLength": 1, "maxLength": 320},
        "contribution_explanation": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 280},
        },
        "strengths": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 220},
        },
        "suggestions": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 220},
        },
        "comparison_text": {"type": "string", "minLength": 1, "maxLength": 280},
        "recent_highlights": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["photo_id", "text"],
                "properties": {
                    "photo_id": {"type": ["integer", "string"]},
                    "text": {"type": "string", "minLength": 1, "maxLength": 220},
                },
            },
        },
        "disclaimer": {
            "type": "string",
            "const": "该说明仅用于现场记录参考，不是绩效评分。",
        },
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    op.create_table(
        "expression_audiences",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_language", sa.String(length=16), nullable=False),
        sa.Column("visibility_policy_json", sa.JSON(), nullable=True),
        sa.Column("forbidden_phrase_set_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_expression_audiences_code"),
    )
    op.create_index("ix_expression_audiences_code", "expression_audiences", ["code"], unique=False)
    op.create_index(
        "ix_expression_audiences_forbidden_phrase_set_id",
        "expression_audiences",
        ["forbidden_phrase_set_id"],
        unique=False,
    )
    op.create_index("ix_expression_audiences_is_active", "expression_audiences", ["is_active"], unique=False)

    op.create_table(
        "expression_prompt_templates",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("artifact_type", sa.String(length=80), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("default_audience_id", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["default_audience_id"], ["expression_audiences.id"]),
        sa.UniqueConstraint("slug", name="uq_expression_prompt_templates_slug"),
    )
    op.create_index("ix_expression_prompt_templates_slug", "expression_prompt_templates", ["slug"], unique=False)
    op.create_index(
        "ix_expression_prompt_templates_artifact_type",
        "expression_prompt_templates",
        ["artifact_type"],
        unique=False,
    )
    op.create_index("ix_expression_prompt_templates_stage", "expression_prompt_templates", ["stage"], unique=False)
    op.create_index(
        "ix_expression_prompt_templates_default_audience_id",
        "expression_prompt_templates",
        ["default_audience_id"],
        unique=False,
    )

    op.create_table(
        "expression_prompt_versions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("template_id", sa.String(length=96), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("user_prompt_template", sa.Text(), nullable=False),
        sa.Column("few_shot_json", sa.JSON(), nullable=True),
        sa.Column("json_schema_override", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["expression_prompt_templates.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("template_id", "version", name="uq_expression_prompt_versions_template_version"),
    )
    op.create_index(
        "ix_expression_prompt_versions_template_status",
        "expression_prompt_versions",
        ["template_id", "status"],
        unique=False,
    )
    op.create_index("ix_expression_prompt_versions_template_id", "expression_prompt_versions", ["template_id"], unique=False)
    op.create_index("ix_expression_prompt_versions_status", "expression_prompt_versions", ["status"], unique=False)
    op.create_index(
        "ix_expression_prompt_versions_created_by_user_id",
        "expression_prompt_versions",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "expression_prompt_bindings",
        sa.Column("id", sa.String(length=160), primary_key=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=True),
        sa.Column("template_id", sa.String(length=96), nullable=False),
        sa.Column("prompt_version_id", sa.String(length=128), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["expression_prompt_templates.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["expression_prompt_versions.id"]),
    )
    op.create_index("ix_expression_prompt_bindings_scope", "expression_prompt_bindings", ["scope_type", "scope_id"], unique=False)
    op.create_index(
        "ix_expression_prompt_bindings_template_priority",
        "expression_prompt_bindings",
        ["template_id", "priority"],
        unique=False,
    )
    op.create_index("ix_expression_prompt_bindings_scope_type", "expression_prompt_bindings", ["scope_type"], unique=False)
    op.create_index("ix_expression_prompt_bindings_scope_id", "expression_prompt_bindings", ["scope_id"], unique=False)
    op.create_index("ix_expression_prompt_bindings_template_id", "expression_prompt_bindings", ["template_id"], unique=False)
    op.create_index(
        "ix_expression_prompt_bindings_prompt_version_id",
        "expression_prompt_bindings",
        ["prompt_version_id"],
        unique=False,
    )
    op.create_index("ix_expression_prompt_bindings_priority", "expression_prompt_bindings", ["priority"], unique=False)

    op.create_table(
        "expression_output_contracts",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("artifact_type", sa.String(length=80), nullable=False),
        sa.Column("audience_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("json_schema", sa.JSON(), nullable=False),
        sa.Column("required_fact_paths_json", sa.JSON(), nullable=True),
        sa.Column("forbidden_claims_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["audience_id"], ["expression_audiences.id"]),
        sa.UniqueConstraint(
            "artifact_type",
            "audience_id",
            "version",
            name="uq_expression_output_contracts_artifact_audience_version",
        ),
    )
    op.create_index("ix_expression_output_contracts_artifact_type", "expression_output_contracts", ["artifact_type"], unique=False)
    op.create_index("ix_expression_output_contracts_audience_id", "expression_output_contracts", ["audience_id"], unique=False)
    op.create_index("ix_expression_output_contracts_is_active", "expression_output_contracts", ["is_active"], unique=False)

    op.create_table(
        "expression_forbidden_phrases",
        sa.Column("id", sa.String(length=96), primary_key=True),
        sa.Column("set_id", sa.String(length=64), nullable=False),
        sa.Column("audience_id", sa.String(length=64), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("phrase", sa.String(length=255), nullable=False),
        sa.Column("match_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("replacement_hint", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["audience_id"], ["expression_audiences.id"]),
    )
    op.create_index("ix_expression_forbidden_phrases_set_language", "expression_forbidden_phrases", ["set_id", "language"], unique=False)
    op.create_index(
        "ix_expression_forbidden_phrases_audience_language",
        "expression_forbidden_phrases",
        ["audience_id", "language"],
        unique=False,
    )
    op.create_index("ix_expression_forbidden_phrases_set_id", "expression_forbidden_phrases", ["set_id"], unique=False)
    op.create_index("ix_expression_forbidden_phrases_audience_id", "expression_forbidden_phrases", ["audience_id"], unique=False)
    op.create_index("ix_expression_forbidden_phrases_language", "expression_forbidden_phrases", ["language"], unique=False)
    op.create_index("ix_expression_forbidden_phrases_severity", "expression_forbidden_phrases", ["severity"], unique=False)
    op.create_index("ix_expression_forbidden_phrases_is_active", "expression_forbidden_phrases", ["is_active"], unique=False)

    op.create_table(
        "fact_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("employee_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_key_json", sa.JSON(), nullable=False),
        sa.Column("scope_key_hash", sa.String(length=128), nullable=False),
        sa.Column("assembler_version", sa.String(length=32), nullable=False),
        sa.Column("source_manifest_json", sa.JSON(), nullable=True),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("coverage_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.employee_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"]),
        sa.UniqueConstraint(
            "company_id",
            "scope_type",
            "scope_key_hash",
            "assembler_version",
            name="uq_fact_snapshots_company_scope_assembler",
        ),
    )
    op.create_index("ix_fact_snapshots_company_scope", "fact_snapshots", ["company_id", "scope_type"], unique=False)
    op.create_index("ix_fact_snapshots_employee_project", "fact_snapshots", ["employee_id", "project_id"], unique=False)
    op.create_index("ix_fact_snapshots_tenant_id", "fact_snapshots", ["tenant_id"], unique=False)
    op.create_index("ix_fact_snapshots_company_id", "fact_snapshots", ["company_id"], unique=False)
    op.create_index("ix_fact_snapshots_employee_id", "fact_snapshots", ["employee_id"], unique=False)
    op.create_index("ix_fact_snapshots_project_id", "fact_snapshots", ["project_id"], unique=False)
    op.create_index("ix_fact_snapshots_scope_type", "fact_snapshots", ["scope_type"], unique=False)
    op.create_index("ix_fact_snapshots_scope_key_hash", "fact_snapshots", ["scope_key_hash"], unique=False)
    op.create_index("ix_fact_snapshots_created_at", "fact_snapshots", ["created_at"], unique=False)

    op.create_table(
        "expression_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_job_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("fact_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("prompt_version_id", sa.String(length=128), nullable=True),
        sa.Column("contract_id", sa.String(length=128), nullable=True),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_key_json", sa.JSON(), nullable=True),
        sa.Column("artifact_type", sa.String(length=80), nullable=False),
        sa.Column("audience_id", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("structured_json", sa.JSON(), nullable=True),
        sa.Column("raw_model_output", sa.Text(), nullable=True),
        sa.Column("rendered_markdown", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_errors_json", sa.JSON(), nullable=True),
        sa.Column("model_used", sa.String(length=255), nullable=True),
        sa.Column("backend_profile_json", sa.JSON(), nullable=True),
        sa.Column("promoted", sa.Boolean(), nullable=False),
        sa.Column("supersedes_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_job_id"], ["task_jobs.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"]),
        sa.ForeignKeyConstraint(["fact_snapshot_id"], ["fact_snapshots.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["expression_prompt_versions.id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["expression_output_contracts.id"]),
        sa.ForeignKeyConstraint(["audience_id"], ["expression_audiences.id"]),
        sa.ForeignKeyConstraint(["supersedes_artifact_id"], ["expression_artifacts.id"]),
    )
    op.create_index("ix_expression_artifacts_company_scope", "expression_artifacts", ["company_id", "scope_type"], unique=False)
    op.create_index(
        "ix_expression_artifacts_scope_audience",
        "expression_artifacts",
        ["scope_type", "audience_id", "artifact_type"],
        unique=False,
    )
    op.create_index(
        "ix_expression_artifacts_promoted_created",
        "expression_artifacts",
        ["promoted", "created_at"],
        unique=False,
    )
    op.create_index("ix_expression_artifacts_task_job_id", "expression_artifacts", ["task_job_id"], unique=False)
    op.create_index("ix_expression_artifacts_tenant_id", "expression_artifacts", ["tenant_id"], unique=False)
    op.create_index("ix_expression_artifacts_company_id", "expression_artifacts", ["company_id"], unique=False)
    op.create_index("ix_expression_artifacts_fact_snapshot_id", "expression_artifacts", ["fact_snapshot_id"], unique=False)
    op.create_index("ix_expression_artifacts_prompt_version_id", "expression_artifacts", ["prompt_version_id"], unique=False)
    op.create_index("ix_expression_artifacts_contract_id", "expression_artifacts", ["contract_id"], unique=False)
    op.create_index("ix_expression_artifacts_scope_type", "expression_artifacts", ["scope_type"], unique=False)
    op.create_index("ix_expression_artifacts_artifact_type", "expression_artifacts", ["artifact_type"], unique=False)
    op.create_index("ix_expression_artifacts_audience_id", "expression_artifacts", ["audience_id"], unique=False)
    op.create_index("ix_expression_artifacts_language", "expression_artifacts", ["language"], unique=False)
    op.create_index("ix_expression_artifacts_validation_status", "expression_artifacts", ["validation_status"], unique=False)
    op.create_index("ix_expression_artifacts_promoted", "expression_artifacts", ["promoted"], unique=False)
    op.create_index("ix_expression_artifacts_supersedes_artifact_id", "expression_artifacts", ["supersedes_artifact_id"], unique=False)
    op.create_index("ix_expression_artifacts_created_at", "expression_artifacts", ["created_at"], unique=False)

    _seed_expression_registry()


def _seed_expression_registry() -> None:
    now = _now()
    audiences = sa.table(
        "expression_audiences",
        sa.column("id", sa.String),
        sa.column("code", sa.String),
        sa.column("title", sa.String),
        sa.column("description", sa.Text),
        sa.column("default_language", sa.String),
        sa.column("visibility_policy_json", sa.JSON),
        sa.column("forbidden_phrase_set_id", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        audiences,
        [
            {
                "id": "employee",
                "code": "employee",
                "title": "Employee mobile contribution",
                "description": "Neutral APP-facing explanation of one employee's field record contribution.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["self"], "allow_manager_preview": True},
                "forbidden_phrase_set_id": "employee_contribution_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "project_manager",
                "code": "project_manager",
                "title": "Project manager field overview",
                "description": "Manager-facing summary for project evidence review.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["project_manager", "admin"]},
                "forbidden_phrase_set_id": "manager_overview_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "client",
                "code": "client",
                "title": "Client progress explanation",
                "description": "Client-safe project progress wording.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["client", "project_manager", "admin"]},
                "forbidden_phrase_set_id": "client_progress_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "executive",
                "code": "executive",
                "title": "Company executive overview",
                "description": "Company-wide executive narrative over verified facts.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["owner", "admin"]},
                "forbidden_phrase_set_id": "executive_overview_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "finance",
                "code": "finance",
                "title": "Finance evidence explanation",
                "description": "Finance-facing wording for receipt and cost evidence.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["owner", "admin", "finance"]},
                "forbidden_phrase_set_id": "finance_evidence_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "operations",
                "code": "operations",
                "title": "Operations field health",
                "description": "Operations-facing field data health summary.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["owner", "admin", "operations"]},
                "forbidden_phrase_set_id": "operations_health_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": "platform_admin",
                "code": "platform_admin",
                "title": "Platform admin diagnostics",
                "description": "Internal platform/admin narrative and validator diagnostics.",
                "default_language": "zh",
                "visibility_policy_json": {"show_to": ["platform_super_admin"]},
                "forbidden_phrase_set_id": "platform_admin_zh_v1",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )

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
    op.bulk_insert(
        templates,
        [
            {
                "id": "employee_contribution_narrative",
                "slug": "employee_contribution_narrative",
                "title": "Employee contribution narrative",
                "artifact_type": "employee_contribution_narrative",
                "stage": "expression",
                "default_audience_id": "employee",
                "description": "Writes a neutral APP-facing contribution explanation from a fact snapshot.",
                "created_at": now,
                "updated_at": now,
            }
        ],
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
    op.bulk_insert(
        versions,
        [
            {
                "id": "employee_contribution_narrative:v1",
                "template_id": "employee_contribution_narrative",
                "version": "v1",
                "system_prompt": (
                    "你是现场记录系统的表达层。只根据输入 JSON 里的 facts 写中文，不新增事实、"
                    "不猜测原因、不评价员工绩效、不输出排名或分数。输出必须是严格 JSON，且必须符合契约。"
                ),
                "user_prompt_template": (
                    "请把以下事实快照写成员工 APP 可展示的贡献说明。"
                    "语气要中性、具体、鼓励员工继续记录现场，但不能使用绩效、排名、努力程度、"
                    "低质量、偷懒等判断性措辞。固定 disclaimer 必须完全一致。\n\n"
                    "事实快照 JSON:\n{facts_json}\n\n"
                    "输出契约 JSON Schema:\n{contract_schema_json}"
                ),
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": "Initial shadow prompt for database-backed employee contribution expression.",
                "created_at": now,
            }
        ],
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
        bindings,
        [
            {
                "id": "global:employee_contribution_narrative:v1",
                "scope_type": "global",
                "scope_id": None,
                "template_id": "employee_contribution_narrative",
                "prompt_version_id": "employee_contribution_narrative:v1",
                "priority": 100,
                "effective_from": now,
                "effective_until": None,
                "created_at": now,
            }
        ],
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
        contracts,
        [
            {
                "id": "employee_contribution_narrative:employee:v1",
                "artifact_type": "employee_contribution_narrative",
                "audience_id": "employee",
                "version": "v1",
                "json_schema": EMPLOYEE_CONTRIBUTION_SCHEMA,
                "required_fact_paths_json": [
                    "scope.employee_id",
                    "scope.project_id",
                    "window.current",
                    "counts.photos_current",
                    "counts.completed_ai_current",
                ],
                "forbidden_claims_json": [
                    "Do not claim performance quality, employee diligence, project completion, or cross-employee rank.",
                    "Do not convert missing coverage into employee fault.",
                ],
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
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
        phrases,
        [
            {
                "id": "employee_contribution_zh_v1:performance",
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": "绩效",
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": "改为现场记录、资料完整度、记录覆盖。",
                "is_active": True,
                "created_at": now,
            },
            {
                "id": "employee_contribution_zh_v1:score",
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": "评分",
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": "不要给分，改为概览或说明。",
                "is_active": True,
                "created_at": now,
            },
            {
                "id": "employee_contribution_zh_v1:rank",
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": "排名",
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": "只做本人当前窗口和上一窗口趋势说明。",
                "is_active": True,
                "created_at": now,
            },
            {
                "id": "employee_contribution_zh_v1:hard_work",
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": "继续努力",
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": "改为继续保持现场记录的及时性或完整性。",
                "is_active": True,
                "created_at": now,
            },
            {
                "id": "employee_contribution_zh_v1:low_quality",
                "set_id": "employee_contribution_zh_v1",
                "audience_id": "employee",
                "language": "zh",
                "phrase": "低质量",
                "match_type": "literal",
                "severity": "error",
                "replacement_hint": "只描述可核验的记录限制，例如可识别信息较少。",
                "is_active": True,
                "created_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_expression_artifacts_created_at", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_supersedes_artifact_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_promoted", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_validation_status", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_language", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_audience_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_artifact_type", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_scope_type", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_contract_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_prompt_version_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_fact_snapshot_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_company_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_tenant_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_task_job_id", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_promoted_created", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_scope_audience", table_name="expression_artifacts")
    op.drop_index("ix_expression_artifacts_company_scope", table_name="expression_artifacts")
    op.drop_table("expression_artifacts")

    op.drop_index("ix_fact_snapshots_created_at", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_scope_key_hash", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_scope_type", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_project_id", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_employee_id", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_company_id", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_tenant_id", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_employee_project", table_name="fact_snapshots")
    op.drop_index("ix_fact_snapshots_company_scope", table_name="fact_snapshots")
    op.drop_table("fact_snapshots")

    op.drop_index("ix_expression_forbidden_phrases_is_active", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_severity", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_language", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_audience_id", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_set_id", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_audience_language", table_name="expression_forbidden_phrases")
    op.drop_index("ix_expression_forbidden_phrases_set_language", table_name="expression_forbidden_phrases")
    op.drop_table("expression_forbidden_phrases")

    op.drop_index("ix_expression_output_contracts_is_active", table_name="expression_output_contracts")
    op.drop_index("ix_expression_output_contracts_audience_id", table_name="expression_output_contracts")
    op.drop_index("ix_expression_output_contracts_artifact_type", table_name="expression_output_contracts")
    op.drop_table("expression_output_contracts")

    op.drop_index("ix_expression_prompt_bindings_priority", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_prompt_version_id", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_template_id", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_scope_id", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_scope_type", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_template_priority", table_name="expression_prompt_bindings")
    op.drop_index("ix_expression_prompt_bindings_scope", table_name="expression_prompt_bindings")
    op.drop_table("expression_prompt_bindings")

    op.drop_index("ix_expression_prompt_versions_created_by_user_id", table_name="expression_prompt_versions")
    op.drop_index("ix_expression_prompt_versions_status", table_name="expression_prompt_versions")
    op.drop_index("ix_expression_prompt_versions_template_id", table_name="expression_prompt_versions")
    op.drop_index("ix_expression_prompt_versions_template_status", table_name="expression_prompt_versions")
    op.drop_table("expression_prompt_versions")

    op.drop_index("ix_expression_prompt_templates_default_audience_id", table_name="expression_prompt_templates")
    op.drop_index("ix_expression_prompt_templates_stage", table_name="expression_prompt_templates")
    op.drop_index("ix_expression_prompt_templates_artifact_type", table_name="expression_prompt_templates")
    op.drop_index("ix_expression_prompt_templates_slug", table_name="expression_prompt_templates")
    op.drop_table("expression_prompt_templates")

    op.drop_index("ix_expression_audiences_is_active", table_name="expression_audiences")
    op.drop_index("ix_expression_audiences_forbidden_phrase_set_id", table_name="expression_audiences")
    op.drop_index("ix_expression_audiences_code", table_name="expression_audiences")
    op.drop_table("expression_audiences")
