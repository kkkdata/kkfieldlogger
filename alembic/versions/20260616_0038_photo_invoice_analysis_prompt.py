"""photo invoice analysis prompt registry shadow

Revision ID: 20260616_0038
Revises: 20260616_0037
Create Date: 2026-06-16 18:40:00
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260616_0038"
down_revision = "20260616_0037"
branch_labels = None
depends_on = None


TEMPLATE_ID = "photo_invoice_analysis"
PROMPT_VERSION_ID = "photo_invoice_analysis:v1"
BINDING_ID = "global:photo_invoice_analysis:v1"


USER_PROMPT_TEMPLATE = """
You are an expense-evidence analyst reviewing a receipt or invoice photo for project audit traceability.
Analyze the attached image and return only valid JSON.
Do not return markdown, code fences, explanations, or any text outside the JSON object.
Required JSON schema:
{schema_block}
Instructions:
- Start with what is clearly visible and financially useful.
- Summarize the document type and the most important readable details that are actually visible.
- If readable, prefer vendor, total amount, date, gallons or units, line items, and payment clues.
- Extract receipt_facts only from visible text; use an empty string for any field that is not readable.
- Use financial_anomaly_flags for visible audit concerns, such as missing totals, unreadable vendor, suspicious cropping, duplicate-looking receipts, missing pump/vehicle evidence for fuel, or mismatch between visible goods and the claimed category.
- Use missing_evidence for evidence that a finance reviewer would reasonably request but that is not visible in this image.
- If text is blurry or cropped, say that it is unreadable instead of inventing values.
- Use labels for document category and notable readable fields, such as receipt, invoice, fuel, vendor_visible, total_visible, date_visible, blurry_document, or cropped_document.
- defects should contain only visually supported audit-usefulness issues, such as vendor not readable, total amount not readable, blurry document, cropped receipt, or no supporting equipment or pump visible when relevant.
- If there is no clear issue visible, return [] for defects.
- Keep the summary factual, finance-useful, and grounded in what can be read.
Format reminder: return the full JSON schema for the actual attached receipt or invoice.
Do not copy placeholder values. Use empty strings for unreadable fields and explain the audit limitation.
- Use the note only as context; do not invent details that are not visually supported.
- If confidence_level is low, explain why in evidence_limitations.
- Return every schema field even when the value is an empty string or empty array.
- Use at most 6 concise, non-duplicate strings in each array field.
- Do not create extra JSON keys. Do not repeat the same noun to fill arrays.
- The first top-level key must be ai_summary. Do not rename ai_summary to scene_description, caption, summary, or description.
- photo_type: {photo_type}
- project_id: {project_id}
- note: {note}{optional_sections}
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
                "title": "Photo invoice analysis prompt",
                "artifact_type": TEMPLATE_ID,
                "stage": "vision_finance",
                "default_audience_id": None,
                "description": "Shadow registry copy of the legacy receipt and invoice photo prompt.",
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
                "system_prompt": "Legacy receipt and invoice photo analysis prompt registry shadow.",
                "user_prompt_template": USER_PROMPT_TEMPLATE,
                "few_shot_json": None,
                "json_schema_override": None,
                "status": "active",
                "created_by_user_id": None,
                "notes": (
                    "Shadow-only parity seed. Runtime invoice AI still uses "
                    "app.services.ai_pipeline.build_ai_prompt_for_context."
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
    op.execute("DELETE FROM expression_prompt_bindings WHERE id = 'global:photo_invoice_analysis:v1'")
    op.execute("DELETE FROM expression_prompt_versions WHERE id = 'photo_invoice_analysis:v1'")
    op.execute("DELETE FROM expression_prompt_templates WHERE id = 'photo_invoice_analysis'")
