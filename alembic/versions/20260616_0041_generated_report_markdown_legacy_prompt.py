"""generated report markdown legacy prompt registry shadow

Revision ID: 20260616_0041
Revises: 20260616_0040
Create Date: 2026-06-16 19:26:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0041"
down_revision = "20260616_0040"
branch_labels = None
depends_on = None


TEMPLATE_ID = "generated_report_markdown_legacy"
PROMPT_VERSION_ID = "generated_report_markdown_legacy:v1"
BINDING_ID = "global:generated_report_markdown_legacy:v1"


USER_PROMPT_TEMPLATE = """
You are a construction reporting assistant.
Generate a professional Markdown report only.
Do not return JSON, code fences, or commentary outside the Markdown report.
The report must include these sections when supported by the evidence:
# Title
## Executive Summary
## Key Findings
## Defects and Risks
## Recommended Actions
## Photo Evidence
Rules:
- Use only the evidence provided in the photo dataset.
- Cite photo IDs in the Photo Evidence section.
- Keep the tone factual and professional.
- If evidence is missing, say that clearly instead of inventing details.
User instruction:
{custom_prompt}
Photo dataset (JSON):
{photo_dataset_json}
""".strip()


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
    op.bulk_insert(
        templates,
        [
            {
                "id": TEMPLATE_ID,
                "slug": TEMPLATE_ID,
                "title": "Generated report markdown legacy prompt",
                "artifact_type": "generated_report_markdown_legacy",
                "stage": "render_legacy_pdf",
                "default_audience_id": "project_manager",
                "description": "Shadow registry copy of the legacy generated_reports Markdown PDF prompt.",
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
                "system_prompt": "Legacy generated report Markdown prompt registry shadow.",
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": (
                    "Shadow-only parity seed. Runtime generated report PDFs still use "
                    "app.services.reports.build_report_markdown_prompt."
                ),
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


def downgrade() -> None:
    op.execute("DELETE FROM expression_prompt_bindings WHERE id = 'global:generated_report_markdown_legacy:v1'")
    op.execute("DELETE FROM expression_prompt_versions WHERE id = 'generated_report_markdown_legacy:v1'")
    op.execute("DELETE FROM expression_prompt_templates WHERE id = 'generated_report_markdown_legacy'")
