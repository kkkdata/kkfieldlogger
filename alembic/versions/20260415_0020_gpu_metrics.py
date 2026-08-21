"""gpu metrics monitor

Revision ID: 20260415_0020
Revises: 20260405_0019
Create Date: 2026-04-15 02:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260415_0020"
down_revision = "20260405_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpu_metric_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hostname", sa.String(length=120), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("gpu_name", sa.String(length=160), nullable=True),
        sa.Column("utilization_gpu_percent", sa.Float(), nullable=True),
        sa.Column("utilization_memory_percent", sa.Float(), nullable=True),
        sa.Column("memory_total_mb", sa.Integer(), nullable=True),
        sa.Column("memory_used_mb", sa.Integer(), nullable=True),
        sa.Column("memory_free_mb", sa.Integer(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("power_draw_w", sa.Float(), nullable=True),
        sa.Column("power_limit_w", sa.Float(), nullable=True),
        sa.Column("fan_speed_percent", sa.Float(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gpu_metric_samples_sampled_at", "gpu_metric_samples", ["sampled_at"], unique=False)
    op.create_index("ix_gpu_metric_samples_hostname", "gpu_metric_samples", ["hostname"], unique=False)
    op.create_index("ix_gpu_metric_samples_gpu_index", "gpu_metric_samples", ["gpu_index"], unique=False)
    op.create_index(
        "ix_gpu_metric_samples_host_gpu_time",
        "gpu_metric_samples",
        ["hostname", "gpu_index", "sampled_at"],
        unique=False,
    )

    op.create_table(
        "gpu_metric_hourly_rollups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hostname", sa.String(length=120), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("gpu_name", sa.String(length=160), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_gpu_utilization_percent", sa.Float(), nullable=True),
        sa.Column("max_gpu_utilization_percent", sa.Float(), nullable=True),
        sa.Column("avg_memory_utilization_percent", sa.Float(), nullable=True),
        sa.Column("max_memory_utilization_percent", sa.Float(), nullable=True),
        sa.Column("avg_memory_used_mb", sa.Float(), nullable=True),
        sa.Column("max_memory_used_mb", sa.Integer(), nullable=True),
        sa.Column("avg_power_draw_w", sa.Float(), nullable=True),
        sa.Column("max_power_draw_w", sa.Float(), nullable=True),
        sa.Column("max_temperature_c", sa.Float(), nullable=True),
        sa.Column("saturated_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory_pressure_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("hostname", "gpu_index", "bucket_start", name="uq_gpu_metric_hourly_host_gpu_bucket"),
    )
    op.create_index("ix_gpu_metric_hourly_bucket_start", "gpu_metric_hourly_rollups", ["bucket_start"], unique=False)
    op.create_index("ix_gpu_metric_hourly_rollups_hostname", "gpu_metric_hourly_rollups", ["hostname"], unique=False)
    op.create_index("ix_gpu_metric_hourly_rollups_gpu_index", "gpu_metric_hourly_rollups", ["gpu_index"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_gpu_metric_hourly_rollups_gpu_index", table_name="gpu_metric_hourly_rollups")
    op.drop_index("ix_gpu_metric_hourly_rollups_hostname", table_name="gpu_metric_hourly_rollups")
    op.drop_index("ix_gpu_metric_hourly_bucket_start", table_name="gpu_metric_hourly_rollups")
    op.drop_table("gpu_metric_hourly_rollups")
    op.drop_index("ix_gpu_metric_samples_host_gpu_time", table_name="gpu_metric_samples")
    op.drop_index("ix_gpu_metric_samples_gpu_index", table_name="gpu_metric_samples")
    op.drop_index("ix_gpu_metric_samples_hostname", table_name="gpu_metric_samples")
    op.drop_index("ix_gpu_metric_samples_sampled_at", table_name="gpu_metric_samples")
    op.drop_table("gpu_metric_samples")
