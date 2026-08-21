"""evidence copilot

Revision ID: 20260405_0016
Revises: 20260404_0015
Create Date: 2026-04-05 11:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260405_0016"
down_revision = "20260404_0015"
branch_labels = None
depends_on = None


copilot_message_status = sa.Enum(
    "pending",
    "processing",
    "completed",
    "failed",
    name="copilot_message_status",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "evidence_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.project_id"), nullable=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("media_asset_id", sa.String(length=36), sa.ForeignKey("media_assets.asset_id"), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("observation_type", sa.String(length=64), nullable=False),
        sa.Column("normalized_key", sa.String(length=160), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_observations_tenant_id", "evidence_observations", ["tenant_id"], unique=False)
    op.create_index("ix_evidence_observations_company_id", "evidence_observations", ["company_id"], unique=False)
    op.create_index("ix_evidence_observations_project_id", "evidence_observations", ["project_id"], unique=False)
    op.create_index("ix_evidence_observations_photo_id", "evidence_observations", ["photo_id"], unique=False)
    op.create_index("ix_evidence_observations_media_asset_id", "evidence_observations", ["media_asset_id"], unique=False)
    op.create_index("ix_evidence_observations_source_kind", "evidence_observations", ["source_kind"], unique=False)
    op.create_index("ix_evidence_observations_observation_type", "evidence_observations", ["observation_type"], unique=False)
    op.create_index("ix_evidence_observations_normalized_key", "evidence_observations", ["normalized_key"], unique=False)
    op.create_index("ix_evidence_observations_severity", "evidence_observations", ["severity"], unique=False)
    op.create_index(
        "ix_evidence_observations_company_project_type",
        "evidence_observations",
        ["company_id", "project_id", "observation_type"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_observations_photo_type",
        "evidence_observations",
        ["photo_id", "observation_type"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_observations_project_observed_at",
        "evidence_observations",
        ["project_id", "observed_at"],
        unique=False,
    )

    op.create_table(
        "receipt_facts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.project_id"), nullable=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("vendor_name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("total_amount", sa.Float(), nullable=True),
        sa.Column("gallons", sa.Float(), nullable=True),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("fuel_type", sa.String(length=120), nullable=True),
        sa.Column("purchaser_name", sa.String(length=160), nullable=True),
        sa.Column("employee_id", sa.String(length=64), sa.ForeignKey("employees.employee_id"), nullable=True),
        sa.Column("receipt_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("currency_code", sa.String(length=16), nullable=True),
        sa.Column("has_pump_photo", sa.Boolean(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("facts_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("photo_id"),
    )
    op.create_index("ix_receipt_facts_tenant_id", "receipt_facts", ["tenant_id"], unique=False)
    op.create_index("ix_receipt_facts_company_id", "receipt_facts", ["company_id"], unique=False)
    op.create_index("ix_receipt_facts_project_id", "receipt_facts", ["project_id"], unique=False)
    op.create_index("ix_receipt_facts_photo_id", "receipt_facts", ["photo_id"], unique=False)
    op.create_index("ix_receipt_facts_employee_id", "receipt_facts", ["employee_id"], unique=False)

    op.create_table(
        "copilot_conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.project_id"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("preferred_backend_id", sa.String(length=64), nullable=True),
        sa.Column("preferred_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_copilot_conversations_tenant_id", "copilot_conversations", ["tenant_id"], unique=False)
    op.create_index("ix_copilot_conversations_company_id", "copilot_conversations", ["company_id"], unique=False)
    op.create_index("ix_copilot_conversations_project_id", "copilot_conversations", ["project_id"], unique=False)
    op.create_index(
        "ix_copilot_conversations_created_by_user_id",
        "copilot_conversations",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "copilot_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("copilot_conversations.id"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", copilot_message_status, nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("selected_backend_id", sa.String(length=64), nullable=True),
        sa.Column("selected_backend_type", sa.String(length=32), nullable=True),
        sa.Column("selected_model", sa.String(length=255), nullable=True),
        sa.Column("context_summary_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_copilot_messages_conversation_id", "copilot_messages", ["conversation_id"], unique=False)
    op.create_index("ix_copilot_messages_role", "copilot_messages", ["role"], unique=False)
    op.create_index("ix_copilot_messages_status", "copilot_messages", ["status"], unique=False)
    op.create_index(
        "ix_copilot_messages_conversation_created_at",
        "copilot_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "copilot_message_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=36), sa.ForeignKey("copilot_messages.id"), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_title", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_copilot_message_sources_message_id", "copilot_message_sources", ["message_id"], unique=False)
    op.create_index("ix_copilot_message_sources_source_type", "copilot_message_sources", ["source_type"], unique=False)
    op.create_index("ix_copilot_message_sources_source_id", "copilot_message_sources", ["source_id"], unique=False)
    op.create_index(
        "ix_copilot_message_sources_message_type",
        "copilot_message_sources",
        ["message_id", "source_type"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrades are disabled for production safety. Use forward Alembic upgrades only.")
