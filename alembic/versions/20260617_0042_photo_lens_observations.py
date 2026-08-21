"""Add photo lens observation tables.

Revision ID: 20260617_0042
Revises: 20260616_0041
Create Date: 2026-06-17 05:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260617_0042"
down_revision = "20260616_0041"
branch_labels = None
depends_on = None


def _json_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "lens_definitions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("lens_key", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False, server_default="core"),
        sa.Column("company_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("current_version_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("lens_key", "scope", "company_id", "project_id", name="uq_lens_definitions_scope_key"),
    )
    op.create_index("ix_lens_definitions_lens_key", "lens_definitions", ["lens_key"])
    op.create_index("ix_lens_definitions_company_id", "lens_definitions", ["company_id"])
    op.create_index("ix_lens_definitions_project_id", "lens_definitions", ["project_id"])
    op.create_index("ix_lens_definitions_current_version_id", "lens_definitions", ["current_version_id"])
    op.create_index(
        "ix_lens_definitions_scope_enabled",
        "lens_definitions",
        ["scope", "company_id", "project_id", "is_enabled"],
    )

    op.create_table(
        "lens_definition_versions",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("lens_id", sa.String(length=64), sa.ForeignKey("lens_definitions.id"), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("output_schema_json", json_type, nullable=True),
        sa.Column("validation_rules_json", sa.JSON(), nullable=True),
        sa.Column("model_profile_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.UniqueConstraint("lens_id", "version", name="uq_lens_definition_versions_lens_version"),
    )
    op.create_index("ix_lens_definition_versions_lens_id", "lens_definition_versions", ["lens_id"])
    op.create_index("ix_lens_definition_versions_status", "lens_definition_versions", ["status"])

    op.create_table(
        "photo_lens_observation_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("employee_id", sa.String(length=64), nullable=True),
        sa.Column("lens_id", sa.String(length=64), sa.ForeignKey("lens_definitions.id"), nullable=False),
        sa.Column("lens_version_id", sa.String(length=80), sa.ForeignKey("lens_definition_versions.id"), nullable=False),
        sa.Column("ai_analysis_log_id", sa.String(length=36), sa.ForeignKey("ai_analysis_logs.id"), nullable=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("model_used", sa.String(length=255), nullable=False),
        sa.Column("backend_profile_json", sa.JSON(), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=128), nullable=True),
        sa.Column("output_hash", sa.String(length=128), nullable=True),
        sa.Column("run_status", sa.String(length=32), nullable=False, server_default="completed"),
        sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="shadow_valid"),
        sa.Column("result_json", json_type, nullable=True),
        sa.Column("raw_output_ref", sa.String(length=255), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_photo_lens_observation_runs_photo_id", "photo_lens_observation_runs", ["photo_id"])
    op.create_index("ix_photo_lens_observation_runs_company_id", "photo_lens_observation_runs", ["company_id"])
    op.create_index("ix_photo_lens_observation_runs_project_id", "photo_lens_observation_runs", ["project_id"])
    op.create_index("ix_photo_lens_observation_runs_employee_id", "photo_lens_observation_runs", ["employee_id"])
    op.create_index("ix_photo_lens_observation_runs_lens_id", "photo_lens_observation_runs", ["lens_id"])
    op.create_index("ix_photo_lens_observation_runs_lens_version_id", "photo_lens_observation_runs", ["lens_version_id"])
    op.create_index("ix_photo_lens_observation_runs_ai_analysis_log_id", "photo_lens_observation_runs", ["ai_analysis_log_id"])
    op.create_index("ix_photo_lens_observation_runs_created_at", "photo_lens_observation_runs", ["created_at"])
    op.create_index("ix_photo_lens_runs_photo_lens_created", "photo_lens_observation_runs", ["photo_id", "lens_id", "created_at"])
    op.create_index("ix_photo_lens_runs_model_prompt", "photo_lens_observation_runs", ["model_used", "prompt_hash"])
    op.create_index("ix_photo_lens_runs_batch", "photo_lens_observation_runs", ["batch_id"])

    op.create_table(
        "photo_lens_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("employee_id", sa.String(length=64), nullable=True),
        sa.Column("lens_id", sa.String(length=64), sa.ForeignKey("lens_definitions.id"), nullable=False),
        sa.Column("lens_version_id", sa.String(length=80), sa.ForeignKey("lens_definition_versions.id"), nullable=False),
        sa.Column("active_run_id", sa.String(length=36), sa.ForeignKey("photo_lens_observation_runs.id"), nullable=False),
        sa.Column("observation_json", json_type, nullable=True),
        sa.Column("state_summary", sa.String(length=32), nullable=False, server_default="mixed"),
        sa.Column("observed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_observed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence_level", sa.String(length=32), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="shadow_valid"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_photo_lens_observations_photo_id", "photo_lens_observations", ["photo_id"])
    op.create_index("ix_photo_lens_observations_company_id", "photo_lens_observations", ["company_id"])
    op.create_index("ix_photo_lens_observations_project_id", "photo_lens_observations", ["project_id"])
    op.create_index("ix_photo_lens_observations_employee_id", "photo_lens_observations", ["employee_id"])
    op.create_index("ix_photo_lens_observations_lens_id", "photo_lens_observations", ["lens_id"])
    op.create_index("ix_photo_lens_observations_lens_version_id", "photo_lens_observations", ["lens_version_id"])
    op.create_index("ix_photo_lens_observations_active_run_id", "photo_lens_observations", ["active_run_id"])
    op.create_index("ix_photo_lens_observations_confidence_level", "photo_lens_observations", ["confidence_level"])
    op.create_index("ix_photo_lens_observations_validation_status", "photo_lens_observations", ["validation_status"])
    op.create_index("ix_photo_lens_observations_status", "photo_lens_observations", ["status"])
    op.create_index(
        "ix_photo_lens_observations_scope",
        "photo_lens_observations",
        ["company_id", "project_id", "lens_id", "validation_status"],
    )
    op.create_index("ix_photo_lens_observations_photo_lens", "photo_lens_observations", ["photo_id", "lens_id"])

    op.create_table(
        "photo_observation_promotions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("lens_id", sa.String(length=64), sa.ForeignKey("lens_definitions.id"), nullable=False),
        sa.Column("observation_id", sa.String(length=36), sa.ForeignKey("photo_lens_observations.id"), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("photo_lens_observation_runs.id"), nullable=False),
        sa.Column("promoted_fields_json", json_type, nullable=True),
        sa.Column("target", sa.String(length=80), nullable=False, server_default="photos_tag_json"),
        sa.Column("gate_name", sa.String(length=120), nullable=False),
        sa.Column("gate_result", sa.String(length=32), nullable=False, server_default="promoted_valid"),
        sa.Column("promoted_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_photo_observation_promotions_photo_id", "photo_observation_promotions", ["photo_id"])
    op.create_index("ix_photo_observation_promotions_company_id", "photo_observation_promotions", ["company_id"])
    op.create_index("ix_photo_observation_promotions_project_id", "photo_observation_promotions", ["project_id"])
    op.create_index("ix_photo_observation_promotions_lens_id", "photo_observation_promotions", ["lens_id"])
    op.create_index("ix_photo_observation_promotions_observation_id", "photo_observation_promotions", ["observation_id"])
    op.create_index("ix_photo_observation_promotions_run_id", "photo_observation_promotions", ["run_id"])
    op.create_index("ix_photo_observation_promotions_gate_result", "photo_observation_promotions", ["gate_result"])
    op.create_index(
        "ix_photo_observation_promotions_photo_lens",
        "photo_observation_promotions",
        ["photo_id", "lens_id", "promoted_at"],
    )
    op.create_index("ix_photo_observation_promotions_target", "photo_observation_promotions", ["target", "gate_result"])


def downgrade() -> None:
    op.drop_index("ix_photo_observation_promotions_target", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_photo_lens", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_gate_result", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_run_id", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_observation_id", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_lens_id", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_project_id", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_company_id", table_name="photo_observation_promotions")
    op.drop_index("ix_photo_observation_promotions_photo_id", table_name="photo_observation_promotions")
    op.drop_table("photo_observation_promotions")

    op.drop_index("ix_photo_lens_observations_photo_lens", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_scope", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_status", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_validation_status", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_confidence_level", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_active_run_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_lens_version_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_lens_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_employee_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_project_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_company_id", table_name="photo_lens_observations")
    op.drop_index("ix_photo_lens_observations_photo_id", table_name="photo_lens_observations")
    op.drop_table("photo_lens_observations")

    op.drop_index("ix_photo_lens_runs_batch", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_runs_model_prompt", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_runs_photo_lens_created", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_created_at", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_ai_analysis_log_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_lens_version_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_lens_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_employee_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_project_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_company_id", table_name="photo_lens_observation_runs")
    op.drop_index("ix_photo_lens_observation_runs_photo_id", table_name="photo_lens_observation_runs")
    op.drop_table("photo_lens_observation_runs")

    op.drop_index("ix_lens_definition_versions_status", table_name="lens_definition_versions")
    op.drop_index("ix_lens_definition_versions_lens_id", table_name="lens_definition_versions")
    op.drop_table("lens_definition_versions")

    op.drop_index("ix_lens_definitions_scope_enabled", table_name="lens_definitions")
    op.drop_index("ix_lens_definitions_current_version_id", table_name="lens_definitions")
    op.drop_index("ix_lens_definitions_project_id", table_name="lens_definitions")
    op.drop_index("ix_lens_definitions_company_id", table_name="lens_definitions")
    op.drop_index("ix_lens_definitions_lens_key", table_name="lens_definitions")
    op.drop_table("lens_definitions")
