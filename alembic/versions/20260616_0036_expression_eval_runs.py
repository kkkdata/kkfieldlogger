"""expression eval runs

Revision ID: 20260616_0036
Revises: 20260616_0035
Create Date: 2026-06-16 11:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260616_0036"
down_revision = "20260616_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expression_eval_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), nullable=True),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_source", sa.String(length=64), nullable=True),
        sa.Column("artifact_type", sa.String(length=80), nullable=True),
        sa.Column("audience_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=128), nullable=True),
        sa.Column("contract_id", sa.String(length=128), nullable=True),
        sa.Column("runner_version", sa.String(length=32), nullable=False),
        sa.Column("input_manifest_json", sa.JSON(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["audience_id"], ["expression_audiences.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.company_id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["expression_output_contracts.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["expression_prompt_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expression_eval_runs_artifact_audience", "expression_eval_runs", ["artifact_type", "audience_id"])
    op.create_index("ix_expression_eval_runs_audience_id", "expression_eval_runs", ["audience_id"])
    op.create_index("ix_expression_eval_runs_company_id", "expression_eval_runs", ["company_id"])
    op.create_index("ix_expression_eval_runs_contract_id", "expression_eval_runs", ["contract_id"])
    op.create_index("ix_expression_eval_runs_created", "expression_eval_runs", ["created_at"])
    op.create_index("ix_expression_eval_runs_prompt_version_id", "expression_eval_runs", ["prompt_version_id"])
    op.create_index("ix_expression_eval_runs_run_type", "expression_eval_runs", ["run_type"])
    op.create_index("ix_expression_eval_runs_status", "expression_eval_runs", ["status"])
    op.create_index("ix_expression_eval_runs_tenant_id", "expression_eval_runs", ["tenant_id"])
    op.create_index("ix_expression_eval_runs_trigger_source", "expression_eval_runs", ["trigger_source"])
    op.create_index("ix_expression_eval_runs_type_status", "expression_eval_runs", ["run_type", "status"])

    op.create_table(
        "expression_eval_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("eval_run_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("fact_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("case_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("validator_name", sa.String(length=96), nullable=True),
        sa.Column("expected_json", sa.JSON(), nullable=True),
        sa.Column("actual_json", sa.JSON(), nullable=True),
        sa.Column("validation_errors_json", sa.JSON(), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["expression_artifacts.id"]),
        sa.ForeignKeyConstraint(["eval_run_id"], ["expression_eval_runs.id"]),
        sa.ForeignKeyConstraint(["fact_snapshot_id"], ["fact_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expression_eval_items_artifact_id", "expression_eval_items", ["artifact_id"])
    op.create_index("ix_expression_eval_items_case", "expression_eval_items", ["case_key"])
    op.create_index("ix_expression_eval_items_created_at", "expression_eval_items", ["created_at"])
    op.create_index("ix_expression_eval_items_eval_run_id", "expression_eval_items", ["eval_run_id"])
    op.create_index("ix_expression_eval_items_fact_snapshot_id", "expression_eval_items", ["fact_snapshot_id"])
    op.create_index("ix_expression_eval_items_run_status", "expression_eval_items", ["eval_run_id", "status"])
    op.create_index("ix_expression_eval_items_status", "expression_eval_items", ["status"])
    op.create_index("ix_expression_eval_items_validator_name", "expression_eval_items", ["validator_name"])


def downgrade() -> None:
    op.drop_index("ix_expression_eval_items_validator_name", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_status", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_run_status", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_fact_snapshot_id", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_eval_run_id", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_created_at", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_case", table_name="expression_eval_items")
    op.drop_index("ix_expression_eval_items_artifact_id", table_name="expression_eval_items")
    op.drop_table("expression_eval_items")

    op.drop_index("ix_expression_eval_runs_type_status", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_trigger_source", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_tenant_id", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_status", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_run_type", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_prompt_version_id", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_created", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_contract_id", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_company_id", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_audience_id", table_name="expression_eval_runs")
    op.drop_index("ix_expression_eval_runs_artifact_audience", table_name="expression_eval_runs")
    op.drop_table("expression_eval_runs")
