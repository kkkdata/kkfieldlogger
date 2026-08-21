"""Add management review workflow tables.

Revision ID: 20260708_0043
Revises: 20260617_0042
Create Date: 2026-07-08 18:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260708_0043"
down_revision = "20260617_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("related_photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("task_type", sa.Enum("retake_photo", "clarify_photo", "correction_followup", "internal_note", name="review_task_type", native_enum=False), nullable=False),
        sa.Column("status", sa.Enum("open", "acknowledged", "completed", "cancelled", name="review_task_status", native_enum=False), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_employee_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_photo_id", sa.Integer(), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by_employee_id", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_employee_id", sa.String(length=64), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_tasks_public_id", "review_tasks", ["public_id"], unique=True)
    op.create_index("ix_review_tasks_tenant_id", "review_tasks", ["tenant_id"])
    op.create_index("ix_review_tasks_company_id", "review_tasks", ["company_id"])
    op.create_index("ix_review_tasks_project_id", "review_tasks", ["project_id"])
    op.create_index("ix_review_tasks_related_photo_id", "review_tasks", ["related_photo_id"])
    op.create_index("ix_review_tasks_task_type", "review_tasks", ["task_type"])
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])
    op.create_index("ix_review_tasks_created_by_user_id", "review_tasks", ["created_by_user_id"])
    op.create_index("ix_review_tasks_assigned_employee_id", "review_tasks", ["assigned_employee_id"])
    op.create_index("ix_review_tasks_completion_photo_id", "review_tasks", ["completion_photo_id"])
    op.create_index("ix_review_tasks_acknowledged_by_employee_id", "review_tasks", ["acknowledged_by_employee_id"])
    op.create_index("ix_review_tasks_completed_by_employee_id", "review_tasks", ["completed_by_employee_id"])
    op.create_index("ix_review_tasks_cancelled_by_user_id", "review_tasks", ["cancelled_by_user_id"])
    op.create_index("ix_review_tasks_created_at", "review_tasks", ["created_at"])
    op.create_index("ix_review_tasks_project_status", "review_tasks", ["company_id", "project_id", "status"])
    op.create_index("ix_review_tasks_employee_status", "review_tasks", ["company_id", "assigned_employee_id", "status"])
    op.create_index("ix_review_tasks_related_photo", "review_tasks", ["related_photo_id"])
    op.create_index("ix_review_tasks_completion_photo", "review_tasks", ["completion_photo_id"])

    op.create_table(
        "review_task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("task_public_id", sa.String(length=36), sa.ForeignKey("review_tasks.public_id"), nullable=False),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.Enum("created", "commented", "acknowledged", "completed", "cancelled", "reopened", name="review_task_event_type", native_enum=False), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_employee_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_task_events_public_id", "review_task_events", ["public_id"], unique=True)
    op.create_index("ix_review_task_events_task_public_id", "review_task_events", ["task_public_id"])
    op.create_index("ix_review_task_events_company_id", "review_task_events", ["company_id"])
    op.create_index("ix_review_task_events_project_id", "review_task_events", ["project_id"])
    op.create_index("ix_review_task_events_event_type", "review_task_events", ["event_type"])
    op.create_index("ix_review_task_events_actor_user_id", "review_task_events", ["actor_user_id"])
    op.create_index("ix_review_task_events_actor_employee_id", "review_task_events", ["actor_employee_id"])
    op.create_index("ix_review_task_events_created_at", "review_task_events", ["created_at"])
    op.create_index("ix_review_task_events_task_created", "review_task_events", ["task_public_id", "created_at"])
    op.create_index("ix_review_task_events_project_created", "review_task_events", ["company_id", "project_id", "created_at"])

    op.create_table(
        "review_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("companies.company_id"), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("review_date", sa.String(length=10), nullable=False),
        sa.Column("status", sa.Enum("open", "closed", "reopened", name="review_session_status", native_enum=False), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("closed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_review_sessions_public_id", "review_sessions", ["public_id"], unique=True)
    op.create_index("ix_review_sessions_tenant_id", "review_sessions", ["tenant_id"])
    op.create_index("ix_review_sessions_company_id", "review_sessions", ["company_id"])
    op.create_index("ix_review_sessions_project_id", "review_sessions", ["project_id"])
    op.create_index("ix_review_sessions_review_date", "review_sessions", ["review_date"])
    op.create_index("ix_review_sessions_status", "review_sessions", ["status"])
    op.create_index("ix_review_sessions_created_by_user_id", "review_sessions", ["created_by_user_id"])
    op.create_index("ix_review_sessions_closed_by_user_id", "review_sessions", ["closed_by_user_id"])
    op.create_index("ix_review_sessions_created_at", "review_sessions", ["created_at"])
    op.create_index("ix_review_sessions_project_date", "review_sessions", ["company_id", "project_id", "review_date"])
    op.create_index("ix_review_sessions_project_status", "review_sessions", ["company_id", "project_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_review_sessions_project_status", table_name="review_sessions")
    op.drop_index("ix_review_sessions_project_date", table_name="review_sessions")
    op.drop_index("ix_review_sessions_created_at", table_name="review_sessions")
    op.drop_index("ix_review_sessions_closed_by_user_id", table_name="review_sessions")
    op.drop_index("ix_review_sessions_created_by_user_id", table_name="review_sessions")
    op.drop_index("ix_review_sessions_status", table_name="review_sessions")
    op.drop_index("ix_review_sessions_review_date", table_name="review_sessions")
    op.drop_index("ix_review_sessions_project_id", table_name="review_sessions")
    op.drop_index("ix_review_sessions_company_id", table_name="review_sessions")
    op.drop_index("ix_review_sessions_tenant_id", table_name="review_sessions")
    op.drop_index("ix_review_sessions_public_id", table_name="review_sessions")
    op.drop_table("review_sessions")

    op.drop_index("ix_review_task_events_project_created", table_name="review_task_events")
    op.drop_index("ix_review_task_events_task_created", table_name="review_task_events")
    op.drop_index("ix_review_task_events_created_at", table_name="review_task_events")
    op.drop_index("ix_review_task_events_actor_employee_id", table_name="review_task_events")
    op.drop_index("ix_review_task_events_actor_user_id", table_name="review_task_events")
    op.drop_index("ix_review_task_events_event_type", table_name="review_task_events")
    op.drop_index("ix_review_task_events_project_id", table_name="review_task_events")
    op.drop_index("ix_review_task_events_company_id", table_name="review_task_events")
    op.drop_index("ix_review_task_events_task_public_id", table_name="review_task_events")
    op.drop_index("ix_review_task_events_public_id", table_name="review_task_events")
    op.drop_table("review_task_events")

    op.drop_index("ix_review_tasks_completion_photo", table_name="review_tasks")
    op.drop_index("ix_review_tasks_related_photo", table_name="review_tasks")
    op.drop_index("ix_review_tasks_employee_status", table_name="review_tasks")
    op.drop_index("ix_review_tasks_project_status", table_name="review_tasks")
    op.drop_index("ix_review_tasks_created_at", table_name="review_tasks")
    op.drop_index("ix_review_tasks_cancelled_by_user_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_completed_by_employee_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_acknowledged_by_employee_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_completion_photo_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_assigned_employee_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_created_by_user_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_status", table_name="review_tasks")
    op.drop_index("ix_review_tasks_task_type", table_name="review_tasks")
    op.drop_index("ix_review_tasks_related_photo_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_project_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_company_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_tenant_id", table_name="review_tasks")
    op.drop_index("ix_review_tasks_public_id", table_name="review_tasks")
    op.drop_table("review_tasks")
