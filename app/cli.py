from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import load_settings
from app.core.security import hash_password
from app.core.time import to_utc_iso, utc_now
from app.db.session import create_engine_from_settings, create_session_maker
from app.models import User, UserRole
from app.models import (
    ApprovalStatus,
    AIAnalysisLog,
    AIAnalysisStatus,
    Company,
    Employee,
    ExpressionArtifact,
    ExpressionAudience,
    ExpressionEvalItem,
    ExpressionEvalRun,
    ExpressionForbiddenPhrase,
    ExpressionOutputContract,
    ExpressionPromptBinding,
    ExpressionPromptTemplate,
    ExpressionPromptVersion,
    FactSnapshot,
    LensDefinition,
    MediaAsset,
    Photo,
    PhotoLensObservation,
    PhotoLensObservationRun,
    PhotoType,
    PhotoVisibility,
    ProgressReport,
    ProgressReportStatus,
    Project,
    ReceiptFact,
)
from app.services.bootstrap import bootstrap_platform_data
from app.services.expression import (
    CLIENT_PROGRESS_DENIED_FACT_KEYS,
    CLIENT_PROGRESS_DISCLAIMER,
    CLIENT_PROGRESS_SUMMARY_ARTIFACT,
    EMPLOYEE_CONTRIBUTION_ARTIFACT,
    EMPLOYEE_CONTRIBUTION_SCOPE,
    EXECUTIVE_COMPANY_HEALTH_ARTIFACT,
    EXECUTIVE_DENIED_FACT_KEYS,
    FINANCE_DENIED_FACT_KEYS,
    FINANCE_SUMMARY_ARTIFACT,
    OPERATIONS_DENIED_FACT_KEYS,
    OPERATIONS_HEALTH_SUMMARY_ARTIFACT,
    _CLIENT_PROGRESS_DENIED_TEXT_RE,
    _FINANCE_SECRET_OR_PII_RE,
    _OPERATIONS_PATH_OR_SECRET_RE,
    _progress_report_translatable_strings,
    _validate_expression_payload,
    build_client_progress_summary_facts,
    build_executive_company_health_facts,
    build_progress_report_translation_facts,
    generate_employee_contribution_shadow,
    generate_client_progress_summary_shadow,
    generate_executive_company_health_shadow,
    generate_finance_summary_shadow,
    generate_operations_health_summary_shadow,
    generate_project_manager_decision_brief_shadow,
    generate_progress_report_translation_shadow,
    generate_progress_report_string_translation_shadow,
    generate_report_markdown_shadow,
    generate_report_markdown_section_shadow,
    GENERATED_REPORT_MARKDOWN_SECTION_KEYS,
    promote_expression_artifact,
    PROMOTABLE_EXPRESSION_STATUSES,
    summarize_project_manager_decision_brief_errors,
)
from app.services.expression_golden_replay import (
    _locked_baseline_for_replay,
    _resolve_replay_contract,
    run_expression_golden_replay,
)
from app.services.expression_roadmap import (
    EXPRESSION_ARTIFACT_IMPLEMENTATION,
    audit_expression_target_roadmap_exit_code,
    build_expression_target_roadmap_gap_audit,
)
from app.services.ai_pipeline import (
    _normalized_annotation_contexts,
    _project_prompt_text,
    build_ai_prompt_for_context,
    build_media_asset_ai_prompt,
    normalize_operator_prompt,
)
from app.services.photo_lenses import (
    backfill_photo_lenses,
    enabled_core_lenses,
    run_photo_lens_shadow,
    seed_core_lenses,
)
from app.services.project_manager_lens_report import (
    build_project_manager_lens_report,
    render_project_manager_lens_report_markdown,
)
from app.services.reports import (
    _format_angle_metadata,
    _format_optional_number,
    _normalize_progress_custom_prompt,
    _progress_prompt_text,
    _serialize_photo_for_report,
    build_multi_image_progress_prompt,
    build_report_markdown_prompt,
)
from app.services.settings import bootstrap_system_settings


EXECUTIVE_HEALTH_AUDIT_DENIED_RE = re.compile(
    r"(https?://|/opt/|/app/|/tmp/|[A-Za-z]:\\|file_path|storage_path|image_url|thumb_url|employee_id|uploaded_by_user_id|created_by_user_id|api_key|secret|password|token|prompt_used|raw_model_output|\bshould\b|\bmust\b|\bpriority\b|\brecommend\b|\bensure\b|\burgent\b|\brank(?:ing)?\b|\bperformance score\b|员工号|排名|绩效评分|责任归因)",
    flags=re.IGNORECASE,
)


def _has_receipt_facts_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    receipt_facts = value.get("receipt_facts")
    if isinstance(receipt_facts, dict):
        return any(str(item or "").strip() for item in receipt_facts.values())
    return False


def audit_finance_fact_health_payload(
    db,
    *,
    company_id: str | None,
    window_days: int,
    limit: int,
) -> dict[str, object]:
    now_value = utc_now()
    current_start = now_value - timedelta(days=max(1, min(int(window_days), 365)))
    limit = max(1, min(int(limit), 200))
    invoice_filters = [
        Photo.photo_type == PhotoType.invoice,
        Photo.deleted.is_(False),
        Photo.captured_at_utc >= current_start,
        Photo.captured_at_utc <= now_value,
    ]
    receipt_observed_at = func.coalesce(ReceiptFact.receipt_timestamp, ReceiptFact.created_at)
    receipt_filters = [
        receipt_observed_at >= current_start,
        receipt_observed_at <= now_value,
    ]
    if company_id:
        invoice_filters.append(Photo.company_id == company_id)
        receipt_filters.append(ReceiptFact.company_id == company_id)

    invoice_count = int(db.scalar(select(func.count(Photo.id)).where(*invoice_filters)) or 0)
    invoice_with_project_count = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                *invoice_filters,
                Photo.project_id.is_not(None),
                Photo.project_id != "",
                Photo.project_id != "invoice",
            )
        )
        or 0
    )
    receipt_count = int(db.scalar(select(func.count(ReceiptFact.id)).where(*receipt_filters)) or 0)
    receipt_with_project_count = int(
        db.scalar(
            select(func.count(ReceiptFact.id)).where(
                *receipt_filters,
                ReceiptFact.project_id.is_not(None),
                ReceiptFact.project_id != "",
            )
        )
        or 0
    )

    invoice_rows = db.execute(select(Photo.id).where(*invoice_filters)).all()
    invoice_photo_ids = [int(row[0]) for row in invoice_rows]
    active_ai_log_count = 0
    active_ai_receipt_fact_photo_ids: set[int] = set()
    if invoice_photo_ids:
        for row in db.execute(
            select(AIAnalysisLog.photo_id, AIAnalysisLog.result_data)
            .where(
                AIAnalysisLog.photo_id.in_(invoice_photo_ids),
                AIAnalysisLog.status == AIAnalysisStatus.active,
            )
            .order_by(AIAnalysisLog.created_at.desc())
        ):
            photo_id = int(row[0])
            active_ai_log_count += 1
            if _has_receipt_facts_payload(row[1]):
                active_ai_receipt_fact_photo_ids.add(photo_id)

    receipt_fact_photo_ids = {
        int(row[0])
        for row in db.execute(
            select(ReceiptFact.photo_id)
            .join(Photo, Photo.id == ReceiptFact.photo_id)
            .where(*invoice_filters)
        ).all()
    }
    invoice_with_receipt_fact_count = len(receipt_fact_photo_ids)

    project_receipt_counts = (
        select(
            ReceiptFact.company_id.label("company_id"),
            ReceiptFact.project_id.label("project_id"),
            func.count(ReceiptFact.id).label("receipt_fact_count"),
        )
        .where(
            *receipt_filters,
            ReceiptFact.project_id.is_not(None),
            ReceiptFact.project_id != "",
        )
        .group_by(ReceiptFact.company_id, ReceiptFact.project_id)
        .subquery()
    )
    project_rows = db.execute(
        select(
            project_receipt_counts.c.company_id,
            project_receipt_counts.c.project_id,
            project_receipt_counts.c.receipt_fact_count,
        )
        .order_by(project_receipt_counts.c.receipt_fact_count.desc(), project_receipt_counts.c.project_id.asc())
        .limit(limit)
    ).all()
    projects = [
        {
            "company_id": row[0],
            "project_id": row[1],
            "receipt_fact_count": int(row[2] or 0),
            "fact_refs": [
                {
                    "source_table": "receipt_facts",
                    "path": "receipt_facts.count_by_company_project",
                    "observed_value": int(row[2] or 0),
                }
            ],
        }
        for row in project_rows
    ]

    missing_receipt_fact_count = max(0, invoice_count - invoice_with_receipt_fact_count)
    ai_receipt_fact_photo_count = len(active_ai_receipt_fact_photo_ids)
    ai_receipt_not_materialized_count = len(active_ai_receipt_fact_photo_ids - receipt_fact_photo_ids)
    if invoice_count == 0:
        status = "no_invoice_photos"
    elif receipt_count == 0:
        status = "no_receipt_facts"
    elif receipt_with_project_count == 0:
        status = "receipt_facts_without_project_scope"
    elif missing_receipt_fact_count > 0 or ai_receipt_not_materialized_count > 0:
        status = "partial_receipt_fact_coverage"
    else:
        status = "ready"

    totals = {
        "invoice_photo_count": invoice_count,
        "invoice_photos_with_project_id": invoice_with_project_count,
        "invoice_photos_missing_project_id": max(0, invoice_count - invoice_with_project_count),
        "active_ai_log_count": active_ai_log_count,
        "active_ai_logs_with_receipt_facts_photo_count": ai_receipt_fact_photo_count,
        "receipt_fact_count": receipt_count,
        "receipt_facts_with_project_id": receipt_with_project_count,
        "invoice_photos_with_receipt_fact": invoice_with_receipt_fact_count,
        "invoice_photos_missing_receipt_fact": missing_receipt_fact_count,
        "ai_receipt_facts_not_materialized": ai_receipt_not_materialized_count,
        "projects_with_receipt_facts": len(projects),
    }
    return {
        "status": status,
        "scope": {
            "company_id": company_id,
            "window_days": max(1, min(int(window_days), 365)),
            "current_start_utc": current_start.isoformat(),
            "current_end_utc": now_value.isoformat(),
        },
        "source_tables": ["photos", "ai_analysis_logs", "receipt_facts", "projects"],
        "totals": totals,
        "projects": projects,
        "fact_refs": [
            {"source_table": "photos", "path": "photos.invoice.count", "observed_value": invoice_count},
            {"source_table": "receipt_facts", "path": "receipt_facts.count", "observed_value": receipt_count},
            {
                "source_table": "ai_analysis_logs",
                "path": "ai_analysis_logs.active_receipt_facts.photo_count",
                "observed_value": ai_receipt_fact_photo_count,
            },
        ],
    }


def _valid_project_id(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text == "invoice":
        return None
    return text


def _project_exists(db, *, company_id: str, project_id: str | None) -> bool:
    if not project_id:
        return False
    return bool(
        db.scalar(
            select(Project.project_id).where(
                Project.company_id == company_id,
                Project.project_id == project_id,
            )
        )
    )


def audit_finance_fact_attribution_payload(
    db,
    *,
    company_id: str | None,
    window_days: int,
    limit: int,
) -> dict[str, object]:
    now_value = utc_now()
    current_start = now_value - timedelta(days=max(1, min(int(window_days), 365)))
    limit = max(1, min(int(limit), 200))
    receipt_observed_at = func.coalesce(ReceiptFact.receipt_timestamp, ReceiptFact.created_at)
    filters = [
        receipt_observed_at >= current_start,
        receipt_observed_at <= now_value,
    ]
    if company_id:
        filters.append(ReceiptFact.company_id == company_id)
    rows = db.execute(
        select(
            ReceiptFact.id,
            ReceiptFact.company_id,
            ReceiptFact.project_id,
            ReceiptFact.photo_id,
            ReceiptFact.employee_id,
            Photo.project_id,
            Employee.project_id,
        )
        .join(Photo, Photo.id == ReceiptFact.photo_id)
        .outerjoin(
            Employee,
            (Employee.company_id == ReceiptFact.company_id) & (Employee.employee_id == ReceiptFact.employee_id),
        )
        .where(*filters)
        .order_by(receipt_observed_at.desc(), ReceiptFact.id.asc())
        .limit(limit)
    ).all()

    totals = {
        "receipt_facts_scanned": 0,
        "already_attributed": 0,
        "suggested": 0,
        "ambiguous": 0,
        "no_candidate": 0,
        "photo_project_candidates": 0,
        "employee_current_project_candidates": 0,
    }
    candidates: list[dict[str, object]] = []
    for row in rows:
        (
            receipt_id,
            row_company_id,
            receipt_project_id,
            photo_id,
            _receipt_employee_id,
            photo_project_id,
            employee_project_id,
        ) = row
        totals["receipt_facts_scanned"] += 1
        current_project_id = _valid_project_id(receipt_project_id)
        if current_project_id and _project_exists(db, company_id=row_company_id, project_id=current_project_id):
            totals["already_attributed"] += 1
            continue

        source_candidates: list[dict[str, str]] = []
        valid_photo_project_id = _valid_project_id(photo_project_id)
        if _project_exists(db, company_id=row_company_id, project_id=valid_photo_project_id):
            source_candidates.append({"rule": "photo_project_id", "project_id": valid_photo_project_id or ""})
            totals["photo_project_candidates"] += 1
        valid_employee_project_id = _valid_project_id(employee_project_id)
        if _project_exists(db, company_id=row_company_id, project_id=valid_employee_project_id):
            source_candidates.append({"rule": "employee_current_project", "project_id": valid_employee_project_id or ""})
            totals["employee_current_project_candidates"] += 1

        project_ids = {item["project_id"] for item in source_candidates if item.get("project_id")}
        if len(project_ids) == 1:
            candidate_project_id = next(iter(project_ids))
            rules = [item["rule"] for item in source_candidates]
            confidence = "high" if len(rules) >= 2 else "medium"
            totals["suggested"] += 1
            candidates.append(
                {
                    "receipt_id": receipt_id,
                    "photo_id": int(photo_id),
                    "company_id": row_company_id,
                    "candidate_project_id": candidate_project_id,
                    "confidence": confidence,
                    "rules": rules,
                    "dry_run": True,
                    "fact_refs": [
                        {
                            "source_table": "receipt_facts",
                            "path": "receipt_facts.project_id",
                            "observed_value": receipt_project_id,
                        },
                        {
                            "source_table": "photos",
                            "path": "photos.project_id",
                            "observed_value": photo_project_id,
                        },
                        {
                            "source_table": "employees",
                            "path": "employees.project_id",
                            "observed_value": "present" if valid_employee_project_id else None,
                        },
                    ],
                }
            )
        elif len(project_ids) > 1:
            totals["ambiguous"] += 1
            candidates.append(
                {
                    "receipt_id": receipt_id,
                    "photo_id": int(photo_id),
                    "company_id": row_company_id,
                    "candidate_project_id": None,
                    "confidence": "none",
                    "rules": [item["rule"] for item in source_candidates],
                    "dry_run": True,
                    "reason": "conflicting_project_candidates",
                }
            )
        else:
            totals["no_candidate"] += 1

    status = "ready_for_review" if totals["suggested"] else "no_safe_candidates"
    if totals["ambiguous"]:
        status = "needs_manual_review"
    return {
        "status": status,
        "mode": "dry_run",
        "scope": {
            "company_id": company_id,
            "window_days": max(1, min(int(window_days), 365)),
            "current_start_utc": current_start.isoformat(),
            "current_end_utc": now_value.isoformat(),
            "limit": limit,
        },
        "source_tables": ["receipt_facts", "photos", "employees", "projects"],
        "totals": totals,
        "candidates": candidates,
        "guardrails": {
            "writes_database": False,
            "uses_ai": False,
            "uses_filesystem_scan": False,
            "requires_project_exists": True,
            "employee_id_redacted": True,
        },
    }


def apply_finance_fact_attribution_payload(
    db,
    *,
    company_id: str | None,
    window_days: int,
    limit: int,
    rollback_file: str,
) -> dict[str, object]:
    dry_run = audit_finance_fact_attribution_payload(
        db,
        company_id=company_id,
        window_days=window_days,
        limit=limit,
    )
    candidates = dry_run.get("candidates") if isinstance(dry_run.get("candidates"), list) else []
    applied: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_project_id = _valid_project_id(candidate.get("candidate_project_id"))
        receipt_id = str(candidate.get("receipt_id") or "").strip()
        if not receipt_id or not candidate_project_id:
            skipped.append({"receipt_id": receipt_id or None, "reason": "missing_candidate_project"})
            continue
        receipt = db.get(ReceiptFact, receipt_id)
        if receipt is None:
            skipped.append({"receipt_id": receipt_id, "reason": "receipt_missing"})
            continue
        current_project_id = _valid_project_id(receipt.project_id)
        if current_project_id:
            skipped.append({"receipt_id": receipt_id, "reason": "already_attributed"})
            continue
        if not _project_exists(db, company_id=receipt.company_id, project_id=candidate_project_id):
            skipped.append({"receipt_id": receipt_id, "reason": "candidate_project_missing"})
            continue
        previous_project_id = receipt.project_id
        receipt.project_id = candidate_project_id
        applied.append(
            {
                "receipt_id": receipt.id,
                "company_id": receipt.company_id,
                "previous_project_id": previous_project_id,
                "applied_project_id": candidate_project_id,
                "rules": candidate.get("rules") if isinstance(candidate.get("rules"), list) else [],
                "confidence": candidate.get("confidence"),
            }
        )

    rollback_path = Path(rollback_file)
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_payload = {
        "operation": "finance_fact_attribution",
        "created_at": utc_now().isoformat(),
        "source": "expression-audit-finance-fact-attribution --apply",
        "entries": applied,
    }
    rollback_path.write_text(json.dumps(rollback_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "status": "applied" if applied and not skipped else ("partial" if applied else "no_changes"),
        "mode": "apply",
        "rollback_file": str(rollback_path),
        "source_status": dry_run.get("status"),
        "totals": {
            "candidates": len(candidates),
            "applied": len(applied),
            "skipped": len(skipped),
        },
        "applied": applied,
        "skipped": skipped,
        "guardrails": {
            "writes_database": bool(applied),
            "uses_ai": False,
            "uses_filesystem_scan": False,
            "updated_tables": ["receipt_facts"] if applied else [],
            "rollback_file_created": True,
        },
    }


def rollback_finance_fact_attribution_payload(db, *, rollback_file: str) -> dict[str, object]:
    rollback_path = Path(rollback_file)
    payload = json.loads(rollback_path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) and isinstance(payload.get("entries"), list) else []
    restored: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        receipt_id = str(entry.get("receipt_id") or "").strip()
        applied_project_id = entry.get("applied_project_id")
        previous_project_id = entry.get("previous_project_id")
        receipt = db.get(ReceiptFact, receipt_id) if receipt_id else None
        if receipt is None:
            skipped.append({"receipt_id": receipt_id or None, "reason": "receipt_missing"})
            continue
        if receipt.project_id != applied_project_id:
            skipped.append({"receipt_id": receipt_id, "reason": "current_project_changed"})
            continue
        receipt.project_id = previous_project_id
        restored.append(
            {
                "receipt_id": receipt_id,
                "company_id": receipt.company_id,
                "restored_project_id": previous_project_id,
                "from_project_id": applied_project_id,
            }
        )
    return {
        "status": "rolled_back" if restored and not skipped else ("partial" if restored else "no_changes"),
        "mode": "rollback",
        "rollback_file": str(rollback_path),
        "totals": {
            "entries": len(entries),
            "restored": len(restored),
            "skipped": len(skipped),
        },
        "restored": restored,
        "skipped": skipped,
        "guardrails": {
            "writes_database": bool(restored),
            "uses_ai": False,
            "uses_filesystem_scan": False,
            "updated_tables": ["receipt_facts"] if restored else [],
        },
    }


EXPRESSION_REGISTRY_EXPECTATIONS = [
    {
        "artifact_type": "employee_contribution_narrative",
        "audience_id": "employee",
        "template_id": "employee_contribution_narrative",
        "stage": "expression",
        "required_fact_paths": ["scope.employee_id", "scope.project_id", "counts.photos_current"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "promoted-only APP slot; validation_status must be promotable and promoted=true",
        "ai_policy": "AI polish allowed only through validators; deterministic fallback required",
    },
    {
        "artifact_type": "progress_report_translation",
        "audience_id": "project_manager",
        "template_id": "progress_report_translation",
        "stage": "translate",
        "required_fact_paths": ["scope.report_id", "scope.project_id", "structured_report.executive_summary"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": False,
        "promotion_gate": "shadow validation before report-facing use; preserve source structure",
        "ai_policy": "AI translation may change language only; source facts locked",
    },
    {
        "artifact_type": "progress_report_string_translation",
        "audience_id": "project_manager",
        "template_id": "progress_report_string_translation",
        "stage": "translate",
        "required_fact_paths": ["source_text", "field_label"],
        "min_forbidden_claims": 0,
        "requires_forbidden_phrases": False,
        "promotion_gate": "chunk quality validator must pass before reuse in report surfaces",
        "ai_policy": "AI translation only; placeholders and field meaning locked",
    },
    {
        "artifact_type": "generated_report_markdown",
        "audience_id": "project_manager",
        "template_id": "generated_report_markdown",
        "stage": "render",
        "required_fact_paths": ["scope.report_id", "scope.project_id", "structured_report.manager_brief"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": False,
        "promotion_gate": "validated markdown only; section refs must match source facts",
        "ai_policy": "AI can draft bounded text; report sections and source refs locked",
    },
    {
        "artifact_type": "generated_report_markdown_section",
        "audience_id": "project_manager",
        "template_id": "generated_report_markdown_section",
        "stage": "render",
        "required_fact_paths": ["section_key", "heading", "allowed_source_refs"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": False,
        "promotion_gate": "section validator must pass before merged markdown is usable",
        "ai_policy": "AI can draft one bounded section; allowed source refs locked",
    },
    {
        "artifact_type": "project_manager_decision_brief",
        "audience_id": "project_manager",
        "template_id": "project_manager_decision_brief",
        "stage": "expression",
        "required_fact_paths": ["scope.project_id", "photo_metrics.photos_current", "progress_report_metrics.completed_with_content"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "shadow_invalid must be 0; AI polish cannot alter decision_surface",
        "ai_policy": "AI polish optional; decision surface, ranking, and action language locked by rules",
    },
    {
        "artifact_type": "client_progress_summary",
        "audience_id": "client",
        "template_id": "client_progress_summary",
        "stage": "expression",
        "required_fact_paths": ["scope.project_id", "client_visible_photo_metrics.photos_current", "latest_progress_report.has_completed_report"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "client-safe facts only; promoted-only public surface",
        "ai_policy": "AI polish optional; summary_surface, fact_refs, and metrics locked by surface_immutability validator",
    },
    {
        "artifact_type": "operations_health_summary",
        "audience_id": "operations",
        "template_id": "operations_health_summary",
        "stage": "expression",
        "required_fact_paths": ["task_jobs.queued_backlog", "photo_processing.project_photos_current", "expression_artifacts.artifacts_current"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "operations-only shadow; no user-visible action advice",
        "ai_policy": "AI polish optional; health_surface, overall_status, and fact_refs locked by surface_immutability validator",
    },
    {
        "artifact_type": "finance_summary",
        "audience_id": "finance",
        "template_id": "finance_summary",
        "stage": "expression",
        "required_fact_paths": ["scope.project_id", "receipt_metrics.receipt_count", "receipt_metrics.total_amount"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "finance-only shadow; no reimbursement or approval claims",
        "ai_policy": "AI polish optional; finance_surface, headline_metrics, status_flags, and fact_refs locked by surface_immutability validator",
    },
    {
        "artifact_type": "executive_company_health_summary",
        "audience_id": "executive",
        "template_id": "executive_company_health_summary",
        "stage": "expression",
        "required_fact_paths": ["project_portfolio.projects_total", "photo_activity.photos_current", "expression_readiness.shadow_invalid_current"],
        "min_forbidden_claims": 1,
        "requires_forbidden_phrases": True,
        "promotion_gate": "company shadow audit must pass; no employee pressure indicators",
        "ai_policy": "AI polish optional; company_health_surface, headline_metrics, overall_status, and fact_refs locked by surface_immutability validator",
    },
]

EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS = {
    "employee_contribution_narrative": [
        {
            "surface_id": "mobile_project_contribution",
            "route": "/api/mobile/projects/{project_id}/contribution",
            "module": "app.api.routes.mobile",
            "visible_status": "promoted_valid",
            "guardrails": {
                "requires_promoted": True,
                "requires_validation_status": "promoted_valid",
                "scope": ["company_id", "employee_id", "project_id", "window_days"],
                "shadow_artifacts_visible": False,
            },
        },
        {
            "surface_id": "mobile_contribution_dashboard",
            "route": "/api/mobile/projects/{project_id}/contribution-dashboard",
            "module": "app.services.expression_display",
            "visible_status": "promoted_valid",
            "guardrails": {
                "requires_promoted": True,
                "requires_validation_status": "promoted_valid",
                "scope": ["company_id", "employee_id", "project_id", "window_days"],
                "shadow_artifacts_visible": False,
            },
        },
    ],
    "project_manager_decision_brief": [
        {
            "surface_id": "mobile_project_manager_status_card",
            "route": "/api/mobile/projects/{project_id}/project-manager-status-card",
            "module": "app.services.expression_display",
            "visible_status": "promoted_valid",
            "guardrails": {
                "requires_promoted": True,
                "requires_validation_status": "promoted_valid",
                "scope": ["company_id", "project_id", "window_days"],
                "shadow_artifacts_visible": False,
                "app_computes_metrics": False,
                "model_output_visible": False,
            },
        },
    ],
}

EXPRESSION_PROMPT_EXTRA_VARIABLES = {
    "progress_report_string_translation": {"field_label": "field_label", "source_text": "source_text"},
    "generated_report_markdown_section": {
        "allowed_source_refs_json": "[]",
        "heading": "heading",
        "section_key": "section_key",
    },
}

LEGACY_AI_PROMPT_SURFACE_EXPECTATIONS = [
    {
        "surface_id": "photo_field_analysis",
        "category": "vision",
        "module": "app.services.ai_pipeline",
        "functions": ["build_ai_prompt_for_context", "build_ai_prompt", "process_photo_with_ai"],
        "current_prompt_storage": "code_inline_with_project_db_guidance",
        "current_result_storage": "ai_analysis_logs",
        "source_tables": ["photos", "projects", "media_annotations", "ai_analysis_logs"],
        "db_backed_prompt_inputs": ["projects.image_video_ai_prompt", "operator_custom_prompt"],
        "target_template_id": "photo_field_analysis",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "product_risk",
        "validator": "vision_json_schema_and_evidence_observation_sync",
        "fallback": "keep_existing_code_prompt_until_shadow_parity",
        "promotion_gate": "registry prompt disabled until photo AI parity passes on production sample",
        "migration_gate": "active DB prompt version resolves without changing prompt text digest for baseline cases",
    },
    {
        "surface_id": "photo_invoice_analysis",
        "category": "vision_finance",
        "module": "app.services.ai_pipeline",
        "functions": ["build_ai_prompt_for_context", "build_ai_prompt", "process_photo_with_ai"],
        "current_prompt_storage": "code_inline_with_project_db_guidance",
        "current_result_storage": "ai_analysis_logs + receipt_facts",
        "source_tables": ["photos", "projects", "ai_analysis_logs", "receipt_facts"],
        "db_backed_prompt_inputs": ["projects.billing_receipt_ai_prompt", "operator_custom_prompt"],
        "target_template_id": "photo_invoice_analysis",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "product_risk",
        "validator": "receipt_schema_and_receipt_fact_sync",
        "fallback": "keep_existing_code_prompt_until_receipt_fact_parity",
        "promotion_gate": "no finance-facing use until receipt_facts parity and redaction checks pass",
        "migration_gate": "receipt_facts counts and numeric values match current production parser on sample",
    },
    {
        "surface_id": "video_frame_insight",
        "category": "vision_video",
        "module": "app.services.media_pipeline",
        "functions": ["_process_video_frame_with_ai", "build_media_asset_ai_prompt"],
        "current_prompt_storage": "code_inline_with_project_db_guidance",
        "current_result_storage": "ai_analysis_logs",
        "source_tables": ["media_assets", "photos", "projects", "ai_analysis_logs"],
        "db_backed_prompt_inputs": ["projects.image_video_ai_prompt", "media_assets.metadata_json.custom_prompt"],
        "target_template_id": "video_frame_insight",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "product_risk",
        "validator": "video_insight_json_schema_and_media_scope",
        "fallback": "keep_existing_media_pipeline_prompt",
        "promotion_gate": "registry prompt enabled only after video insight parity and queue safety pass",
        "migration_gate": "same media_asset scopes produce compatible ai_analysis_logs without queue regression",
    },
    {
        "surface_id": "progress_report_multi_image",
        "category": "project_report",
        "module": "app.services.reports",
        "functions": ["build_multi_image_progress_prompt", "generate_progress_report"],
        "current_prompt_storage": "code_inline_with_project_db_guidance",
        "current_result_storage": "progress_reports",
        "source_tables": ["photos", "projects", "progress_reports", "ai_analysis_logs"],
        "db_backed_prompt_inputs": ["projects.image_video_ai_prompt", "progress_report_custom_prompt"],
        "target_template_id": "progress_report_multi_image",
        "target_artifact_type": "progress_report_generation",
        "migration_phase": "expression_registry_candidate",
        "risk_class": "product_risk",
        "validator": "progress_report_schema_photo_id_parity",
        "fallback": "keep_existing_report_generation_path",
        "promotion_gate": "do not replace report generation until registry shadow parity passes",
        "migration_gate": "selected photo ids, report structure, and project scope match current generator",
    },
    {
        "surface_id": "generated_report_markdown_legacy",
        "category": "project_report",
        "module": "app.services.reports",
        "functions": ["build_report_markdown_prompt", "process_generated_report"],
        "current_prompt_storage": "code_inline_custom_prompt",
        "current_result_storage": "generated_reports",
        "source_tables": ["generated_reports", "photos", "projects"],
        "db_backed_prompt_inputs": ["generated_reports.prompt"],
        "target_template_id": "generated_report_markdown_legacy",
        "target_artifact_type": "generated_report_markdown_legacy",
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "future",
        "validator": "legacy_markdown_prompt_digest_parity",
        "fallback": "keep existing generated_reports output table",
        "promotion_gate": "expression artifact can assist only after report markdown shadow parity",
        "migration_gate": "markdown source refs and rendered sections match current report behavior",
    },
    {
        "surface_id": "progress_report_translation_legacy",
        "category": "translation",
        "module": "app.services.reports",
        "functions": ["_build_progress_report_translation_prompt", "translate_progress_report"],
        "current_prompt_storage": "code_inline_legacy_fallback",
        "current_result_storage": "progress_reports.translations_json",
        "source_tables": ["progress_reports", "expression_artifacts", "fact_snapshots"],
        "db_backed_prompt_inputs": [],
        "target_template_id": "progress_report_translation",
        "target_artifact_type": "progress_report_translation",
        "migration_phase": "partially_expression_registry_backed",
        "risk_class": "future",
        "validator": "structure_parity_and_language_quality",
        "fallback": "deterministic skeleton or existing legacy translation path",
        "promotion_gate": "registry translation remains shadow until broad multilingual sample review",
        "migration_gate": "50-report shadow batch passes with zero preserve drift and no fallback chunks",
    },
    {
        "surface_id": "evidence_copilot_answer",
        "category": "copilot",
        "module": "app.services.evidence_copilot",
        "functions": ["_build_answer_prompt", "create_copilot_turn"],
        "current_prompt_storage": "code_inline",
        "current_result_storage": "copilot_messages + copilot_message_sources",
        "source_tables": ["copilot_messages", "copilot_message_sources", "photos", "projects", "evidence_observations"],
        "db_backed_prompt_inputs": ["projects.image_video_ai_prompt"],
        "target_template_id": "evidence_copilot_turn_summary",
        "target_artifact_type": "evidence_copilot_turn_summary",
        "migration_phase": "future_expression_registry",
        "risk_class": "future",
        "validator": "source_ref_grounding_and_no_new_claims",
        "fallback": "keep existing copilot_messages flow",
        "promotion_gate": "do not retire copilot path until registry shadow parity gate passes",
        "migration_gate": "all answer claims map to copilot_message_sources rows",
    },
    {
        "surface_id": "evidence_role_text_review",
        "category": "evidence_role",
        "module": "app.services.ai_role_prompts",
        "functions": ["build_role_prompt", "generate_role_completion"],
        "current_prompt_storage": "code_generated_role_prompt",
        "current_result_storage": "evidence_observations",
        "source_tables": ["evidence_observations", "photos", "media_assets", "ai_analysis_logs"],
        "db_backed_prompt_inputs": [],
        "target_template_id": "evidence_role_text_review",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "future",
        "validator": "role_result_schema_and_observation_scope",
        "fallback": "rule_result_for_role_or_existing_prompt",
        "promotion_gate": "role prompts stay internal until observation parity and confidence policy pass",
        "migration_gate": "observation type/severity/confidence distributions stay within reviewed tolerance",
    },
    {
        "surface_id": "evidence_role_vision_review",
        "category": "evidence_role",
        "module": "app.services.ai_role_prompts",
        "functions": ["build_role_vision_prompt", "generate_role_vision_completion"],
        "current_prompt_storage": "code_generated_role_prompt",
        "current_result_storage": "evidence_observations",
        "source_tables": ["evidence_observations", "photos", "media_assets"],
        "db_backed_prompt_inputs": [],
        "target_template_id": "evidence_role_vision_review",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "future",
        "validator": "role_vision_schema_and_observation_scope",
        "fallback": "rule_result_for_role_or_existing_prompt",
        "promotion_gate": "role prompts stay internal until observation parity and queue safety pass",
        "migration_gate": "role vision outputs preserve evidence_observations sync semantics",
    },
    {
        "surface_id": "generic_text_translation",
        "category": "translation",
        "module": "app.services.ai_pipeline",
        "functions": ["translate_text_with_ollama_backends", "translate_text"],
        "current_prompt_storage": "code_inline",
        "current_result_storage": "caller_owned",
        "source_tables": ["media_annotations", "progress_reports"],
        "db_backed_prompt_inputs": [],
        "target_template_id": "generic_text_translation",
        "target_artifact_type": None,
        "migration_phase": "legacy_prompt_registry",
        "risk_class": "future",
        "validator": "placeholder_preservation_and_language_check",
        "fallback": "return source text or existing caller fallback",
        "promotion_gate": "no user-visible replacement until caller-specific validators exist",
        "migration_gate": "per-caller parity tests define allowed transformations",
    },
    {
        "surface_id": "ai_backend_healthcheck",
        "category": "diagnostic",
        "module": "app.services.ai_pipeline",
        "functions": ["test_ai_backend_connection"],
        "current_prompt_storage": "code_inline_diagnostic",
        "current_result_storage": "diagnostic_response_only",
        "source_tables": ["system_settings", "tenants"],
        "db_backed_prompt_inputs": ["custom_prompt"],
        "target_template_id": "ai_backend_healthcheck",
        "target_artifact_type": None,
        "migration_phase": "low_priority_registry",
        "risk_class": "future",
        "validator": "diagnostic_no_secret_echo",
        "fallback": "keep code-inline diagnostic prompt",
        "promotion_gate": "admin-only diagnostic; never mobile-visible",
        "migration_gate": "healthcheck stays non-persistent and never logs secrets",
    },
]

PROJECT_MANAGER_STATUS_CARD_ARTIFACT = "project_manager_decision_brief"
PROJECT_MANAGER_STATUS_CARD_READ_CONTRACT_VERSION = "project_manager_status_card_read_contract:v1"
PROJECT_MANAGER_STATUS_CARD_SCOPE_TYPES = {"project_period", "project_window"}
PROJECT_MANAGER_STATUS_CARD_ACTION_LANGUAGE_RE = re.compile(
    r"\b(should|must|priority|prioritize|recommend|ensure|urgent|need to|escalate)\b|责任归因|员工排名|绩效评分",
    flags=re.IGNORECASE,
)
MOBILE_PROMOTED_READ_CONTRACT_VERSION = "mobile_promoted_read_contracts:v1"
EMPLOYEE_CONTRIBUTION_READ_CONTRACT_VERSION = "employee_contribution_read_contract:v1"
EMPLOYEE_CONTRIBUTION_FORBIDDEN_READ_TEXT_RE = re.compile(
    r"(raw_model_output|prompt_used|api_key|secret|password|token|员工排名|责任归因|低质量|\branking\b)",
    flags=re.IGNORECASE,
)

MIN_GOLDEN_REPLAY_CASES_PER_ARTIFACT = 1
PHOTO_FIELD_ANALYSIS_TEMPLATE_ID = "photo_field_analysis"
PHOTO_FIELD_ANALYSIS_PROMPT_VERSION_ID = "photo_field_analysis:v1"
PHOTO_FIELD_ANALYSIS_BINDING_ID = "global:photo_field_analysis:v1"
PHOTO_FIELD_ANALYSIS_PARITY_VERSION = "photo_field_analysis_prompt_parity:v1"
PHOTO_INVOICE_ANALYSIS_TEMPLATE_ID = "photo_invoice_analysis"
PHOTO_INVOICE_ANALYSIS_PROMPT_VERSION_ID = "photo_invoice_analysis:v1"
PHOTO_INVOICE_ANALYSIS_BINDING_ID = "global:photo_invoice_analysis:v1"
PHOTO_INVOICE_ANALYSIS_PARITY_VERSION = "photo_invoice_analysis_prompt_parity:v1"
VIDEO_FRAME_INSIGHT_TEMPLATE_ID = "video_frame_insight"
VIDEO_FRAME_INSIGHT_PROMPT_VERSION_ID = "video_frame_insight:v1"
VIDEO_FRAME_INSIGHT_BINDING_ID = "global:video_frame_insight:v1"
VIDEO_FRAME_INSIGHT_PARITY_VERSION = "video_frame_insight_prompt_parity:v1"
PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID = "progress_report_multi_image"
PROGRESS_REPORT_MULTI_IMAGE_PROMPT_VERSION_ID = "progress_report_multi_image:v1"
PROGRESS_REPORT_MULTI_IMAGE_BINDING_ID = "global:progress_report_multi_image:v1"
PROGRESS_REPORT_MULTI_IMAGE_PARITY_VERSION = "progress_report_multi_image_prompt_parity:v1"
GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID = "generated_report_markdown_legacy"
GENERATED_REPORT_MARKDOWN_LEGACY_PROMPT_VERSION_ID = "generated_report_markdown_legacy:v1"
GENERATED_REPORT_MARKDOWN_LEGACY_BINDING_ID = "global:generated_report_markdown_legacy:v1"
GENERATED_REPORT_MARKDOWN_LEGACY_PARITY_VERSION = "generated_report_markdown_legacy_prompt_parity:v1"
PHOTO_FIELD_ANALYSIS_USER_PROMPT_TEMPLATE = """
You are a field evidence triage analyst. The image may be useful work evidence, or it may be ordinary/non-auditable media.
Analyze the attached image and return only valid JSON.
Do not return markdown, code fences, explanations, or any text outside the JSON object.
Required JSON schema:
{schema_block}
Instructions:
- Describe visible facts only.
- Do not infer construction context, hazards, defects, progress, PPE issues, materials, or equipment unless clearly visible.
- Do not use possibly/probably/may indicate to turn ordinary photos into project, worksite, or construction evidence.
- If the image is ordinary or unclear, say so in ai_summary and leave risk/action fields empty.
- Empty fields mean not visible or not applicable.
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
VIDEO_FRAME_INSIGHT_USER_PROMPT_TEMPLATE = PHOTO_FIELD_ANALYSIS_USER_PROMPT_TEMPLATE
PROGRESS_REPORT_MULTI_IMAGE_USER_PROMPT_TEMPLATE = """
你是一个资深的工程监理，正在为项目经理生成正式的进度演变报告。
本项目的最高审查标准是：{project_prompt_text}
{custom_prompt_instruction}
以下是同一施工位置按时间顺序拍摄的多张现场照片。请结合拍摄角度差异、拍摄距离变化和现场遮挡，尽量消除视觉误差。
请只基于这些原始图片和元数据进行判断，不要引用图片外的信息，不要臆测看不见的施工内容。
报告目标不是简单描述图片，而是帮助项目经理判断：进度是否推进、风险是否变大、材料/工具/库存是否支持下一步施工、现场是否有积水或整理问题、哪些事项需要马上安排人员跟进。
每条重要判断尽量写清楚依据来自哪些 photo_id；如果只能从单张图推断，必须说明证据范围有限。
如果下面附带了既有的单图 AI 识别结果，请把它们当作弱约束参考：当前图像与既有结论一致时，优先沿用已有的对象命名。
如果跨图片的一致性提示中已经反复把同一对象命名为某个设备，除非当前图片有明显相反证据，否则不要重新换成别的具体机器名称。
如果你无法高把握确认某个设备或物体的具体类型，请使用保守描述，例如“黑色矩形设备”或“地面设备”，不要仅凭猜测就写成咖啡机、碎纸机或取暖器。
你必须输出一个严格合法的 JSON 对象，不要输出 Markdown，不要包裹 ```json 代码块，不要输出任何 JSON 之外的解释文字。
JSON 字段要求：
- overall_progress_percent 必须是 0 到 100 的整数。
- overall_status 只能是 "on_track"、"at_risk"、"blocked"、"unknown" 之一。
- confidence_level 只能是 "high"、"medium"、"low" 之一。
- 所有列表字段都必须返回数组，没有内容时返回空数组。
- timeline_observations 必须按时间顺序返回，并尽量覆盖每一张图片。
- timeline_observations[].photo_id 必须引用下面提供的真实 photo_id。
- manager_brief 要让项目经理一眼看懂是否需要立即介入。
- material_inventory_signals 要区分“可见库存线索”和“无法确认数量”，不要编造精确数量。
- water_housekeeping_signals 要专门记录积水、泥泞、垃圾、通道遮挡和场地整理情况。
- uncertain_items 用于承认无法可靠识别的设备/材料，并说明为什么不确定。
- immediate_decisions 必须是经理可以立刻执行或安排的事项，不要写空泛建议。
- evidence_limitations 必须明确影响结论可靠性的限制。
输出 JSON 结构示例：
{example_schema_json}

{existing_ai_contexts_section}{preferred_ai_hints_section}照片元数据：
{photo_metadata_lines}
""".strip()
GENERATED_REPORT_MARKDOWN_LEGACY_USER_PROMPT_TEMPLATE = """
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
PHOTO_INVOICE_ANALYSIS_USER_PROMPT_TEMPLATE = """
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

EXPRESSION_AUDIENCE_BOOTSTRAP_SPECS = {
    "employee": {
        "title": "Employee mobile contribution",
        "description": "Neutral APP-facing explanation of one employee's field record contribution.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["self"], "allow_manager_preview": True},
        "forbidden_phrase_set_id": "employee_contribution_zh_v1",
    },
    "project_manager": {
        "title": "Project manager field overview",
        "description": "Manager-facing summary for project evidence review.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["project_manager", "admin"]},
        "forbidden_phrase_set_id": "manager_overview_zh_v1",
    },
    "client": {
        "title": "Client progress explanation",
        "description": "Client-safe project progress wording.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["client", "project_manager", "admin"]},
        "forbidden_phrase_set_id": "client_progress_zh_v1",
    },
    "operations": {
        "title": "Operations field health",
        "description": "Operations-facing field data health summary.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["owner", "admin", "operations"]},
        "forbidden_phrase_set_id": "operations_health_zh_v1",
    },
    "finance": {
        "title": "Finance evidence explanation",
        "description": "Finance-facing wording for receipt and cost evidence.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["owner", "admin", "finance"]},
        "forbidden_phrase_set_id": "finance_evidence_zh_v1",
    },
    "executive": {
        "title": "Company executive overview",
        "description": "Company-wide executive narrative over verified facts.",
        "default_language": "zh",
        "visibility_policy_json": {"show_to": ["owner", "admin"]},
        "forbidden_phrase_set_id": "executive_overview_zh_v1",
    },
}

EXPRESSION_BOOTSTRAP_FORBIDDEN_PHRASES = {
    "employee": ["绩效", "评分", "排名", "继续努力", "低质量"],
    "project_manager": ["责任归因", "员工排名", "绩效评分", "should", "must", "priority", "recommend", "ensure", "urgent"],
    "client": ["内部", "员工号", "成本", "责任归因", "urgent", "must"],
    "operations": ["password", "secret", "token", "api_key", "urgent", "must"],
    "finance": ["报销批准", "付款承诺", "员工排名", "绩效评分", "must"],
    "executive": ["员工排名", "绩效评分", "责任归因", "password", "secret", "token", "urgent", "must"],
}


def _registry_replay_coverage(
    artifact_type_counts: dict[str, object],
    *,
    min_cases_per_artifact: int = MIN_GOLDEN_REPLAY_CASES_PER_ARTIFACT,
) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    covered = 0
    missing_samples = 0
    below_min_cases = 0
    artifacts_with_failures = 0

    for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
        artifact_type = str(expectation["artifact_type"])
        raw_counts = artifact_type_counts.get(artifact_type)
        counts = raw_counts if isinstance(raw_counts, dict) else {}
        cases = int(counts.get("cases") or 0)
        passed = int(counts.get("passed") or 0)
        failed = int(counts.get("failed") or 0)
        skipped = int(counts.get("skipped") or 0)

        if failed > 0:
            status = "has_failures"
            artifacts_with_failures += 1
        elif cases <= 0:
            status = "missing_sample"
            missing_samples += 1
        elif cases < min_cases_per_artifact:
            status = "below_min_cases"
            below_min_cases += 1
        else:
            status = "covered"
            covered += 1

        artifacts.append(
            {
                "artifact_type": artifact_type,
                "status": status,
                "cases": cases,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            }
        )

    return {
        "schema_version": "registry_replay_coverage_v1",
        "min_cases_per_artifact": min_cases_per_artifact,
        "totals": {
            "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
            "artifacts_covered": covered,
            "missing_samples": missing_samples,
            "below_min_cases": below_min_cases,
            "artifacts_with_failures": artifacts_with_failures,
        },
        "artifacts": artifacts,
    }


def _latest_golden_replay_artifact_type_counts(db) -> dict[str, dict[str, int]]:
    latest_by_artifact_type: dict[str, dict[str, int]] = {}
    summaries = db.scalars(
        select(ExpressionEvalRun.summary_json)
        .where(ExpressionEvalRun.run_type == "golden_replay")
        .order_by(
            ExpressionEvalRun.completed_at.desc().nullslast(),
            ExpressionEvalRun.created_at.desc(),
            ExpressionEvalRun.id.desc(),
        )
    )
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        artifact_type_counts = summary.get("artifact_type_counts")
        if not isinstance(artifact_type_counts, dict):
            continue
        for artifact_type, raw_counts in artifact_type_counts.items():
            if not isinstance(raw_counts, dict):
                continue
            artifact_key = str(artifact_type)
            if artifact_key in latest_by_artifact_type:
                continue
            latest_by_artifact_type[artifact_key] = {
                key: int(raw_counts.get(key) or 0)
                for key in ("cases", "passed", "failed", "skipped")
            }
    return latest_by_artifact_type


def _run_json_cli_step(step_name: str, fn, **kwargs: object) -> tuple[int, dict[str, object]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = int(fn(**kwargs))
    raw_output = buffer.getvalue().strip()
    try:
        payload = json.loads(raw_output) if raw_output else {}
    except json.JSONDecodeError as exc:
        payload = {"status": "fail", "parse_error": str(exc), "raw_output": raw_output[:2000]}
        exit_code = 1
    if not isinstance(payload, dict):
        payload = {"status": "fail", "parse_error": "json_root_not_object", "raw_output": raw_output[:2000]}
        exit_code = 1
    payload["step_name"] = step_name
    payload["exit_code"] = exit_code
    return exit_code, payload


def _shadow_batch_gate(
    *,
    gate_name: str,
    payload: dict[str, object],
    exit_code: int,
    min_samples: int,
    sample_key: str = "projects_selected",
) -> tuple[dict[str, object], list[str]]:
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    samples = int(totals.get(sample_key) or 0)
    errors = int(totals.get("errors") or 0)
    shadow_invalid = int(totals.get("shadow_invalid") or 0)
    gate = {
        sample_key: samples,
        "errors": errors,
        "shadow_invalid": shadow_invalid,
        "shadow_valid": int(totals.get("shadow_valid") or 0),
        "shadow_fallback_valid": int(totals.get("shadow_fallback_valid") or 0),
    }
    blocking_reasons: list[str] = []
    if exit_code != 0:
        blocking_reasons.append(f"{gate_name}_exception")
    if samples < min_samples:
        blocking_reasons.append(f"{gate_name}_sample_too_small")
    if errors:
        blocking_reasons.append(f"{gate_name}_errors")
    if shadow_invalid:
        blocking_reasons.append(f"{gate_name}_shadow_invalid")
    return gate, blocking_reasons


def summarize_client_progress_summary_errors(errors: list[str]) -> dict[str, int]:
    reason_counts = {
        "fact_visibility": 0,
        "payload_visibility": 0,
        "imperative_language": 0,
        "surface_mismatch": 0,
        "schema_or_contract": 0,
        "other": 0,
    }
    for error in errors:
        text = str(error)
        if text.startswith("client_visibility:facts:"):
            reason_counts["fact_visibility"] += 1
        elif text.startswith("client_visibility:payload:") or text.startswith("forbidden_claim:"):
            reason_counts["payload_visibility"] += 1
        elif "imperative_language" in text:
            reason_counts["imperative_language"] += 1
        elif "not_from_summary_surface" in text or "summary_surface" in text:
            reason_counts["surface_mismatch"] += 1
        elif text.startswith("schema:") or text.startswith("contract:") or "json_schema" in text:
            reason_counts["schema_or_contract"] += 1
        else:
            reason_counts["other"] += 1
    return {reason: count for reason, count in reason_counts.items() if count}


def create_admin(
    *,
    from_env: bool,
    if_missing: bool,
    username: str | None,
    password: str | None,
    email: str | None,
    display_name: str | None,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)

    username = settings.default_admin_username if from_env else username
    password = settings.default_admin_password if from_env else password
    email = settings.default_admin_email if from_env else email
    display_name = settings.default_admin_display_name if from_env else display_name

    if not username or not password:
        raise SystemExit("Username and password are required")

    try:
        with session_maker() as db:
            # Keep CLI bootstrap disabled to avoid production-side initialization side effects.
            # The runtime entrypoint already applies migrations and startup bootstrap separately.
            # bootstrap_platform_data(db, settings)
            # bootstrap_system_settings(db, settings)
            existing = db.scalar(select(User).where(User.username == username))
            if existing:
                if if_missing:
                    return 0
                raise SystemExit(f"User '{username}' already exists")

            db.add(
                User(
                    company_id=settings.default_company_id,
                    username=username,
                    password_hash=hash_password(password),
                    # The bootstrap admin operates the platform (tenant
                    # approval, company management), not just one tenant.
                    role=UserRole.platform_super_admin,
                    display_name=display_name or "System Administrator",
                    email=email,
                    active=True,
                )
            )
            db.commit()
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit(
            "Database schema is not initialized. Run 'alembic upgrade head' before creating the admin user."
        ) from exc
    return 0


def photo_lens_seed_core() -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        count = seed_core_lenses(db)
        db.commit()
        lenses = enabled_core_lenses(db)
        print(f"seeded_or_updated={count}")
        print(f"active_core_lenses={len(lenses)}")
        for lens, version in lenses:
            print(f"{lens.lens_key}|{lens.id}|{version.id}|priority={lens.priority}")
    return 0


def photo_lens_run(
    *,
    photo_id: int,
    lens_key: str,
    model: str | None,
    backend_url: str | None,
    force: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        if photo is None:
            raise SystemExit(f"Photo {photo_id} was not found")
        matches = [(lens, version) for lens, version in enabled_core_lenses(db, lens_keys=[lens_key]) if lens.lens_key == lens_key]
        if not matches:
            raise SystemExit(f"Lens '{lens_key}' was not found or is disabled")
        result = run_photo_lens_shadow(
            db,
            app_settings=settings,
            photo=photo,
            lens=matches[0][0],
            version=matches[0][1],
            model=model,
            backend_url=backend_url,
            force=force,
        )
        db.commit()
        if result is None:
            print("skipped_existing=true")
            return 0
        print(
            "\n".join(
                [
                    f"run_id={result.run.id}",
                    f"observation_id={result.observation.id if result.observation else ''}",
                    f"ai_analysis_log_id={result.ai_log.id}",
                    f"validation_status={result.run.validation_status}",
                    f"run_status={result.run.run_status}",
                    f"model_used={result.run.model_used}",
                    f"errors={len(result.validation_errors)}",
                ]
            )
        )
    return 0


def photo_lens_backfill(
    *,
    company_id: str | None,
    project_id: str | None,
    lens_keys: list[str] | None,
    limit: int,
    model: str | None,
    backend_url: str | None,
    sleep_seconds: float | None,
    force: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        summary = backfill_photo_lenses(
            db,
            app_settings=settings,
            company_id=company_id,
            project_id=project_id,
            lens_keys=lens_keys,
            limit=limit,
            model=model,
            backend_url=backend_url,
            sleep_seconds=sleep_seconds,
            force=force,
        )
        db.commit()
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        for key in sorted(summary):
            print(f"{key}={summary[key]}")
    return 0


def queue_requeue_dead_letter(
    *,
    task_type: str | None,
    company_id: str | None,
    since_hours: int | None,
    limit: int,
    dry_run: bool,
) -> int:
    from app.models import TaskJob, TaskStatus
    from app.services.job_queue import requeue_task_job

    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        stmt = (
            select(TaskJob)
            .where(TaskJob.status == TaskStatus.dead_letter)
            .order_by(TaskJob.updated_at.desc())
            .limit(max(1, limit))
        )
        if task_type:
            stmt = stmt.where(TaskJob.task_type == task_type)
        if company_id:
            stmt = stmt.where(TaskJob.company_id == company_id)
        if since_hours is not None:
            stmt = stmt.where(TaskJob.updated_at >= utc_now() - timedelta(hours=max(1, since_hours)))
        jobs = list(db.scalars(stmt))
        if not jobs:
            print("No matching dead-letter jobs.")
            return 0
        if dry_run:
            for job in jobs:
                print(f"would requeue {job.public_id} type={job.task_type} company={job.company_id} error={str(job.last_error or '')[:80]}")
            print(f"dry run: {len(jobs)} job(s) matched")
            return 0
        for job in jobs:
            requeue_task_job(db, app_settings=settings, job=job, actor_user_id=None, reason="cli_bulk_requeue")
        db.commit()
        print(f"requeued {len(jobs)} job(s)")
    return 0


def photo_lens_coverage(*, company_id: str | None, project_id: str | None, recent_hours: int | None) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    with session_maker() as db:
        seed_core_lenses(db)
        lens_count = db.scalar(select(func.count()).select_from(LensDefinition).where(LensDefinition.is_enabled.is_(True))) or 0
        photo_stmt = select(func.count()).select_from(Photo).where(Photo.deleted.is_(False), Photo.soft_deleted_at.is_(None))
        obs_stmt = select(PhotoLensObservation.validation_status, func.count()).group_by(PhotoLensObservation.validation_status)
        run_stmt = select(PhotoLensObservationRun.validation_status, PhotoLensObservationRun.run_status, func.count()).group_by(
            PhotoLensObservationRun.validation_status,
            PhotoLensObservationRun.run_status,
        )
        if company_id:
            photo_stmt = photo_stmt.where(Photo.company_id == company_id)
            obs_stmt = obs_stmt.where(PhotoLensObservation.company_id == company_id)
            run_stmt = run_stmt.where(PhotoLensObservationRun.company_id == company_id)
        if project_id:
            photo_stmt = photo_stmt.where(Photo.project_id == project_id)
            obs_stmt = obs_stmt.where(PhotoLensObservation.project_id == project_id)
            run_stmt = run_stmt.where(PhotoLensObservationRun.project_id == project_id)
        if recent_hours is not None:
            since = utc_now() - timedelta(hours=max(1, recent_hours))
            obs_stmt = obs_stmt.where(PhotoLensObservation.created_at >= since)
            run_stmt = run_stmt.where(PhotoLensObservationRun.created_at >= since)
        photo_count = int(db.scalar(photo_stmt) or 0)
        obs_rows = db.execute(obs_stmt).all()
        run_rows = db.execute(run_stmt).all()
        active_observations = sum(int(count) for _, count in obs_rows)
        expected = photo_count * int(lens_count)
        print(f"photos={photo_count}")
        print(f"enabled_lenses={lens_count}")
        print(f"expected_photo_lens_pairs={expected}")
        print(f"active_observations={active_observations}")
        print(f"coverage_ratio={round(active_observations / expected, 4) if expected else 0}")
        for status, count in obs_rows:
            print(f"observation_status.{status}={count}")
        for validation_status, run_status, count in run_rows:
            print(f"run_status.{validation_status}.{run_status}={count}")
    return 0


def project_manager_lens_report(
    *,
    company_id: str,
    project_id: str,
    window_days: int,
    max_photos: int,
    max_evidence_per_lens: int,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            report = build_project_manager_lens_report(
                db,
                company_id=company_id,
                project_id=project_id,
                window_days=window_days,
                max_photos=max_photos,
                max_evidence_per_lens=max_evidence_per_lens,
            )
            if json_output:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(render_project_manager_lens_report_markdown(report))
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_employee(
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_employee_contribution_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                employee_id=employee_id,
                project_id=project_id,
                window_days=window_days,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_promote_artifact(*, artifact_id: str) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            artifact = promote_expression_artifact(db, artifact_id=artifact_id)
            db.commit()
            print(
                "\n".join(
                    [
                        f"artifact_id={artifact.id}",
                        f"validation_status={artifact.validation_status}",
                        f"promoted={str(artifact.promoted).lower()}",
                        f"supersedes_artifact_id={artifact.supersedes_artifact_id or ''}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_progress_report(
    *,
    report_id: str,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_progress_report_translation_shadow(
                db,
                app_settings=settings,
                report_id=report_id,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_progress_report_batch(
    *,
    limit: int,
    company_id: str | None,
    project_id: str | None,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    try:
        with session_maker() as db:
            stmt = select(ProgressReport.id).where(
                ProgressReport.status == ProgressReportStatus.completed,
                ProgressReport.report_content.is_not(None),
            )
            if company_id:
                stmt = stmt.where(ProgressReport.company_id == company_id)
            if project_id:
                stmt = stmt.where(ProgressReport.project_id == project_id)
            stmt = stmt.order_by(
                ProgressReport.completed_at.is_(None),
                ProgressReport.completed_at.desc(),
                ProgressReport.created_at.desc(),
            ).limit(limit)
            report_ids = list(db.scalars(stmt))

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "reports_selected": len(report_ids),
            "reports_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "translated_chunks": 0,
            "fallback_chunks": 0,
            "total_chunks": 0,
        }
        for report_id in report_ids:
            totals["reports_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_progress_report_translation_shadow(
                        db,
                        app_settings=settings,
                        report_id=report_id,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    backend_profile = artifact.backend_profile_json if isinstance(artifact.backend_profile_json, dict) else {}
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["translated_chunks"] += int(backend_profile.get("translated_chunks") or 0)
                    totals["fallback_chunks"] += int(backend_profile.get("fallback_chunks") or 0)
                    totals["total_chunks"] += int(backend_profile.get("total_chunks") or 0)
                    runs.append(
                        {
                            "report_id": report_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "errors": result.errors,
                            "backend_profile": backend_profile,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    runs.append({"report_id": report_id, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"reports_selected={totals['reports_selected']}",
                        f"reports_attempted={totals['reports_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"translated_chunks={totals['translated_chunks']}",
                        f"fallback_chunks={totals['fallback_chunks']}",
                        f"total_chunks={totals['total_chunks']}",
                    ]
                )
            )
            for run in runs:
                if "exception" in run:
                    print(f"report_id={run['report_id']} exception={run['exception']}")
                else:
                    print(
                        " ".join(
                            [
                                f"report_id={run['report_id']}",
                                f"artifact_id={run['artifact_id']}",
                                f"validation_status={run['validation_status']}",
                                f"used_fallback={str(run['used_fallback']).lower()}",
                            ]
                        )
                    )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_progress_report_string(
    *,
    report_id: str,
    field_path: str,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_progress_report_string_translation_shadow(
                db,
                app_settings=settings,
                report_id=report_id,
                field_path=field_path,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"field_path={field_path}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_progress_report_string_batch(
    *,
    limit: int,
    company_id: str | None,
    project_id: str | None,
    chunks_per_report: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    chunks_per_report = max(1, min(chunks_per_report, 50))
    try:
        with session_maker() as db:
            stmt = select(ProgressReport.id).where(
                ProgressReport.status == ProgressReportStatus.completed,
                ProgressReport.report_content.is_not(None),
            )
            if company_id:
                stmt = stmt.where(ProgressReport.company_id == company_id)
            if project_id:
                stmt = stmt.where(ProgressReport.project_id == project_id)
            stmt = stmt.order_by(
                ProgressReport.completed_at.is_(None),
                ProgressReport.completed_at.desc(),
                ProgressReport.created_at.desc(),
            ).limit(limit)
            report_ids = list(db.scalars(stmt))

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "reports_selected": len(report_ids),
            "chunks_per_report": chunks_per_report,
            "chunks_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
        }
        for report_id in report_ids:
            with session_maker() as db:
                report = db.get(ProgressReport, report_id)
                if report is None:
                    continue
                facts = build_progress_report_translation_facts(db, report=report)
                structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
                field_paths = [field_path for field_path, _ in _progress_report_translatable_strings(structured_report)]
            for field_path in field_paths[:chunks_per_report]:
                totals["chunks_attempted"] += 1
                with session_maker() as db:
                    try:
                        result = generate_progress_report_string_translation_shadow(
                            db,
                            app_settings=settings,
                            report_id=report_id,
                            field_path=field_path,
                            language=language,
                            preferred_model=preferred_model,
                            use_ai=not no_ai,
                        )
                        artifact = result.artifact
                        db.commit()
                        status = artifact.validation_status
                        if status in totals:
                            totals[status] += 1
                        totals["errors"] += len(result.errors)
                        runs.append(
                            {
                                "report_id": report_id,
                                "field_path": field_path,
                                "snapshot_id": result.snapshot.id,
                                "artifact_id": artifact.id,
                                "validation_status": status,
                                "model_used": artifact.model_used,
                                "used_fallback": result.used_fallback,
                                "errors": result.errors,
                            }
                        )
                    except Exception as exc:
                        db.rollback()
                        totals["errors"] += 1
                        runs.append({"report_id": report_id, "field_path": field_path, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"reports_selected={totals['reports_selected']}",
                        f"chunks_per_report={totals['chunks_per_report']}",
                        f"chunks_attempted={totals['chunks_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                    ]
                )
            )
            for run in runs:
                if "exception" in run:
                    print(f"report_id={run['report_id']} field_path={run['field_path']} exception={run['exception']}")
                else:
                    print(
                        " ".join(
                            [
                                f"report_id={run['report_id']}",
                                f"field_path={run['field_path']}",
                                f"artifact_id={run['artifact_id']}",
                                f"validation_status={run['validation_status']}",
                                f"used_fallback={str(run['used_fallback']).lower()}",
                            ]
                        )
                    )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_report_markdown(
    *,
    report_id: str,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_report_markdown_shadow(
                db,
                app_settings=settings,
                report_id=report_id,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_report_markdown_batch(
    *,
    limit: int,
    company_id: str | None,
    project_id: str | None,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    try:
        with session_maker() as db:
            stmt = select(ProgressReport.id).where(
                ProgressReport.status == ProgressReportStatus.completed,
                ProgressReport.report_content.is_not(None),
            )
            if company_id:
                stmt = stmt.where(ProgressReport.company_id == company_id)
            if project_id:
                stmt = stmt.where(ProgressReport.project_id == project_id)
            stmt = stmt.order_by(
                ProgressReport.completed_at.is_(None),
                ProgressReport.completed_at.desc(),
                ProgressReport.created_at.desc(),
            ).limit(limit)
            report_ids = list(db.scalars(stmt))

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "reports_selected": len(report_ids),
            "reports_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "ai_sections": 0,
            "fallback_sections": 0,
            "total_sections": 0,
        }
        for report_id in report_ids:
            totals["reports_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_report_markdown_shadow(
                        db,
                        app_settings=settings,
                        report_id=report_id,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    backend_profile = artifact.backend_profile_json if isinstance(artifact.backend_profile_json, dict) else {}
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["ai_sections"] += int(backend_profile.get("ai_sections") or 0)
                    totals["fallback_sections"] += int(backend_profile.get("fallback_sections") or 0)
                    totals["total_sections"] += int(backend_profile.get("total_sections") or 0)
                    runs.append(
                        {
                            "report_id": report_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "errors": result.errors,
                            "backend_profile": backend_profile,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    runs.append({"report_id": report_id, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"reports_selected={totals['reports_selected']}",
                        f"reports_attempted={totals['reports_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"ai_sections={totals['ai_sections']}",
                        f"fallback_sections={totals['fallback_sections']}",
                        f"total_sections={totals['total_sections']}",
                    ]
                )
            )
            for run in runs:
                if "exception" in run:
                    print(f"report_id={run['report_id']} exception={run['exception']}")
                else:
                    print(
                        " ".join(
                            [
                                f"report_id={run['report_id']}",
                                f"artifact_id={run['artifact_id']}",
                                f"validation_status={run['validation_status']}",
                                f"used_fallback={str(run['used_fallback']).lower()}",
                            ]
                        )
                    )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def _markdown_section_keys_for_batch(section_keys: str | None) -> list[str]:
    if not section_keys:
        return sorted(GENERATED_REPORT_MARKDOWN_SECTION_KEYS)
    keys = [part.strip() for part in section_keys.split(",") if part.strip()]
    unknown = [key for key in keys if key not in GENERATED_REPORT_MARKDOWN_SECTION_KEYS]
    if unknown:
        allowed = ", ".join(sorted(GENERATED_REPORT_MARKDOWN_SECTION_KEYS))
        raise SystemExit(f"Unknown section key(s): {', '.join(unknown)}. Expected one of: {allowed}")
    return keys


def expression_shadow_report_markdown_section(
    *,
    report_id: str,
    section_key: str,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_report_markdown_section_shadow(
                db,
                app_settings=settings,
                report_id=report_id,
                section_key=section_key,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"section_key={section_key}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_report_markdown_section_batch(
    *,
    limit: int,
    company_id: str | None,
    project_id: str | None,
    section_keys: str | None,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    keys = _markdown_section_keys_for_batch(section_keys)
    try:
        with session_maker() as db:
            stmt = select(ProgressReport.id).where(
                ProgressReport.status == ProgressReportStatus.completed,
                ProgressReport.report_content.is_not(None),
            )
            if company_id:
                stmt = stmt.where(ProgressReport.company_id == company_id)
            if project_id:
                stmt = stmt.where(ProgressReport.project_id == project_id)
            stmt = stmt.order_by(
                ProgressReport.completed_at.is_(None),
                ProgressReport.completed_at.desc(),
                ProgressReport.created_at.desc(),
            ).limit(limit)
            report_ids = list(db.scalars(stmt))

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "reports_selected": len(report_ids),
            "sections_per_report": len(keys),
            "runs_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
        }
        for report_id in report_ids:
            for section_key in keys:
                totals["runs_attempted"] += 1
                with session_maker() as db:
                    try:
                        result = generate_report_markdown_section_shadow(
                            db,
                            app_settings=settings,
                            report_id=report_id,
                            section_key=section_key,
                            language=language,
                            preferred_model=preferred_model,
                            use_ai=not no_ai,
                        )
                        artifact = result.artifact
                        db.commit()
                        status = artifact.validation_status
                        if status in totals:
                            totals[status] += 1
                        totals["errors"] += len(result.errors)
                        runs.append(
                            {
                                "report_id": report_id,
                                "section_key": section_key,
                                "snapshot_id": result.snapshot.id,
                                "artifact_id": artifact.id,
                                "validation_status": status,
                                "model_used": artifact.model_used,
                                "used_fallback": result.used_fallback,
                                "errors": result.errors,
                            }
                        )
                    except Exception as exc:
                        db.rollback()
                        totals["errors"] += 1
                        runs.append({"report_id": report_id, "section_key": section_key, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"reports_selected={totals['reports_selected']}",
                        f"sections_per_report={totals['sections_per_report']}",
                        f"runs_attempted={totals['runs_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                    ]
                )
            )
            for run in runs:
                if "exception" in run:
                    print(f"report_id={run['report_id']} section_key={run['section_key']} exception={run['exception']}")
                else:
                    print(
                        " ".join(
                            [
                                f"report_id={run['report_id']}",
                                f"section_key={run['section_key']}",
                                f"artifact_id={run['artifact_id']}",
                                f"validation_status={run['validation_status']}",
                                f"used_fallback={str(run['used_fallback']).lower()}",
                            ]
                        )
                    )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_project_manager_brief(
    *,
    company_id: str,
    project_id: str,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_project_manager_decision_brief_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                project_id=project_id,
                window_days=window_days,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_project_manager_brief_batch(
    *,
    limit: int,
    company_id: str | None,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    try:
        with session_maker() as db:
            photo_counts = (
                select(Photo.project_id, func.count(Photo.id).label("evidence_count"))
                .where(Photo.photo_type == PhotoType.project, Photo.deleted.is_(False), Photo.project_id.is_not(None))
                .group_by(Photo.project_id)
                .subquery()
            )
            stmt = (
                select(Project.company_id, Project.project_id, func.coalesce(photo_counts.c.evidence_count, 0))
                .outerjoin(photo_counts, photo_counts.c.project_id == Project.project_id)
                .order_by(func.coalesce(photo_counts.c.evidence_count, 0).desc(), Project.created_at.desc())
                .limit(limit)
            )
            if company_id:
                stmt = stmt.where(Project.company_id == company_id)
            project_rows = [(row[0], row[1]) for row in db.execute(stmt).all()]

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "projects_selected": len(project_rows),
            "projects_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "decision_items": 0,
            "ai_polish_retry_used": 0,
        }
        reason_totals: dict[str, int] = {}
        retry_reason_totals: dict[str, int] = {}
        for row_company_id, project_id in project_rows:
            totals["projects_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_project_manager_decision_brief_shadow(
                        db,
                        app_settings=settings,
                        company_id=row_company_id,
                        project_id=project_id,
                        window_days=window_days,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                    items = (structured.get("decision_surface") or {}).get("items") if isinstance(structured.get("decision_surface"), dict) else []
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["decision_items"] += len(items) if isinstance(items, list) else 0
                    backend_profile = artifact.backend_profile_json if isinstance(artifact.backend_profile_json, dict) else {}
                    retry_reason_counts = (
                        backend_profile.get("retry_reason_counts")
                        if isinstance(backend_profile.get("retry_reason_counts"), dict)
                        else {}
                    )
                    if backend_profile.get("retry_used") is True:
                        totals["ai_polish_retry_used"] += 1
                    for reason, count in retry_reason_counts.items():
                        retry_reason_totals[str(reason)] = retry_reason_totals.get(str(reason), 0) + int(count or 0)
                    error_reason_counts = summarize_project_manager_decision_brief_errors(result.errors)
                    for reason, count in error_reason_counts.items():
                        reason_totals[reason] = reason_totals.get(reason, 0) + count
                    runs.append(
                        {
                            "company_id": row_company_id,
                            "project_id": project_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "ai_polish_retry_used": backend_profile.get("retry_used") is True,
                            "decision_items": len(items) if isinstance(items, list) else 0,
                            "errors": result.errors,
                            "error_reason_counts": error_reason_counts,
                            "retry_reason_counts": retry_reason_counts,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    reason_totals["exception"] = reason_totals.get("exception", 0) + 1
                    runs.append(
                        {
                            "company_id": row_company_id,
                            "project_id": project_id,
                            "validation_status": "exception",
                            "exception_type": exc.__class__.__name__,
                        }
                    )

        summary = {
            "totals": totals,
            "error_reason_totals": reason_totals,
            "retry_reason_totals": retry_reason_totals,
            "runs": runs,
        }
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"projects_selected={totals['projects_selected']}",
                        f"projects_attempted={totals['projects_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"decision_items={totals['decision_items']}",
                        f"ai_polish_retry_used={totals['ai_polish_retry_used']}",
                        f"error_reason_totals={json.dumps(reason_totals, ensure_ascii=False, sort_keys=True)}",
                        f"retry_reason_totals={json.dumps(retry_reason_totals, ensure_ascii=False, sort_keys=True)}",
                    ]
                )
            )
            for run in runs:
                if "exception" in run:
                    print(f"project_id={run['project_id']} exception={run['exception']}")
                else:
                    print(
                        " ".join(
                            [
                                f"project_id={run['project_id']}",
                                f"artifact_id={run['artifact_id']}",
                                f"validation_status={run['validation_status']}",
                                f"decision_items={run['decision_items']}",
                                f"used_fallback={str(run['used_fallback']).lower()}",
                                f"ai_polish_retry_used={str(run['ai_polish_retry_used']).lower()}",
                            ]
                        )
                    )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_client_progress_summary(
    *,
    company_id: str,
    project_id: str,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_client_progress_summary_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                project_id=project_id,
                window_days=window_days,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_client_progress_summary_batch(
    *,
    limit: int,
    company_id: str | None,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    try:
        with session_maker() as db:
            visible_photo_counts = (
                select(Photo.project_id, func.count(Photo.id).label("visible_count"))
                .where(
                    Photo.photo_type == PhotoType.project,
                    Photo.deleted.is_(False),
                    Photo.approval_status == ApprovalStatus.approved,
                    Photo.visibility == PhotoVisibility.client_visible,
                    Photo.project_id.is_not(None),
                )
                .group_by(Photo.project_id)
                .subquery()
            )
            report_counts = (
                select(ProgressReport.project_id, func.count(ProgressReport.id).label("report_count"))
                .where(
                    ProgressReport.status == ProgressReportStatus.completed,
                    ProgressReport.report_content.is_not(None),
                )
                .group_by(ProgressReport.project_id)
                .subquery()
            )
            stmt = (
                select(
                    Project.company_id,
                    Project.project_id,
                    func.coalesce(visible_photo_counts.c.visible_count, 0),
                    func.coalesce(report_counts.c.report_count, 0),
                )
                .outerjoin(visible_photo_counts, visible_photo_counts.c.project_id == Project.project_id)
                .outerjoin(report_counts, report_counts.c.project_id == Project.project_id)
                .order_by(
                    func.coalesce(visible_photo_counts.c.visible_count, 0).desc(),
                    func.coalesce(report_counts.c.report_count, 0).desc(),
                    Project.created_at.desc(),
                )
                .limit(limit)
            )
            if company_id:
                stmt = stmt.where(Project.company_id == company_id)
            project_rows = [(row[0], row[1]) for row in db.execute(stmt).all()]

        runs: list[dict[str, object]] = []
        totals: dict[str, int] = {
            "projects_selected": len(project_rows),
            "projects_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "summary_items": 0,
            "ai_disabled_runs": 0,
        }
        reason_totals: dict[str, int] = {}
        for row_company_id, project_id in project_rows:
            totals["projects_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_client_progress_summary_shadow(
                        db,
                        app_settings=settings,
                        company_id=row_company_id,
                        project_id=project_id,
                        window_days=window_days,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                    items = (structured.get("summary_surface") or {}).get("items") if isinstance(structured.get("summary_surface"), dict) else []
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["summary_items"] += len(items) if isinstance(items, list) else 0
                    backend_profile = artifact.backend_profile_json if isinstance(artifact.backend_profile_json, dict) else {}
                    ai_disabled = artifact.model_used == "deterministic_fallback" and result.used_fallback and not no_ai
                    if no_ai or ai_disabled:
                        totals["ai_disabled_runs"] += 1
                    error_reason_counts = summarize_client_progress_summary_errors(result.errors)
                    for reason, count in error_reason_counts.items():
                        reason_totals[reason] = reason_totals.get(reason, 0) + count
                    runs.append(
                        {
                            "company_id": row_company_id,
                            "project_id": project_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "ai_disabled": bool(no_ai or ai_disabled),
                            "backend_id": backend_profile.get("backend_id"),
                            "summary_items": len(items) if isinstance(items, list) else 0,
                            "errors": result.errors,
                            "error_reason_counts": error_reason_counts,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    reason_totals["exception"] = reason_totals.get("exception", 0) + 1
                    runs.append({"company_id": row_company_id, "project_id": project_id, "exception": str(exc)})

        summary = {"totals": totals, "error_reason_totals": reason_totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"projects_selected={totals['projects_selected']}",
                        f"projects_attempted={totals['projects_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"summary_items={totals['summary_items']}",
                        f"ai_disabled_runs={totals['ai_disabled_runs']}",
                        f"error_reason_totals={json.dumps(reason_totals, ensure_ascii=False, sort_keys=True)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_operations_health_summary(
    *,
    company_id: str,
    window_hours: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_operations_health_summary_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                window_hours=window_hours,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            artifact = result.artifact
            structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
            dimensions = (
                (structured.get("health_surface") or {}).get("dimensions")
                if isinstance(structured.get("health_surface"), dict)
                else []
            )
            db.commit()
            summary = {
                "company_id": company_id,
                "snapshot_id": result.snapshot.id,
                "artifact_id": artifact.id,
                "validation_status": artifact.validation_status,
                "model_used": artifact.model_used,
                "used_fallback": result.used_fallback,
                "overall_status": structured.get("overall_status"),
                "dimensions": len(dimensions) if isinstance(dimensions, list) else 0,
                "errors": result.errors,
            }
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(
                    "\n".join(
                        [
                            f"snapshot_id={summary['snapshot_id']}",
                            f"artifact_id={summary['artifact_id']}",
                            f"validation_status={summary['validation_status']}",
                            f"overall_status={summary['overall_status']}",
                            f"dimensions={summary['dimensions']}",
                            f"model_used={summary['model_used']}",
                            f"used_fallback={str(summary['used_fallback']).lower()}",
                            f"errors={len(result.errors)}",
                        ]
                    )
                )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_operations_health_summary_batch(
    *,
    limit: int,
    company_id: str | None,
    window_hours: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 100))
    try:
        with session_maker() as db:
            stmt = select(Company.company_id).where(Company.active.is_(True)).order_by(Company.created_at.desc()).limit(limit)
            if company_id:
                stmt = stmt.where(Company.company_id == company_id)
            company_ids = [row[0] for row in db.execute(stmt).all()]

        totals: dict[str, int] = {
            "companies_selected": len(company_ids),
            "companies_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "dimensions": 0,
            "ai_disabled_runs": 0,
        }
        runs: list[dict[str, object]] = []
        for selected_company_id in company_ids:
            totals["companies_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_operations_health_summary_shadow(
                        db,
                        app_settings=settings,
                        company_id=selected_company_id,
                        window_hours=window_hours,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                    dimensions = (
                        (structured.get("health_surface") or {}).get("dimensions")
                        if isinstance(structured.get("health_surface"), dict)
                        else []
                    )
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["dimensions"] += len(dimensions) if isinstance(dimensions, list) else 0
                    if no_ai or (artifact.model_used == "deterministic_fallback" and result.used_fallback):
                        totals["ai_disabled_runs"] += 1
                    runs.append(
                        {
                            "company_id": selected_company_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "overall_status": structured.get("overall_status"),
                            "dimensions": len(dimensions) if isinstance(dimensions, list) else 0,
                            "errors": result.errors,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    runs.append({"company_id": selected_company_id, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"companies_selected={totals['companies_selected']}",
                        f"companies_attempted={totals['companies_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"dimensions={totals['dimensions']}",
                        f"ai_disabled_runs={totals['ai_disabled_runs']}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_finance_summary(
    *,
    company_id: str,
    project_id: str,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_finance_summary_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                project_id=project_id,
                window_days=window_days,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            db.commit()
            structured = result.artifact.structured_json if isinstance(result.artifact.structured_json, dict) else {}
            print(
                "\n".join(
                    [
                        f"snapshot_id={result.snapshot.id}",
                        f"artifact_id={result.artifact.id}",
                        f"validation_status={result.artifact.validation_status}",
                        f"model_used={result.artifact.model_used}",
                        f"used_fallback={str(result.used_fallback).lower()}",
                        f"receipt_count={(structured.get('headline_metrics') or {}).get('receipt_count')}",
                        f"errors={len(result.errors)}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_finance_summary_batch(
    *,
    limit: int,
    company_id: str | None,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 200))
    try:
        with session_maker() as db:
            now_value = utc_now()
            current_start = now_value - timedelta(days=max(1, min(int(window_days), 365)))
            receipt_observed_at = func.coalesce(ReceiptFact.receipt_timestamp, ReceiptFact.created_at)
            receipt_counts = (
                select(
                    ReceiptFact.company_id,
                    ReceiptFact.project_id,
                    func.count(ReceiptFact.id).label("receipt_count"),
                )
                .where(
                    ReceiptFact.project_id.is_not(None),
                    receipt_observed_at >= current_start,
                    receipt_observed_at <= now_value,
                )
                .group_by(ReceiptFact.company_id, ReceiptFact.project_id)
                .subquery()
            )
            stmt = (
                select(Project.company_id, Project.project_id, func.coalesce(receipt_counts.c.receipt_count, 0))
                .join(
                    receipt_counts,
                    (receipt_counts.c.company_id == Project.company_id)
                    & (receipt_counts.c.project_id == Project.project_id),
                )
                .order_by(func.coalesce(receipt_counts.c.receipt_count, 0).desc(), Project.created_at.desc())
                .limit(limit)
            )
            if company_id:
                stmt = stmt.where(Project.company_id == company_id)
            project_rows = [(row[0], row[1]) for row in db.execute(stmt).all()]

        totals: dict[str, int] = {
            "projects_selected": len(project_rows),
            "projects_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "receipt_count": 0,
        }
        runs: list[dict[str, object]] = []
        for row_company_id, project_id in project_rows:
            totals["projects_attempted"] += 1
            with session_maker() as db:
                try:
                    result = generate_finance_summary_shadow(
                        db,
                        app_settings=settings,
                        company_id=row_company_id,
                        project_id=project_id,
                        window_days=window_days,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                    receipt_count = int((structured.get("headline_metrics") or {}).get("receipt_count") or 0)
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["receipt_count"] += receipt_count
                    runs.append(
                        {
                            "company_id": row_company_id,
                            "project_id": project_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "receipt_count": receipt_count,
                            "errors": result.errors,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    runs.append({"company_id": row_company_id, "project_id": project_id, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"projects_selected={totals['projects_selected']}",
                        f"projects_attempted={totals['projects_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"receipt_count={totals['receipt_count']}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_audit_finance_fact_health(
    *,
    company_id: str | None,
    window_days: int,
    limit: int,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            payload = audit_finance_fact_health_payload(
                db,
                company_id=company_id,
                window_days=window_days,
                limit=limit,
            )
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                totals = payload["totals"] if isinstance(payload.get("totals"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={payload.get('status')}",
                            f"invoice_photo_count={totals.get('invoice_photo_count')}",
                            f"invoice_photos_with_project_id={totals.get('invoice_photos_with_project_id')}",
                            f"active_ai_logs_with_receipt_facts_photo_count={totals.get('active_ai_logs_with_receipt_facts_photo_count')}",
                            f"receipt_fact_count={totals.get('receipt_fact_count')}",
                            f"receipt_facts_with_project_id={totals.get('receipt_facts_with_project_id')}",
                            f"invoice_photos_missing_receipt_fact={totals.get('invoice_photos_missing_receipt_fact')}",
                            f"ai_receipt_facts_not_materialized={totals.get('ai_receipt_facts_not_materialized')}",
                            f"projects_with_receipt_facts={totals.get('projects_with_receipt_facts')}",
                        ]
                    )
                )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_audit_finance_fact_attribution(
    *,
    company_id: str | None,
    window_days: int,
    limit: int,
    apply: bool,
    rollback_file: str | None,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            if apply:
                if not rollback_file:
                    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
                    rollback_file = f"logs/finance_fact_attribution_rollback_{timestamp}.json"
                payload = apply_finance_fact_attribution_payload(
                    db,
                    company_id=company_id,
                    window_days=window_days,
                    limit=limit,
                    rollback_file=rollback_file,
                )
                db.commit()
            else:
                payload = audit_finance_fact_attribution_payload(
                    db,
                    company_id=company_id,
                    window_days=window_days,
                    limit=limit,
                )
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                totals = payload["totals"] if isinstance(payload.get("totals"), dict) else {}
                lines = [f"status={payload.get('status')}", f"mode={payload.get('mode')}"]
                if payload.get("mode") == "apply":
                    lines.extend(
                        [
                            f"applied={totals.get('applied')}",
                            f"skipped={totals.get('skipped')}",
                            f"rollback_file={payload.get('rollback_file')}",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            f"receipt_facts_scanned={totals.get('receipt_facts_scanned')}",
                            f"already_attributed={totals.get('already_attributed')}",
                            f"suggested={totals.get('suggested')}",
                            f"ambiguous={totals.get('ambiguous')}",
                            f"no_candidate={totals.get('no_candidate')}",
                            f"photo_project_candidates={totals.get('photo_project_candidates')}",
                            f"employee_current_project_candidates={totals.get('employee_current_project_candidates')}",
                        ]
                    )
                print("\n".join(lines))
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_rollback_finance_fact_attribution(*, rollback_file: str, json_output: bool) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            payload = rollback_finance_fact_attribution_payload(db, rollback_file=rollback_file)
            db.commit()
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                totals = payload["totals"] if isinstance(payload.get("totals"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={payload.get('status')}",
                            "mode=rollback",
                            f"restored={totals.get('restored')}",
                            f"skipped={totals.get('skipped')}",
                            f"rollback_file={payload.get('rollback_file')}",
                        ]
                    )
                )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def expression_shadow_executive_company_health(
    *,
    company_id: str,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            result = generate_executive_company_health_shadow(
                db,
                app_settings=settings,
                company_id=company_id,
                window_days=window_days,
                language=language,
                preferred_model=preferred_model,
                use_ai=not no_ai,
            )
            artifact = result.artifact
            structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
            cards = (
                (structured.get("company_health_surface") or {}).get("cards")
                if isinstance(structured.get("company_health_surface"), dict)
                else []
            )
            db.commit()
            summary = {
                "company_id": company_id,
                "snapshot_id": result.snapshot.id,
                "artifact_id": artifact.id,
                "validation_status": artifact.validation_status,
                "model_used": artifact.model_used,
                "used_fallback": result.used_fallback,
                "overall_status": structured.get("overall_status"),
                "cards": len(cards) if isinstance(cards, list) else 0,
                "headline_metrics": structured.get("headline_metrics") or {},
                "errors": result.errors,
            }
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(
                    "\n".join(
                        [
                            f"snapshot_id={summary['snapshot_id']}",
                            f"artifact_id={summary['artifact_id']}",
                            f"validation_status={summary['validation_status']}",
                            f"overall_status={summary['overall_status']}",
                            f"cards={summary['cards']}",
                            f"model_used={summary['model_used']}",
                            f"used_fallback={str(summary['used_fallback']).lower()}",
                            f"errors={len(result.errors)}",
                        ]
                    )
                )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def expression_shadow_executive_company_health_batch(
    *,
    limit: int,
    company_id: str | None,
    window_days: int,
    language: str,
    preferred_model: str | None,
    no_ai: bool,
    json_output: bool,
) -> int:
    settings = load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 100))
    try:
        with session_maker() as db:
            stmt = select(Company.company_id).where(Company.active.is_(True)).order_by(Company.created_at.desc()).limit(limit)
            if company_id:
                stmt = stmt.where(Company.company_id == company_id)
            company_ids = [row[0] for row in db.execute(stmt).all()]

        totals: dict[str, int] = {
            "companies_selected": len(company_ids),
            "companies_attempted": 0,
            "shadow_valid": 0,
            "shadow_invalid": 0,
            "shadow_fallback_valid": 0,
            "errors": 0,
            "cards": 0,
            "ai_disabled_runs": 0,
        }
        runs: list[dict[str, object]] = []
        for selected_company_id in company_ids:
            totals["companies_attempted"] += 1
            if no_ai:
                totals["ai_disabled_runs"] += 1
            with session_maker() as db:
                try:
                    result = generate_executive_company_health_shadow(
                        db,
                        app_settings=settings,
                        company_id=selected_company_id,
                        window_days=window_days,
                        language=language,
                        preferred_model=preferred_model,
                        use_ai=not no_ai,
                    )
                    artifact = result.artifact
                    structured = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                    cards = (
                        (structured.get("company_health_surface") or {}).get("cards")
                        if isinstance(structured.get("company_health_surface"), dict)
                        else []
                    )
                    card_count = len(cards) if isinstance(cards, list) else 0
                    db.commit()
                    status = artifact.validation_status
                    if status in totals:
                        totals[status] += 1
                    totals["errors"] += len(result.errors)
                    totals["cards"] += card_count
                    runs.append(
                        {
                            "company_id": selected_company_id,
                            "snapshot_id": result.snapshot.id,
                            "artifact_id": artifact.id,
                            "validation_status": status,
                            "overall_status": structured.get("overall_status"),
                            "model_used": artifact.model_used,
                            "used_fallback": result.used_fallback,
                            "cards": card_count,
                            "headline_metrics": structured.get("headline_metrics") or {},
                            "errors": result.errors,
                        }
                    )
                except Exception as exc:
                    db.rollback()
                    totals["errors"] += 1
                    runs.append({"company_id": selected_company_id, "exception": str(exc)})

        summary = {"totals": totals, "runs": runs}
        if json_output:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(
                "\n".join(
                    [
                        f"companies_selected={totals['companies_selected']}",
                        f"companies_attempted={totals['companies_attempted']}",
                        f"shadow_valid={totals['shadow_valid']}",
                        f"shadow_invalid={totals['shadow_invalid']}",
                        f"shadow_fallback_valid={totals['shadow_fallback_valid']}",
                        f"errors={totals['errors']}",
                        f"cards={totals['cards']}",
                        f"ai_disabled_runs={totals['ai_disabled_runs']}",
                    ]
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    return 0


def _json_text_without_guardrails(value: object) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    sanitized = dict(value)
    sanitized.pop("guardrails", None)
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True)


def audit_executive_company_health_shadow(
    *,
    company_id: str | None,
    limit: int,
    json_output: bool,
    fail_on_warnings: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(limit, 500))
    try:
        with session_maker() as db:
            stmt = (
                select(ExpressionArtifact)
                .where(ExpressionArtifact.artifact_type == "executive_company_health_summary")
                .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
                .limit(limit)
            )
            if company_id:
                stmt = stmt.where(ExpressionArtifact.company_id == company_id)
            artifacts = list(db.scalars(stmt))

            totals: dict[str, object] = {
                "artifacts_checked": len(artifacts),
                "validation_status_counts": {},
                "overall_status_counts": {},
                "card_status_counts": {},
                "card_id_counts": {},
                "rule_flag_counts": {},
                "missing_fact_refs": 0,
                "payload_denied_text": 0,
                "facts_denied_text": 0,
                "raw_model_output_present": 0,
                "validation_error_artifacts": 0,
                "shadow_invalid": 0,
                "not_shadow_visibility": 0,
            }
            runs: list[dict[str, object]] = []
            for artifact in artifacts:
                payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id)
                facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
                status_counts = totals["validation_status_counts"]
                assert isinstance(status_counts, dict)
                status_counts[artifact.validation_status] = int(status_counts.get(artifact.validation_status, 0)) + 1
                if artifact.validation_status == "shadow_invalid":
                    totals["shadow_invalid"] = int(totals["shadow_invalid"]) + 1
                if artifact.validation_errors_json:
                    totals["validation_error_artifacts"] = int(totals["validation_error_artifacts"]) + 1
                if artifact.raw_model_output is not None:
                    totals["raw_model_output_present"] = int(totals["raw_model_output_present"]) + 1

                payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                payload_denied = bool(EXECUTIVE_HEALTH_AUDIT_DENIED_RE.search(payload_text))
                facts_denied = bool(EXECUTIVE_HEALTH_AUDIT_DENIED_RE.search(_json_text_without_guardrails(facts)))
                if payload_denied:
                    totals["payload_denied_text"] = int(totals["payload_denied_text"]) + 1
                if facts_denied:
                    totals["facts_denied_text"] = int(totals["facts_denied_text"]) + 1
                if payload.get("visibility") != "shadow":
                    totals["not_shadow_visibility"] = int(totals["not_shadow_visibility"]) + 1

                overall_status = str(payload.get("overall_status") or "missing")
                overall_counts = totals["overall_status_counts"]
                assert isinstance(overall_counts, dict)
                overall_counts[overall_status] = int(overall_counts.get(overall_status, 0)) + 1

                cards = []
                surface = payload.get("company_health_surface") if isinstance(payload.get("company_health_surface"), dict) else {}
                if isinstance(surface.get("cards"), list):
                    cards = [card for card in surface["cards"] if isinstance(card, dict)]
                missing_refs = 0
                card_ids: list[str] = []
                for card in cards:
                    card_id = str(card.get("card_id") or "missing")
                    card_ids.append(card_id)
                    card_counts = totals["card_id_counts"]
                    assert isinstance(card_counts, dict)
                    card_counts[card_id] = int(card_counts.get(card_id, 0)) + 1
                    card_status = str(card.get("status") or "missing")
                    card_status_counts = totals["card_status_counts"]
                    assert isinstance(card_status_counts, dict)
                    card_status_counts[card_status] = int(card_status_counts.get(card_status, 0)) + 1
                    fact_refs = card.get("fact_refs") if isinstance(card.get("fact_refs"), list) else []
                    if not fact_refs:
                        missing_refs += 1
                    for flag in card.get("rule_flags") or []:
                        flag_text = str(flag)
                        flag_counts = totals["rule_flag_counts"]
                        assert isinstance(flag_counts, dict)
                        flag_counts[flag_text] = int(flag_counts.get(flag_text, 0)) + 1
                totals["missing_fact_refs"] = int(totals["missing_fact_refs"]) + missing_refs
                runs.append(
                    {
                        "artifact_id": artifact.id,
                        "snapshot_id": artifact.fact_snapshot_id,
                        "company_id": artifact.company_id,
                        "validation_status": artifact.validation_status,
                        "overall_status": overall_status,
                        "card_ids": card_ids,
                        "missing_fact_refs": missing_refs,
                        "payload_denied_text": payload_denied,
                        "facts_denied_text": facts_denied,
                        "raw_model_output_present": artifact.raw_model_output is not None,
                    }
                )

            blocking_issues = (
                int(totals["shadow_invalid"])
                + int(totals["missing_fact_refs"])
                + int(totals["payload_denied_text"])
                + int(totals["facts_denied_text"])
                + int(totals["raw_model_output_present"])
                + int(totals["not_shadow_visibility"])
            )
            warnings = int(totals["validation_error_artifacts"])
            summary = {
                "totals": totals,
                "blocking_issues": blocking_issues,
                "warnings": warnings,
                "status": "pass" if blocking_issues == 0 and (warnings == 0 or not fail_on_warnings) else "fail",
                "runs": runs,
            }
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"artifacts_checked={totals['artifacts_checked']}",
                            f"status={summary['status']}",
                            f"blocking_issues={blocking_issues}",
                            f"warnings={warnings}",
                            f"validation_status_counts={json.dumps(totals['validation_status_counts'], ensure_ascii=False, sort_keys=True)}",
                            f"overall_status_counts={json.dumps(totals['overall_status_counts'], ensure_ascii=False, sort_keys=True)}",
                            f"card_status_counts={json.dumps(totals['card_status_counts'], ensure_ascii=False, sort_keys=True)}",
                            f"rule_flag_counts={json.dumps(totals['rule_flag_counts'], ensure_ascii=False, sort_keys=True)}",
                            f"missing_fact_refs={totals['missing_fact_refs']}",
                            f"payload_denied_text={totals['payload_denied_text']}",
                            f"facts_denied_text={totals['facts_denied_text']}",
                            f"raw_model_output_present={totals['raw_model_output_present']}",
                        ]
                    )
                )
            return 1 if summary["status"] == "fail" else 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _count_list(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _contains_all_required_paths(actual_paths: object, required_paths: list[str]) -> tuple[bool, list[str]]:
    actual = set(str(path) for path in actual_paths) if isinstance(actual_paths, list) else set()
    missing = [path for path in required_paths if path not in actual]
    return not missing, missing


def audit_expression_registry(
    *,
    json_output: bool,
    fail_on_warnings: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            totals: dict[str, object] = {
                "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                "artifacts_checked": 0,
                "blocking_issues": 0,
                "warnings": 0,
                "missing_template": 0,
                "missing_active_version": 0,
                "missing_binding": 0,
                "missing_contract": 0,
                "missing_required_fact_paths": 0,
                "missing_forbidden_claims": 0,
                "missing_forbidden_phrases": 0,
                "stage_mismatch": 0,
            }
            artifacts: list[dict[str, object]] = []
            for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
                artifact_type = str(expectation["artifact_type"])
                template_id = str(expectation["template_id"])
                audience_id = str(expectation["audience_id"])
                expected_stage = str(expectation["stage"])
                issues: list[str] = []
                warnings: list[str] = []
                template = db.get(ExpressionPromptTemplate, template_id)
                if template is None:
                    issues.append("missing_template")
                    totals["missing_template"] = int(totals["missing_template"]) + 1
                else:
                    if template.artifact_type != artifact_type:
                        issues.append("template_artifact_type_mismatch")
                    if template.stage != expected_stage:
                        issues.append("stage_mismatch")
                        totals["stage_mismatch"] = int(totals["stage_mismatch"]) + 1
                    if template.default_audience_id != audience_id:
                        warnings.append("default_audience_mismatch")

                audience = db.get(ExpressionAudience, audience_id)
                if audience is None or not audience.is_active:
                    issues.append("audience_missing_or_inactive")

                versions = []
                if template is not None:
                    versions = list(
                        db.scalars(
                            select(ExpressionPromptVersion)
                            .where(
                                ExpressionPromptVersion.template_id == template_id,
                                ExpressionPromptVersion.status == "active",
                            )
                            .order_by(ExpressionPromptVersion.created_at.desc(), ExpressionPromptVersion.id.desc())
                        )
                    )
                if not versions:
                    issues.append("missing_active_version")
                    totals["missing_active_version"] = int(totals["missing_active_version"]) + 1

                bindings = []
                if template is not None:
                    bindings = list(
                        db.scalars(
                            select(ExpressionPromptBinding)
                            .where(
                                ExpressionPromptBinding.template_id == template_id,
                                ExpressionPromptBinding.scope_type == "global",
                            )
                            .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
                        )
                    )
                active_version_ids = {version.id for version in versions}
                active_binding = next((binding for binding in bindings if binding.prompt_version_id in active_version_ids), None)
                if active_binding is None:
                    issues.append("missing_binding_to_active_version")
                    totals["missing_binding"] = int(totals["missing_binding"]) + 1

                contract = db.scalar(
                    select(ExpressionOutputContract)
                    .where(
                        ExpressionOutputContract.artifact_type == artifact_type,
                        ExpressionOutputContract.audience_id == audience_id,
                        ExpressionOutputContract.is_active.is_(True),
                    )
                    .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
                )
                missing_paths: list[str] = []
                forbidden_claim_count = 0
                if contract is None:
                    issues.append("missing_active_contract")
                    totals["missing_contract"] = int(totals["missing_contract"]) + 1
                else:
                    has_paths, missing_paths = _contains_all_required_paths(
                        contract.required_fact_paths_json,
                        list(expectation["required_fact_paths"]),
                    )
                    if not has_paths:
                        issues.append("missing_required_fact_paths")
                        totals["missing_required_fact_paths"] = int(totals["missing_required_fact_paths"]) + 1
                    forbidden_claim_count = _count_list(contract.forbidden_claims_json)
                    if forbidden_claim_count < int(expectation["min_forbidden_claims"]):
                        issues.append("missing_forbidden_claims")
                        totals["missing_forbidden_claims"] = int(totals["missing_forbidden_claims"]) + 1

                phrase_count = 0
                if audience is not None and audience.forbidden_phrase_set_id:
                    phrase_count = int(
                        db.scalar(
                            select(func.count(ExpressionForbiddenPhrase.id)).where(
                                ExpressionForbiddenPhrase.set_id == audience.forbidden_phrase_set_id,
                                ExpressionForbiddenPhrase.is_active.is_(True),
                            )
                        )
                        or 0
                    )
                if bool(expectation["requires_forbidden_phrases"]) and phrase_count == 0:
                    issues.append("missing_forbidden_phrases")
                    totals["missing_forbidden_phrases"] = int(totals["missing_forbidden_phrases"]) + 1

                totals["artifacts_checked"] = int(totals["artifacts_checked"]) + 1
                totals["blocking_issues"] = int(totals["blocking_issues"]) + len(issues)
                totals["warnings"] = int(totals["warnings"]) + len(warnings)
                artifacts.append(
                    {
                        "artifact_type": artifact_type,
                        "audience_id": audience_id,
                        "template_id": template_id,
                        "template_present": template is not None,
                        "active_version_ids": sorted(active_version_ids),
                        "active_binding_id": active_binding.id if active_binding is not None else None,
                        "contract_id": contract.id if contract is not None else None,
                        "required_fact_paths_present": missing_paths == [],
                        "missing_required_fact_paths": missing_paths,
                        "forbidden_claim_count": forbidden_claim_count,
                        "forbidden_phrase_count": phrase_count,
                        "promotion_gate": expectation["promotion_gate"],
                        "ai_policy": expectation["ai_policy"],
                        "issues": issues,
                        "warnings": warnings,
                    }
                )
            status = (
                "pass"
                if int(totals["blocking_issues"]) == 0 and (int(totals["warnings"]) == 0 or not fail_on_warnings)
                else "fail"
            )
            summary = {"status": status, "totals": totals, "artifacts": artifacts}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={status}",
                            f"artifacts_expected={totals['artifacts_expected']}",
                            f"artifacts_checked={totals['artifacts_checked']}",
                            f"blocking_issues={totals['blocking_issues']}",
                            f"warnings={totals['warnings']}",
                            f"missing_template={totals['missing_template']}",
                            f"missing_active_version={totals['missing_active_version']}",
                            f"missing_binding={totals['missing_binding']}",
                            f"missing_contract={totals['missing_contract']}",
                            f"missing_required_fact_paths={totals['missing_required_fact_paths']}",
                            f"missing_forbidden_claims={totals['missing_forbidden_claims']}",
                            f"missing_forbidden_phrases={totals['missing_forbidden_phrases']}",
                        ]
                    )
                )
            return 1 if status == "fail" else 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _expression_bootstrap_contract_schema(artifact_type: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "artifact_type": {"type": "string", "const": artifact_type},
        },
    }


def _expression_bootstrap_forbidden_claims(expectation: dict[str, object]) -> list[str]:
    base_claims = [
        "Do not add facts that are not present in FactSnapshot.facts_json.",
        "Do not include raw model output, secrets, API keys, passwords, filesystem paths, or private storage paths.",
        "Do not create rankings, performance scores, blame, responsibility attribution, urgency claims, or action advice.",
    ]
    minimum = int(expectation["min_forbidden_claims"])
    return base_claims[: max(0, min(len(base_claims), minimum or 1))]


def _expression_bootstrap_system_prompt(expectation: dict[str, object]) -> str:
    artifact_type = str(expectation["artifact_type"])
    audience_id = str(expectation["audience_id"])
    return (
        "You are the kk photoserver-v2 AI expression layer. Use only FactSnapshot.facts_json and the output "
        f"contract for artifact_type={artifact_type}, audience={audience_id}. Do not invent facts, rankings, "
        "performance scores, responsibility attribution, urgency, priority, recommendations, secrets, file paths, "
        "or raw model output. Return strict JSON only."
    )


def _expression_bootstrap_user_prompt(expectation: dict[str, object]) -> str:
    extra_variables = EXPRESSION_PROMPT_EXTRA_VARIABLES.get(str(expectation["template_id"]), {})
    extra_lines = "\n".join(f"{key}: {{{key}}}" for key in sorted(extra_variables))
    if extra_lines:
        extra_lines = f"\n\nAdditional locked inputs:\n{extra_lines}"
    return (
        "Facts JSON:\n{facts_json}\n\n"
        "Output contract JSON Schema:\n{contract_schema_json}"
        f"{extra_lines}\n\n"
        "Write only the requested structured JSON. Keep all deterministic surfaces, metrics, and fact_refs grounded "
        "in the input facts and contract."
    )


def _expression_bootstrap_next_id(db, model, base_id: str) -> str:
    existing = db.get(model, base_id)
    if existing is None:
        return base_id
    suffix = 2
    while True:
        candidate = f"{base_id}-bootstrap-{suffix}"
        if db.get(model, candidate) is None:
            return candidate
        suffix += 1


def build_expression_registry_bootstrap_payload(db, *, apply: bool) -> dict[str, object]:
    actions: list[dict[str, object]] = []
    created: list[dict[str, object]] = []

    def record(kind: str, item_id: str, *, reason: str) -> None:
        actions.append({"kind": kind, "id": item_id, "reason": reason})

    for audience_id in sorted({str(expectation["audience_id"]) for expectation in EXPRESSION_REGISTRY_EXPECTATIONS}):
        spec = EXPRESSION_AUDIENCE_BOOTSTRAP_SPECS.get(audience_id, {})
        phrase_set_id = str(spec.get("forbidden_phrase_set_id") or f"{audience_id}_expression_v1")
        if db.get(ExpressionAudience, audience_id) is None:
            record("audience", audience_id, reason="missing_audience")
            if apply:
                db.add(
                    ExpressionAudience(
                        id=audience_id,
                        code=audience_id,
                        title=str(spec.get("title") or audience_id),
                        description=str(spec.get("description") or ""),
                        default_language=str(spec.get("default_language") or "zh"),
                        visibility_policy_json=spec.get("visibility_policy_json") if isinstance(spec, dict) else {},
                        forbidden_phrase_set_id=phrase_set_id,
                        is_active=True,
                    )
                )
                created.append({"kind": "audience", "id": audience_id})

        active_phrase_count = int(
            db.scalar(
                select(func.count(ExpressionForbiddenPhrase.id)).where(
                    ExpressionForbiddenPhrase.set_id == phrase_set_id,
                    ExpressionForbiddenPhrase.is_active.is_(True),
                )
            )
            or 0
        )
        if active_phrase_count == 0:
            for phrase in EXPRESSION_BOOTSTRAP_FORBIDDEN_PHRASES.get(audience_id, []):
                phrase_id = f"{phrase_set_id}:{hashlib.sha256(phrase.encode('utf-8')).hexdigest()[:12]}"
                record("forbidden_phrase", phrase_id, reason="missing_forbidden_phrase")
                if apply:
                    db.add(
                        ExpressionForbiddenPhrase(
                            id=phrase_id,
                            set_id=phrase_set_id,
                            audience_id=audience_id,
                            language="zh" if not phrase.isascii() else "en",
                            phrase=phrase,
                            match_type="literal",
                            severity="error",
                            replacement_hint="Use neutral database-backed wording.",
                            is_active=True,
                        )
                    )
                    created.append({"kind": "forbidden_phrase", "id": phrase_id})

    if apply:
        db.flush()

    for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
        artifact_type = str(expectation["artifact_type"])
        audience_id = str(expectation["audience_id"])
        template_id = str(expectation["template_id"])
        stage = str(expectation["stage"])

        if db.get(ExpressionPromptTemplate, template_id) is None:
            record("prompt_template", template_id, reason="missing_template")
            if apply:
                db.add(
                    ExpressionPromptTemplate(
                        id=template_id,
                        slug=template_id,
                        title=template_id.replace("_", " ").title(),
                        artifact_type=artifact_type,
                        stage=stage,
                        default_audience_id=audience_id,
                        description=f"Bootstrap prompt template for {artifact_type}.",
                    )
                )
                created.append({"kind": "prompt_template", "id": template_id})

        active_version = db.scalar(
            select(ExpressionPromptVersion)
            .where(
                ExpressionPromptVersion.template_id == template_id,
                ExpressionPromptVersion.status == "active",
            )
            .order_by(ExpressionPromptVersion.created_at.desc(), ExpressionPromptVersion.id.desc())
        )
        if active_version is None:
            version_id = _expression_bootstrap_next_id(db, ExpressionPromptVersion, f"{template_id}:v1")
            record("prompt_version", version_id, reason="missing_active_version")
            if apply:
                db.add(
                    ExpressionPromptVersion(
                        id=version_id,
                        template_id=template_id,
                        version="v1" if version_id == f"{template_id}:v1" else version_id.rsplit(":", 1)[-1],
                        system_prompt=_expression_bootstrap_system_prompt(expectation),
                        user_prompt_template=_expression_bootstrap_user_prompt(expectation),
                        few_shot_json=None,
                        json_schema_override=None,
                        status="active",
                        notes="Bootstrap fallback prompt; validators and deterministic fallbacks remain authoritative.",
                    )
                )
                active_version = db.get(ExpressionPromptVersion, version_id)
                created.append({"kind": "prompt_version", "id": version_id})

        active_version_id = active_version.id if active_version is not None else f"{template_id}:v1"
        active_binding = db.scalar(
            select(ExpressionPromptBinding)
            .where(
                ExpressionPromptBinding.template_id == template_id,
                ExpressionPromptBinding.scope_type == "global",
                ExpressionPromptBinding.prompt_version_id == active_version_id,
            )
            .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
        )
        if active_binding is None:
            binding_id = _expression_bootstrap_next_id(
                db,
                ExpressionPromptBinding,
                f"global:{template_id}:{active_version_id.rsplit(':', 1)[-1]}",
            )
            record("prompt_binding", binding_id, reason="missing_global_binding_to_active_version")
            if apply:
                db.add(
                    ExpressionPromptBinding(
                        id=binding_id,
                        scope_type="global",
                        scope_id=None,
                        template_id=template_id,
                        prompt_version_id=active_version_id,
                        priority=100,
                        effective_from=utc_now(),
                        effective_until=None,
                    )
                )
                created.append({"kind": "prompt_binding", "id": binding_id})

        active_contract = db.scalar(
            select(ExpressionOutputContract)
            .where(
                ExpressionOutputContract.artifact_type == artifact_type,
                ExpressionOutputContract.audience_id == audience_id,
                ExpressionOutputContract.is_active.is_(True),
            )
            .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
        )
        if active_contract is None:
            contract_id = _expression_bootstrap_next_id(
                db,
                ExpressionOutputContract,
                f"{artifact_type}:{audience_id}:v1",
            )
            record("output_contract", contract_id, reason="missing_active_contract")
            if apply:
                db.add(
                    ExpressionOutputContract(
                        id=contract_id,
                        artifact_type=artifact_type,
                        audience_id=audience_id,
                        version="v1" if contract_id == f"{artifact_type}:{audience_id}:v1" else "bootstrap-v1",
                        json_schema=_expression_bootstrap_contract_schema(artifact_type),
                        required_fact_paths_json=list(expectation["required_fact_paths"]),
                        forbidden_claims_json=_expression_bootstrap_forbidden_claims(expectation),
                        is_active=True,
                    )
                )
                created.append({"kind": "output_contract", "id": contract_id})

    if apply:
        db.flush()

    return {
        "status": "applied" if apply else "dry_run",
        "schema_version": "expression_registry_bootstrap_v1",
        "guardrails": {
            "uses_ai": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "writes_database": bool(apply and actions),
            "overwrites_existing_rows": False,
        },
        "totals": {
            "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
            "planned_changes": len(actions),
            "created_rows": len(created),
        },
        "actions": actions,
        "created": created,
    }


def expression_bootstrap_registry(
    *,
    apply: bool,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            payload = build_expression_registry_bootstrap_payload(db, apply=apply)
            if apply:
                db.commit()
            else:
                db.rollback()
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                totals = payload["totals"] if isinstance(payload.get("totals"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={payload['status']}",
                            f"artifacts_expected={totals.get('artifacts_expected')}",
                            f"planned_changes={totals.get('planned_changes')}",
                            f"created_rows={totals.get('created_rows')}",
                        ]
                    )
                )
            return 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def audit_expression_audience_coverage(
    *,
    json_output: bool,
    fail_on_warnings: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    expected_by_audience: dict[str, list[dict[str, object]]] = {}
    for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
        expected_by_audience.setdefault(str(expectation["audience_id"]), []).append(expectation)

    try:
        with session_maker() as db:
            totals: dict[str, object] = {
                "audiences_expected": len(expected_by_audience),
                "audiences_present": 0,
                "audiences_ready": 0,
                "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                "artifacts_ready": 0,
                "blocking_issues": 0,
                "warnings": 0,
                "missing_audience": 0,
                "missing_template": 0,
                "missing_active_version": 0,
                "missing_binding": 0,
                "missing_contract": 0,
            }
            audiences: list[dict[str, object]] = []
            for audience_id in sorted(expected_by_audience):
                audience = db.get(ExpressionAudience, audience_id)
                audience_issues: list[str] = []
                audience_warnings: list[str] = []
                if audience is None or not audience.is_active:
                    audience_issues.append("audience_missing_or_inactive")
                    totals["missing_audience"] = int(totals["missing_audience"]) + 1
                else:
                    totals["audiences_present"] = int(totals["audiences_present"]) + 1

                artifact_entries: list[dict[str, object]] = []
                ready_count = 0
                for expectation in sorted(expected_by_audience[audience_id], key=lambda item: str(item["artifact_type"])):
                    artifact_type = str(expectation["artifact_type"])
                    template_id = str(expectation["template_id"])
                    issues: list[str] = []
                    warnings: list[str] = []
                    template = db.get(ExpressionPromptTemplate, template_id)
                    versions: list[ExpressionPromptVersion] = []
                    active_binding: ExpressionPromptBinding | None = None
                    contract: ExpressionOutputContract | None = None

                    if template is None:
                        issues.append("missing_template")
                        totals["missing_template"] = int(totals["missing_template"]) + 1
                    else:
                        if template.default_audience_id != audience_id:
                            warnings.append("default_audience_mismatch")
                        versions = list(
                            db.scalars(
                                select(ExpressionPromptVersion)
                                .where(
                                    ExpressionPromptVersion.template_id == template_id,
                                    ExpressionPromptVersion.status == "active",
                                )
                                .order_by(ExpressionPromptVersion.created_at.desc(), ExpressionPromptVersion.id.desc())
                            )
                        )
                        if not versions:
                            issues.append("missing_active_version")
                            totals["missing_active_version"] = int(totals["missing_active_version"]) + 1
                        active_version_ids = {version.id for version in versions}
                        bindings = list(
                            db.scalars(
                                select(ExpressionPromptBinding)
                                .where(
                                    ExpressionPromptBinding.template_id == template_id,
                                    ExpressionPromptBinding.scope_type == "global",
                                )
                                .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
                            )
                        )
                        active_binding = next(
                            (binding for binding in bindings if binding.prompt_version_id in active_version_ids),
                            None,
                        )
                        if active_binding is None:
                            issues.append("missing_binding_to_active_version")
                            totals["missing_binding"] = int(totals["missing_binding"]) + 1

                    contract = db.scalar(
                        select(ExpressionOutputContract)
                        .where(
                            ExpressionOutputContract.artifact_type == artifact_type,
                            ExpressionOutputContract.audience_id == audience_id,
                            ExpressionOutputContract.is_active.is_(True),
                        )
                        .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
                    )
                    if contract is None:
                        issues.append("missing_active_contract")
                        totals["missing_contract"] = int(totals["missing_contract"]) + 1

                    is_ready = not issues
                    if is_ready:
                        ready_count += 1
                        totals["artifacts_ready"] = int(totals["artifacts_ready"]) + 1
                    totals["blocking_issues"] = int(totals["blocking_issues"]) + len(issues)
                    totals["warnings"] = int(totals["warnings"]) + len(warnings)
                    artifact_entries.append(
                        {
                            "artifact_type": artifact_type,
                            "template_id": template_id,
                            "stage": expectation["stage"],
                            "template_present": template is not None,
                            "active_version_count": len(versions),
                            "active_binding_present": active_binding is not None,
                            "contract_present": contract is not None,
                            "promotion_gate": expectation["promotion_gate"],
                            "ai_policy": expectation["ai_policy"],
                            "ready": is_ready,
                            "issues": issues,
                            "warnings": warnings,
                        }
                    )

                if audience is not None and audience.is_active and ready_count == len(expected_by_audience[audience_id]):
                    totals["audiences_ready"] = int(totals["audiences_ready"]) + 1
                audiences.append(
                    {
                        "audience_id": audience_id,
                        "audience_present": audience is not None and bool(getattr(audience, "is_active", False)),
                        "expected_artifacts": len(expected_by_audience[audience_id]),
                        "ready_artifacts": ready_count,
                        "missing_artifacts": [
                            entry["artifact_type"] for entry in artifact_entries if not bool(entry["ready"])
                        ],
                        "issues": audience_issues,
                        "warnings": audience_warnings,
                        "artifacts": artifact_entries,
                    }
                )

            status = (
                "pass"
                if int(totals["blocking_issues"]) == 0 and (int(totals["warnings"]) == 0 or not fail_on_warnings)
                else "fail"
            )
            summary = {"status": status, "totals": totals, "audiences": audiences}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={status}",
                            f"audiences_expected={totals['audiences_expected']}",
                            f"audiences_present={totals['audiences_present']}",
                            f"audiences_ready={totals['audiences_ready']}",
                            f"artifacts_expected={totals['artifacts_expected']}",
                            f"artifacts_ready={totals['artifacts_ready']}",
                            f"blocking_issues={totals['blocking_issues']}",
                            f"warnings={totals['warnings']}",
                            f"missing_audience={totals['missing_audience']}",
                            f"missing_template={totals['missing_template']}",
                            f"missing_active_version={totals['missing_active_version']}",
                            f"missing_binding={totals['missing_binding']}",
                            f"missing_contract={totals['missing_contract']}",
                        ]
                    )
                )
            return 1 if status == "fail" else 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def audit_expression_target_roadmap(
    *,
    json_output: bool,
    fail_on_hard_bugs: bool,
    fail_on_product_risk: bool,
) -> int:
    summary = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    exit_code = audit_expression_target_roadmap_exit_code(
        summary,
        fail_on_hard_bugs=fail_on_hard_bugs,
        fail_on_product_risk=fail_on_product_risk,
    )
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        print(
            "\n".join(
                [
                    f"status={summary.get('status')}",
                    f"schema_version={summary.get('schema_version')}",
                    f"registry_artifacts={totals.get('registry_artifacts')}",
                    f"future_targets={totals.get('future_targets')}",
                    f"gaps={totals.get('gaps')}",
                    f"hard_bug={totals.get('hard_bug')}",
                    f"product_risk={totals.get('product_risk')}",
                    f"future={totals.get('future')}",
                ]
            )
        )
    return exit_code


def _count_grouped(db, model, column) -> dict[str, int]:
    rows = db.execute(select(column, func.count(model.id)).group_by(column)).all()
    return {str(key): int(count or 0) for key, count in rows}


def expression_golden_replay(
    *,
    company_id: str | None,
    artifact_type: str | None,
    limit: int,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = run_expression_golden_replay(
                db,
                company_id=company_id,
                artifact_type=artifact_type,
                limit=limit,
            )
            db.commit()
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary.get('status')}",
                            f"eval_run_id={summary.get('eval_run_id')}",
                            f"run_status={summary.get('run_status')}",
                            f"cases={totals.get('cases')}",
                            f"passed={totals.get('passed')}",
                            f"failed={totals.get('failed')}",
                            f"skipped={totals.get('skipped')}",
                        ]
                    )
                )
            return 1 if summary.get("status") == "fail" else 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_revalidate_artifacts(
    *,
    artifact_type: str,
    company_id: str | None,
    limit: int,
    apply: bool,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    limit = max(1, min(int(limit), 1000))
    pass_like_statuses = ("shadow_valid", "shadow_fallback_valid", "promoted_valid")
    try:
        with session_maker() as db:
            stmt = (
                select(ExpressionArtifact)
                .where(
                    ExpressionArtifact.artifact_type == artifact_type,
                    ExpressionArtifact.promoted.is_(False),
                    ExpressionArtifact.validation_status.in_(pass_like_statuses),
                )
                .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
                .limit(limit)
            )
            if company_id:
                stmt = stmt.where(ExpressionArtifact.company_id == company_id)
            artifacts = list(db.scalars(stmt))

            runs: list[dict[str, object]] = []
            totals = {
                "artifacts_selected": len(artifacts),
                "would_update": 0,
                "updated": 0,
                "unchanged": 0,
                "missing_context": 0,
                "promoted_skipped": 0,
            }
            for artifact in artifacts:
                if artifact.promoted:
                    totals["promoted_skipped"] += 1
                    continue
                payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
                snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id)
                audience = db.get(ExpressionAudience, artifact.audience_id)
                contract = _resolve_replay_contract(db, artifact)
                missing = []
                if snapshot is None:
                    missing.append("fact_snapshot")
                if audience is None:
                    missing.append("audience")
                if contract is None:
                    missing.append("contract")
                if missing:
                    totals["missing_context"] += 1
                    runs.append(
                        {
                            "artifact_id": artifact.id,
                            "previous_validation_status": artifact.validation_status,
                            "action": "missing_context",
                            "missing": missing,
                        }
                    )
                    continue

                previous_status = artifact.validation_status
                facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
                errors = _validate_expression_payload(
                    db,
                    audience=audience,
                    contract=contract,
                    payload=payload,
                    language=artifact.language,
                    facts=facts,
                    locked_baseline=_locked_baseline_for_replay(
                        artifact_type=artifact.artifact_type,
                        snapshot=snapshot,
                        payload=payload,
                    ),
                )
                should_update = bool(errors)
                action = "would_mark_shadow_invalid" if should_update else "unchanged"
                if should_update:
                    totals["would_update"] += 1
                    if apply:
                        artifact.validation_status = "shadow_invalid"
                        artifact.validation_errors_json = errors
                        db.add(artifact)
                        totals["updated"] += 1
                        action = "marked_shadow_invalid"
                else:
                    totals["unchanged"] += 1
                runs.append(
                    {
                        "artifact_id": artifact.id,
                        "company_id": artifact.company_id,
                        "artifact_type": artifact.artifact_type,
                        "previous_validation_status": previous_status,
                        "current_validation_status": artifact.validation_status,
                        "promoted": artifact.promoted,
                        "action": action,
                        "error_count": len(errors),
                        "errors": errors,
                    }
                )
            if apply:
                db.commit()
            else:
                db.rollback()

            summary = {
                "status": "applied" if apply else "dry_run",
                "artifact_type": artifact_type,
                "company_id": company_id,
                "guardrails": {
                    "uses_ai": False,
                    "writes_database": apply,
                    "uses_filesystem_scan": False,
                    "touches_promoted_artifacts": False,
                    "raw_output_included": False,
                    "mutates_payload": False,
                },
                "totals": totals,
                "runs": runs,
            }
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_type={artifact_type}",
                            f"artifacts_selected={totals['artifacts_selected']}",
                            f"would_update={totals['would_update']}",
                            f"updated={totals['updated']}",
                            f"unchanged={totals['unchanged']}",
                            f"missing_context={totals['missing_context']}",
                        ]
                    )
                )
            return 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _registry_prompt_contract_state(db, expectation: dict[str, object]) -> dict[str, object]:
    artifact_type = str(expectation["artifact_type"])
    template_id = str(expectation["template_id"])
    audience_id = str(expectation["audience_id"])
    issues: list[str] = []
    warnings: list[str] = []

    template = db.get(ExpressionPromptTemplate, template_id)
    if template is None:
        issues.append("missing_template")
    elif template.stage != str(expectation["stage"]):
        issues.append("stage_mismatch")

    audience = db.get(ExpressionAudience, audience_id)
    if audience is None or not audience.is_active:
        issues.append("audience_missing_or_inactive")

    versions: list[ExpressionPromptVersion] = []
    if template is not None:
        versions = list(
            db.scalars(
                select(ExpressionPromptVersion)
                .where(
                    ExpressionPromptVersion.template_id == template_id,
                    ExpressionPromptVersion.status == "active",
                )
                .order_by(ExpressionPromptVersion.created_at.desc(), ExpressionPromptVersion.id.desc())
            )
        )
    if not versions:
        issues.append("missing_active_version")

    active_version_ids = {version.id for version in versions}
    bindings: list[ExpressionPromptBinding] = []
    if template is not None:
        bindings = list(
            db.scalars(
                select(ExpressionPromptBinding)
                .where(
                    ExpressionPromptBinding.template_id == template_id,
                    ExpressionPromptBinding.scope_type == "global",
                )
                .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
            )
        )
    active_binding = next((binding for binding in bindings if binding.prompt_version_id in active_version_ids), None)
    if active_binding is None:
        issues.append("missing_binding_to_active_version")

    contract = db.scalar(
        select(ExpressionOutputContract)
        .where(
            ExpressionOutputContract.artifact_type == artifact_type,
            ExpressionOutputContract.audience_id == audience_id,
            ExpressionOutputContract.is_active.is_(True),
        )
        .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
    )
    missing_paths: list[str] = []
    forbidden_claim_count = 0
    if contract is None:
        issues.append("missing_active_contract")
    else:
        _, missing_paths = _contains_all_required_paths(
            contract.required_fact_paths_json,
            list(expectation["required_fact_paths"]),
        )
        if missing_paths:
            issues.append("missing_required_fact_paths")
        forbidden_claim_count = _count_list(contract.forbidden_claims_json)
        if forbidden_claim_count < int(expectation["min_forbidden_claims"]):
            issues.append("missing_forbidden_claims")

    phrase_count = 0
    if audience is not None and audience.forbidden_phrase_set_id:
        phrase_count = int(
            db.scalar(
                select(func.count(ExpressionForbiddenPhrase.id)).where(
                    ExpressionForbiddenPhrase.set_id == audience.forbidden_phrase_set_id,
                    ExpressionForbiddenPhrase.is_active.is_(True),
                )
            )
            or 0
        )
    if bool(expectation["requires_forbidden_phrases"]) and phrase_count == 0:
        issues.append("missing_forbidden_phrases")

    status_rows = db.execute(
        select(ExpressionArtifact.validation_status, func.count(ExpressionArtifact.id))
        .where(ExpressionArtifact.artifact_type == artifact_type)
        .group_by(ExpressionArtifact.validation_status)
    ).all()

    return {
        "active_version_ids": sorted(active_version_ids),
        "active_binding_id": active_binding.id if active_binding is not None else None,
        "contract_id": contract.id if contract is not None else None,
        "required_fact_paths_present": missing_paths == [],
        "missing_required_fact_paths": missing_paths,
        "forbidden_claim_count": forbidden_claim_count,
        "forbidden_phrase_count": phrase_count,
        "artifact_status_counts": {str(status): int(count) for status, count in status_rows},
        "issues": issues,
        "warnings": warnings,
    }


def build_expression_layer_readiness_payload(db) -> dict[str, object]:
    roadmap = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    implementation_by_artifact = EXPRESSION_ARTIFACT_IMPLEMENTATION
    registry_gaps_by_artifact: dict[str, list[dict[str, object]]] = {}
    for gap in roadmap.get("registry_gaps", []):
        if isinstance(gap, dict):
            registry_gaps_by_artifact.setdefault(str(gap.get("artifact_type")), []).append(gap)

    replay_coverage = _registry_replay_coverage(_latest_golden_replay_artifact_type_counts(db))
    replay_by_artifact = {
        str(row.get("artifact_type")): row
        for row in replay_coverage.get("artifacts", [])
        if isinstance(row, dict)
    }

    artifacts: list[dict[str, object]] = []
    totals = {
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_shadow_verified": 0,
        "artifacts_blocked": 0,
        "artifacts_with_product_risk": 0,
        "artifacts_replay_covered": int((replay_coverage.get("totals") or {}).get("artifacts_covered") or 0)
        if isinstance(replay_coverage.get("totals"), dict)
        else 0,
        "artifacts_with_replay_failures": int((replay_coverage.get("totals") or {}).get("artifacts_with_failures") or 0)
        if isinstance(replay_coverage.get("totals"), dict)
        else 0,
        "missing_replay_samples": int((replay_coverage.get("totals") or {}).get("missing_samples") or 0)
        if isinstance(replay_coverage.get("totals"), dict)
        else 0,
        "registry_blocking_issues": 0,
    }

    for expectation in sorted(EXPRESSION_REGISTRY_EXPECTATIONS, key=lambda item: str(item["artifact_type"])):
        artifact_type = str(expectation["artifact_type"])
        registry_state = _registry_prompt_contract_state(db, expectation)
        replay_state = replay_by_artifact.get(artifact_type, {"status": "missing_sample", "cases": 0, "passed": 0, "failed": 0, "skipped": 0})
        roadmap_gaps = registry_gaps_by_artifact.get(artifact_type, [])
        hard_gaps = [gap for gap in roadmap_gaps if str(gap.get("risk_class")) == "hard_bug"]
        product_gaps = [gap for gap in roadmap_gaps if str(gap.get("risk_class")) == "product_risk"]
        blockers = list(registry_state["issues"])
        if str(replay_state.get("status")) != "covered":
            blockers.append(f"replay:{replay_state.get('status')}")
        if hard_gaps:
            blockers.append("roadmap:hard_bug")

        if blockers:
            readiness_status = "blocked"
            totals["artifacts_blocked"] += 1
        elif product_gaps:
            readiness_status = "shadow_verified_with_product_risk"
            totals["artifacts_shadow_verified"] += 1
            totals["artifacts_with_product_risk"] += 1
        else:
            readiness_status = "shadow_verified"
            totals["artifacts_shadow_verified"] += 1
        totals["registry_blocking_issues"] += len(registry_state["issues"])

        implementation = implementation_by_artifact.get(artifact_type, {})
        artifacts.append(
            {
                "artifact_type": artifact_type,
                "audience_id": str(expectation["audience_id"]),
                "stage": str(expectation.get("stage")),
                "readiness_status": readiness_status,
                "blockers": blockers,
                "warnings": list(registry_state["warnings"]) + [str(gap.get("reason")) for gap in product_gaps],
                "template_id": str(expectation["template_id"]),
                "active_version_ids": registry_state["active_version_ids"],
                "active_binding_id": registry_state["active_binding_id"],
                "contract_id": registry_state["contract_id"],
                "required_fact_paths": list(expectation["required_fact_paths"]),
                "required_fact_paths_present": registry_state["required_fact_paths_present"],
                "missing_required_fact_paths": registry_state["missing_required_fact_paths"],
                "forbidden_claim_count": registry_state["forbidden_claim_count"],
                "forbidden_phrase_count": registry_state["forbidden_phrase_count"],
                "artifact_status_counts": registry_state["artifact_status_counts"],
                "replay": replay_state,
                "promotion_gate": expectation["promotion_gate"],
                "ai_policy": expectation["ai_policy"],
                "deterministic_fallback": bool(implementation.get("deterministic_fallback")),
                "shadow_generator": implementation.get("shadow_generator"),
                "production_gate_step": implementation.get("production_gate_step"),
                "promoted_read_surfaces": list(implementation.get("promoted_read_surfaces") or []),
            }
        )

    return {
        "status": "pass" if int(totals["artifacts_blocked"]) == 0 else "fail",
        "schema_version": "expression_layer_readiness_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "raw_output_included": False,
            "does_not_promote_artifacts": True,
        },
        "totals": totals,
        "artifacts": artifacts,
        "registry_replay_coverage": replay_coverage,
        "roadmap_totals": roadmap.get("totals") if isinstance(roadmap.get("totals"), dict) else {},
    }


def expression_audit_layer_readiness(
    *,
    json_output: bool,
    fail_on_product_risk: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_layer_readiness_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if fail_on_product_risk and int(totals.get("artifacts_with_product_risk") or 0) > 0:
                summary["status"] = "fail"
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifacts_expected={totals.get('artifacts_expected')}",
                            f"artifacts_shadow_verified={totals.get('artifacts_shadow_verified')}",
                            f"artifacts_blocked={totals.get('artifacts_blocked')}",
                            f"artifacts_with_product_risk={totals.get('artifacts_with_product_risk')}",
                            f"artifacts_replay_covered={totals.get('artifacts_replay_covered')}",
                            f"missing_replay_samples={totals.get('missing_replay_samples')}",
                            f"artifacts_with_replay_failures={totals.get('artifacts_with_replay_failures')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _artifact_promotion_status_counts(db, artifact_type: str) -> dict[str, dict[str, int]]:
    rows = db.execute(
        select(ExpressionArtifact.promoted, ExpressionArtifact.validation_status, func.count(ExpressionArtifact.id))
        .where(ExpressionArtifact.artifact_type == artifact_type)
        .group_by(ExpressionArtifact.promoted, ExpressionArtifact.validation_status)
    ).all()
    counts = {
        "promoted": {},
        "shadow": {},
    }
    for promoted, validation_status, count in rows:
        bucket = "promoted" if bool(promoted) else "shadow"
        status = str(validation_status or "unknown")
        counts[bucket][status] = int(count or 0)
    return counts


def build_expression_promotion_surface_readiness_payload(db) -> dict[str, object]:
    roadmap = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    implementation_by_artifact = {
        str(row.get("artifact_type")): row
        for row in roadmap.get("registry_implementation", [])
        if isinstance(row, dict)
    }

    artifacts: list[dict[str, object]] = []
    totals = {
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "promotion_surface_verified": 0,
        "shadow_only_no_surface": 0,
        "promotion_surface_needs_review": 0,
        "blocked": 0,
        "promoted_valid_artifacts": 0,
        "promoted_non_visible_artifacts": 0,
        "unexpected_promoted_artifacts": 0,
    }

    for expectation in sorted(EXPRESSION_REGISTRY_EXPECTATIONS, key=lambda item: str(item["artifact_type"])):
        artifact_type = str(expectation["artifact_type"])
        implementation = implementation_by_artifact.get(artifact_type, {})
        expected_surfaces = list(implementation.get("promoted_read_surfaces") or [])
        verified_surfaces = EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS.get(artifact_type, [])
        status_counts = _artifact_promotion_status_counts(db, artifact_type)
        promoted_counts = status_counts["promoted"]
        promoted_valid_count = int(promoted_counts.get("promoted_valid") or 0)
        promoted_total = sum(int(value) for value in promoted_counts.values())
        promoted_non_visible_count = max(0, promoted_total - promoted_valid_count)

        blockers: list[str] = []
        warnings: list[str] = []
        if expected_surfaces and not verified_surfaces:
            blockers.append("missing_verified_promotion_surface")
        if expected_surfaces and promoted_non_visible_count > 0:
            warnings.append("promoted_artifacts_not_visible_due_to_validation_status")
        if not expected_surfaces and promoted_total > 0:
            warnings.append("promoted_artifacts_without_declared_read_surface")

        if blockers:
            readiness_status = "blocked"
            totals["blocked"] += 1
        elif expected_surfaces:
            readiness_status = "promotion_surface_verified"
            totals["promotion_surface_verified"] += 1
        elif warnings:
            readiness_status = "promotion_surface_needs_review"
            totals["promotion_surface_needs_review"] += 1
        else:
            readiness_status = "shadow_only_no_surface"
            totals["shadow_only_no_surface"] += 1

        totals["promoted_valid_artifacts"] += promoted_valid_count
        totals["promoted_non_visible_artifacts"] += promoted_non_visible_count
        if not expected_surfaces:
            totals["unexpected_promoted_artifacts"] += promoted_total

        artifacts.append(
            {
                "artifact_type": artifact_type,
                "audience_id": str(expectation["audience_id"]),
                "readiness_status": readiness_status,
                "blockers": blockers,
                "warnings": warnings,
                "declared_promoted_read_surfaces": expected_surfaces,
                "verified_surfaces": verified_surfaces,
                "artifact_status_counts": status_counts,
                "promoted_valid_count": promoted_valid_count,
                "promoted_non_visible_count": promoted_non_visible_count,
                "promotion_policy": {
                    "visible_status": "promoted_valid",
                    "shadow_artifacts_visible": False,
                    "model_raw_content_visible": False,
                    "requires_explicit_promotion": True,
                },
            }
        )

    return {
        "status": "pass" if int(totals["blocked"]) == 0 else "fail",
        "schema_version": "expression_promotion_surface_readiness_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "raw_output_included": False,
            "does_not_promote_artifacts": True,
            "source_tables": ["expression_artifacts"],
        },
        "totals": totals,
        "artifacts": artifacts,
    }


def expression_audit_promotion_surfaces(
    *,
    json_output: bool,
    fail_on_review: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_promotion_surface_readiness_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if fail_on_review and int(totals.get("promotion_surface_needs_review") or 0) > 0:
                summary["status"] = "fail"
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"promotion_surface_verified={totals.get('promotion_surface_verified')}",
                            f"shadow_only_no_surface={totals.get('shadow_only_no_surface')}",
                            f"promotion_surface_needs_review={totals.get('promotion_surface_needs_review')}",
                            f"blocked={totals.get('blocked')}",
                            f"promoted_valid_artifacts={totals.get('promoted_valid_artifacts')}",
                            f"unexpected_promoted_artifacts={totals.get('unexpected_promoted_artifacts')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _prompt_digest(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _prompt_format_errors(user_prompt_template: str | None, *, extra_variables: dict[str, str]) -> list[str]:
    text = user_prompt_template or ""
    try:
        text.format(facts_json="{}", contract_schema_json="{}", **extra_variables)
    except KeyError as exc:
        return [f"unsupported_prompt_variable:{exc.args[0]}"]
    except (IndexError, ValueError) as exc:
        return [f"prompt_format_error:{exc.__class__.__name__}"]
    return []


def build_expression_prompt_catalog_readiness_payload(db) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    totals = {
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_checked": 0,
        "artifacts_ready": 0,
        "blocking_issues": 0,
        "missing_template": 0,
        "missing_active_version": 0,
        "missing_active_binding": 0,
        "missing_active_contract": 0,
        "missing_facts_placeholder": 0,
        "missing_contract_schema_placeholder": 0,
        "prompt_format_errors": 0,
        "contract_schema_issues": 0,
    }

    for expectation in sorted(EXPRESSION_REGISTRY_EXPECTATIONS, key=lambda item: str(item["artifact_type"])):
        artifact_type = str(expectation["artifact_type"])
        template_id = str(expectation["template_id"])
        audience_id = str(expectation["audience_id"])
        extra_variables = dict(EXPRESSION_PROMPT_EXTRA_VARIABLES.get(artifact_type, {}))
        issues: list[str] = []
        checks: dict[str, object] = {
            "has_facts_json_placeholder": False,
            "has_contract_schema_placeholder": False,
            "prompt_formats_with_runtime_variables": False,
            "contract_schema_object": False,
            "contract_required_fields_count": 0,
        }

        template = db.get(ExpressionPromptTemplate, template_id)
        if template is None:
            issues.append("missing_template")
            totals["missing_template"] += 1

        versions: list[ExpressionPromptVersion] = []
        active_binding: ExpressionPromptBinding | None = None
        if template is not None:
            versions = list(
                db.scalars(
                    select(ExpressionPromptVersion)
                    .where(
                        ExpressionPromptVersion.template_id == template_id,
                        ExpressionPromptVersion.status == "active",
                    )
                    .order_by(ExpressionPromptVersion.created_at.desc(), ExpressionPromptVersion.id.desc())
                )
            )
        if not versions:
            issues.append("missing_active_version")
            totals["missing_active_version"] += 1
            selected_version = None
        else:
            selected_version = versions[0]

        active_version_ids = {version.id for version in versions}
        if template is not None:
            bindings = list(
                db.scalars(
                    select(ExpressionPromptBinding)
                    .where(
                        ExpressionPromptBinding.template_id == template_id,
                        ExpressionPromptBinding.scope_type == "global",
                    )
                    .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
                )
            )
            active_binding = next((binding for binding in bindings if binding.prompt_version_id in active_version_ids), None)
        if active_binding is None:
            issues.append("missing_global_binding_to_active_version")
            totals["missing_active_binding"] += 1

        contract = db.scalar(
            select(ExpressionOutputContract)
            .where(
                ExpressionOutputContract.artifact_type == artifact_type,
                ExpressionOutputContract.audience_id == audience_id,
                ExpressionOutputContract.is_active.is_(True),
            )
            .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
        )
        if contract is None:
            issues.append("missing_active_contract")
            totals["missing_active_contract"] += 1
        else:
            schema = contract.json_schema if isinstance(contract.json_schema, dict) else {}
            checks["contract_schema_object"] = schema.get("type") == "object"
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            checks["contract_required_fields_count"] = len(required)
            if not checks["contract_schema_object"]:
                issues.append("contract_schema_not_object")
                totals["contract_schema_issues"] += 1

        prompt_metadata: dict[str, object] = {
            "active_version_id": selected_version.id if selected_version is not None else None,
            "active_binding_id": active_binding.id if active_binding is not None else None,
            "contract_id": contract.id if contract is not None else None,
            "system_prompt_chars": len((selected_version.system_prompt or "").strip()) if selected_version is not None else 0,
            "user_prompt_template_chars": len((selected_version.user_prompt_template or "").strip())
            if selected_version is not None
            else 0,
            "system_prompt_sha256_16": _prompt_digest(selected_version.system_prompt)
            if selected_version is not None
            else None,
            "user_prompt_template_sha256_16": _prompt_digest(selected_version.user_prompt_template)
            if selected_version is not None
            else None,
        }

        if selected_version is not None:
            system_prompt = (selected_version.system_prompt or "").strip()
            user_prompt_template = selected_version.user_prompt_template or ""
            if len(system_prompt) < 20:
                issues.append("system_prompt_too_short")
            if len(user_prompt_template.strip()) < 20:
                issues.append("user_prompt_template_too_short")
            checks["has_facts_json_placeholder"] = "{facts_json}" in user_prompt_template
            checks["has_contract_schema_placeholder"] = "{contract_schema_json}" in user_prompt_template
            if not checks["has_facts_json_placeholder"]:
                issues.append("missing_facts_json_placeholder")
                totals["missing_facts_placeholder"] += 1
            if not checks["has_contract_schema_placeholder"]:
                issues.append("missing_contract_schema_json_placeholder")
                totals["missing_contract_schema_placeholder"] += 1
            format_errors = _prompt_format_errors(user_prompt_template, extra_variables=extra_variables)
            if format_errors:
                issues.extend(format_errors)
                totals["prompt_format_errors"] += 1
            else:
                checks["prompt_formats_with_runtime_variables"] = True

        totals["artifacts_checked"] += 1
        totals["blocking_issues"] += len(issues)
        if not issues:
            totals["artifacts_ready"] += 1
        artifacts.append(
            {
                "artifact_type": artifact_type,
                "audience_id": audience_id,
                "template_id": template_id,
                "ready": not issues,
                "issues": issues,
                "prompt_metadata": prompt_metadata,
                "checks": checks,
                "allowed_extra_variables": sorted(extra_variables),
                "required_fact_paths": list(expectation["required_fact_paths"]),
                "promotion_gate": expectation["promotion_gate"],
                "ai_policy": expectation["ai_policy"],
            }
        )

    return {
        "status": "pass" if int(totals["blocking_issues"]) == 0 else "fail",
        "schema_version": "expression_prompt_catalog_readiness_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "source_tables": [
                "expression_prompt_templates",
                "expression_prompt_versions",
                "expression_prompt_bindings",
                "expression_output_contracts",
            ],
        },
        "totals": totals,
        "artifacts": artifacts,
    }


def expression_audit_prompt_catalog(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_prompt_catalog_readiness_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifacts_expected={totals.get('artifacts_expected')}",
                            f"artifacts_ready={totals.get('artifacts_ready')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"missing_active_version={totals.get('missing_active_version')}",
                            f"missing_active_binding={totals.get('missing_active_binding')}",
                            f"missing_active_contract={totals.get('missing_active_contract')}",
                            f"missing_facts_placeholder={totals.get('missing_facts_placeholder')}",
                            f"missing_contract_schema_placeholder={totals.get('missing_contract_schema_placeholder')}",
                            f"prompt_format_errors={totals.get('prompt_format_errors')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _registry_prompt_surface_row(
    expectation: dict[str, object],
    *,
    prompt_catalog_by_artifact: dict[str, dict[str, object]],
) -> dict[str, object]:
    artifact_type = str(expectation["artifact_type"])
    catalog_row = prompt_catalog_by_artifact.get(artifact_type, {})
    prompt_metadata = catalog_row.get("prompt_metadata") if isinstance(catalog_row.get("prompt_metadata"), dict) else {}
    implementation = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(artifact_type, {})
    return {
        "surface_id": f"expression_registry:{artifact_type}",
        "category": str(expectation.get("stage") or "expression"),
        "module": "app.services.expression",
        "functions": [str(implementation.get("shadow_generator") or "registry_runtime_not_declared")],
        "current_prompt_storage": "expression_prompt_registry",
        "current_result_storage": "expression_artifacts + fact_snapshots",
        "source_tables": ["expression_prompt_versions", "expression_prompt_bindings", "expression_artifacts", "fact_snapshots"],
        "db_backed_prompt_inputs": ["expression_prompt_versions.system_prompt", "expression_prompt_versions.user_prompt_template"],
        "target_template_id": str(expectation["template_id"]),
        "target_artifact_type": artifact_type,
        "migration_phase": "expression_registry",
        "risk_class": "implemented",
        "coverage_status": "registry_backed",
        "registry_ready": bool(catalog_row.get("ready")),
        "issues": list(catalog_row.get("issues") or []) if isinstance(catalog_row.get("issues"), list) else [],
        "validator": str(implementation.get("immutability_validator") or "missing_validator"),
        "fallback": "deterministic_fallback" if implementation.get("deterministic_fallback") else "missing_deterministic_fallback",
        "promotion_gate": str(expectation.get("promotion_gate") or ""),
        "migration_gate": "already registry-backed; keep prompt catalog and contract matrix passing",
        "prompt_metadata": {
            "active_version_id": prompt_metadata.get("active_version_id"),
            "active_binding_id": prompt_metadata.get("active_binding_id"),
            "contract_id": prompt_metadata.get("contract_id"),
            "system_prompt_sha256_16": prompt_metadata.get("system_prompt_sha256_16"),
            "user_prompt_template_sha256_16": prompt_metadata.get("user_prompt_template_sha256_16"),
        },
    }


def _legacy_prompt_surface_row(db, surface: dict[str, object]) -> dict[str, object]:
    target_artifact_type = surface.get("target_artifact_type")
    registry_matches_target = bool(target_artifact_type and target_artifact_type in EXPRESSION_ARTIFACT_IMPLEMENTATION)
    db_inputs = list(surface.get("db_backed_prompt_inputs") or [])
    target_template_id = str(surface.get("target_template_id") or "")
    active_shadow_version = (
        _resolve_active_prompt_version_for_template(db, template_id=target_template_id)
        if target_template_id
        else None
    )
    if active_shadow_version is not None:
        coverage_status = "db_prompt_shadow_ready"
    elif registry_matches_target:
        coverage_status = "partially_registry_backed"
    elif db_inputs:
        coverage_status = "legacy_db_input_backed"
    else:
        coverage_status = "code_inline"
    return {
        "surface_id": str(surface["surface_id"]),
        "category": str(surface["category"]),
        "module": str(surface["module"]),
        "functions": list(surface.get("functions") or []),
        "current_prompt_storage": str(surface["current_prompt_storage"]),
        "current_result_storage": str(surface["current_result_storage"]),
        "source_tables": list(surface.get("source_tables") or []),
        "db_backed_prompt_inputs": db_inputs,
        "target_template_id": surface.get("target_template_id"),
        "target_artifact_type": target_artifact_type,
        "migration_phase": surface.get("migration_phase"),
        "risk_class": surface.get("risk_class"),
        "coverage_status": coverage_status,
        "registry_ready": registry_matches_target or active_shadow_version is not None,
        "issues": [],
        "validator": surface.get("validator"),
        "fallback": surface.get("fallback"),
        "promotion_gate": surface.get("promotion_gate"),
        "migration_gate": surface.get("migration_gate"),
        "prompt_metadata": {
            "prompt_text_included": False,
            "raw_output_included": False,
            "active_shadow_template_id": target_template_id or None,
            "active_shadow_version_id": active_shadow_version.id if active_shadow_version is not None else None,
        },
    }


def build_ai_prompt_surface_coverage_payload(db) -> dict[str, object]:
    prompt_catalog = build_expression_prompt_catalog_readiness_payload(db)
    prompt_catalog_by_artifact = _prompt_catalog_by_artifact(prompt_catalog)
    registry_rows = [
        _registry_prompt_surface_row(expectation, prompt_catalog_by_artifact=prompt_catalog_by_artifact)
        for expectation in sorted(EXPRESSION_REGISTRY_EXPECTATIONS, key=lambda item: str(item["artifact_type"]))
    ]
    legacy_rows = [
        _legacy_prompt_surface_row(db, surface)
        for surface in sorted(LEGACY_AI_PROMPT_SURFACE_EXPECTATIONS, key=lambda item: str(item["surface_id"]))
    ]
    rows = registry_rows + legacy_rows

    totals = {
        "surfaces_total": len(rows),
        "registry_backed": 0,
        "registry_ready": 0,
        "db_prompt_shadow_ready": 0,
        "partially_registry_backed": 0,
        "legacy_db_input_backed": 0,
        "code_inline": 0,
        "implemented": 0,
        "product_risk": 0,
        "future": 0,
        "blocking_issues": 0,
    }
    for row in rows:
        coverage_status = str(row.get("coverage_status") or "")
        risk_class = str(row.get("risk_class") or "")
        if coverage_status in totals:
            totals[coverage_status] += 1
        if risk_class in totals:
            totals[risk_class] += 1
        if row.get("registry_ready"):
            totals["registry_ready"] += 1
        if coverage_status == "registry_backed" and row.get("issues"):
            totals["blocking_issues"] += len(row.get("issues") or [])

    legacy_migration_order = [
        {
            "phase": "inventory",
            "status": "current",
            "gate": "this audit lists every known prompt surface without prompt text or runtime side effects",
        },
        {
            "phase": "registry_shadow",
            "status": "next",
            "gate": "add DB prompt templates and bindings behind a resolver while keeping code prompt behavior unchanged",
        },
        {
            "phase": "parity_experiment",
            "status": "future",
            "gate": "production shadow sample proves output shape, fact grounding, and queue safety match existing path",
        },
        {
            "phase": "controlled_cutover",
            "status": "future",
            "gate": "enable one prompt surface at a time with rollback to current code prompt",
        },
    ]
    return {
        "status": "pass" if int(totals["blocking_issues"]) == 0 else "fail",
        "schema_version": "ai_prompt_surface_coverage_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "changes_runtime_prompt_behavior": False,
            "exposes_user_visible_content": False,
        },
        "totals": totals,
        "registry_prompt_catalog_status": prompt_catalog.get("status"),
        "coverage_policy": {
            "registry_backed": "prompt body and binding are in expression prompt tables",
            "db_prompt_shadow_ready": "legacy runtime still uses code prompt, but an active DB prompt version exists for parity audits",
            "partially_registry_backed": "target artifact exists in registry, but legacy production path still has inline prompt behavior",
            "legacy_db_input_backed": "operator/project prompt input is stored in DB, while system prompt framing is still code-inline",
            "code_inline": "prompt body is still constructed from code constants/functions",
        },
        "migration_order": legacy_migration_order,
        "surfaces": rows,
    }


def expression_audit_ai_prompt_surface_coverage(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_ai_prompt_surface_coverage_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"surfaces_total={totals.get('surfaces_total')}",
                            f"registry_backed={totals.get('registry_backed')}",
                            f"registry_ready={totals.get('registry_ready')}",
                            f"db_prompt_shadow_ready={totals.get('db_prompt_shadow_ready')}",
                            f"partially_registry_backed={totals.get('partially_registry_backed')}",
                            f"legacy_db_input_backed={totals.get('legacy_db_input_backed')}",
                            f"code_inline={totals.get('code_inline')}",
                            f"product_risk={totals.get('product_risk')}",
                            f"future={totals.get('future')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _resolve_active_prompt_version_for_template(db, *, template_id: str) -> ExpressionPromptVersion | None:
    binding = db.scalar(
        select(ExpressionPromptBinding)
        .where(
            ExpressionPromptBinding.template_id == template_id,
            ExpressionPromptBinding.scope_type == "global",
        )
        .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
    )
    if binding is None:
        return None
    version = db.get(ExpressionPromptVersion, binding.prompt_version_id)
    if version is None or version.status != "active":
        return None
    return version


def _photo_field_analysis_schema_block() -> str:
    return "\n".join(
        [
            "{",
            '  "ai_summary": "", "labels": [], "defects": [], "scene_type": "",',
            '  "visible_objects": [], "materials": [], "equipment": [], "people_ppe": [],',
            '  "safety_observations": [], "quality_observations": [], "inventory_observations": [],',
            '  "water_or_housekeeping_observations": [], "recommended_actions": [],',
            '  "confidence_level": "high|medium|low", "evidence_limitations": []',
            "}",
        ]
    )


def _photo_invoice_analysis_schema_block() -> str:
    return "\n".join(
        [
            "{",
            '  "ai_summary": "English 2-3 sentence finance-useful summary",',
            '  "labels": ["short_lowercase_tags"],',
            '  "defects": ["specific visible audit gaps"],',
            '  "receipt_facts": {"document_type": "", "vendor": "", "date": "", "total_amount": "", "currency": "", "tax_amount": "", "fuel_gallons": "", "unit_price": "", "location_hint": "", "payment_hint": "", "employee_or_project_hint": ""},',
            '  "financial_anomaly_flags": ["receipt/audit concerns, or empty array"],',
            '  "missing_evidence": ["important supporting evidence not visible, or empty array"],',
            '  "recommended_actions": ["finance next actions sorted by priority"],',
            '  "confidence_level": "high|medium|low",',
            '  "evidence_limitations": ["why extraction may be limited"]',
            "}",
        ]
    )


def _photo_field_analysis_optional_sections(
    *,
    project_prompt: str | None,
    annotation_contexts: list[str] | None,
    operator_prompt: str | None,
) -> str:
    sections: list[str] = []
    project_prompt_text = _project_prompt_text(project_prompt)
    normalized_annotations = _normalized_annotation_contexts(annotation_contexts)
    normalized_operator_prompt = normalize_operator_prompt(operator_prompt)
    if project_prompt_text:
        sections.extend(
            [
                "Project-specific audit priorities (sanitized; not evidence):",
                project_prompt_text,
                "- Treat these priorities as review focus areas, not conclusions.",
                "- Do not copy words from project guidance into output unless the attached image visibly supports them.",
            ]
        )
    if normalized_annotations:
        sections.extend(
            [
                "Public annotation context:",
                *[f"- {item}" for item in normalized_annotations],
                "- Treat annotation context as hints only; visible evidence in the image must remain the source of truth.",
            ]
        )
    if normalized_operator_prompt:
        sections.extend(
            [
                "Operator guidance:",
                normalized_operator_prompt,
                "- Apply operator guidance only when it remains consistent with visible evidence in the image.",
            ]
        )
    return ("\n" + "\n".join(sections)) if sections else ""


def _render_legacy_photo_prompt_from_registry(
    db,
    *,
    template_id: str,
    schema_block: str,
    photo_type: PhotoType,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None = None,
    custom_prompt: str | None = None,
    annotation_contexts: list[str] | None = None,
) -> tuple[str | None, dict[str, object]]:
    version = _resolve_active_prompt_version_for_template(db, template_id=template_id)
    if version is None:
        return None, {
            "template_id": template_id,
            "prompt_version_id": None,
            "active_version_found": False,
            "issues": ["missing_active_db_prompt_version"],
        }
    note_context = note.strip() if note else ""
    try:
        prompt = version.user_prompt_template.format(
            schema_block=schema_block,
            photo_type=photo_type.value,
            project_id=project_id or "",
            note=note_context,
            optional_sections=_photo_field_analysis_optional_sections(
                project_prompt=project_prompt,
                annotation_contexts=annotation_contexts,
                operator_prompt=custom_prompt,
            ),
        )
    except KeyError as exc:
        return None, {
            "template_id": template_id,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"unsupported_prompt_variable:{exc.args[0]}"],
        }
    except (IndexError, ValueError) as exc:
        return None, {
            "template_id": template_id,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"prompt_format_error:{exc.__class__.__name__}"],
        }
    return prompt, {
        "template_id": template_id,
        "prompt_version_id": version.id,
        "active_version_found": True,
        "issues": [],
        "prompt_sha256_16": _prompt_digest(prompt),
    }


def build_photo_field_analysis_db_shadow_prompt(
    db,
    *,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None = None,
    custom_prompt: str | None = None,
    annotation_contexts: list[str] | None = None,
) -> tuple[str | None, dict[str, object]]:
    return _render_legacy_photo_prompt_from_registry(
        db,
        template_id=PHOTO_FIELD_ANALYSIS_TEMPLATE_ID,
        schema_block=_photo_field_analysis_schema_block(),
        photo_type=PhotoType.project,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )


def build_photo_invoice_analysis_db_shadow_prompt(
    db,
    *,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None = None,
    custom_prompt: str | None = None,
    annotation_contexts: list[str] | None = None,
) -> tuple[str | None, dict[str, object]]:
    return _render_legacy_photo_prompt_from_registry(
        db,
        template_id=PHOTO_INVOICE_ANALYSIS_TEMPLATE_ID,
        schema_block=_photo_invoice_analysis_schema_block(),
        photo_type=PhotoType.invoice,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )


def build_video_frame_insight_db_shadow_prompt(
    db,
    *,
    asset: MediaAsset,
    custom_prompt: str | None = None,
) -> tuple[str | None, dict[str, object]]:
    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    project_id = str(metadata_json.get("project_id") or "").strip() or None
    project_prompt = None
    if project_id:
        project = db.scalar(
            select(Project).where(
                Project.project_id == project_id,
                Project.company_id == asset.company_id,
            )
        )
        if project is not None:
            project_prompt = _project_prompt_text(project.image_video_ai_prompt)
    return _render_legacy_photo_prompt_from_registry(
        db,
        template_id=VIDEO_FRAME_INSIGHT_TEMPLATE_ID,
        schema_block=_photo_field_analysis_schema_block(),
        photo_type=PhotoType.project,
        project_id=project_id,
        note=str(metadata_json.get("note") or "").strip() or None,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=[],
    )


def _progress_report_example_schema(photos: list[Photo]) -> str:
    example_schema = {
        "executive_summary": "用 2 到 4 句话总结现场整体进展、当前风险和最重要的结论。",
        "overall_progress_percent": 65,
        "overall_status": "at_risk",
        "confidence_level": "medium",
        "manager_brief": "给项目经理的简短结论，突出是否需要立即介入。",
        "angle_bias_notes": [
            "如果拍摄角度或距离差异可能影响判断，就明确写在这里。没有则返回空数组。"
        ],
        "key_changes": [
            "按整体层面总结照片序列中最关键的变化。"
        ],
        "work_completed": [
            "已经明显完成或推进的工作项。"
        ],
        "work_remaining": [
            "仍未完成、遗漏或需要补做的工作项。"
        ],
        "safety_risks": [
            "明确可见的安全风险。没有则返回空数组。"
        ],
        "quality_risks": [
            "返工、质量偏差、材料堆放或工序衔接风险。没有则返回空数组。"
        ],
        "material_inventory_signals": [
            "材料库存、设备、工具、临时设施、堆放数量或短缺线索；不能确认数量时必须说明不能确认。"
        ],
        "water_housekeeping_signals": [
            "积水、泥泞、垃圾、通道阻挡、清洁度或现场整理问题；没有则返回空数组。"
        ],
        "uncertain_items": [
            "无法高把握识别的物体或设备，说明可见特征和不确定原因。"
        ],
        "evidence_limitations": [
            "限制结论可靠性的证据不足点，例如角度变化、遮挡、缺少近景、照片数量不足。"
        ],
        "immediate_decisions": [
            "项目经理现在可以直接做出的决定，必须基于证据且按紧急程度排序。"
        ],
        "recommended_actions": [
            "下一步建议，按优先级排序。"
        ],
        "timeline_observations": [
            {
                "photo_id": photos[0].id,
                "captured_at": to_utc_iso(photos[0].captured_at_utc) or "unknown",
                "observation": "结合该图说明看到了什么，以及它和前后图相比代表了什么变化。",
                "progress_signal": "该图体现的进度推进或停滞判断。",
                "risk_signal": "该图暴露出的主要风险或注意事项。没有则写“none”。",
            }
        ],
    }
    return json.dumps(example_schema, ensure_ascii=False, indent=2)


def _progress_report_context_section(title: str, values: list[str] | None) -> str:
    normalized_values = [str(value).strip() for value in values or [] if str(value).strip()]
    if not normalized_values:
        return ""
    return "\n".join([title, *normalized_values, ""]) + "\n"


def _progress_report_photo_metadata_lines(photos: list[Photo]) -> str:
    lines: list[str] = []
    for index, photo in enumerate(photos, start=1):
        lines.append(
            (
                f"[图片{index}] photo_id: {photo.id}, 时间: {to_utc_iso(photo.captured_at_utc) or 'unknown'}, "
                f"GPS: {_format_optional_number(photo.gps_lat)},{_format_optional_number(photo.gps_lon)}, "
                f"相机角度: {_format_angle_metadata(photo)}, "
                f"位置描述: {photo.location or 'unknown'}, "
                f"文件名: {photo.original_file_name or 'unknown'}"
            )
        )
    return "\n".join(lines)


def build_progress_report_multi_image_db_shadow_prompt(
    db,
    project: Project,
    photos: list[Photo],
    *,
    custom_prompt: str | None = None,
    existing_ai_contexts: list[str] | None = None,
    preferred_ai_hints: list[str] | None = None,
) -> tuple[str | None, dict[str, object]]:
    version = _resolve_active_prompt_version_for_template(
        db,
        template_id=PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
    )
    if version is None:
        return None, {
            "template_id": PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
            "prompt_version_id": None,
            "active_version_found": False,
            "issues": ["missing_active_db_prompt_version"],
        }

    normalized_custom_prompt = _normalize_progress_custom_prompt(custom_prompt)
    try:
        prompt = version.user_prompt_template.format(
            project_prompt_text=_progress_prompt_text(project),
            custom_prompt_instruction=(
                f"操作员追加说明：{normalized_custom_prompt}"
                if normalized_custom_prompt
                else "如果操作员没有追加说明，就严格依据项目审查标准生成报告。"
            ),
            example_schema_json=_progress_report_example_schema(photos),
            existing_ai_contexts_section=_progress_report_context_section(
                "既有单图 AI 识别结果（仅作弱约束参考）：",
                existing_ai_contexts,
            ),
            preferred_ai_hints_section=_progress_report_context_section(
                "跨图片既有 AI 一致性提示（若当前图像没有明显相反证据，请优先沿用这些命名）：",
                preferred_ai_hints,
            ),
            photo_metadata_lines=_progress_report_photo_metadata_lines(photos),
        )
    except KeyError as exc:
        return None, {
            "template_id": PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"unsupported_prompt_variable:{exc.args[0]}"],
        }
    except (IndexError, ValueError) as exc:
        return None, {
            "template_id": PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"prompt_format_error:{exc.__class__.__name__}"],
        }

    return prompt, {
        "template_id": PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
        "prompt_version_id": version.id,
        "active_version_found": True,
        "issues": [],
        "prompt_sha256_16": _prompt_digest(prompt),
    }


def _progress_report_fixture_project(*, project_id: str = "P100") -> Project:
    return Project(
        project_id=project_id,
        company_id="default",
        project_name="North Lift Station",
        client_name="Example Client",
        location="North yard",
        image_video_ai_prompt="重点关注施工进度、可见材料、通道整理和积水情况。",
    )


def _progress_report_fixture_photos(*, count: int = 2) -> list[Photo]:
    captured_at = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    photos: list[Photo] = []
    for index in range(1, count + 1):
        photos.append(
            Photo(
                id=index,
                company_id="default",
                employee_id="E100",
                project_id="P100",
                photo_type=PhotoType.project,
                visibility=PhotoVisibility.internal,
                file_path=f"/fixtures/progress-photo-{index}.jpg",
                image_url=f"/media/progress-photo-{index}.jpg",
                original_file_name=f"progress-photo-{index}.jpg",
                captured_at_utc=captured_at + timedelta(minutes=15 * (index - 1)),
                location=f"north bay {index}",
                gps_lat=41.87 + (index / 1000),
                gps_lon=-87.62 - (index / 1000),
                heading=90 + index,
                pitch=-3.5 + index,
                roll=0.25 * index,
            )
        )
    return photos


def build_generated_report_markdown_legacy_db_shadow_prompt(
    db,
    photos: list[Photo],
    custom_prompt: str,
) -> tuple[str | None, dict[str, object]]:
    version = _resolve_active_prompt_version_for_template(
        db,
        template_id=GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
    )
    if version is None:
        return None, {
            "template_id": GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
            "prompt_version_id": None,
            "active_version_found": False,
            "issues": ["missing_active_db_prompt_version"],
        }

    try:
        prompt = version.user_prompt_template.format(
            custom_prompt=custom_prompt.strip(),
            photo_dataset_json=json.dumps(
                [_serialize_photo_for_report(photo) for photo in photos],
                ensure_ascii=False,
                indent=2,
            ),
        )
    except KeyError as exc:
        return None, {
            "template_id": GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"unsupported_prompt_variable:{exc.args[0]}"],
        }
    except (IndexError, ValueError) as exc:
        return None, {
            "template_id": GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
            "prompt_version_id": version.id,
            "active_version_found": True,
            "issues": [f"prompt_format_error:{exc.__class__.__name__}"],
        }

    return prompt, {
        "template_id": GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
        "prompt_version_id": version.id,
        "active_version_found": True,
        "issues": [],
        "prompt_sha256_16": _prompt_digest(prompt),
    }


def _generated_report_markdown_fixture_photos(*, count: int = 2, with_tags: bool = True) -> list[Photo]:
    captured_at = datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)
    photos: list[Photo] = []
    for index in range(1, count + 1):
        tag_json: dict[str, object] = {}
        if with_tags:
            tag_json = {
                "ai_summary": f"Visible conduit and staging area in photo {index}.",
                "labels": ["conduit", "staging", f"sequence_{index}"],
                "defects": ["open trench"] if index == 2 else [],
            }
        photos.append(
            Photo(
                id=100 + index,
                company_id="default",
                employee_id=f"E10{index}",
                project_id="P100",
                photo_type=PhotoType.project,
                visibility=PhotoVisibility.client_visible if index == 1 else PhotoVisibility.internal,
                approval_status=ApprovalStatus.approved if index == 1 else ApprovalStatus.pending,
                file_path=f"/fixtures/generated-report-photo-{index}.jpg",
                image_url=f"/media/generated-report-photo-{index}.jpg",
                original_file_name=f"generated-report-photo-{index}.jpg",
                captured_at_utc=captured_at + timedelta(minutes=20 * (index - 1)),
                location=f"report bay {index}",
                gps=f"41.88{index},-87.63{index}",
                note=f"Field note {index}",
                tag_json=tag_json,
            )
        )
    return photos


def _photo_field_analysis_parity_case(
    db,
    *,
    case_id: str,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None,
    custom_prompt: str | None,
    annotation_contexts: list[str] | None,
) -> dict[str, object]:
    legacy_prompt = build_ai_prompt_for_context(
        photo_type=PhotoType.project,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )
    db_prompt, metadata = build_photo_field_analysis_db_shadow_prompt(
        db,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )
    legacy_digest = _prompt_digest(legacy_prompt)
    db_digest = _prompt_digest(db_prompt)
    issues = list(metadata.get("issues") or [])
    if db_prompt is None:
        issues.append("db_prompt_not_rendered")
    elif legacy_prompt != db_prompt:
        issues.append("prompt_digest_mismatch")
    return {
        "case_id": case_id,
        "project_id_present": bool(project_id),
        "note_present": bool(str(note or "").strip()),
        "project_prompt_present": bool(str(project_prompt or "").strip()),
        "custom_prompt_present": bool(str(custom_prompt or "").strip()),
        "annotation_count": len(annotation_contexts or []),
        "legacy_prompt_sha256_16": legacy_digest,
        "db_prompt_sha256_16": db_digest,
        "prompt_chars": len(legacy_prompt),
        "metadata": metadata,
        "issues": issues,
    }


def build_photo_field_analysis_prompt_parity_payload(db, *, include_fixture_cases: bool = True) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    if include_fixture_cases:
        fixture_inputs = [
            {
                "case_id": "project_basic",
                "project_id": "P100",
                "note": "North wall framing progress",
                "project_prompt": None,
                "custom_prompt": None,
                "annotation_contexts": None,
            },
            {
                "case_id": "project_guidance",
                "project_id": "P200",
                "note": "Check staging",
                "project_prompt": "Focus on staging, housekeeping, and visible safety gaps.",
                "custom_prompt": None,
                "annotation_contexts": None,
            },
            {
                "case_id": "annotations_and_operator",
                "project_id": "P300",
                "note": "Crew area",
                "project_prompt": "Track visible materials and completed work areas.",
                "custom_prompt": "Pay attention to guardrails.",
                "annotation_contexts": ["public crack near north wall", "北墙附近有裂缝"],
            },
        ]
        for fixture in fixture_inputs:
            cases.append(_photo_field_analysis_parity_case(db, **fixture))

    blocking_issues = sum(1 for case in cases if case.get("issues"))
    active_version = _resolve_active_prompt_version_for_template(db, template_id=PHOTO_FIELD_ANALYSIS_TEMPLATE_ID)
    return {
        "status": "pass" if blocking_issues == 0 else "fail",
        "schema_version": PHOTO_FIELD_ANALYSIS_PARITY_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "changes_runtime_prompt_behavior": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "photo_types_covered": ["project"],
            "photo_types_not_covered": ["invoice", "video"],
        },
        "registry": {
            "template_id": PHOTO_FIELD_ANALYSIS_TEMPLATE_ID,
            "active_prompt_version_id": active_version.id if active_version is not None else None,
            "active_version_found": active_version is not None,
        },
        "totals": {
            "cases_checked": len(cases),
            "matching_cases": sum(1 for case in cases if not case.get("issues")),
            "blocking_issues": blocking_issues,
            "prompt_digest_mismatch": sum(1 for case in cases if "prompt_digest_mismatch" in case.get("issues", [])),
            "db_prompt_not_rendered": sum(1 for case in cases if "db_prompt_not_rendered" in case.get("issues", [])),
        },
        "migration_gate": {
            "current_phase": "registry_shadow",
            "next_gate": "run production sample parity without AI before any runtime resolver cutover",
            "fallback": "build_ai_prompt_for_context remains the production prompt source",
        },
        "cases": cases,
    }


def _photo_invoice_analysis_parity_case(
    db,
    *,
    case_id: str,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None,
    custom_prompt: str | None,
    annotation_contexts: list[str] | None,
) -> dict[str, object]:
    legacy_prompt = build_ai_prompt_for_context(
        photo_type=PhotoType.invoice,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )
    db_prompt, metadata = build_photo_invoice_analysis_db_shadow_prompt(
        db,
        project_id=project_id,
        note=note,
        project_prompt=project_prompt,
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )
    legacy_digest = _prompt_digest(legacy_prompt)
    db_digest = _prompt_digest(db_prompt)
    issues = list(metadata.get("issues") or [])
    if db_prompt is None:
        issues.append("db_prompt_not_rendered")
    elif legacy_prompt != db_prompt:
        issues.append("prompt_digest_mismatch")
    return {
        "case_id": case_id,
        "project_id_present": bool(project_id),
        "note_present": bool(str(note or "").strip()),
        "billing_prompt_present": bool(str(project_prompt or "").strip()),
        "custom_prompt_present": bool(str(custom_prompt or "").strip()),
        "annotation_count": len(annotation_contexts or []),
        "legacy_prompt_sha256_16": legacy_digest,
        "db_prompt_sha256_16": db_digest,
        "prompt_chars": len(legacy_prompt),
        "metadata": metadata,
        "issues": issues,
    }


def build_photo_invoice_analysis_prompt_parity_payload(db, *, include_fixture_cases: bool = True) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    if include_fixture_cases:
        fixture_inputs = [
            {
                "case_id": "invoice_basic",
                "project_id": "invoice",
                "note": "Fuel receipt",
                "project_prompt": None,
                "custom_prompt": None,
                "annotation_contexts": None,
            },
            {
                "case_id": "invoice_billing_guidance",
                "project_id": "P200",
                "note": "Vendor receipt for materials",
                "project_prompt": "Focus on vendor, total amount, date, and job number if readable.",
                "custom_prompt": None,
                "annotation_contexts": None,
            },
            {
                "case_id": "invoice_annotations_and_operator",
                "project_id": "P300",
                "note": "Receipt image from truck",
                "project_prompt": "Track fuel receipts and material purchases.",
                "custom_prompt": "Check whether gallons and unit price are visible.",
                "annotation_contexts": ["public note: top right corner is cropped", "金额区域较模糊"],
            },
        ]
        for fixture in fixture_inputs:
            cases.append(_photo_invoice_analysis_parity_case(db, **fixture))

    blocking_issues = sum(1 for case in cases if case.get("issues"))
    active_version = _resolve_active_prompt_version_for_template(db, template_id=PHOTO_INVOICE_ANALYSIS_TEMPLATE_ID)
    return {
        "status": "pass" if blocking_issues == 0 else "fail",
        "schema_version": PHOTO_INVOICE_ANALYSIS_PARITY_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "changes_runtime_prompt_behavior": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "photo_types_covered": ["invoice"],
            "photo_types_not_covered": ["project", "video"],
            "receipt_facts_mutated": False,
        },
        "registry": {
            "template_id": PHOTO_INVOICE_ANALYSIS_TEMPLATE_ID,
            "active_prompt_version_id": active_version.id if active_version is not None else None,
            "active_version_found": active_version is not None,
        },
        "totals": {
            "cases_checked": len(cases),
            "matching_cases": sum(1 for case in cases if not case.get("issues")),
            "blocking_issues": blocking_issues,
            "prompt_digest_mismatch": sum(1 for case in cases if "prompt_digest_mismatch" in case.get("issues", [])),
            "db_prompt_not_rendered": sum(1 for case in cases if "db_prompt_not_rendered" in case.get("issues", [])),
        },
        "migration_gate": {
            "current_phase": "registry_shadow",
            "next_gate": "run production invoice prompt parity without AI before any runtime resolver cutover",
            "fallback": "build_ai_prompt_for_context remains the production invoice prompt source",
        },
        "cases": cases,
    }


def _video_fixture_asset(*, asset_id: str, company_id: str, project_id: str | None, note: str | None) -> MediaAsset:
    metadata_json: dict[str, object] = {}
    if project_id is not None:
        metadata_json["project_id"] = project_id
    if note is not None:
        metadata_json["note"] = note
    return MediaAsset(
        asset_id=asset_id,
        tenant_id=company_id,
        company_id=company_id,
        uploaded_by_user_id=None,
        media_type="video",
        source="parity_fixture",
        file_path=f"/tmp/{asset_id}.mp4",
        original_file_name=f"{asset_id}.mp4",
        mime_type="video/mp4",
        file_size=0,
        metadata_json=metadata_json,
    )


def _video_frame_insight_parity_case(
    db,
    *,
    case_id: str,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None,
    custom_prompt: str | None,
) -> dict[str, object]:
    if project_prompt is None:
        asset = _video_fixture_asset(
            asset_id=f"video-parity-{case_id}",
            company_id="default",
            project_id=project_id,
            note=note,
        )
        legacy_prompt = build_media_asset_ai_prompt(db, asset, photo_type=PhotoType.project, custom_prompt=custom_prompt)
        db_prompt, metadata = build_video_frame_insight_db_shadow_prompt(db, asset=asset, custom_prompt=custom_prompt)
        legacy_builder = "build_media_asset_ai_prompt"
    else:
        legacy_prompt = build_ai_prompt_for_context(
            photo_type=PhotoType.project,
            project_id=project_id,
            note=note,
            project_prompt=project_prompt,
            custom_prompt=custom_prompt,
            annotation_contexts=[],
        )
        db_prompt, metadata = _render_legacy_photo_prompt_from_registry(
            db,
            template_id=VIDEO_FRAME_INSIGHT_TEMPLATE_ID,
            schema_block=_photo_field_analysis_schema_block(),
            photo_type=PhotoType.project,
            project_id=project_id,
            note=note,
            project_prompt=project_prompt,
            custom_prompt=custom_prompt,
            annotation_contexts=[],
        )
        legacy_builder = "build_ai_prompt_for_context_project_guidance_fixture"
    legacy_digest = _prompt_digest(legacy_prompt)
    db_digest = _prompt_digest(db_prompt)
    issues = list(metadata.get("issues") or [])
    if db_prompt is None:
        issues.append("db_prompt_not_rendered")
    elif legacy_prompt != db_prompt:
        issues.append("prompt_digest_mismatch")
    return {
        "case_id": case_id,
        "legacy_builder": legacy_builder,
        "project_id_present": bool(project_id),
        "note_present": bool(str(note or "").strip()),
        "project_prompt_present": bool(str(project_prompt or "").strip()),
        "custom_prompt_present": bool(str(custom_prompt or "").strip()),
        "annotation_count": 0,
        "legacy_prompt_sha256_16": legacy_digest,
        "db_prompt_sha256_16": db_digest,
        "prompt_chars": len(legacy_prompt),
        "metadata": metadata,
        "issues": issues,
    }


def build_video_frame_insight_prompt_parity_payload(db, *, include_fixture_cases: bool = True) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    if include_fixture_cases:
        fixture_inputs = [
            {
                "case_id": "video_basic_asset",
                "project_id": "P100",
                "note": "Frame around north wall",
                "project_prompt": None,
                "custom_prompt": None,
            },
            {
                "case_id": "video_operator_asset",
                "project_id": "P200",
                "note": "Walkthrough frame",
                "project_prompt": None,
                "custom_prompt": "Pay attention to visible housekeeping.",
            },
            {
                "case_id": "video_project_guidance_context",
                "project_id": "P300",
                "note": "Crew staging frame",
                "project_prompt": "Track visible materials and completed work areas.",
                "custom_prompt": "Pay attention to guardrails.",
            },
        ]
        for fixture in fixture_inputs:
            cases.append(_video_frame_insight_parity_case(db, **fixture))

    blocking_issues = sum(1 for case in cases if case.get("issues"))
    active_version = _resolve_active_prompt_version_for_template(db, template_id=VIDEO_FRAME_INSIGHT_TEMPLATE_ID)
    return {
        "status": "pass" if blocking_issues == 0 else "fail",
        "schema_version": VIDEO_FRAME_INSIGHT_PARITY_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "reads_video_file": False,
            "queues_video_task": False,
            "changes_runtime_prompt_behavior": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "photo_types_covered": ["project"],
            "media_types_covered": ["video"],
            "photo_types_not_covered": ["invoice"],
        },
        "registry": {
            "template_id": VIDEO_FRAME_INSIGHT_TEMPLATE_ID,
            "active_prompt_version_id": active_version.id if active_version is not None else None,
            "active_version_found": active_version is not None,
        },
        "totals": {
            "cases_checked": len(cases),
            "media_asset_builder_cases": sum(1 for case in cases if case.get("legacy_builder") == "build_media_asset_ai_prompt"),
            "matching_cases": sum(1 for case in cases if not case.get("issues")),
            "blocking_issues": blocking_issues,
            "prompt_digest_mismatch": sum(1 for case in cases if "prompt_digest_mismatch" in case.get("issues", [])),
            "db_prompt_not_rendered": sum(1 for case in cases if "db_prompt_not_rendered" in case.get("issues", [])),
        },
        "migration_gate": {
            "current_phase": "registry_shadow",
            "next_gate": "run production video prompt parity without AI before any worker resolver cutover",
            "fallback": "build_media_asset_ai_prompt remains the production video frame prompt source",
        },
        "cases": cases,
    }


def _progress_report_multi_image_parity_case(
    db,
    *,
    case_id: str,
    photo_count: int,
    custom_prompt: str | None,
    existing_ai_contexts: list[str] | None,
    preferred_ai_hints: list[str] | None,
) -> dict[str, object]:
    project = _progress_report_fixture_project(project_id="P100")
    photos = _progress_report_fixture_photos(count=photo_count)
    legacy_prompt = build_multi_image_progress_prompt(
        project,
        photos,
        custom_prompt=custom_prompt,
        existing_ai_contexts=existing_ai_contexts,
        preferred_ai_hints=preferred_ai_hints,
    )
    db_prompt, metadata = build_progress_report_multi_image_db_shadow_prompt(
        db,
        project,
        photos,
        custom_prompt=custom_prompt,
        existing_ai_contexts=existing_ai_contexts,
        preferred_ai_hints=preferred_ai_hints,
    )
    legacy_digest = _prompt_digest(legacy_prompt)
    db_digest = _prompt_digest(db_prompt)
    issues = list(metadata.get("issues") or [])
    if db_prompt is None:
        issues.append("db_prompt_not_rendered")
    elif legacy_prompt != db_prompt:
        issues.append("prompt_digest_mismatch")
    return {
        "case_id": case_id,
        "photo_count": len(photos),
        "custom_prompt_present": bool(_normalize_progress_custom_prompt(custom_prompt)),
        "existing_ai_context_count": len(existing_ai_contexts or []),
        "preferred_ai_hint_count": len(preferred_ai_hints or []),
        "first_photo_id": photos[0].id,
        "last_photo_id": photos[-1].id,
        "legacy_prompt_sha256_16": legacy_digest,
        "db_prompt_sha256_16": db_digest,
        "prompt_chars": len(legacy_prompt),
        "metadata": metadata,
        "issues": issues,
    }


def build_progress_report_multi_image_prompt_parity_payload(
    db,
    *,
    include_fixture_cases: bool = True,
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    if include_fixture_cases:
        fixture_inputs = [
            {
                "case_id": "two_photos_basic",
                "photo_count": 2,
                "custom_prompt": None,
                "existing_ai_contexts": None,
                "preferred_ai_hints": None,
            },
            {
                "case_id": "operator_custom_prompt",
                "photo_count": 2,
                "custom_prompt": "请重点看北侧通道是否被材料挡住。",
                "existing_ai_contexts": None,
                "preferred_ai_hints": None,
            },
            {
                "case_id": "existing_ai_contexts",
                "photo_count": 2,
                "custom_prompt": None,
                "existing_ai_contexts": [
                    "[existing_single_image_ai photo_id=1] summary=可见材料堆放在北侧墙边",
                    "[existing_single_image_ai photo_id=2] labels=pipe, trench, water",
                ],
                "preferred_ai_hints": None,
            },
            {
                "case_id": "preferred_ai_hints",
                "photo_count": 2,
                "custom_prompt": None,
                "existing_ai_contexts": None,
                "preferred_ai_hints": [
                    "[preferred_sequence_label] black rectangular device appears consistently across 2 selected photos",
                    "[preferred_sequence_label] trench appears consistently across 2 selected photos",
                ],
            },
            {
                "case_id": "combined_three_photo_sequence",
                "photo_count": 3,
                "custom_prompt": "对比前后照片时，请保守描述无法确认的设备。",
                "existing_ai_contexts": [
                    "[existing_single_image_ai photo_id=1] summary=入口附近可见临时材料",
                    "[existing_single_image_ai photo_id=3] materials=pipe sections; gravel",
                ],
                "preferred_ai_hints": [
                    "[preferred_sequence_label] black rectangular device appears consistently across 3 selected photos",
                ],
            },
        ]
        for fixture in fixture_inputs:
            cases.append(_progress_report_multi_image_parity_case(db, **fixture))

    blocking_issues = sum(1 for case in cases if case.get("issues"))
    active_version = _resolve_active_prompt_version_for_template(
        db,
        template_id=PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
    )
    return {
        "status": "pass" if blocking_issues == 0 else "fail",
        "schema_version": PROGRESS_REPORT_MULTI_IMAGE_PARITY_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "reads_image_files": False,
            "calls_generate_progress_report": False,
            "changes_runtime_prompt_behavior": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "photo_types_covered": ["project"],
            "report_types_covered": ["progress_report_multi_image"],
        },
        "registry": {
            "template_id": PROGRESS_REPORT_MULTI_IMAGE_TEMPLATE_ID,
            "active_prompt_version_id": active_version.id if active_version is not None else None,
            "active_version_found": active_version is not None,
        },
        "totals": {
            "cases_checked": len(cases),
            "matching_cases": sum(1 for case in cases if not case.get("issues")),
            "custom_prompt_cases": sum(1 for case in cases if case.get("custom_prompt_present")),
            "existing_ai_context_cases": sum(1 for case in cases if int(case.get("existing_ai_context_count") or 0) > 0),
            "preferred_ai_hint_cases": sum(1 for case in cases if int(case.get("preferred_ai_hint_count") or 0) > 0),
            "three_photo_sequence_cases": sum(1 for case in cases if int(case.get("photo_count") or 0) == 3),
            "blocking_issues": blocking_issues,
            "prompt_digest_mismatch": sum(1 for case in cases if "prompt_digest_mismatch" in case.get("issues", [])),
            "db_prompt_not_rendered": sum(1 for case in cases if "db_prompt_not_rendered" in case.get("issues", [])),
        },
        "migration_gate": {
            "current_phase": "registry_shadow",
            "next_gate": "run production sample parity without AI or image reads before any report resolver cutover",
            "fallback": "build_multi_image_progress_prompt remains the production progress report prompt source",
        },
        "cases": cases,
    }


def _generated_report_markdown_legacy_parity_case(
    db,
    *,
    case_id: str,
    photo_count: int,
    custom_prompt: str,
    with_tags: bool,
) -> dict[str, object]:
    photos = _generated_report_markdown_fixture_photos(count=photo_count, with_tags=with_tags)
    legacy_prompt = build_report_markdown_prompt(photos, custom_prompt)
    db_prompt, metadata = build_generated_report_markdown_legacy_db_shadow_prompt(
        db,
        photos,
        custom_prompt,
    )
    legacy_digest = _prompt_digest(legacy_prompt)
    db_digest = _prompt_digest(db_prompt)
    issues = list(metadata.get("issues") or [])
    if db_prompt is None:
        issues.append("db_prompt_not_rendered")
    elif legacy_prompt != db_prompt:
        issues.append("prompt_digest_mismatch")
    return {
        "case_id": case_id,
        "photo_count": len(photos),
        "custom_prompt_present": bool(custom_prompt.strip()),
        "tagged_photo_count": sum(1 for photo in photos if isinstance(photo.tag_json, dict) and photo.tag_json),
        "first_photo_id": photos[0].id if photos else None,
        "last_photo_id": photos[-1].id if photos else None,
        "legacy_prompt_sha256_16": legacy_digest,
        "db_prompt_sha256_16": db_digest,
        "prompt_chars": len(legacy_prompt),
        "metadata": metadata,
        "issues": issues,
    }


def build_generated_report_markdown_legacy_prompt_parity_payload(
    db,
    *,
    include_fixture_cases: bool = True,
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    if include_fixture_cases:
        fixture_inputs = [
            {
                "case_id": "single_photo_blank_instruction",
                "photo_count": 1,
                "custom_prompt": "   ",
                "with_tags": False,
            },
            {
                "case_id": "two_photos_custom_instruction",
                "photo_count": 2,
                "custom_prompt": "Focus on completed work, defects, and photo evidence.",
                "with_tags": True,
            },
            {
                "case_id": "three_photos_multilingual_instruction",
                "photo_count": 3,
                "custom_prompt": "请用正式语气总结现场进展，并引用 photo_id。",
                "with_tags": True,
            },
        ]
        for fixture in fixture_inputs:
            cases.append(_generated_report_markdown_legacy_parity_case(db, **fixture))

    blocking_issues = sum(1 for case in cases if case.get("issues"))
    active_version = _resolve_active_prompt_version_for_template(
        db,
        template_id=GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
    )
    return {
        "status": "pass" if blocking_issues == 0 else "fail",
        "schema_version": GENERATED_REPORT_MARKDOWN_LEGACY_PARITY_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "reads_image_files": False,
            "calls_process_generated_report": False,
            "changes_runtime_prompt_behavior": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "target_runtime_table": "generated_reports",
        },
        "registry": {
            "template_id": GENERATED_REPORT_MARKDOWN_LEGACY_TEMPLATE_ID,
            "active_prompt_version_id": active_version.id if active_version is not None else None,
            "active_version_found": active_version is not None,
        },
        "totals": {
            "cases_checked": len(cases),
            "matching_cases": sum(1 for case in cases if not case.get("issues")),
            "custom_prompt_cases": sum(1 for case in cases if case.get("custom_prompt_present")),
            "tagged_photo_cases": sum(1 for case in cases if int(case.get("tagged_photo_count") or 0) > 0),
            "blocking_issues": blocking_issues,
            "prompt_digest_mismatch": sum(1 for case in cases if "prompt_digest_mismatch" in case.get("issues", [])),
            "db_prompt_not_rendered": sum(1 for case in cases if "db_prompt_not_rendered" in case.get("issues", [])),
        },
        "migration_gate": {
            "current_phase": "registry_shadow",
            "next_gate": "run production generated report prompt parity without AI or image reads before any report cutover",
            "fallback": "build_report_markdown_prompt remains the production generated report prompt source",
        },
        "cases": cases,
    }


def expression_audit_photo_field_analysis_prompt_parity(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_photo_field_analysis_prompt_parity_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"cases_checked={totals.get('cases_checked')}",
                            f"matching_cases={totals.get('matching_cases')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"prompt_digest_mismatch={totals.get('prompt_digest_mismatch')}",
                            f"db_prompt_not_rendered={totals.get('db_prompt_not_rendered')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_photo_invoice_analysis_prompt_parity(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_photo_invoice_analysis_prompt_parity_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"cases_checked={totals.get('cases_checked')}",
                            f"matching_cases={totals.get('matching_cases')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"prompt_digest_mismatch={totals.get('prompt_digest_mismatch')}",
                            f"db_prompt_not_rendered={totals.get('db_prompt_not_rendered')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_video_frame_insight_prompt_parity(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_video_frame_insight_prompt_parity_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"cases_checked={totals.get('cases_checked')}",
                            f"media_asset_builder_cases={totals.get('media_asset_builder_cases')}",
                            f"matching_cases={totals.get('matching_cases')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"prompt_digest_mismatch={totals.get('prompt_digest_mismatch')}",
                            f"db_prompt_not_rendered={totals.get('db_prompt_not_rendered')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_progress_report_multi_image_prompt_parity(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_progress_report_multi_image_prompt_parity_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"cases_checked={totals.get('cases_checked')}",
                            f"matching_cases={totals.get('matching_cases')}",
                            f"custom_prompt_cases={totals.get('custom_prompt_cases')}",
                            f"existing_ai_context_cases={totals.get('existing_ai_context_cases')}",
                            f"preferred_ai_hint_cases={totals.get('preferred_ai_hint_cases')}",
                            f"three_photo_sequence_cases={totals.get('three_photo_sequence_cases')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"prompt_digest_mismatch={totals.get('prompt_digest_mismatch')}",
                            f"db_prompt_not_rendered={totals.get('db_prompt_not_rendered')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_generated_report_markdown_legacy_prompt_parity(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_generated_report_markdown_legacy_prompt_parity_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"cases_checked={totals.get('cases_checked')}",
                            f"matching_cases={totals.get('matching_cases')}",
                            f"custom_prompt_cases={totals.get('custom_prompt_cases')}",
                            f"tagged_photo_cases={totals.get('tagged_photo_cases')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"prompt_digest_mismatch={totals.get('prompt_digest_mismatch')}",
                            f"db_prompt_not_rendered={totals.get('db_prompt_not_rendered')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _prompt_catalog_by_artifact(prompt_catalog: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(row.get("artifact_type")): row
        for row in prompt_catalog.get("artifacts", [])
        if isinstance(row, dict)
    }


def build_expression_contract_matrix_payload(db) -> dict[str, object]:
    roadmap = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    implementation_by_artifact = EXPRESSION_ARTIFACT_IMPLEMENTATION
    prompt_catalog = build_expression_prompt_catalog_readiness_payload(db)
    prompt_catalog_by_artifact = _prompt_catalog_by_artifact(prompt_catalog)

    rows: list[dict[str, object]] = []
    totals = {
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_ready": 0,
        "blocking_issues": 0,
        "missing_prompt_catalog": 0,
        "missing_required_fact_paths": 0,
        "missing_forbidden_policy": 0,
        "missing_validator": 0,
        "missing_fallback": 0,
        "missing_promotion_gate": 0,
        "missing_surface_policy": 0,
    }

    for expectation in sorted(EXPRESSION_REGISTRY_EXPECTATIONS, key=lambda item: str(item["artifact_type"])):
        artifact_type = str(expectation["artifact_type"])
        audience_id = str(expectation["audience_id"])
        implementation = implementation_by_artifact.get(artifact_type, {})
        prompt_entry = prompt_catalog_by_artifact.get(artifact_type)
        blockers: list[str] = []

        prompt_ready = bool(prompt_entry and prompt_entry.get("ready"))
        if not prompt_ready:
            blockers.append("prompt_catalog_not_ready")
            totals["missing_prompt_catalog"] += 1

        required_fact_paths = list(expectation.get("required_fact_paths") or [])
        if not required_fact_paths:
            blockers.append("missing_required_fact_paths")
            totals["missing_required_fact_paths"] += 1

        forbidden_claim_count = int(expectation.get("min_forbidden_claims") or 0)
        requires_forbidden_phrases = bool(expectation.get("requires_forbidden_phrases"))
        if forbidden_claim_count <= 0 and requires_forbidden_phrases:
            blockers.append("missing_forbidden_policy")
            totals["missing_forbidden_policy"] += 1

        validator = implementation.get("immutability_validator")
        if not validator:
            blockers.append("missing_validator")
            totals["missing_validator"] += 1

        if not bool(implementation.get("deterministic_fallback")):
            blockers.append("missing_deterministic_fallback")
            totals["missing_fallback"] += 1

        promotion_gate = str(expectation.get("promotion_gate") or "").strip()
        if not promotion_gate:
            blockers.append("missing_promotion_gate")
            totals["missing_promotion_gate"] += 1

        promoted_surfaces = list(implementation.get("promoted_read_surfaces") or [])
        if promoted_surfaces and artifact_type not in EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS:
            blockers.append("missing_promoted_surface_guardrail")
            totals["missing_surface_policy"] += 1

        fallback_policy = (
            "deterministic_fallback_required"
            if bool(implementation.get("deterministic_fallback"))
            else "missing_deterministic_fallback"
        )
        promotion_policy = (
            "promoted_only_with_surface_guardrail"
            if promoted_surfaces
            else "shadow_only_until_surface_declared"
        )

        row = {
            "artifact_type": artifact_type,
            "audience_id": audience_id,
            "stage": str(expectation.get("stage")),
            "maturity": implementation.get("maturity"),
            "ready": not blockers,
            "blockers": blockers,
            "input_facts": {
                "source": "FactSnapshot.facts_json",
                "required_fact_paths": required_fact_paths,
                "db_first": True,
            },
            "forbidden_content": {
                "min_forbidden_claims": forbidden_claim_count,
                "requires_forbidden_phrases": requires_forbidden_phrases,
                "policy_source": "ExpressionOutputContract + ExpressionForbiddenPhrase",
            },
            "validator": {
                "name": validator,
                "ai_polish_allowed": bool(implementation.get("ai_polish_allowed")),
            },
            "fallback": {
                "policy": fallback_policy,
                "deterministic_fallback": bool(implementation.get("deterministic_fallback")),
            },
            "promotion": {
                "gate": promotion_gate,
                "policy": promotion_policy,
                "promoted_read_surfaces": promoted_surfaces,
                "verified_surfaces": EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS.get(artifact_type, []),
            },
            "runtime": {
                "shadow_generator": implementation.get("shadow_generator"),
                "shadow_batch_cli": implementation.get("shadow_batch_cli"),
                "production_gate_step": implementation.get("production_gate_step"),
                "prompt_catalog_ready": prompt_ready,
            },
        }
        totals["blocking_issues"] += len(blockers)
        if not blockers:
            totals["artifacts_ready"] += 1
        rows.append(row)

    return {
        "status": "pass" if int(totals["blocking_issues"]) == 0 else "fail",
        "schema_version": "expression_contract_matrix_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "decision_or_ranking_generated": False,
        },
        "totals": totals,
        "artifacts": rows,
        "prompt_catalog_status": prompt_catalog.get("status"),
        "roadmap_status": roadmap.get("status"),
    }


def expression_audit_contract_matrix(
    *,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_contract_matrix_payload(db)
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifacts_expected={totals.get('artifacts_expected')}",
                            f"artifacts_ready={totals.get('artifacts_ready')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"missing_prompt_catalog={totals.get('missing_prompt_catalog')}",
                            f"missing_validator={totals.get('missing_validator')}",
                            f"missing_fallback={totals.get('missing_fallback')}",
                            f"missing_surface_policy={totals.get('missing_surface_policy')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _cli_value_at_path(value: object, path: str) -> object:
    current: object = value
    for part in path.split("."):
        if not part:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _cli_payload_has_key(value: object, key_name: str) -> bool:
    if isinstance(value, dict):
        return any(str(key) == key_name or _cli_payload_has_key(child, key_name) for key, child in value.items())
    if isinstance(value, list):
        return any(_cli_payload_has_key(child, key_name) for child in value)
    return False


def _cli_flatten_strings(value: object) -> list[str]:
    if isinstance(value, dict):
        strings: list[str] = []
        for child in value.values():
            strings.extend(_cli_flatten_strings(child))
        return strings
    if isinstance(value, list):
        strings: list[str] = []
        for child in value:
            strings.extend(_cli_flatten_strings(child))
        return strings
    return [value] if isinstance(value, str) else []


def _cli_count_leaf_values(value: object) -> int:
    if isinstance(value, dict):
        return sum(_cli_count_leaf_values(child) for child in value.values())
    if isinstance(value, list):
        return sum(_cli_count_leaf_values(child) for child in value)
    return 1 if value is not None else 0


def _generic_artifact_preflight_sample(
    db,
    artifact: ExpressionArtifact,
    snapshot: FactSnapshot | None,
    audience: ExpressionAudience | None,
    contract: ExpressionOutputContract | None,
) -> dict[str, object]:
    payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    validation_errors: list[str] = []
    if payload and audience is not None and contract is not None:
        validation_errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=payload,
            language=artifact.language,
            facts=facts,
            locked_baseline=_locked_baseline_for_replay(
                artifact_type=artifact.artifact_type,
                snapshot=snapshot,
                payload=payload,
            )
            if snapshot is not None
            else None,
        )

    issues: list[str] = []
    if artifact.validation_status not in PROMOTABLE_EXPRESSION_STATUSES:
        issues.append("validation_status_not_promotable")
    if not payload:
        issues.append("structured_payload_missing")
    if _cli_payload_has_key(payload, "raw_model_output"):
        issues.append("structured_payload_contains_model_output_key")
    if validation_errors:
        issues.append("validator_errors")

    return {
        "artifact_id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "audience_id": artifact.audience_id,
        "validation_status": artifact.validation_status,
        "promoted": bool(artifact.promoted),
        "fact_snapshot_id": artifact.fact_snapshot_id,
        "contract_id": artifact.contract_id,
        "scope_type": artifact.scope_type,
        "company_id": artifact.company_id,
        "project_id": snapshot.project_id if snapshot is not None else None,
        "language": artifact.language,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "checks": {
            "structured_payload_present": bool(payload),
            "structured_payload_contains_model_output_key": _cli_payload_has_key(payload, "raw_model_output"),
            "model_output_stored": bool(artifact.raw_model_output),
            "rendered_body_stored": bool(artifact.rendered_markdown),
            "validator_error_count": len(validation_errors),
        },
        "issues": issues,
        "validation_errors": validation_errors[:20],
    }


def build_expression_artifact_promotion_preflight_payload(db, *, artifact_id: str) -> dict[str, object]:
    blockers: list[str] = []
    artifact = db.get(ExpressionArtifact, artifact_id)
    if artifact is None:
        return {
            "status": "fail",
            "schema_version": "expression_artifact_promotion_preflight_v1",
            "guardrails": {
                "uses_ai": False,
                "writes_database": False,
                "uses_filesystem_scan": False,
                "prompt_text_included": False,
                "raw_output_included": False,
                "rendered_body_included": False,
                "promotes_artifact": False,
                "user_visible_ready_implied": False,
            },
            "target": {"artifact_id": artifact_id},
            "visibility_policy": {},
            "blockers": ["artifact_not_found"],
            "target_sample": None,
        }

    snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact.fact_snapshot_id else None
    audience = db.get(ExpressionAudience, artifact.audience_id) if artifact.audience_id else None
    contract = _resolve_replay_contract(db, artifact)
    implementation = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(artifact.artifact_type, {})
    declared_surfaces = list(implementation.get("promoted_read_surfaces") or [])
    verified_surfaces = EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS.get(artifact.artifact_type, [])

    if snapshot is None:
        blockers.append("missing_fact_snapshot")
    if audience is None or not bool(getattr(audience, "is_active", False)):
        blockers.append("audience_missing_or_inactive")
    if contract is None:
        blockers.append("missing_active_contract")
    elif contract.id != artifact.contract_id:
        blockers.append("artifact_contract_not_active_contract")
    if artifact.validation_status not in PROMOTABLE_EXPRESSION_STATUSES:
        blockers.append("validation_status_not_promotable")
    if not isinstance(artifact.structured_json, dict):
        blockers.append("structured_payload_missing")
    if not declared_surfaces:
        blockers.append("no_promoted_read_surface_declared")
    elif not verified_surfaces:
        blockers.append("promoted_read_surface_not_verified")

    target_sample = _generic_artifact_preflight_sample(db, artifact, snapshot, audience, contract)
    if target_sample["issues"]:
        blockers.append("target_artifact_contract_issues")

    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": "expression_artifact_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "rendered_body_included": False,
            "promotes_artifact": False,
            "user_visible_ready_implied": False,
        },
        "target": {
            "artifact_id": artifact.id,
            "artifact_type": artifact.artifact_type,
            "audience_id": artifact.audience_id,
            "validation_status": artifact.validation_status,
            "promoted": bool(artifact.promoted),
            "fact_snapshot_id": artifact.fact_snapshot_id,
            "contract_id": artifact.contract_id,
            "scope_type": artifact.scope_type,
        },
        "visibility_policy": {
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
            "requires_explicit_promotion": True,
            "visible_status": "promoted_valid",
            "shadow_artifacts_visible": False,
            "promotion_preflight_pass_does_not_create_user_visibility": True,
        },
        "blockers": sorted(set(blockers)),
        "target_sample": target_sample,
    }


def build_client_progress_summary_promotion_preflight_payload(db, *, artifact_id: str) -> dict[str, object]:
    blockers: list[str] = []
    generic = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
    artifact = db.get(ExpressionArtifact, artifact_id)
    snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact is not None and artifact.fact_snapshot_id else None
    payload = artifact.structured_json if artifact is not None and isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
    coverage = snapshot.coverage_json if snapshot is not None and isinstance(snapshot.coverage_json, dict) else {}
    source_manifest = snapshot.source_manifest_json if snapshot is not None and isinstance(snapshot.source_manifest_json, dict) else {}
    photo_filters = source_manifest.get("photo_filters") if isinstance(source_manifest.get("photo_filters"), dict) else {}
    surface = payload.get("summary_surface") if isinstance(payload.get("summary_surface"), dict) else {}
    items = surface.get("items") if isinstance(surface.get("items"), list) else []
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    if generic.get("status") != "pass":
        generic_blockers = generic.get("blockers") if isinstance(generic.get("blockers"), list) else []
        non_surface_blockers = [str(item) for item in generic_blockers if str(item) != "no_promoted_read_surface_declared"]
        if non_surface_blockers:
            blockers.append("generic_artifact_preflight_not_ready")

    if artifact is None:
        blockers.append("artifact_not_found")
    else:
        if artifact.artifact_type != CLIENT_PROGRESS_SUMMARY_ARTIFACT:
            blockers.append("artifact_type_not_client_progress_summary")
        if artifact.audience_id != "client":
            blockers.append("audience_not_client")
        if artifact.scope_type != "client_project_period":
            blockers.append("scope_not_client_project_period")

    if guardrails.get("redaction_profile") != "client_progress_v1":
        blockers.append("redaction_profile_mismatch")
    if guardrails.get("approved_client_visible_photos_only") is not True:
        blockers.append("client_visible_photo_filter_missing")
    if not CLIENT_PROGRESS_DENIED_FACT_KEYS:
        blockers.append("client_denied_fact_keys_missing")
    if _contains_denied_key_path(facts, set(CLIENT_PROGRESS_DENIED_FACT_KEYS)):
        blockers.append("denied_fact_key_in_facts")
    facts_text = " ".join(_cli_flatten_strings({key: value for key, value in facts.items() if key != "guardrails"}))
    payload_text = " ".join(_cli_flatten_strings(payload))
    if _CLIENT_PROGRESS_DENIED_TEXT_RE.search(facts_text):
        blockers.append("denied_text_in_facts")
    if _CLIENT_PROGRESS_DENIED_TEXT_RE.search(payload_text):
        blockers.append("denied_text_in_payload")
    if photo_filters.get("visibility") != PhotoVisibility.client_visible.value:
        blockers.append("source_manifest_not_client_visible_only")
    if photo_filters.get("approval_status") != ApprovalStatus.approved.value:
        blockers.append("source_manifest_not_approved_only")
    if coverage.get("redaction_profile") != "client_progress_v1":
        blockers.append("coverage_redaction_profile_mismatch")
    if payload.get("visibility") != "shadow":
        blockers.append("payload_visibility_not_shadow")
    if payload.get("audience") != "client":
        blockers.append("payload_audience_not_client")
    if payload.get("disclaimer") != CLIENT_PROGRESS_DISCLAIMER:
        blockers.append("disclaimer_mismatch")
    if validation.get("summary_surface_locked") is not True:
        blockers.append("summary_surface_not_locked")
    if validation.get("ai_changed_summary_surface") is True:
        blockers.append("ai_changed_summary_surface")
    if not items:
        blockers.append("summary_surface_items_empty")

    item_count = len(items)
    fact_ref_count = 0
    items_missing_fact_refs = 0
    for item in items:
        refs = item.get("fact_refs") if isinstance(item, dict) and isinstance(item.get("fact_refs"), list) else []
        if not refs:
            items_missing_fact_refs += 1
        fact_ref_count += len(refs)
    if items_missing_fact_refs:
        blockers.append("summary_surface_items_missing_fact_refs")

    declared_surfaces = (
        generic.get("visibility_policy", {}).get("declared_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    verified_surfaces = (
        generic.get("visibility_policy", {}).get("verified_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )

    return {
        "status": "pass" if not blockers and declared_surfaces and verified_surfaces else "fail",
        "schema_version": "client_progress_summary_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "rendered_body_included": False,
            "promotes_artifact": False,
            "client_visible_content_included": False,
            "promotion_preflight_pass_does_not_create_user_visibility": True,
        },
        "target": {
            "artifact_id": artifact.id if artifact is not None else artifact_id,
            "artifact_type": artifact.artifact_type if artifact is not None else None,
            "audience_id": artifact.audience_id if artifact is not None else None,
            "validation_status": artifact.validation_status if artifact is not None else None,
            "promoted": bool(artifact.promoted) if artifact is not None else None,
            "fact_snapshot_id": artifact.fact_snapshot_id if artifact is not None else None,
            "scope_type": artifact.scope_type if artifact is not None else None,
        },
        "client_contract": {
            "facts_redaction_profile": guardrails.get("redaction_profile"),
            "approved_client_visible_photos_only": guardrails.get("approved_client_visible_photos_only") is True,
            "denied_fact_key_count": len(CLIENT_PROGRESS_DENIED_FACT_KEYS),
            "source_manifest_visibility": photo_filters.get("visibility"),
            "source_manifest_approval_status": photo_filters.get("approval_status"),
            "coverage_redaction_profile": coverage.get("redaction_profile"),
            "payload_visibility": payload.get("visibility"),
            "payload_audience": payload.get("audience"),
            "disclaimer_present": payload.get("disclaimer") == CLIENT_PROGRESS_DISCLAIMER,
            "summary_surface_locked": validation.get("summary_surface_locked") is True,
            "ai_changed_summary_surface": validation.get("ai_changed_summary_surface") is True,
            "body_generation_path": body.get("generation_path"),
            "summary_item_count": item_count,
            "fact_ref_count": fact_ref_count,
            "items_missing_fact_refs": items_missing_fact_refs,
        },
        "visibility_policy": {
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
            "shadow_artifacts_visible": False,
            "visible_status": "promoted_valid",
            "requires_explicit_promotion": True,
            "customer_surface_available": bool(declared_surfaces and verified_surfaces),
        },
        "blockers": sorted(set(blockers + ([] if declared_surfaces else ["no_promoted_read_surface_declared"]))),
        "generic_preflight": {
            "status": generic.get("status"),
            "blockers": generic.get("blockers") if isinstance(generic.get("blockers"), list) else [],
        },
    }


def build_executive_company_health_promotion_preflight_payload(db, *, artifact_id: str) -> dict[str, object]:
    blockers: list[str] = []
    generic = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
    artifact = db.get(ExpressionArtifact, artifact_id)
    snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact is not None and artifact.fact_snapshot_id else None
    payload = artifact.structured_json if artifact is not None and isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
    coverage = snapshot.coverage_json if snapshot is not None and isinstance(snapshot.coverage_json, dict) else {}
    source_manifest = snapshot.source_manifest_json if snapshot is not None and isinstance(snapshot.source_manifest_json, dict) else {}
    surface = payload.get("company_health_surface") if isinstance(payload.get("company_health_surface"), dict) else {}
    cards = surface.get("cards") if isinstance(surface.get("cards"), list) else []
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    if generic.get("status") != "pass":
        generic_blockers = generic.get("blockers") if isinstance(generic.get("blockers"), list) else []
        non_surface_blockers = [str(item) for item in generic_blockers if str(item) != "no_promoted_read_surface_declared"]
        if non_surface_blockers:
            blockers.append("generic_artifact_preflight_not_ready")

    if artifact is None:
        blockers.append("artifact_not_found")
    else:
        if artifact.artifact_type != EXECUTIVE_COMPANY_HEALTH_ARTIFACT:
            blockers.append("artifact_type_not_executive_company_health_summary")
        if artifact.audience_id != "executive":
            blockers.append("audience_not_executive")
        if artifact.scope_type != "company_health_period":
            blockers.append("scope_not_company_health_period")

    if guardrails.get("db_only") is not True:
        blockers.append("db_only_guardrail_missing")
    if guardrails.get("no_disk_file_reads") is not True:
        blockers.append("no_disk_file_reads_guardrail_missing")
    if guardrails.get("redaction_profile") != "executive_company_health_v1":
        blockers.append("redaction_profile_mismatch")
    if guardrails.get("no_employee_ranking") is not True:
        blockers.append("employee_ranking_guardrail_missing")
    if guardrails.get("no_performance_scoring") is not True:
        blockers.append("performance_scoring_guardrail_missing")
    if not EXECUTIVE_DENIED_FACT_KEYS:
        blockers.append("executive_denied_fact_keys_missing")
    if _contains_denied_key_path(facts, set(EXECUTIVE_DENIED_FACT_KEYS)):
        blockers.append("denied_fact_key_in_facts")
    if EXECUTIVE_HEALTH_AUDIT_DENIED_RE.search(_json_text_without_guardrails(facts)):
        blockers.append("denied_text_in_facts")
    if EXECUTIVE_HEALTH_AUDIT_DENIED_RE.search(json.dumps(payload, ensure_ascii=False, sort_keys=True)):
        blockers.append("denied_text_in_payload")
    if source_manifest.get("redaction_profile") != "executive_company_health_v1":
        blockers.append("source_manifest_redaction_profile_mismatch")
    if coverage.get("redaction_profile") != "executive_company_health_v1":
        blockers.append("coverage_redaction_profile_mismatch")
    if payload.get("visibility") != "shadow":
        blockers.append("payload_visibility_not_shadow")
    if payload.get("audience") != "executive":
        blockers.append("payload_audience_not_executive")
    if validation.get("company_health_surface_locked") is not True:
        blockers.append("company_health_surface_not_locked")
    if validation.get("ai_changed_company_health_surface") is True:
        blockers.append("ai_changed_company_health_surface")
    if validation.get("employee_ordering_disabled") is not True:
        blockers.append("employee_ordering_guardrail_missing")
    if validation.get("performance_scoring_disabled") is not True:
        blockers.append("performance_scoring_validation_missing")
    if not cards:
        blockers.append("company_health_cards_empty")

    card_count = len(cards)
    fact_ref_count = 0
    cards_missing_fact_refs = 0
    unresolved_fact_refs = 0
    mismatched_fact_refs = 0
    rule_flag_count = 0
    status_counts: dict[str, int] = {}
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            blockers.append(f"company_health_cards[{index}]:type")
            continue
        status = str(card.get("status") or "missing")
        status_counts[status] = status_counts.get(status, 0) + 1
        rule_flags = card.get("rule_flags") if isinstance(card.get("rule_flags"), list) else []
        rule_flag_count += len(rule_flags)
        refs = card.get("fact_refs") if isinstance(card.get("fact_refs"), list) else []
        if not refs:
            cards_missing_fact_refs += 1
            continue
        fact_ref_count += len(refs)
        for ref in refs:
            if not isinstance(ref, dict):
                unresolved_fact_refs += 1
                continue
            field_path = str(ref.get("field_path") or "")
            observed_value = ref.get("observed_value")
            actual_value = _cli_value_at_path(facts, field_path)
            if not field_path or actual_value is None:
                unresolved_fact_refs += 1
            elif observed_value != actual_value:
                mismatched_fact_refs += 1
    if cards_missing_fact_refs:
        blockers.append("company_health_cards_missing_fact_refs")
    if unresolved_fact_refs:
        blockers.append("company_health_fact_refs_unresolved")
    if mismatched_fact_refs:
        blockers.append("company_health_fact_refs_mismatched")

    declared_surfaces = (
        generic.get("visibility_policy", {}).get("declared_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    verified_surfaces = (
        generic.get("visibility_policy", {}).get("verified_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    surface_blockers: list[str] = []
    if not declared_surfaces:
        surface_blockers.append("no_promoted_read_surface_declared")
    elif not verified_surfaces:
        surface_blockers.append("promoted_read_surface_not_verified")

    return {
        "status": "pass" if not blockers and declared_surfaces and verified_surfaces else "fail",
        "schema_version": "executive_company_health_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "rendered_body_included": False,
            "promotes_artifact": False,
            "executive_visible_content_included": False,
            "promotion_preflight_pass_does_not_create_user_visibility": True,
        },
        "target": {
            "artifact_id": artifact.id if artifact is not None else artifact_id,
            "artifact_type": artifact.artifact_type if artifact is not None else None,
            "audience_id": artifact.audience_id if artifact is not None else None,
            "validation_status": artifact.validation_status if artifact is not None else None,
            "promoted": bool(artifact.promoted) if artifact is not None else None,
            "fact_snapshot_id": artifact.fact_snapshot_id if artifact is not None else None,
            "scope_type": artifact.scope_type if artifact is not None else None,
        },
        "executive_contract": {
            "facts_redaction_profile": guardrails.get("redaction_profile"),
            "db_only": guardrails.get("db_only") is True,
            "no_disk_file_reads": guardrails.get("no_disk_file_reads") is True,
            "no_employee_ranking": guardrails.get("no_employee_ranking") is True,
            "no_performance_scoring": guardrails.get("no_performance_scoring") is True,
            "denied_fact_key_count": len(EXECUTIVE_DENIED_FACT_KEYS),
            "source_manifest_redaction_profile": source_manifest.get("redaction_profile"),
            "coverage_redaction_profile": coverage.get("redaction_profile"),
            "payload_visibility": payload.get("visibility"),
            "payload_audience": payload.get("audience"),
            "overall_status": payload.get("overall_status"),
            "company_health_surface_locked": validation.get("company_health_surface_locked") is True,
            "ai_changed_company_health_surface": validation.get("ai_changed_company_health_surface") is True,
            "employee_ordering_disabled": validation.get("employee_ordering_disabled") is True,
            "performance_scoring_disabled": validation.get("performance_scoring_disabled") is True,
            "body_generation_path": body.get("generation_path"),
            "card_count": card_count,
            "card_status_counts": dict(sorted(status_counts.items())),
            "rule_flag_count": rule_flag_count,
            "fact_ref_count": fact_ref_count,
            "cards_missing_fact_refs": cards_missing_fact_refs,
            "unresolved_fact_refs": unresolved_fact_refs,
            "mismatched_fact_refs": mismatched_fact_refs,
        },
        "visibility_policy": {
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
            "shadow_artifacts_visible": False,
            "visible_status": "promoted_valid",
            "requires_explicit_promotion": True,
            "executive_surface_available": bool(declared_surfaces and verified_surfaces),
        },
        "blockers": sorted(set(blockers + surface_blockers)),
        "generic_preflight": {
            "status": generic.get("status"),
            "blockers": generic.get("blockers") if isinstance(generic.get("blockers"), list) else [],
        },
    }


def build_operations_health_summary_promotion_preflight_payload(db, *, artifact_id: str) -> dict[str, object]:
    blockers: list[str] = []
    generic = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
    artifact = db.get(ExpressionArtifact, artifact_id)
    snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact is not None and artifact.fact_snapshot_id else None
    payload = artifact.structured_json if artifact is not None and isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
    coverage = snapshot.coverage_json if snapshot is not None and isinstance(snapshot.coverage_json, dict) else {}
    source_manifest = snapshot.source_manifest_json if snapshot is not None and isinstance(snapshot.source_manifest_json, dict) else {}
    surface = payload.get("health_surface") if isinstance(payload.get("health_surface"), dict) else {}
    dimensions = surface.get("dimensions") if isinstance(surface.get("dimensions"), list) else []
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    if generic.get("status") != "pass":
        generic_blockers = generic.get("blockers") if isinstance(generic.get("blockers"), list) else []
        non_surface_blockers = [str(item) for item in generic_blockers if str(item) != "no_promoted_read_surface_declared"]
        if non_surface_blockers:
            blockers.append("generic_artifact_preflight_not_ready")

    if artifact is None:
        blockers.append("artifact_not_found")
    else:
        if artifact.artifact_type != OPERATIONS_HEALTH_SUMMARY_ARTIFACT:
            blockers.append("artifact_type_not_operations_health_summary")
        if artifact.audience_id != "operations":
            blockers.append("audience_not_operations")
        if artifact.scope_type != "company_operations":
            blockers.append("scope_not_company_operations")

    if guardrails.get("db_only") is not True:
        blockers.append("db_only_guardrail_missing")
    if guardrails.get("no_disk_file_reads") is not True:
        blockers.append("no_disk_file_reads_guardrail_missing")
    if guardrails.get("redaction_profile") != "operations_health_v1":
        blockers.append("redaction_profile_mismatch")
    if not OPERATIONS_DENIED_FACT_KEYS:
        blockers.append("operations_denied_fact_keys_missing")
    if _contains_denied_key_path(facts, set(OPERATIONS_DENIED_FACT_KEYS)):
        blockers.append("denied_fact_key_in_facts")
    if _OPERATIONS_PATH_OR_SECRET_RE.search(_json_text_without_guardrails(facts)):
        blockers.append("path_or_secret_in_facts")
    if _OPERATIONS_PATH_OR_SECRET_RE.search(json.dumps(payload, ensure_ascii=False, sort_keys=True)):
        blockers.append("path_or_secret_in_payload")
    if source_manifest.get("redaction_profile") != "operations_health_v1":
        blockers.append("source_manifest_redaction_profile_mismatch")
    if coverage.get("redaction_profile") != "operations_health_v1":
        blockers.append("coverage_redaction_profile_mismatch")
    if payload.get("visibility") != "shadow":
        blockers.append("payload_visibility_not_shadow")
    if payload.get("audience") != "operations":
        blockers.append("payload_audience_not_operations")
    if validation.get("health_surface_locked") is not True:
        blockers.append("health_surface_not_locked")
    if validation.get("ai_changed_health_surface") is True:
        blockers.append("ai_changed_health_surface")
    if not dimensions:
        blockers.append("health_surface_dimensions_empty")

    dimension_count = len(dimensions)
    fact_ref_count = 0
    dimensions_missing_fact_refs = 0
    unresolved_fact_refs = 0
    mismatched_fact_refs = 0
    threshold_breach_count = 0
    status_counts: dict[str, int] = {}
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, dict):
            blockers.append(f"health_surface_dimensions[{index}]:type")
            continue
        status = str(dimension.get("status") or "missing")
        status_counts[status] = status_counts.get(status, 0) + 1
        threshold_breaches = (
            dimension.get("threshold_breaches") if isinstance(dimension.get("threshold_breaches"), list) else []
        )
        threshold_breach_count += len(threshold_breaches)
        refs = dimension.get("fact_refs") if isinstance(dimension.get("fact_refs"), list) else []
        if not refs:
            dimensions_missing_fact_refs += 1
            continue
        fact_ref_count += len(refs)
        for ref in refs:
            if not isinstance(ref, dict):
                unresolved_fact_refs += 1
                continue
            field_path = str(ref.get("field_path") or "")
            observed_value = ref.get("observed_value")
            actual_value = _cli_value_at_path(facts, field_path)
            if not field_path or actual_value is None:
                unresolved_fact_refs += 1
            elif observed_value != actual_value:
                mismatched_fact_refs += 1
    if dimensions_missing_fact_refs:
        blockers.append("health_surface_dimensions_missing_fact_refs")
    if unresolved_fact_refs:
        blockers.append("health_surface_fact_refs_unresolved")
    if mismatched_fact_refs:
        blockers.append("health_surface_fact_refs_mismatched")

    declared_surfaces = (
        generic.get("visibility_policy", {}).get("declared_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    verified_surfaces = (
        generic.get("visibility_policy", {}).get("verified_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    surface_blockers: list[str] = []
    if not declared_surfaces:
        surface_blockers.append("no_promoted_read_surface_declared")
    elif not verified_surfaces:
        surface_blockers.append("promoted_read_surface_not_verified")

    return {
        "status": "pass" if not blockers and declared_surfaces and verified_surfaces else "fail",
        "schema_version": "operations_health_summary_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "rendered_body_included": False,
            "promotes_artifact": False,
            "operations_visible_content_included": False,
            "promotion_preflight_pass_does_not_create_user_visibility": True,
        },
        "target": {
            "artifact_id": artifact.id if artifact is not None else artifact_id,
            "artifact_type": artifact.artifact_type if artifact is not None else None,
            "audience_id": artifact.audience_id if artifact is not None else None,
            "validation_status": artifact.validation_status if artifact is not None else None,
            "promoted": bool(artifact.promoted) if artifact is not None else None,
            "fact_snapshot_id": artifact.fact_snapshot_id if artifact is not None else None,
            "scope_type": artifact.scope_type if artifact is not None else None,
        },
        "operations_contract": {
            "facts_redaction_profile": guardrails.get("redaction_profile"),
            "db_only": guardrails.get("db_only") is True,
            "no_disk_file_reads": guardrails.get("no_disk_file_reads") is True,
            "denied_fact_key_count": len(OPERATIONS_DENIED_FACT_KEYS),
            "source_manifest_redaction_profile": source_manifest.get("redaction_profile"),
            "coverage_redaction_profile": coverage.get("redaction_profile"),
            "payload_visibility": payload.get("visibility"),
            "payload_audience": payload.get("audience"),
            "overall_status": payload.get("overall_status"),
            "health_surface_locked": validation.get("health_surface_locked") is True,
            "ai_changed_health_surface": validation.get("ai_changed_health_surface") is True,
            "body_generation_path": body.get("generation_path"),
            "dimension_count": dimension_count,
            "dimension_status_counts": dict(sorted(status_counts.items())),
            "threshold_breach_count": threshold_breach_count,
            "fact_ref_count": fact_ref_count,
            "dimensions_missing_fact_refs": dimensions_missing_fact_refs,
            "unresolved_fact_refs": unresolved_fact_refs,
            "mismatched_fact_refs": mismatched_fact_refs,
        },
        "visibility_policy": {
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
            "shadow_artifacts_visible": False,
            "visible_status": "promoted_valid",
            "requires_explicit_promotion": True,
            "operations_surface_available": bool(declared_surfaces and verified_surfaces),
        },
        "blockers": sorted(set(blockers + surface_blockers)),
        "generic_preflight": {
            "status": generic.get("status"),
            "blockers": generic.get("blockers") if isinstance(generic.get("blockers"), list) else [],
        },
    }


def build_finance_summary_promotion_preflight_payload(db, *, artifact_id: str) -> dict[str, object]:
    blockers: list[str] = []
    generic = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
    artifact = db.get(ExpressionArtifact, artifact_id)
    snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact is not None and artifact.fact_snapshot_id else None
    payload = artifact.structured_json if artifact is not None and isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
    coverage = snapshot.coverage_json if snapshot is not None and isinstance(snapshot.coverage_json, dict) else {}
    source_manifest = snapshot.source_manifest_json if snapshot is not None and isinstance(snapshot.source_manifest_json, dict) else {}
    receipt_metrics = facts.get("receipt_metrics") if isinstance(facts.get("receipt_metrics"), dict) else {}
    surface = payload.get("finance_surface") if isinstance(payload.get("finance_surface"), dict) else {}
    sections = surface.get("sections") if isinstance(surface.get("sections"), list) else []
    headline = payload.get("headline_metrics") if isinstance(payload.get("headline_metrics"), dict) else {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    if generic.get("status") != "pass":
        generic_blockers = generic.get("blockers") if isinstance(generic.get("blockers"), list) else []
        non_surface_blockers = [str(item) for item in generic_blockers if str(item) != "no_promoted_read_surface_declared"]
        if non_surface_blockers:
            blockers.append("generic_artifact_preflight_not_ready")

    if artifact is None:
        blockers.append("artifact_not_found")
    else:
        if artifact.artifact_type != FINANCE_SUMMARY_ARTIFACT:
            blockers.append("artifact_type_not_finance_summary")
        if artifact.audience_id != "finance":
            blockers.append("audience_not_finance")
        if artifact.scope_type != "project_finance_period":
            blockers.append("scope_not_project_finance_period")

    if guardrails.get("db_only") is not True:
        blockers.append("db_only_guardrail_missing")
    if guardrails.get("redaction_profile") != "finance_summary_v1":
        blockers.append("redaction_profile_mismatch")
    if not FINANCE_DENIED_FACT_KEYS:
        blockers.append("finance_denied_fact_keys_missing")
    if _contains_denied_key_path(facts, set(FINANCE_DENIED_FACT_KEYS)):
        blockers.append("denied_fact_key_in_facts")
    if _FINANCE_SECRET_OR_PII_RE.search(_json_text_without_guardrails(facts)):
        blockers.append("pii_or_secret_in_facts")
    if _FINANCE_SECRET_OR_PII_RE.search(json.dumps(payload, ensure_ascii=False, sort_keys=True)):
        blockers.append("pii_or_secret_in_payload")
    if source_manifest.get("redaction_profile") != "finance_summary_v1":
        blockers.append("source_manifest_redaction_profile_mismatch")
    if coverage.get("redaction_profile") != "finance_summary_v1":
        blockers.append("coverage_redaction_profile_mismatch")
    if payload.get("visibility") != "shadow":
        blockers.append("payload_visibility_not_shadow")
    if payload.get("audience") != "finance":
        blockers.append("payload_audience_not_finance")
    if validation.get("finance_surface_locked") is not True:
        blockers.append("finance_surface_not_locked")
    if validation.get("ai_changed_finance_surface") is True:
        blockers.append("ai_changed_finance_surface")
    if not sections:
        blockers.append("finance_surface_sections_empty")

    expected_headline = {
        "receipt_count": receipt_metrics.get("receipt_count"),
        "total_amount": receipt_metrics.get("total_amount"),
        "receipts_missing_amount": receipt_metrics.get("receipts_missing_amount"),
        "fuel_gallons_total": receipt_metrics.get("fuel_gallons_total"),
    }
    headline_mismatch_count = 0
    headline_missing_count = 0
    headline_negative_count = 0
    for key, expected_value in expected_headline.items():
        if key not in headline:
            headline_missing_count += 1
            continue
        actual_value = headline.get(key)
        if actual_value != expected_value:
            headline_mismatch_count += 1
        if isinstance(actual_value, (int, float)) and actual_value < 0:
            headline_negative_count += 1
    if headline_missing_count:
        blockers.append("headline_metrics_missing")
    if headline_mismatch_count:
        blockers.append("headline_metrics_mismatched")
    if headline_negative_count:
        blockers.append("headline_metrics_negative")

    section_count = len(sections)
    fact_ref_count = 0
    sections_missing_fact_refs = 0
    unresolved_fact_refs = 0
    mismatched_fact_refs = 0
    flag_count = 0
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            blockers.append(f"finance_surface_sections[{index}]:type")
            continue
        flags = section.get("flags") if isinstance(section.get("flags"), list) else []
        flag_count += len(flags)
        refs = section.get("fact_refs") if isinstance(section.get("fact_refs"), list) else []
        if not refs:
            sections_missing_fact_refs += 1
            continue
        fact_ref_count += len(refs)
        for ref in refs:
            if not isinstance(ref, dict):
                unresolved_fact_refs += 1
                continue
            field_path = str(ref.get("field_path") or "")
            observed_value = ref.get("observed_value")
            actual_value = _cli_value_at_path(facts, field_path)
            if not field_path or actual_value is None:
                unresolved_fact_refs += 1
            elif observed_value != actual_value:
                mismatched_fact_refs += 1
    if sections_missing_fact_refs:
        blockers.append("finance_surface_sections_missing_fact_refs")
    if unresolved_fact_refs:
        blockers.append("finance_surface_fact_refs_unresolved")
    if mismatched_fact_refs:
        blockers.append("finance_surface_fact_refs_mismatched")

    declared_surfaces = (
        generic.get("visibility_policy", {}).get("declared_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    verified_surfaces = (
        generic.get("visibility_policy", {}).get("verified_promoted_read_surfaces")
        if isinstance(generic.get("visibility_policy"), dict)
        else []
    )
    surface_blockers: list[str] = []
    if not declared_surfaces:
        surface_blockers.append("no_promoted_read_surface_declared")
    elif not verified_surfaces:
        surface_blockers.append("promoted_read_surface_not_verified")

    return {
        "status": "pass" if not blockers and declared_surfaces and verified_surfaces else "fail",
        "schema_version": "finance_summary_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "rendered_body_included": False,
            "promotes_artifact": False,
            "finance_visible_content_included": False,
            "promotion_preflight_pass_does_not_create_user_visibility": True,
        },
        "target": {
            "artifact_id": artifact.id if artifact is not None else artifact_id,
            "artifact_type": artifact.artifact_type if artifact is not None else None,
            "audience_id": artifact.audience_id if artifact is not None else None,
            "validation_status": artifact.validation_status if artifact is not None else None,
            "promoted": bool(artifact.promoted) if artifact is not None else None,
            "fact_snapshot_id": artifact.fact_snapshot_id if artifact is not None else None,
            "scope_type": artifact.scope_type if artifact is not None else None,
        },
        "finance_contract": {
            "facts_redaction_profile": guardrails.get("redaction_profile"),
            "db_only": guardrails.get("db_only") is True,
            "denied_fact_key_count": len(FINANCE_DENIED_FACT_KEYS),
            "source_manifest_redaction_profile": source_manifest.get("redaction_profile"),
            "coverage_redaction_profile": coverage.get("redaction_profile"),
            "payload_visibility": payload.get("visibility"),
            "payload_audience": payload.get("audience"),
            "finance_surface_locked": validation.get("finance_surface_locked") is True,
            "ai_changed_finance_surface": validation.get("ai_changed_finance_surface") is True,
            "body_generation_path": body.get("generation_path"),
            "section_count": section_count,
            "flag_count": flag_count,
            "fact_ref_count": fact_ref_count,
            "sections_missing_fact_refs": sections_missing_fact_refs,
            "unresolved_fact_refs": unresolved_fact_refs,
            "mismatched_fact_refs": mismatched_fact_refs,
            "headline_metric_count": len(headline),
            "headline_missing_count": headline_missing_count,
            "headline_mismatch_count": headline_mismatch_count,
            "headline_negative_count": headline_negative_count,
            "receipt_count": headline.get("receipt_count"),
            "receipts_missing_amount": headline.get("receipts_missing_amount"),
            "has_receipts": coverage.get("has_receipts") is True,
            "has_amounts": coverage.get("has_amounts") is True,
        },
        "visibility_policy": {
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
            "shadow_artifacts_visible": False,
            "visible_status": "promoted_valid",
            "requires_explicit_promotion": True,
            "finance_surface_available": bool(declared_surfaces and verified_surfaces),
        },
        "blockers": sorted(set(blockers + surface_blockers)),
        "generic_preflight": {
            "status": generic.get("status"),
            "blockers": generic.get("blockers") if isinstance(generic.get("blockers"), list) else [],
        },
    }


def _employee_contribution_artifact_sample(
    artifact: ExpressionArtifact,
    snapshot: FactSnapshot | None,
) -> dict[str, object]:
    payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    issues: list[str] = []

    if artifact.promoted and artifact.validation_status != "promoted_valid":
        issues.append("promoted_artifact_not_promoted_valid")
    if _cli_payload_has_key(payload, "raw_model_output"):
        issues.append("structured_payload_contains_raw_output_key")
    if snapshot is None:
        issues.append("missing_fact_snapshot")
    else:
        if snapshot.scope_type != EMPLOYEE_CONTRIBUTION_SCOPE:
            issues.append("scope_not_employee_project_window")
        if not snapshot.company_id or not snapshot.employee_id or not snapshot.project_id:
            issues.append("snapshot_scope_missing_company_employee_or_project")
    if artifact.audience_id != "employee":
        issues.append("audience_not_employee")
    if artifact.artifact_type != EMPLOYEE_CONTRIBUTION_ARTIFACT:
        issues.append("artifact_type_not_employee_contribution")
    if EMPLOYEE_CONTRIBUTION_FORBIDDEN_READ_TEXT_RE.search(" ".join(_cli_flatten_strings(payload))):
        issues.append("payload_forbidden_read_text")

    required_fields = ("summary_line", "contribution_explanation", "strengths", "suggestions", "comparison_text")
    missing_fields = [field for field in required_fields if field not in payload]
    if artifact.promoted and missing_fields:
        issues.append("promoted_payload_missing_display_fields")

    return {
        "artifact_id": artifact.id,
        "promoted": bool(artifact.promoted),
        "validation_status": artifact.validation_status,
        "fact_snapshot_id": artifact.fact_snapshot_id,
        "company_id": artifact.company_id,
        "project_id": snapshot.project_id if snapshot is not None else None,
        "employee_id_present": bool(snapshot.employee_id) if snapshot is not None else False,
        "scope_type": snapshot.scope_type if snapshot is not None else artifact.scope_type,
        "checks": {
            "structured_payload_contains_raw_output_key": _cli_payload_has_key(payload, "raw_model_output"),
            "model_output_stored": artifact.raw_model_output is not None,
            "rendered_body_stored": artifact.rendered_markdown is not None,
            "display_field_count": sum(1 for field in required_fields if field in payload),
            "highlight_count": len(payload.get("recent_highlights"))
            if isinstance(payload.get("recent_highlights"), list)
            else 0,
            "facts_count": _cli_count_leaf_values(facts),
        },
        "issues": issues,
    }


def _pm_status_card_artifact_sample(artifact: ExpressionArtifact, snapshot: FactSnapshot | None) -> dict[str, object]:
    payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else {}
    facts = snapshot.facts_json if snapshot is not None and isinstance(snapshot.facts_json, dict) else {}
    decision_surface = payload.get("decision_surface") if isinstance(payload.get("decision_surface"), dict) else {}
    items = decision_surface.get("items") if isinstance(decision_surface.get("items"), list) else []
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}

    issues: list[str] = []
    fact_ref_count = 0
    unresolved_fact_refs = 0
    mismatched_fact_refs = 0
    items_missing_fact_refs = 0

    if artifact.promoted and artifact.validation_status != "promoted_valid":
        issues.append("promoted_artifact_not_promoted_valid")
    if _cli_payload_has_key(payload, "raw_model_output"):
        issues.append("structured_payload_contains_raw_output_key")
    if PROJECT_MANAGER_STATUS_CARD_ACTION_LANGUAGE_RE.search(" ".join(_cli_flatten_strings(payload))):
        issues.append("action_language_hit")
    if validation.get("decision_surface_locked") is not True:
        issues.append("decision_surface_not_locked")
    if validation.get("ai_changed_decision_surface") is True:
        issues.append("ai_changed_decision_surface")
    if not items:
        issues.append("decision_surface_items_empty")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(f"decision_surface_items[{index}]:type")
            continue
        refs = item.get("fact_refs") if isinstance(item.get("fact_refs"), list) else []
        if not refs:
            items_missing_fact_refs += 1
            continue
        fact_ref_count += len(refs)
        for ref in refs:
            if not isinstance(ref, dict):
                unresolved_fact_refs += 1
                continue
            field_path = str(ref.get("field_path") or "")
            observed_value = ref.get("observed_value")
            actual_value = _cli_value_at_path(facts, field_path)
            if not field_path or actual_value is None:
                unresolved_fact_refs += 1
            elif observed_value != actual_value:
                mismatched_fact_refs += 1

    if items_missing_fact_refs:
        issues.append("decision_surface_items_missing_fact_refs")
    if unresolved_fact_refs:
        issues.append("fact_refs_unresolved")
    if mismatched_fact_refs:
        issues.append("fact_refs_value_mismatch")

    return {
        "artifact_id": artifact.id,
        "fact_snapshot_id": artifact.fact_snapshot_id,
        "project_id": snapshot.project_id if snapshot is not None else None,
        "validation_status": artifact.validation_status,
        "promoted": bool(artifact.promoted),
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "checks": {
            "decision_surface_locked": validation.get("decision_surface_locked") is True,
            "ai_changed_decision_surface": validation.get("ai_changed_decision_surface") is True,
            "body_generation_path": str(body.get("generation_path") or "unknown"),
            "decision_item_count": len(items),
            "fact_ref_count": fact_ref_count,
            "items_missing_fact_refs": items_missing_fact_refs,
            "unresolved_fact_refs": unresolved_fact_refs,
            "mismatched_fact_refs": mismatched_fact_refs,
            "structured_payload_contains_raw_output_key": _cli_payload_has_key(payload, "raw_model_output"),
        },
        "issues": issues,
    }


def build_project_manager_status_card_contract_payload(
    db,
    *,
    company_id: str | None,
    sample_limit: int,
) -> dict[str, object]:
    sample_limit = max(1, min(int(sample_limit), 200))
    implementation = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(PROJECT_MANAGER_STATUS_CARD_ARTIFACT, {})
    declared_surfaces = list(implementation.get("promoted_read_surfaces") or [])
    verified_surfaces = EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS.get(PROJECT_MANAGER_STATUS_CARD_ARTIFACT, [])

    filters = [
        ExpressionArtifact.artifact_type == PROJECT_MANAGER_STATUS_CARD_ARTIFACT,
        ExpressionArtifact.audience_id == "project_manager",
    ]
    if company_id:
        filters.append(ExpressionArtifact.company_id == company_id)
    rows = (
        db.execute(
            select(ExpressionArtifact, FactSnapshot)
            .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id, isouter=True)
            .where(*filters)
            .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
            .limit(sample_limit)
        )
        .tuples()
        .all()
    )

    status_counts: dict[str, int] = {}
    promoted_status_counts: dict[str, int] = {}
    samples: list[dict[str, object]] = []
    promoted_contract_issues = 0
    shadow_contract_issues = 0
    for artifact, snapshot in rows:
        status_counts[artifact.validation_status] = status_counts.get(artifact.validation_status, 0) + 1
        if artifact.promoted:
            promoted_status_counts[artifact.validation_status] = promoted_status_counts.get(artifact.validation_status, 0) + 1
        sample = _pm_status_card_artifact_sample(artifact, snapshot)
        if sample["issues"]:
            if artifact.promoted:
                promoted_contract_issues += 1
            else:
                shadow_contract_issues += 1
        samples.append(sample)

    active_contract = db.scalar(
        select(ExpressionOutputContract)
        .where(
            ExpressionOutputContract.artifact_type == PROJECT_MANAGER_STATUS_CARD_ARTIFACT,
            ExpressionOutputContract.audience_id == "project_manager",
            ExpressionOutputContract.is_active.is_(True),
        )
        .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
    )

    blockers: list[str] = []
    if declared_surfaces and not verified_surfaces:
        blockers.append("declared_read_surface_without_verified_guardrail")
    if promoted_contract_issues:
        blockers.append("promoted_artifacts_contract_issues")
    if active_contract is None:
        blockers.append("missing_active_expression_contract")

    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": "project_manager_status_card_contract_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "exposes_user_visible_content": False,
            "server_computed_response": True,
        },
        "read_contract": {
            "contract_version": PROJECT_MANAGER_STATUS_CARD_READ_CONTRACT_VERSION,
            "artifact_type": PROJECT_MANAGER_STATUS_CARD_ARTIFACT,
            "audience_id": "project_manager",
            "state": "promoted_only_surface_declared",
            "visible_artifact_filter": {
                "artifact_type": PROJECT_MANAGER_STATUS_CARD_ARTIFACT,
                "audience_id": "project_manager",
                "promoted": True,
                "validation_status": "promoted_valid",
                "company_project_scoped": True,
            },
            "response_shape": {
                "status_card": "server-computed project state card",
                "decision_items": "bounded items with category, severity, metrics, and fact_refs",
                "narrative": "validated body paragraphs only after promotion",
                "provenance": "artifact and fact snapshot metadata without raw model output",
            },
            "visibility_rules": {
                "shadow_artifacts_visible": False,
                "unvalidated_ai_visible": False,
                "app_may_compute_metrics": False,
                "fact_refs_required": True,
                "decision_surface_locked": True,
                "action_language_allowed": False,
                "employee_ranking_allowed": False,
                "performance_scoring_allowed": False,
                "responsibility_attribution_allowed": False,
            },
            "source_tables": ["expression_artifacts", "fact_snapshots", "projects", "photos", "progress_reports"],
        },
        "implementation": {
            "maturity": implementation.get("maturity"),
            "validator": implementation.get("immutability_validator"),
            "deterministic_fallback": bool(implementation.get("deterministic_fallback")),
            "shadow_generator": implementation.get("shadow_generator"),
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
        },
        "contract_registry": {
            "active_contract_id": active_contract.id if active_contract is not None else None,
            "required_fact_path_count": len(active_contract.required_fact_paths_json or [])
            if active_contract is not None and isinstance(active_contract.required_fact_paths_json, list)
            else 0,
            "forbidden_claim_count": len(active_contract.forbidden_claims_json or [])
            if active_contract is not None and isinstance(active_contract.forbidden_claims_json, list)
            else 0,
            "disallowed_action_rule_count": 9,
        },
        "totals": {
            "artifacts_sampled": len(samples),
            "promoted_artifacts_sampled": sum(1 for row in samples if row.get("promoted")),
            "promoted_contract_issues": promoted_contract_issues,
            "shadow_contract_issues": shadow_contract_issues,
            "blocking_issues": len(blockers),
            "declared_read_surfaces": len(declared_surfaces),
            "verified_read_surfaces": len(verified_surfaces),
        },
        "status_counts": status_counts,
        "promoted_status_counts": promoted_status_counts,
        "blockers": blockers,
        "samples": samples,
    }


def build_employee_contribution_read_contract_payload(
    db,
    *,
    company_id: str | None,
    sample_limit: int,
) -> dict[str, object]:
    sample_limit = max(1, min(int(sample_limit), 200))
    implementation = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(EMPLOYEE_CONTRIBUTION_ARTIFACT, {})
    declared_surfaces = list(implementation.get("promoted_read_surfaces") or [])
    verified_surfaces = EXPRESSION_PROMOTION_SURFACE_EXPECTATIONS.get(EMPLOYEE_CONTRIBUTION_ARTIFACT, [])

    filters = [
        ExpressionArtifact.artifact_type == EMPLOYEE_CONTRIBUTION_ARTIFACT,
        ExpressionArtifact.audience_id == "employee",
    ]
    if company_id:
        filters.append(ExpressionArtifact.company_id == company_id)
    rows = (
        db.execute(
            select(ExpressionArtifact, FactSnapshot)
            .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id, isouter=True)
            .where(*filters)
            .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
            .limit(sample_limit)
        )
        .tuples()
        .all()
    )

    status_counts: dict[str, int] = {}
    promoted_status_counts: dict[str, int] = {}
    samples: list[dict[str, object]] = []
    promoted_contract_issues = 0
    shadow_contract_issues = 0
    for artifact, snapshot in rows:
        status_counts[artifact.validation_status] = status_counts.get(artifact.validation_status, 0) + 1
        if artifact.promoted:
            promoted_status_counts[artifact.validation_status] = promoted_status_counts.get(artifact.validation_status, 0) + 1
        sample = _employee_contribution_artifact_sample(artifact, snapshot)
        if sample["issues"]:
            if artifact.promoted:
                promoted_contract_issues += 1
            else:
                shadow_contract_issues += 1
        samples.append(sample)

    active_contract = db.scalar(
        select(ExpressionOutputContract)
        .where(
            ExpressionOutputContract.artifact_type == EMPLOYEE_CONTRIBUTION_ARTIFACT,
            ExpressionOutputContract.audience_id == "employee",
            ExpressionOutputContract.is_active.is_(True),
        )
        .order_by(ExpressionOutputContract.created_at.desc(), ExpressionOutputContract.id.desc())
    )

    blockers: list[str] = []
    if declared_surfaces and not verified_surfaces:
        blockers.append("declared_read_surface_without_verified_guardrail")
    if promoted_contract_issues:
        blockers.append("promoted_artifacts_contract_issues")
    if active_contract is None:
        blockers.append("missing_active_expression_contract")

    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": "employee_contribution_read_contract_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "exposes_user_visible_content": False,
            "server_computed_response": True,
        },
        "read_contract": {
            "contract_version": EMPLOYEE_CONTRIBUTION_READ_CONTRACT_VERSION,
            "artifact_type": EMPLOYEE_CONTRIBUTION_ARTIFACT,
            "audience_id": "employee",
            "state": "promoted_only_surface_declared",
            "visible_artifact_filter": {
                "artifact_type": EMPLOYEE_CONTRIBUTION_ARTIFACT,
                "audience_id": "employee",
                "promoted": True,
                "validation_status": "promoted_valid",
                "company_employee_project_scoped": True,
            },
            "response_shape": {
                "summary": "validated contribution summary and sections",
                "metrics": "server-computed contribution metrics",
                "provenance": "artifact and fact snapshot metadata without raw model output",
            },
            "visibility_rules": {
                "shadow_artifacts_visible": False,
                "unvalidated_ai_visible": False,
                "app_may_compute_metrics": False,
                "employee_ranking_allowed": False,
                "performance_scoring_allowed": False,
                "responsibility_attribution_allowed": False,
                "blame_language_allowed": False,
            },
            "source_tables": ["expression_artifacts", "fact_snapshots", "photos", "evidence_observations"],
        },
        "implementation": {
            "maturity": implementation.get("maturity"),
            "validator": implementation.get("immutability_validator"),
            "deterministic_fallback": bool(implementation.get("deterministic_fallback")),
            "shadow_generator": implementation.get("shadow_generator"),
            "declared_promoted_read_surfaces": declared_surfaces,
            "verified_promoted_read_surfaces": verified_surfaces,
        },
        "contract_registry": {
            "active_contract_id": active_contract.id if active_contract is not None else None,
            "required_fact_path_count": len(active_contract.required_fact_paths_json or [])
            if active_contract is not None and isinstance(active_contract.required_fact_paths_json, list)
            else 0,
            "forbidden_claim_count": len(active_contract.forbidden_claims_json or [])
            if active_contract is not None and isinstance(active_contract.forbidden_claims_json, list)
            else 0,
        },
        "totals": {
            "artifacts_sampled": len(samples),
            "promoted_artifacts_sampled": sum(1 for row in samples if row.get("promoted")),
            "promoted_contract_issues": promoted_contract_issues,
            "shadow_contract_issues": shadow_contract_issues,
            "blocking_issues": len(blockers),
            "declared_read_surfaces": len(declared_surfaces),
            "verified_read_surfaces": len(verified_surfaces),
        },
        "status_counts": status_counts,
        "promoted_status_counts": promoted_status_counts,
        "blockers": blockers,
        "samples": samples,
    }


def build_mobile_promoted_read_contracts_payload(
    db,
    *,
    company_id: str | None,
    sample_limit: int,
) -> dict[str, object]:
    employee_contract = build_employee_contribution_read_contract_payload(
        db,
        company_id=company_id,
        sample_limit=sample_limit,
    )
    pm_contract = build_project_manager_status_card_contract_payload(
        db,
        company_id=company_id,
        sample_limit=sample_limit,
    )
    blockers: list[str] = []
    if employee_contract.get("status") != "pass":
        blockers.append("employee_contribution_read_contract_not_ready")
    if pm_contract.get("status") != "pass":
        blockers.append("project_manager_status_card_read_contract_not_ready")

    employee_totals = employee_contract.get("totals") if isinstance(employee_contract.get("totals"), dict) else {}
    pm_totals = pm_contract.get("totals") if isinstance(pm_contract.get("totals"), dict) else {}
    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": MOBILE_PROMOTED_READ_CONTRACT_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "exposes_user_visible_content": False,
            "promotes_artifact": False,
            "server_computed_response": True,
        },
        "scope": {
            "company_id": company_id,
            "sample_limit": max(1, min(int(sample_limit), 200)),
            "visible_status": "promoted_valid",
            "shadow_artifacts_visible": False,
        },
        "totals": {
            "contracts_checked": 2,
            "contracts_ready": sum(
                1
                for contract in (employee_contract, pm_contract)
                if isinstance(contract, dict) and contract.get("status") == "pass"
            ),
            "blocking_issues": len(blockers),
            "promoted_artifacts_sampled": int(employee_totals.get("promoted_artifacts_sampled") or 0)
            + int(pm_totals.get("promoted_artifacts_sampled") or 0),
            "promoted_contract_issues": int(employee_totals.get("promoted_contract_issues") or 0)
            + int(pm_totals.get("promoted_contract_issues") or 0),
            "shadow_contract_issues": int(employee_totals.get("shadow_contract_issues") or 0)
            + int(pm_totals.get("shadow_contract_issues") or 0),
        },
        "blockers": blockers,
        "contracts": {
            "employee_contribution": {
                "status": employee_contract.get("status"),
                "schema_version": employee_contract.get("schema_version"),
                "read_contract": employee_contract.get("read_contract"),
                "implementation": employee_contract.get("implementation"),
                "contract_registry": employee_contract.get("contract_registry"),
                "totals": employee_contract.get("totals"),
                "status_counts": employee_contract.get("status_counts"),
                "promoted_status_counts": employee_contract.get("promoted_status_counts"),
                "blockers": employee_contract.get("blockers"),
            },
            "project_manager_status_card": {
                "status": pm_contract.get("status"),
                "schema_version": pm_contract.get("schema_version"),
                "read_contract": pm_contract.get("read_contract"),
                "implementation": pm_contract.get("implementation"),
                "contract_registry": pm_contract.get("contract_registry"),
                "totals": pm_contract.get("totals"),
                "status_counts": pm_contract.get("status_counts"),
                "promoted_status_counts": pm_contract.get("promoted_status_counts"),
                "blockers": pm_contract.get("blockers"),
            },
        },
    }


def build_project_manager_status_card_promotion_preflight_payload(
    db,
    *,
    artifact_id: str,
) -> dict[str, object]:
    artifact = db.get(ExpressionArtifact, artifact_id)
    blockers: list[str] = []
    target_sample: dict[str, object] | None = None
    contract_summary: dict[str, object] | None = None
    target_metadata: dict[str, object] = {"artifact_id": artifact_id, "found": artifact is not None}

    if artifact is None:
        blockers.append("artifact_not_found")
    else:
        snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id) if artifact.fact_snapshot_id else None
        target_metadata.update(
            {
                "artifact_type": artifact.artifact_type,
                "audience_id": artifact.audience_id,
                "scope_type": artifact.scope_type,
                "company_id": artifact.company_id,
                "fact_snapshot_id": artifact.fact_snapshot_id,
                "validation_status": artifact.validation_status,
                "promoted": bool(artifact.promoted),
                "contract_id": artifact.contract_id,
                "language": artifact.language,
            }
        )
        if artifact.artifact_type != PROJECT_MANAGER_STATUS_CARD_ARTIFACT:
            blockers.append("artifact_type_not_project_manager_status_card")
        if artifact.audience_id != "project_manager":
            blockers.append("audience_not_project_manager")
        if artifact.scope_type not in PROJECT_MANAGER_STATUS_CARD_SCOPE_TYPES:
            blockers.append("scope_not_project_manager_status_card")
        if artifact.validation_status not in {"shadow_valid", "shadow_fallback_valid", "promoted_valid"}:
            blockers.append("validation_status_not_promotable")
        if artifact.validation_errors_json:
            blockers.append("validation_errors_present")
        if not isinstance(artifact.structured_json, dict):
            blockers.append("structured_payload_missing")
        if snapshot is None:
            blockers.append("fact_snapshot_missing")

        target_sample = _pm_status_card_artifact_sample(artifact, snapshot)
        if target_sample["issues"]:
            blockers.append("target_artifact_contract_issues")

        contract_payload = build_project_manager_status_card_contract_payload(
            db,
            company_id=artifact.company_id,
            sample_limit=100,
        )
        totals = contract_payload["totals"] if isinstance(contract_payload.get("totals"), dict) else {}
        contract_blockers = contract_payload["blockers"] if isinstance(contract_payload.get("blockers"), list) else []
        contract_registry = (
            contract_payload["contract_registry"] if isinstance(contract_payload.get("contract_registry"), dict) else {}
        )
        active_contract_id = contract_registry.get("active_contract_id")
        contract_summary = {
            "status": contract_payload.get("status"),
            "active_contract_id": active_contract_id,
            "blocking_issues": totals.get("blocking_issues"),
            "promoted_contract_issues": totals.get("promoted_contract_issues"),
            "declared_read_surfaces": totals.get("declared_read_surfaces"),
            "verified_read_surfaces": totals.get("verified_read_surfaces"),
            "blockers": contract_blockers,
        }
        if contract_payload.get("status") != "pass":
            blockers.append("status_card_contract_not_ready")
        if int(totals.get("declared_read_surfaces") or 0) < 1:
            blockers.append("promoted_read_surface_not_declared")
        if int(totals.get("verified_read_surfaces") or 0) < 1:
            blockers.append("promoted_read_surface_not_verified")
        if int(totals.get("promoted_contract_issues") or 0) > 0:
            blockers.append("existing_promoted_artifacts_have_contract_issues")
        if active_contract_id is not None and artifact.contract_id != active_contract_id:
            blockers.append("artifact_contract_not_active")

    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": "project_manager_status_card_promotion_preflight_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "exposes_user_visible_content": False,
            "promotes_artifact": False,
            "server_computed_response": True,
        },
        "target": target_metadata,
        "contract_summary": contract_summary,
        "target_sample": target_sample,
        "blockers": blockers,
    }


def expression_audit_project_manager_status_card_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_project_manager_status_card_promotion_preflight_payload(
                db,
                artifact_id=artifact_id,
            )
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary["target"] if isinstance(summary.get("target"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"validation_status={target.get('validation_status') or ''}",
                            f"promoted={str(bool(target.get('promoted'))).lower()}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_promote_project_manager_status_card(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            preflight = build_project_manager_status_card_promotion_preflight_payload(
                db,
                artifact_id=artifact_id,
            )
            if preflight["status"] != "pass":
                if json_output:
                    print(json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    print(f"status=fail\nartifact_id={artifact_id}\nblockers={len(preflight.get('blockers') or [])}")
                return 1

            promoted = promote_expression_artifact(db, artifact_id=artifact_id)
            db.commit()
            result = {
                "status": "promoted",
                "schema_version": "project_manager_status_card_promotion_apply_v1",
                "guardrails": {
                    "uses_ai": False,
                    "writes_database": True,
                    "uses_filesystem_scan": False,
                    "prompt_text_included": False,
                    "raw_output_included": False,
                    "exposes_shadow_content": False,
                    "promotes_artifact": True,
                },
                "artifact": {
                    "artifact_id": promoted.id,
                    "artifact_type": promoted.artifact_type,
                    "audience_id": promoted.audience_id,
                    "validation_status": promoted.validation_status,
                    "promoted": bool(promoted.promoted),
                    "supersedes_artifact_id": promoted.supersedes_artifact_id,
                },
                "preflight_status": preflight["status"],
            }
            if json_output:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                artifact = result["artifact"]
                print(
                    "\n".join(
                        [
                            f"status={result['status']}",
                            f"artifact_id={artifact['artifact_id']}",
                            f"validation_status={artifact['validation_status']}",
                            f"promoted={str(bool(artifact['promoted'])).lower()}",
                            f"supersedes_artifact_id={artifact['supersedes_artifact_id'] or ''}",
                        ]
                    )
                )
            return 0
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _readiness_section(payload: dict[str, object], *, fields: tuple[str, ...] = ()) -> dict[str, object]:
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    section: dict[str, object] = {
        "status": payload.get("status"),
        "schema_version": payload.get("schema_version"),
        "totals": {field: totals.get(field) for field in fields if field in totals},
    }
    return section


def build_expression_promotion_readiness_payload(
    db,
    *,
    artifact_id: str | None,
    company_id: str | None,
    sample_limit: int,
) -> dict[str, object]:
    blockers: list[str] = []
    sections: dict[str, object] = {}

    bootstrap = build_expression_registry_bootstrap_payload(db, apply=False)
    bootstrap_totals = bootstrap["totals"] if isinstance(bootstrap.get("totals"), dict) else {}
    sections["registry_bootstrap"] = _readiness_section(
        bootstrap,
        fields=("artifacts_expected", "planned_changes", "created_rows"),
    )
    if int(bootstrap_totals.get("planned_changes") or 0) > 0:
        blockers.append("registry_bootstrap_has_planned_changes")

    prompt_catalog = build_expression_prompt_catalog_readiness_payload(db)
    sections["prompt_catalog"] = _readiness_section(
        prompt_catalog,
        fields=("artifacts_expected", "artifacts_ready", "blocking_issues"),
    )
    if prompt_catalog.get("status") != "pass":
        blockers.append("prompt_catalog_not_ready")

    contract_matrix = build_expression_contract_matrix_payload(db)
    sections["contract_matrix"] = _readiness_section(
        contract_matrix,
        fields=("artifacts_expected", "artifacts_ready", "blocking_issues"),
    )
    if contract_matrix.get("status") != "pass":
        blockers.append("contract_matrix_not_ready")

    layer_readiness = build_expression_layer_readiness_payload(db)
    sections["layer_readiness"] = _readiness_section(
        layer_readiness,
        fields=(
            "artifacts_expected",
            "artifacts_shadow_verified",
            "artifacts_blocked",
            "artifacts_replay_covered",
            "missing_replay_samples",
            "artifacts_with_replay_failures",
        ),
    )
    if layer_readiness.get("status") != "pass":
        blockers.append("layer_readiness_not_ready")

    promotion_surfaces = build_expression_promotion_surface_readiness_payload(db)
    sections["promotion_surfaces"] = _readiness_section(
        promotion_surfaces,
        fields=(
            "promotion_surface_verified",
            "shadow_only_no_surface",
            "promotion_surface_needs_review",
            "blocked",
            "unexpected_promoted_artifacts",
        ),
    )
    if promotion_surfaces.get("status") != "pass":
        blockers.append("promotion_surfaces_not_ready")

    pm_contract = build_project_manager_status_card_contract_payload(
        db,
        company_id=company_id,
        sample_limit=sample_limit,
    )
    sections["project_manager_status_card_contract"] = _readiness_section(
        pm_contract,
        fields=(
            "artifacts_sampled",
            "promoted_artifacts_sampled",
            "promoted_contract_issues",
            "shadow_contract_issues",
            "declared_read_surfaces",
            "verified_read_surfaces",
            "blocking_issues",
        ),
    )
    if pm_contract.get("status") != "pass":
        blockers.append("project_manager_status_card_contract_not_ready")

    artifact_preflight: dict[str, object] | None = None
    if artifact_id:
        preflight = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
        pm_preflight: dict[str, object] | None = None
        client_preflight: dict[str, object] | None = None
        executive_preflight: dict[str, object] | None = None
        operations_preflight: dict[str, object] | None = None
        finance_preflight: dict[str, object] | None = None
        target = preflight.get("target") if isinstance(preflight.get("target"), dict) else {}
        if target.get("artifact_type") == PROJECT_MANAGER_STATUS_CARD_ARTIFACT:
            pm_preflight = build_project_manager_status_card_promotion_preflight_payload(db, artifact_id=artifact_id)
            if pm_preflight.get("status") != "pass":
                blockers.append("project_manager_status_card_preflight_not_ready")
        if target.get("artifact_type") == CLIENT_PROGRESS_SUMMARY_ARTIFACT:
            client_preflight = build_client_progress_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if client_preflight.get("status") != "pass":
                blockers.append("client_progress_summary_preflight_not_ready")
        if target.get("artifact_type") == EXECUTIVE_COMPANY_HEALTH_ARTIFACT:
            executive_preflight = build_executive_company_health_promotion_preflight_payload(db, artifact_id=artifact_id)
            if executive_preflight.get("status") != "pass":
                blockers.append("executive_company_health_preflight_not_ready")
        if target.get("artifact_type") == OPERATIONS_HEALTH_SUMMARY_ARTIFACT:
            operations_preflight = build_operations_health_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if operations_preflight.get("status") != "pass":
                blockers.append("operations_health_summary_preflight_not_ready")
        if target.get("artifact_type") == FINANCE_SUMMARY_ARTIFACT:
            finance_preflight = build_finance_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if finance_preflight.get("status") != "pass":
                blockers.append("finance_summary_preflight_not_ready")
        sample = preflight.get("target_sample") if isinstance(preflight.get("target_sample"), dict) else {}
        checks = sample.get("checks") if isinstance(sample.get("checks"), dict) else {}
        artifact_preflight = {
            "status": preflight.get("status"),
            "schema_version": preflight.get("schema_version"),
            "artifact_id": target.get("artifact_id"),
            "artifact_type": target.get("artifact_type"),
            "audience_id": target.get("audience_id"),
            "validation_status": target.get("validation_status"),
            "promoted": target.get("promoted"),
            "fact_snapshot_id": target.get("fact_snapshot_id"),
            "project_id": sample.get("project_id"),
            "checks": {
                "decision_item_count": checks.get("decision_item_count"),
                "fact_ref_count": checks.get("fact_ref_count"),
                "items_missing_fact_refs": checks.get("items_missing_fact_refs"),
                "unresolved_fact_refs": checks.get("unresolved_fact_refs"),
                "mismatched_fact_refs": checks.get("mismatched_fact_refs"),
                "structured_payload_contains_raw_output_key": checks.get("structured_payload_contains_raw_output_key"),
                "validator_error_count": checks.get("validator_error_count"),
                "model_output_stored": checks.get("model_output_stored"),
            },
            "blockers": preflight.get("blockers") if isinstance(preflight.get("blockers"), list) else [],
            "issues": sample.get("issues") if isinstance(sample.get("issues"), list) else [],
            "visibility_policy": preflight.get("visibility_policy")
            if isinstance(preflight.get("visibility_policy"), dict)
            else {},
            "project_manager_status_card_preflight": (
                {
                    "status": pm_preflight.get("status"),
                    "blockers": pm_preflight.get("blockers") if isinstance(pm_preflight.get("blockers"), list) else [],
                }
                if pm_preflight is not None
                else None
            ),
            "client_progress_summary_preflight": (
                {
                    "status": client_preflight.get("status"),
                    "blockers": client_preflight.get("blockers")
                    if isinstance(client_preflight.get("blockers"), list)
                    else [],
                    "client_contract": client_preflight.get("client_contract")
                    if isinstance(client_preflight.get("client_contract"), dict)
                    else {},
                }
                if client_preflight is not None
                else None
            ),
            "executive_company_health_preflight": (
                {
                    "status": executive_preflight.get("status"),
                    "blockers": executive_preflight.get("blockers")
                    if isinstance(executive_preflight.get("blockers"), list)
                    else [],
                    "executive_contract": executive_preflight.get("executive_contract")
                    if isinstance(executive_preflight.get("executive_contract"), dict)
                    else {},
                }
                if executive_preflight is not None
                else None
            ),
            "operations_health_summary_preflight": (
                {
                    "status": operations_preflight.get("status"),
                    "blockers": operations_preflight.get("blockers")
                    if isinstance(operations_preflight.get("blockers"), list)
                    else [],
                    "operations_contract": operations_preflight.get("operations_contract")
                    if isinstance(operations_preflight.get("operations_contract"), dict)
                    else {},
                }
                if operations_preflight is not None
                else None
            ),
            "finance_summary_preflight": (
                {
                    "status": finance_preflight.get("status"),
                    "blockers": finance_preflight.get("blockers")
                    if isinstance(finance_preflight.get("blockers"), list)
                    else [],
                    "finance_contract": finance_preflight.get("finance_contract")
                    if isinstance(finance_preflight.get("finance_contract"), dict)
                    else {},
                }
                if finance_preflight is not None
                else None
            ),
        }
        if preflight.get("status") != "pass":
            blockers.append("artifact_preflight_not_ready")

    return {
        "status": "pass" if not blockers else "fail",
        "schema_version": "expression_promotion_readiness_v1",
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
            "promotes_artifact": False,
            "runs_shadow_generation": False,
            "exposes_user_visible_content": False,
        },
        "scope": {
            "company_id": company_id,
            "artifact_id": artifact_id,
            "pm_contract_sample_limit": max(1, min(int(sample_limit), 200)),
        },
        "blockers": blockers,
        "sections": sections,
        "artifact_preflight": artifact_preflight,
    }


def expression_audit_promotion_readiness(
    *,
    artifact_id: str | None,
    company_id: str | None,
    sample_limit: int,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            payload = build_expression_promotion_readiness_payload(
                db,
                artifact_id=artifact_id,
                company_id=company_id,
                sample_limit=sample_limit,
            )
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={payload['status']}",
                            f"blockers={len(payload.get('blockers') or [])}",
                            f"artifact_id={artifact_id or ''}",
                        ]
                    )
                )
            return 0 if payload["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_artifact_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_artifact_promotion_preflight_payload(db, artifact_id=artifact_id)
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
                visibility = summary.get("visibility_policy") if isinstance(summary.get("visibility_policy"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"artifact_type={target.get('artifact_type')}",
                            f"audience_id={target.get('audience_id')}",
                            f"validation_status={target.get('validation_status')}",
                            f"declared_surfaces={len(visibility.get('declared_promoted_read_surfaces') or [])}",
                            f"verified_surfaces={len(visibility.get('verified_promoted_read_surfaces') or [])}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_client_progress_summary_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_client_progress_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
                contract = summary.get("client_contract") if isinstance(summary.get("client_contract"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"artifact_type={target.get('artifact_type')}",
                            f"validation_status={target.get('validation_status')}",
                            f"summary_items={contract.get('summary_item_count')}",
                            f"fact_refs={contract.get('fact_ref_count')}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_executive_company_health_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_executive_company_health_promotion_preflight_payload(db, artifact_id=artifact_id)
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
                contract = summary.get("executive_contract") if isinstance(summary.get("executive_contract"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"artifact_type={target.get('artifact_type')}",
                            f"validation_status={target.get('validation_status')}",
                            f"cards={contract.get('card_count')}",
                            f"fact_refs={contract.get('fact_ref_count')}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_operations_health_summary_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_operations_health_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
                contract = summary.get("operations_contract") if isinstance(summary.get("operations_contract"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"artifact_type={target.get('artifact_type')}",
                            f"validation_status={target.get('validation_status')}",
                            f"dimensions={contract.get('dimension_count')}",
                            f"fact_refs={contract.get('fact_ref_count')}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_finance_summary_promotion(
    *,
    artifact_id: str,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_finance_summary_promotion_preflight_payload(db, artifact_id=artifact_id)
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
                contract = summary.get("finance_contract") if isinstance(summary.get("finance_contract"), dict) else {}
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifact_id={target.get('artifact_id')}",
                            f"artifact_type={target.get('artifact_type')}",
                            f"validation_status={target.get('validation_status')}",
                            f"sections={contract.get('section_count')}",
                            f"fact_refs={contract.get('fact_ref_count')}",
                            f"blockers={len(summary.get('blockers') or [])}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_project_manager_status_card_contract(
    *,
    company_id: str | None,
    sample_limit: int,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_project_manager_status_card_contract_payload(
                db,
                company_id=company_id,
                sample_limit=sample_limit,
            )
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"artifacts_sampled={totals.get('artifacts_sampled')}",
                            f"promoted_artifacts_sampled={totals.get('promoted_artifacts_sampled')}",
                            f"promoted_contract_issues={totals.get('promoted_contract_issues')}",
                            f"shadow_contract_issues={totals.get('shadow_contract_issues')}",
                            f"declared_read_surfaces={totals.get('declared_read_surfaces')}",
                            f"verified_read_surfaces={totals.get('verified_read_surfaces')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_mobile_promoted_read_contracts(
    *,
    company_id: str | None,
    sample_limit: int,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_mobile_promoted_read_contracts_payload(
                db,
                company_id=company_id,
                sample_limit=sample_limit,
            )
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"contracts_checked={totals.get('contracts_checked')}",
                            f"contracts_ready={totals.get('contracts_ready')}",
                            f"promoted_artifacts_sampled={totals.get('promoted_artifacts_sampled')}",
                            f"promoted_contract_issues={totals.get('promoted_contract_issues')}",
                            f"shadow_contract_issues={totals.get('shadow_contract_issues')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def _contains_denied_key_path(value: object, denied_keys: set[str], *, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if key_text in denied_keys:
                hits.append(path)
            hits.extend(_contains_denied_key_path(child, denied_keys, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            hits.extend(_contains_denied_key_path(child, denied_keys, prefix=path))
    return hits


def _redaction_policy_metadata(denied_keys: set[str]) -> dict[str, object]:
    joined = "\n".join(sorted(denied_keys))
    return {
        "denied_key_count": len(denied_keys),
        "denied_key_sha256_16": hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16] if joined else None,
    }


def _sample_project_id_for_client_boundary(db, *, company_id: str | None) -> tuple[str | None, str | None]:
    filters = []
    if company_id:
        filters.append(Project.company_id == company_id)
    project = db.scalar(select(Project).where(*filters).order_by(Project.created_at.desc(), Project.id.desc()).limit(1))
    if project is None:
        return None, None
    return project.company_id, project.project_id


def build_expression_visibility_fact_boundary_payload(
    db,
    *,
    company_id: str | None,
    window_days: int,
) -> dict[str, object]:
    window_days = max(1, min(int(window_days), 365))
    boundaries: list[dict[str, object]] = []
    totals = {
        "boundaries_expected": 2,
        "boundaries_ready": 0,
        "blocking_issues": 0,
        "sampled_boundaries": 0,
        "sample_denied_key_hits": 0,
    }

    client_company_id, client_project_id = _sample_project_id_for_client_boundary(db, company_id=company_id)
    client_blockers: list[str] = []
    client_sample: dict[str, object] = {
        "sampled": False,
        "company_id": client_company_id,
        "project_id": client_project_id,
        "denied_key_hits": [],
    }
    if not CLIENT_PROGRESS_DENIED_FACT_KEYS:
        client_blockers.append("missing_client_denied_fact_keys")
    if client_company_id and client_project_id:
        facts = build_client_progress_summary_facts(
            db,
            company_id=client_company_id,
            project_id=client_project_id,
            window_days=window_days,
        )
        denied_hits = _contains_denied_key_path(facts, set(CLIENT_PROGRESS_DENIED_FACT_KEYS))
        guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
        if guardrails.get("redaction_profile") != "client_progress_v1":
            client_blockers.append("client_redaction_profile_mismatch")
        if guardrails.get("approved_client_visible_photos_only") is not True:
            client_blockers.append("client_visible_photo_filter_missing")
        if denied_hits:
            client_blockers.append("client_denied_keys_in_facts")
        client_sample = {
            "sampled": True,
            "company_id": client_company_id,
            "project_id": client_project_id,
            "denied_key_hits": denied_hits,
            "source_tables": ["projects", "photos", "progress_reports"],
        }
        totals["sampled_boundaries"] += 1
        totals["sample_denied_key_hits"] += len(denied_hits)
    boundaries.append(
        {
            "artifact_type": CLIENT_PROGRESS_SUMMARY_ARTIFACT,
            "audience_id": "client",
            "ready": not client_blockers,
            "blockers": client_blockers,
            "fact_source_policy": {
                "db_first": True,
                "source_tables": ["projects", "photos", "progress_reports"],
                "filesystem_scan_allowed": False,
            },
            "visibility_policy": {
                "client_visible_photos_only": True,
                "approved_photos_only": True,
                "public_sensitive_level": "low",
                "shadow_only_until_promoted": True,
            },
            "redaction_policy": _redaction_policy_metadata(set(CLIENT_PROGRESS_DENIED_FACT_KEYS)),
            "sample": client_sample,
        }
    )

    executive_company_id = company_id or db.scalar(select(Company.company_id).order_by(Company.id.asc()).limit(1))
    executive_blockers: list[str] = []
    executive_sample: dict[str, object] = {
        "sampled": False,
        "company_id": executive_company_id,
        "denied_key_hits": [],
    }
    if not EXECUTIVE_DENIED_FACT_KEYS:
        executive_blockers.append("missing_executive_denied_fact_keys")
    if executive_company_id:
        facts = build_executive_company_health_facts(
            db,
            company_id=str(executive_company_id),
            window_days=window_days,
        )
        denied_hits = _contains_denied_key_path(facts, set(EXECUTIVE_DENIED_FACT_KEYS))
        guardrails = facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}
        if guardrails.get("redaction_profile") != "executive_company_health_v1":
            executive_blockers.append("executive_redaction_profile_mismatch")
        if guardrails.get("no_employee_ranking") is not True:
            executive_blockers.append("executive_employee_ranking_guardrail_missing")
        if guardrails.get("no_performance_scoring") is not True:
            executive_blockers.append("executive_performance_scoring_guardrail_missing")
        if guardrails.get("no_disk_file_reads") is not True:
            executive_blockers.append("executive_no_disk_file_reads_guardrail_missing")
        if denied_hits:
            executive_blockers.append("executive_denied_keys_in_facts")
        executive_sample = {
            "sampled": True,
            "company_id": str(executive_company_id),
            "denied_key_hits": denied_hits,
            "source_tables": [
                "companies",
                "projects",
                "photos",
                "progress_reports",
                "expression_artifacts",
                "receipt_facts",
            ],
        }
        totals["sampled_boundaries"] += 1
        totals["sample_denied_key_hits"] += len(denied_hits)
    boundaries.append(
        {
            "artifact_type": EXECUTIVE_COMPANY_HEALTH_ARTIFACT,
            "audience_id": "executive",
            "ready": not executive_blockers,
            "blockers": executive_blockers,
            "fact_source_policy": {
                "db_first": True,
                "source_tables": [
                    "companies",
                    "projects",
                    "photos",
                    "progress_reports",
                    "expression_artifacts",
                    "receipt_facts",
                ],
                "filesystem_scan_allowed": False,
            },
            "visibility_policy": {
                "aggregate_only": True,
                "no_employee_ranking": True,
                "no_performance_scoring": True,
                "shadow_only_until_promoted": True,
            },
            "redaction_policy": _redaction_policy_metadata(set(EXECUTIVE_DENIED_FACT_KEYS)),
            "sample": executive_sample,
        }
    )

    for boundary in boundaries:
        blockers = boundary.get("blockers") if isinstance(boundary.get("blockers"), list) else []
        totals["blocking_issues"] += len(blockers)
        if not blockers:
            totals["boundaries_ready"] += 1

    return {
        "status": "pass" if int(totals["blocking_issues"]) == 0 else "fail",
        "schema_version": "expression_visibility_fact_boundary_v1",
        "scope": {
            "company_id": company_id,
            "window_days": window_days,
        },
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
            "prompt_text_included": False,
            "raw_output_included": False,
        },
        "totals": totals,
        "boundaries": boundaries,
    }


def expression_audit_visibility_fact_boundary(
    *,
    company_id: str | None,
    window_days: int,
    json_output: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            summary = build_expression_visibility_fact_boundary_payload(
                db,
                company_id=company_id,
                window_days=window_days,
            )
            totals = summary["totals"] if isinstance(summary.get("totals"), dict) else {}
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"boundaries_expected={totals.get('boundaries_expected')}",
                            f"boundaries_ready={totals.get('boundaries_ready')}",
                            f"blocking_issues={totals.get('blocking_issues')}",
                            f"sampled_boundaries={totals.get('sampled_boundaries')}",
                            f"sample_denied_key_hits={totals.get('sample_denied_key_hits')}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_eval_readiness(
    *,
    json_output: bool,
    fail_on_missing_golden: bool,
    settings_override: object | None = None,
) -> int:
    settings = settings_override or load_settings()
    engine = create_engine_from_settings(settings)
    session_maker = create_session_maker(engine)
    try:
        with session_maker() as db:
            run_count = int(db.scalar(select(func.count(ExpressionEvalRun.id))) or 0)
            item_count = int(db.scalar(select(func.count(ExpressionEvalItem.id))) or 0)
            golden_count = int(
                db.scalar(
                    select(func.count(ExpressionEvalRun.id)).where(ExpressionEvalRun.run_type == "golden_replay")
                )
                or 0
            )
            latest_golden = db.scalar(
                select(ExpressionEvalRun)
                .where(ExpressionEvalRun.run_type == "golden_replay")
                .order_by(ExpressionEvalRun.created_at.desc(), ExpressionEvalRun.id.desc())
            )
            blocking_reasons: list[str] = []
            if fail_on_missing_golden and golden_count <= 0:
                blocking_reasons.append("missing_golden_replay_run")

            latest_summary = latest_golden.summary_json if latest_golden is not None and isinstance(latest_golden.summary_json, dict) else {}
            latest_artifact_type_counts = (
                latest_summary.get("artifact_type_counts")
                if isinstance(latest_summary.get("artifact_type_counts"), dict)
                else {}
            )
            replay_artifact_type_counts = _latest_golden_replay_artifact_type_counts(db)
            registry_replay_coverage = _registry_replay_coverage(replay_artifact_type_counts)
            summary = {
                "status": "pass" if not blocking_reasons else "fail",
                "schema_version": "expression_eval_readiness_v1",
                "guardrails": {
                    "uses_ai": False,
                    "writes_database": False,
                    "uses_filesystem_scan": False,
                    "raw_output_included": False,
                },
                "totals": {
                    "eval_runs": run_count,
                    "eval_items": item_count,
                    "golden_replay_runs": golden_count,
                },
                "run_status_counts": _count_grouped(db, ExpressionEvalRun, ExpressionEvalRun.status),
                "item_status_counts": _count_grouped(db, ExpressionEvalItem, ExpressionEvalItem.status),
                "latest_golden_replay": (
                    {
                        "id": latest_golden.id,
                        "status": latest_golden.status,
                        "artifact_type": latest_golden.artifact_type,
                        "audience_id": latest_golden.audience_id,
                        "prompt_version_id": latest_golden.prompt_version_id,
                        "contract_id": latest_golden.contract_id,
                        "runner_version": latest_golden.runner_version,
                        "created_at": latest_golden.created_at.isoformat() if latest_golden.created_at else None,
                        "completed_at": latest_golden.completed_at.isoformat() if latest_golden.completed_at else None,
                        "summary": {
                            "cases": latest_summary.get("cases"),
                            "passed": latest_summary.get("passed"),
                            "failed": latest_summary.get("failed"),
                            "skipped": latest_summary.get("skipped"),
                            "artifact_type_counts": latest_artifact_type_counts,
                        },
                    }
                    if latest_golden is not None
                    else None
                ),
                "replay_artifact_type_counts": replay_artifact_type_counts,
                "registry_replay_coverage": registry_replay_coverage,
                "blocking_reasons": blocking_reasons,
            }
            if json_output:
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(
                    "\n".join(
                        [
                            f"status={summary['status']}",
                            f"eval_runs={run_count}",
                            f"eval_items={item_count}",
                            f"golden_replay_runs={golden_count}",
                            f"blocking_reasons={json.dumps(blocking_reasons, ensure_ascii=False, sort_keys=True)}",
                        ]
                    )
                )
            return 0 if summary["status"] == "pass" else 1
    except (OperationalError, ProgrammingError) as exc:
        raise SystemExit("Database schema is not initialized. Run 'alembic upgrade head' first.") from exc


def expression_audit_production_gate(
    *,
    company_id: str | None,
    project_limit: int,
    client_limit: int,
    operations_limit: int,
    finance_limit: int,
    executive_limit: int,
    window_days: int,
    finance_readiness_window_days: int,
    operations_window_hours: int,
    language: str,
    json_output: bool,
    fail_on_warnings: bool,
    min_project_samples: int,
    min_client_samples: int,
    min_operations_samples: int,
    min_finance_samples: int,
    min_finance_readiness_samples: int,
) -> int:
    step_results: list[dict[str, object]] = []
    blocking_reasons: list[str] = []

    registry_exit, registry_payload = _run_json_cli_step(
        "registry",
        audit_expression_registry,
        json_output=True,
        fail_on_warnings=fail_on_warnings,
    )
    step_results.append(registry_payload)
    if registry_exit != 0 or registry_payload.get("status") != "pass":
        blocking_reasons.append("registry_audit_failed")

    roadmap_exit, roadmap_payload = _run_json_cli_step(
        "target_roadmap",
        audit_expression_target_roadmap,
        json_output=True,
        fail_on_hard_bugs=True,
        fail_on_product_risk=False,
    )
    step_results.append(roadmap_payload)
    roadmap_totals = roadmap_payload.get("totals") if isinstance(roadmap_payload.get("totals"), dict) else {}
    if roadmap_exit != 0 or int(roadmap_totals.get("hard_bug") or 0) > 0:
        blocking_reasons.append("target_roadmap_hard_bug")

    eval_exit, eval_payload = _run_json_cli_step(
        "eval_readiness",
        expression_audit_eval_readiness,
        json_output=True,
        fail_on_missing_golden=False,
    )
    step_results.append(eval_payload)
    if eval_exit != 0 or eval_payload.get("status") != "pass":
        blocking_reasons.append("eval_readiness_failed")

    layer_readiness_exit, layer_readiness_payload = _run_json_cli_step(
        "layer_readiness",
        expression_audit_layer_readiness,
        json_output=True,
        fail_on_product_risk=False,
    )
    step_results.append(layer_readiness_payload)
    layer_readiness_totals = (
        layer_readiness_payload.get("totals") if isinstance(layer_readiness_payload.get("totals"), dict) else {}
    )
    if layer_readiness_exit != 0 or layer_readiness_payload.get("status") != "pass":
        blocking_reasons.append("layer_readiness_failed")

    prompt_catalog_exit, prompt_catalog_payload = _run_json_cli_step(
        "prompt_catalog",
        expression_audit_prompt_catalog,
        json_output=True,
    )
    step_results.append(prompt_catalog_payload)
    prompt_catalog_totals = (
        prompt_catalog_payload.get("totals") if isinstance(prompt_catalog_payload.get("totals"), dict) else {}
    )
    if prompt_catalog_exit != 0 or prompt_catalog_payload.get("status") != "pass":
        blocking_reasons.append("prompt_catalog_audit_failed")

    contract_matrix_exit, contract_matrix_payload = _run_json_cli_step(
        "contract_matrix",
        expression_audit_contract_matrix,
        json_output=True,
    )
    step_results.append(contract_matrix_payload)
    contract_matrix_totals = (
        contract_matrix_payload.get("totals") if isinstance(contract_matrix_payload.get("totals"), dict) else {}
    )
    if contract_matrix_exit != 0 or contract_matrix_payload.get("status") != "pass":
        blocking_reasons.append("contract_matrix_audit_failed")

    visibility_boundary_exit, visibility_boundary_payload = _run_json_cli_step(
        "visibility_fact_boundary",
        expression_audit_visibility_fact_boundary,
        company_id=company_id,
        window_days=window_days,
        json_output=True,
    )
    step_results.append(visibility_boundary_payload)
    visibility_boundary_totals = (
        visibility_boundary_payload.get("totals")
        if isinstance(visibility_boundary_payload.get("totals"), dict)
        else {}
    )
    if visibility_boundary_exit != 0 or visibility_boundary_payload.get("status") != "pass":
        blocking_reasons.append("visibility_fact_boundary_audit_failed")

    promotion_surface_exit, promotion_surface_payload = _run_json_cli_step(
        "promotion_surfaces",
        expression_audit_promotion_surfaces,
        json_output=True,
        fail_on_review=fail_on_warnings,
    )
    step_results.append(promotion_surface_payload)
    promotion_surface_totals = (
        promotion_surface_payload.get("totals")
        if isinstance(promotion_surface_payload.get("totals"), dict)
        else {}
    )
    if promotion_surface_exit != 0 or promotion_surface_payload.get("status") != "pass":
        blocking_reasons.append("promotion_surface_audit_failed")

    pm_status_card_exit, pm_status_card_payload = _run_json_cli_step(
        "project_manager_status_card_contract",
        expression_audit_project_manager_status_card_contract,
        company_id=company_id,
        sample_limit=50,
        json_output=True,
    )
    step_results.append(pm_status_card_payload)
    pm_status_card_totals = (
        pm_status_card_payload.get("totals")
        if isinstance(pm_status_card_payload.get("totals"), dict)
        else {}
    )
    if pm_status_card_exit != 0 or pm_status_card_payload.get("status") != "pass":
        blocking_reasons.append("project_manager_status_card_contract_failed")

    pm_exit, pm_payload = _run_json_cli_step(
        "project_manager_decision_brief_shadow_batch",
        expression_shadow_project_manager_brief_batch,
        limit=project_limit,
        company_id=company_id,
        window_days=window_days,
        language=language,
        preferred_model=None,
        no_ai=True,
        json_output=True,
    )
    step_results.append(pm_payload)
    pm_totals = pm_payload.get("totals") if isinstance(pm_payload.get("totals"), dict) else {}
    pm_gate, pm_reasons = _shadow_batch_gate(
        gate_name="project_manager",
        payload=pm_payload,
        exit_code=pm_exit,
        min_samples=min_project_samples,
    )
    blocking_reasons.extend(pm_reasons)

    client_exit, client_payload = _run_json_cli_step(
        "client_progress_summary_shadow_batch",
        expression_shadow_client_progress_summary_batch,
        limit=client_limit,
        company_id=company_id,
        window_days=window_days,
        language=language,
        preferred_model=None,
        no_ai=True,
        json_output=True,
    )
    step_results.append(client_payload)
    client_gate, client_reasons = _shadow_batch_gate(
        gate_name="client",
        payload=client_payload,
        exit_code=client_exit,
        min_samples=min_client_samples,
    )
    blocking_reasons.extend(client_reasons)

    operations_exit, operations_payload = _run_json_cli_step(
        "operations_health_summary_shadow_batch",
        expression_shadow_operations_health_summary_batch,
        limit=operations_limit,
        company_id=company_id,
        window_hours=operations_window_hours,
        language=language,
        preferred_model=None,
        no_ai=True,
        json_output=True,
    )
    step_results.append(operations_payload)
    operations_gate, operations_reasons = _shadow_batch_gate(
        gate_name="operations",
        payload=operations_payload,
        exit_code=operations_exit,
        min_samples=min_operations_samples,
        sample_key="companies_selected",
    )
    blocking_reasons.extend(operations_reasons)

    finance_exit, finance_payload = _run_json_cli_step(
        "finance_summary_shadow_batch",
        expression_shadow_finance_summary_batch,
        limit=finance_limit,
        company_id=company_id,
        window_days=window_days,
        language=language,
        preferred_model=None,
        no_ai=True,
        json_output=True,
    )
    step_results.append(finance_payload)
    finance_gate, finance_reasons = _shadow_batch_gate(
        gate_name="finance",
        payload=finance_payload,
        exit_code=finance_exit,
        min_samples=min_finance_samples,
    )
    blocking_reasons.extend(finance_reasons)

    finance_health_exit, finance_health_payload = _run_json_cli_step(
        "finance_fact_health",
        expression_audit_finance_fact_health,
        company_id=company_id,
        window_days=finance_readiness_window_days,
        limit=finance_limit,
        json_output=True,
    )
    step_results.append(finance_health_payload)
    finance_health_totals = (
        finance_health_payload.get("totals") if isinstance(finance_health_payload.get("totals"), dict) else {}
    )
    readiness_receipt_count = int(finance_health_totals.get("receipt_fact_count") or 0)
    finance_readiness_exit, finance_readiness_payload = _run_json_cli_step(
        "finance_summary_readiness_shadow_batch",
        expression_shadow_finance_summary_batch,
        limit=finance_limit,
        company_id=company_id,
        window_days=finance_readiness_window_days,
        language=language,
        preferred_model=None,
        no_ai=True,
        json_output=True,
    )
    step_results.append(finance_readiness_payload)
    finance_readiness_min_samples = min_finance_readiness_samples if readiness_receipt_count > 0 else 0
    finance_readiness_gate, finance_readiness_reasons = _shadow_batch_gate(
        gate_name="finance_readiness",
        payload=finance_readiness_payload,
        exit_code=finance_readiness_exit if finance_health_exit == 0 else 1,
        min_samples=finance_readiness_min_samples,
    )
    if finance_health_exit != 0:
        finance_readiness_reasons.append("finance_readiness_health_failed")
    blocking_reasons.extend(finance_readiness_reasons)

    executive_exit, executive_payload = _run_json_cli_step(
        "executive_company_health_shadow_audit",
        audit_executive_company_health_shadow,
        company_id=company_id,
        limit=executive_limit,
        json_output=True,
        fail_on_warnings=fail_on_warnings,
    )
    step_results.append(executive_payload)
    executive_totals = executive_payload.get("totals") if isinstance(executive_payload.get("totals"), dict) else {}
    if executive_exit != 0 or executive_payload.get("status") != "pass":
        blocking_reasons.append("executive_shadow_audit_failed")
    if int(executive_totals.get("artifacts_checked") or 0) <= 0:
        blocking_reasons.append("executive_shadow_sample_empty")

    status = "pass" if not blocking_reasons else "fail"
    summary = {
        "status": status,
        "blocking_reasons": blocking_reasons,
        "gates": {
            "registry": {
                "status": registry_payload.get("status"),
                "blocking_issues": (registry_payload.get("totals") or {}).get("blocking_issues")
                if isinstance(registry_payload.get("totals"), dict)
                else None,
            },
            "target_roadmap": {
                "status": roadmap_payload.get("status"),
                "schema_version": roadmap_payload.get("schema_version"),
                "gaps": int(roadmap_totals.get("gaps") or 0),
                "hard_bug": int(roadmap_totals.get("hard_bug") or 0),
                "product_risk": int(roadmap_totals.get("product_risk") or 0),
                "future": int(roadmap_totals.get("future") or 0),
            },
            "eval_readiness": {
                "status": eval_payload.get("status"),
                "eval_runs": int(
                    (eval_payload.get("totals") if isinstance(eval_payload.get("totals"), dict) else {}).get(
                        "eval_runs"
                    )
                    or 0
                ),
                "eval_items": int(
                    (eval_payload.get("totals") if isinstance(eval_payload.get("totals"), dict) else {}).get(
                        "eval_items"
                    )
                    or 0
                ),
                "golden_replay_runs": int(
                    (eval_payload.get("totals") if isinstance(eval_payload.get("totals"), dict) else {}).get(
                        "golden_replay_runs"
                    )
                    or 0
                ),
                "latest_artifact_type_counts": (
                    (
                        eval_payload.get("latest_golden_replay", {}).get("summary", {}).get("artifact_type_counts")
                        if isinstance(eval_payload.get("latest_golden_replay"), dict)
                        and isinstance(eval_payload.get("latest_golden_replay", {}).get("summary"), dict)
                        else {}
                    )
                ),
                "replay_artifact_type_counts": (
                    eval_payload.get("replay_artifact_type_counts")
                    if isinstance(eval_payload.get("replay_artifact_type_counts"), dict)
                    else {}
                ),
                "registry_replay_coverage": (
                    eval_payload.get("registry_replay_coverage")
                    if isinstance(eval_payload.get("registry_replay_coverage"), dict)
                    else {}
                ),
            },
            "layer_readiness": {
                "status": layer_readiness_payload.get("status"),
                "artifacts_expected": int(layer_readiness_totals.get("artifacts_expected") or 0),
                "artifacts_shadow_verified": int(layer_readiness_totals.get("artifacts_shadow_verified") or 0),
                "artifacts_blocked": int(layer_readiness_totals.get("artifacts_blocked") or 0),
                "artifacts_replay_covered": int(layer_readiness_totals.get("artifacts_replay_covered") or 0),
                "missing_replay_samples": int(layer_readiness_totals.get("missing_replay_samples") or 0),
                "artifacts_with_replay_failures": int(
                    layer_readiness_totals.get("artifacts_with_replay_failures") or 0
                ),
            },
            "prompt_catalog": {
                "status": prompt_catalog_payload.get("status"),
                "artifacts_ready": int(prompt_catalog_totals.get("artifacts_ready") or 0),
                "blocking_issues": int(prompt_catalog_totals.get("blocking_issues") or 0),
                "missing_active_version": int(prompt_catalog_totals.get("missing_active_version") or 0),
                "missing_active_binding": int(prompt_catalog_totals.get("missing_active_binding") or 0),
                "missing_active_contract": int(prompt_catalog_totals.get("missing_active_contract") or 0),
                "missing_facts_placeholder": int(prompt_catalog_totals.get("missing_facts_placeholder") or 0),
                "missing_contract_schema_placeholder": int(
                    prompt_catalog_totals.get("missing_contract_schema_placeholder") or 0
                ),
                "prompt_format_errors": int(prompt_catalog_totals.get("prompt_format_errors") or 0),
            },
            "contract_matrix": {
                "status": contract_matrix_payload.get("status"),
                "artifacts_ready": int(contract_matrix_totals.get("artifacts_ready") or 0),
                "blocking_issues": int(contract_matrix_totals.get("blocking_issues") or 0),
                "missing_prompt_catalog": int(contract_matrix_totals.get("missing_prompt_catalog") or 0),
                "missing_validator": int(contract_matrix_totals.get("missing_validator") or 0),
                "missing_fallback": int(contract_matrix_totals.get("missing_fallback") or 0),
                "missing_surface_policy": int(contract_matrix_totals.get("missing_surface_policy") or 0),
            },
            "visibility_fact_boundary": {
                "status": visibility_boundary_payload.get("status"),
                "boundaries_ready": int(visibility_boundary_totals.get("boundaries_ready") or 0),
                "blocking_issues": int(visibility_boundary_totals.get("blocking_issues") or 0),
                "sampled_boundaries": int(visibility_boundary_totals.get("sampled_boundaries") or 0),
                "sample_denied_key_hits": int(visibility_boundary_totals.get("sample_denied_key_hits") or 0),
            },
            "promotion_surfaces": {
                "status": promotion_surface_payload.get("status"),
                "promotion_surface_verified": int(promotion_surface_totals.get("promotion_surface_verified") or 0),
                "shadow_only_no_surface": int(promotion_surface_totals.get("shadow_only_no_surface") or 0),
                "promotion_surface_needs_review": int(
                    promotion_surface_totals.get("promotion_surface_needs_review") or 0
                ),
                "blocked": int(promotion_surface_totals.get("blocked") or 0),
                "unexpected_promoted_artifacts": int(
                    promotion_surface_totals.get("unexpected_promoted_artifacts") or 0
                ),
            },
            "project_manager_status_card_contract": {
                "status": pm_status_card_payload.get("status"),
                "artifacts_sampled": int(pm_status_card_totals.get("artifacts_sampled") or 0),
                "promoted_artifacts_sampled": int(pm_status_card_totals.get("promoted_artifacts_sampled") or 0),
                "promoted_contract_issues": int(pm_status_card_totals.get("promoted_contract_issues") or 0),
                "shadow_contract_issues": int(pm_status_card_totals.get("shadow_contract_issues") or 0),
                "declared_read_surfaces": int(pm_status_card_totals.get("declared_read_surfaces") or 0),
                "verified_read_surfaces": int(pm_status_card_totals.get("verified_read_surfaces") or 0),
                "blocking_issues": int(pm_status_card_totals.get("blocking_issues") or 0),
            },
            "project_manager_decision_brief_shadow_batch": {
                **pm_gate,
                "decision_items": int(pm_totals.get("decision_items") or 0),
            },
            "client_progress_summary_shadow_batch": {
                **client_gate,
                "summary_items": int(
                    (client_payload.get("totals") if isinstance(client_payload.get("totals"), dict) else {}).get(
                        "summary_items"
                    )
                    or 0
                ),
            },
            "operations_health_summary_shadow_batch": {
                **operations_gate,
                "dimensions": int(
                    (operations_payload.get("totals") if isinstance(operations_payload.get("totals"), dict) else {}).get(
                        "dimensions"
                    )
                    or 0
                ),
            },
            "finance_summary_shadow_batch": {
                **finance_gate,
                "receipt_count": int(
                    (finance_payload.get("totals") if isinstance(finance_payload.get("totals"), dict) else {}).get(
                        "receipt_count"
                    )
                    or 0
                ),
            },
            "finance_fact_health": {
                "status": finance_health_payload.get("status"),
                "window_days": finance_readiness_window_days,
                "receipt_fact_count": readiness_receipt_count,
                "projects_with_receipt_facts": int(finance_health_totals.get("projects_with_receipt_facts") or 0),
            },
            "finance_summary_readiness_shadow_batch": {
                **finance_readiness_gate,
                "window_days": finance_readiness_window_days,
                "receipt_count": int(
                    (
                        finance_readiness_payload.get("totals")
                        if isinstance(finance_readiness_payload.get("totals"), dict)
                        else {}
                    ).get("receipt_count")
                    or 0
                ),
                "readiness_receipt_count": readiness_receipt_count,
            },
            "executive_company_health_shadow_audit": {
                "status": executive_payload.get("status"),
                "artifacts_checked": int(executive_totals.get("artifacts_checked") or 0),
                "blocking_issues": executive_payload.get("blocking_issues"),
                "raw_model_output_present": int(executive_totals.get("raw_model_output_present") or 0),
                "missing_fact_refs": int(executive_totals.get("missing_fact_refs") or 0),
            },
        },
        "step_results": step_results if json_output else [],
    }
    if json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "\n".join(
                [
                    f"status={summary['status']}",
                    f"blocking_reasons={json.dumps(blocking_reasons, ensure_ascii=False, sort_keys=True)}",
                    f"registry_status={summary['gates']['registry']['status']}",
                    (
                        "target_roadmap="
                        f"status={summary['gates']['target_roadmap']['status']} "
                        f"hard_bug={summary['gates']['target_roadmap']['hard_bug']} "
                        f"product_risk={summary['gates']['target_roadmap']['product_risk']} "
                        f"future={summary['gates']['target_roadmap']['future']}"
                    ),
                    (
                        "eval_readiness="
                        f"status={summary['gates']['eval_readiness']['status']} "
                        f"eval_runs={summary['gates']['eval_readiness']['eval_runs']} "
                        f"eval_items={summary['gates']['eval_readiness']['eval_items']} "
                        f"golden_replay_runs={summary['gates']['eval_readiness']['golden_replay_runs']}"
                    ),
                    (
                        "layer_readiness="
                        f"status={summary['gates']['layer_readiness']['status']} "
                        f"shadow_verified={summary['gates']['layer_readiness']['artifacts_shadow_verified']} "
                        f"blocked={summary['gates']['layer_readiness']['artifacts_blocked']} "
                        f"replay_covered={summary['gates']['layer_readiness']['artifacts_replay_covered']} "
                        f"missing_replay={summary['gates']['layer_readiness']['missing_replay_samples']}"
                    ),
                    (
                        "prompt_catalog="
                        f"status={summary['gates']['prompt_catalog']['status']} "
                        f"ready={summary['gates']['prompt_catalog']['artifacts_ready']} "
                        f"blocking={summary['gates']['prompt_catalog']['blocking_issues']} "
                        f"format_errors={summary['gates']['prompt_catalog']['prompt_format_errors']}"
                    ),
                    (
                        "contract_matrix="
                        f"status={summary['gates']['contract_matrix']['status']} "
                        f"ready={summary['gates']['contract_matrix']['artifacts_ready']} "
                        f"blocking={summary['gates']['contract_matrix']['blocking_issues']} "
                        f"missing_validator={summary['gates']['contract_matrix']['missing_validator']}"
                    ),
                    (
                        "visibility_fact_boundary="
                        f"status={summary['gates']['visibility_fact_boundary']['status']} "
                        f"ready={summary['gates']['visibility_fact_boundary']['boundaries_ready']} "
                        f"blocking={summary['gates']['visibility_fact_boundary']['blocking_issues']} "
                        f"denied_hits={summary['gates']['visibility_fact_boundary']['sample_denied_key_hits']}"
                    ),
                    (
                        "promotion_surfaces="
                        f"status={summary['gates']['promotion_surfaces']['status']} "
                        f"verified={summary['gates']['promotion_surfaces']['promotion_surface_verified']} "
                        f"shadow_only={summary['gates']['promotion_surfaces']['shadow_only_no_surface']} "
                        f"review={summary['gates']['promotion_surfaces']['promotion_surface_needs_review']} "
                        f"unexpected_promoted={summary['gates']['promotion_surfaces']['unexpected_promoted_artifacts']}"
                    ),
                    (
                        "pm_shadow="
                        f"projects_selected={pm_gate['projects_selected']} "
                        f"errors={pm_gate['errors']} "
                        f"shadow_invalid={pm_gate['shadow_invalid']} "
                        f"shadow_fallback_valid={pm_gate['shadow_fallback_valid']}"
                    ),
                    (
                        "client_shadow="
                        f"projects_selected={client_gate['projects_selected']} "
                        f"errors={client_gate['errors']} "
                        f"shadow_invalid={client_gate['shadow_invalid']} "
                        f"shadow_fallback_valid={client_gate['shadow_fallback_valid']}"
                    ),
                    (
                        "operations_shadow="
                        f"companies_selected={operations_gate['companies_selected']} "
                        f"errors={operations_gate['errors']} "
                        f"shadow_invalid={operations_gate['shadow_invalid']} "
                        f"shadow_fallback_valid={operations_gate['shadow_fallback_valid']}"
                    ),
                    (
                        "finance_shadow="
                        f"projects_selected={finance_gate['projects_selected']} "
                        f"errors={finance_gate['errors']} "
                        f"shadow_invalid={finance_gate['shadow_invalid']} "
                        f"shadow_fallback_valid={finance_gate['shadow_fallback_valid']}"
                    ),
                    (
                        "finance_readiness="
                        f"window_days={finance_readiness_window_days} "
                        f"receipt_fact_count={readiness_receipt_count} "
                        f"projects_selected={finance_readiness_gate['projects_selected']} "
                        f"errors={finance_readiness_gate['errors']} "
                        f"shadow_invalid={finance_readiness_gate['shadow_invalid']} "
                        f"shadow_fallback_valid={finance_readiness_gate['shadow_fallback_valid']}"
                    ),
                    (
                        "executive_shadow="
                        f"status={summary['gates']['executive_company_health_shadow_audit']['status']} "
                        f"artifacts_checked={summary['gates']['executive_company_health_shadow_audit']['artifacts_checked']} "
                        f"blocking_issues={summary['gates']['executive_company_health_shadow_audit']['blocking_issues']} "
                        f"raw_model_output_present={summary['gates']['executive_company_health_shadow_audit']['raw_model_output_present']} "
                        f"missing_fact_refs={summary['gates']['executive_company_health_shadow_audit']['missing_fact_refs']}"
                    ),
                ]
            )
        )
    return 0 if status == "pass" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="KK Field Logger CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--from-env", action="store_true")
    create_admin_parser.add_argument("--if-missing", action="store_true")
    create_admin_parser.add_argument("--username")
    create_admin_parser.add_argument("--password")
    create_admin_parser.add_argument("--email")
    create_admin_parser.add_argument("--display-name")

    subparsers.add_parser("photo-lens-seed-core")

    photo_lens_run_parser = subparsers.add_parser("photo-lens-run")
    photo_lens_run_parser.add_argument("--photo-id", type=int, required=True)
    photo_lens_run_parser.add_argument("--lens-key", required=True)
    photo_lens_run_parser.add_argument("--model", default=None)
    photo_lens_run_parser.add_argument("--backend-url", default=None)
    photo_lens_run_parser.add_argument("--force", action="store_true")

    photo_lens_backfill_parser = subparsers.add_parser("photo-lens-backfill")
    photo_lens_backfill_parser.add_argument("--company-id", default=None)
    photo_lens_backfill_parser.add_argument("--project-id", default=None)
    photo_lens_backfill_parser.add_argument("--lens-key", action="append", dest="lens_keys")
    photo_lens_backfill_parser.add_argument("--limit", type=int, default=10)
    photo_lens_backfill_parser.add_argument("--model", default=None)
    photo_lens_backfill_parser.add_argument("--backend-url", default=None)
    photo_lens_backfill_parser.add_argument("--sleep-seconds", type=float, default=None)
    photo_lens_backfill_parser.add_argument("--force", action="store_true")
    photo_lens_backfill_parser.add_argument("--json", action="store_true", dest="json_output")

    requeue_dead_letter_parser = subparsers.add_parser("queue-requeue-dead-letter")
    requeue_dead_letter_parser.add_argument("--task-type", default=None)
    requeue_dead_letter_parser.add_argument("--company-id", default=None)
    requeue_dead_letter_parser.add_argument("--since-hours", type=int, default=None)
    requeue_dead_letter_parser.add_argument("--limit", type=int, default=50)
    requeue_dead_letter_parser.add_argument("--dry-run", action="store_true")

    photo_lens_coverage_parser = subparsers.add_parser("photo-lens-coverage")
    photo_lens_coverage_parser.add_argument("--company-id", default=None)
    photo_lens_coverage_parser.add_argument("--project-id", default=None)
    photo_lens_coverage_parser.add_argument("--recent-hours", type=int, default=None)

    pm_lens_report_parser = subparsers.add_parser("project-manager-lens-report")
    pm_lens_report_parser.add_argument("--company-id", required=True)
    pm_lens_report_parser.add_argument("--project-id", required=True)
    pm_lens_report_parser.add_argument("--window-days", type=int, default=30)
    pm_lens_report_parser.add_argument("--max-photos", type=int, default=200)
    pm_lens_report_parser.add_argument("--max-evidence-per-lens", type=int, default=5)
    pm_lens_report_parser.add_argument("--json", action="store_true", dest="json_output")

    expression_parser = subparsers.add_parser("expression-shadow-employee")
    expression_parser.add_argument("--company-id", required=True)
    expression_parser.add_argument("--employee-id", required=True)
    expression_parser.add_argument("--project-id", required=True)
    expression_parser.add_argument("--window-days", type=int, default=30)
    expression_parser.add_argument("--language", default="zh")
    expression_parser.add_argument("--preferred-model", default=None)
    expression_parser.add_argument("--no-ai", action="store_true")

    promote_parser = subparsers.add_parser("expression-promote-artifact")
    promote_parser.add_argument("--artifact-id", required=True)

    progress_parser = subparsers.add_parser("expression-shadow-progress-report")
    progress_parser.add_argument("--report-id", required=True)
    progress_parser.add_argument("--language", default="multi")
    progress_parser.add_argument("--preferred-model", default=None)
    progress_parser.add_argument("--no-ai", action="store_true")

    progress_batch_parser = subparsers.add_parser("expression-shadow-progress-report-batch")
    progress_batch_parser.add_argument("--limit", type=int, default=10)
    progress_batch_parser.add_argument("--company-id", default=None)
    progress_batch_parser.add_argument("--project-id", default=None)
    progress_batch_parser.add_argument("--language", default="multi")
    progress_batch_parser.add_argument("--preferred-model", default=None)
    progress_batch_parser.add_argument("--no-ai", action="store_true")
    progress_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    progress_string_parser = subparsers.add_parser("expression-shadow-progress-report-string")
    progress_string_parser.add_argument("--report-id", required=True)
    progress_string_parser.add_argument("--field-path", required=True)
    progress_string_parser.add_argument("--language", default="multi")
    progress_string_parser.add_argument("--preferred-model", default=None)
    progress_string_parser.add_argument("--no-ai", action="store_true", default=True)

    progress_string_batch_parser = subparsers.add_parser("expression-shadow-progress-report-string-batch")
    progress_string_batch_parser.add_argument("--limit", type=int, default=10)
    progress_string_batch_parser.add_argument("--company-id", default=None)
    progress_string_batch_parser.add_argument("--project-id", default=None)
    progress_string_batch_parser.add_argument("--chunks-per-report", type=int, default=5)
    progress_string_batch_parser.add_argument("--language", default="multi")
    progress_string_batch_parser.add_argument("--preferred-model", default=None)
    progress_string_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    progress_string_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    markdown_parser = subparsers.add_parser("expression-shadow-report-markdown")
    markdown_parser.add_argument("--report-id", required=True)
    markdown_parser.add_argument("--language", default="en")
    markdown_parser.add_argument("--preferred-model", default=None)
    markdown_parser.add_argument("--no-ai", action="store_true")

    markdown_batch_parser = subparsers.add_parser("expression-shadow-report-markdown-batch")
    markdown_batch_parser.add_argument("--limit", type=int, default=10)
    markdown_batch_parser.add_argument("--company-id", default=None)
    markdown_batch_parser.add_argument("--project-id", default=None)
    markdown_batch_parser.add_argument("--language", default="en")
    markdown_batch_parser.add_argument("--preferred-model", default=None)
    markdown_batch_parser.add_argument("--no-ai", action="store_true")
    markdown_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    markdown_section_parser = subparsers.add_parser("expression-shadow-report-markdown-section")
    markdown_section_parser.add_argument("--report-id", required=True)
    markdown_section_parser.add_argument("--section-key", required=True)
    markdown_section_parser.add_argument("--language", default="en")
    markdown_section_parser.add_argument("--preferred-model", default=None)
    markdown_section_parser.add_argument("--no-ai", action="store_true")

    markdown_section_batch_parser = subparsers.add_parser("expression-shadow-report-markdown-section-batch")
    markdown_section_batch_parser.add_argument("--limit", type=int, default=10)
    markdown_section_batch_parser.add_argument("--company-id", default=None)
    markdown_section_batch_parser.add_argument("--project-id", default=None)
    markdown_section_batch_parser.add_argument("--section-keys", default=None)
    markdown_section_batch_parser.add_argument("--language", default="en")
    markdown_section_batch_parser.add_argument("--preferred-model", default=None)
    markdown_section_batch_parser.add_argument("--no-ai", action="store_true")
    markdown_section_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    manager_parser = subparsers.add_parser("expression-shadow-project-manager-brief")
    manager_parser.add_argument("--company-id", required=True)
    manager_parser.add_argument("--project-id", required=True)
    manager_parser.add_argument("--window-days", type=int, default=30)
    manager_parser.add_argument("--language", default="en")
    manager_parser.add_argument("--preferred-model", default=None)
    manager_parser.add_argument("--no-ai", action="store_true", default=True)
    manager_parser.add_argument("--use-ai", action="store_false", dest="no_ai")

    manager_batch_parser = subparsers.add_parser("expression-shadow-project-manager-brief-batch")
    manager_batch_parser.add_argument("--limit", type=int, default=10)
    manager_batch_parser.add_argument("--company-id", default=None)
    manager_batch_parser.add_argument("--window-days", type=int, default=30)
    manager_batch_parser.add_argument("--language", default="en")
    manager_batch_parser.add_argument("--preferred-model", default=None)
    manager_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    manager_batch_parser.add_argument("--use-ai", action="store_false", dest="no_ai")
    manager_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    client_parser = subparsers.add_parser("expression-shadow-client-progress-summary")
    client_parser.add_argument("--company-id", required=True)
    client_parser.add_argument("--project-id", required=True)
    client_parser.add_argument("--window-days", type=int, default=30)
    client_parser.add_argument("--language", default="en")
    client_parser.add_argument("--preferred-model", default=None)
    client_parser.add_argument("--no-ai", action="store_true", default=True)

    client_batch_parser = subparsers.add_parser("expression-shadow-client-progress-summary-batch")
    client_batch_parser.add_argument("--limit", type=int, default=10)
    client_batch_parser.add_argument("--company-id", default=None)
    client_batch_parser.add_argument("--window-days", type=int, default=30)
    client_batch_parser.add_argument("--language", default="en")
    client_batch_parser.add_argument("--preferred-model", default=None)
    client_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    client_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    operations_parser = subparsers.add_parser("expression-shadow-operations-health-summary")
    operations_parser.add_argument("--company-id", default="default")
    operations_parser.add_argument("--window-hours", type=int, default=24)
    operations_parser.add_argument("--language", default="en")
    operations_parser.add_argument("--preferred-model", default=None)
    operations_parser.add_argument("--no-ai", action="store_true", default=True)
    operations_parser.add_argument("--json", action="store_true", dest="json_output")

    operations_batch_parser = subparsers.add_parser("expression-shadow-operations-health-summary-batch")
    operations_batch_parser.add_argument("--limit", type=int, default=10)
    operations_batch_parser.add_argument("--company-id", default=None)
    operations_batch_parser.add_argument("--window-hours", type=int, default=24)
    operations_batch_parser.add_argument("--language", default="en")
    operations_batch_parser.add_argument("--preferred-model", default=None)
    operations_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    operations_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    finance_parser = subparsers.add_parser("expression-shadow-finance-summary")
    finance_parser.add_argument("--company-id", required=True)
    finance_parser.add_argument("--project-id", required=True)
    finance_parser.add_argument("--window-days", type=int, default=30)
    finance_parser.add_argument("--language", default="en")
    finance_parser.add_argument("--preferred-model", default=None)
    finance_parser.add_argument("--no-ai", action="store_true", default=True)

    finance_batch_parser = subparsers.add_parser("expression-shadow-finance-summary-batch")
    finance_batch_parser.add_argument("--limit", type=int, default=10)
    finance_batch_parser.add_argument("--company-id", default=None)
    finance_batch_parser.add_argument("--window-days", type=int, default=30)
    finance_batch_parser.add_argument("--language", default="en")
    finance_batch_parser.add_argument("--preferred-model", default=None)
    finance_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    finance_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    finance_health_parser = subparsers.add_parser("expression-audit-finance-fact-health")
    finance_health_parser.add_argument("--company-id", default=None)
    finance_health_parser.add_argument("--window-days", type=int, default=30)
    finance_health_parser.add_argument("--limit", type=int, default=20)
    finance_health_parser.add_argument("--json", action="store_true", dest="json_output")

    finance_attribution_parser = subparsers.add_parser("expression-audit-finance-fact-attribution")
    finance_attribution_parser.add_argument("--company-id", default=None)
    finance_attribution_parser.add_argument("--window-days", type=int, default=365)
    finance_attribution_parser.add_argument("--limit", type=int, default=50)
    finance_attribution_parser.add_argument("--apply", action="store_true")
    finance_attribution_parser.add_argument("--rollback-file", default=None)
    finance_attribution_parser.add_argument("--json", action="store_true", dest="json_output")

    finance_attribution_rollback_parser = subparsers.add_parser("expression-rollback-finance-fact-attribution")
    finance_attribution_rollback_parser.add_argument("--rollback-file", required=True)
    finance_attribution_rollback_parser.add_argument("--json", action="store_true", dest="json_output")

    executive_parser = subparsers.add_parser("expression-shadow-executive-company-health")
    executive_parser.add_argument("--company-id", required=True)
    executive_parser.add_argument("--window-days", type=int, default=30)
    executive_parser.add_argument("--language", default="en")
    executive_parser.add_argument("--preferred-model", default=None)
    executive_parser.add_argument("--no-ai", action="store_true", default=True)
    executive_parser.add_argument("--json", action="store_true", dest="json_output")

    executive_batch_parser = subparsers.add_parser("expression-shadow-executive-company-health-batch")
    executive_batch_parser.add_argument("--limit", type=int, default=10)
    executive_batch_parser.add_argument("--company-id", default=None)
    executive_batch_parser.add_argument("--window-days", type=int, default=30)
    executive_batch_parser.add_argument("--language", default="en")
    executive_batch_parser.add_argument("--preferred-model", default=None)
    executive_batch_parser.add_argument("--no-ai", action="store_true", default=True)
    executive_batch_parser.add_argument("--json", action="store_true", dest="json_output")

    executive_audit_parser = subparsers.add_parser("expression-audit-executive-company-health-shadow")
    executive_audit_parser.add_argument("--company-id", default=None)
    executive_audit_parser.add_argument("--limit", type=int, default=50)
    executive_audit_parser.add_argument("--json", action="store_true", dest="json_output")
    executive_audit_parser.add_argument("--fail-on-warnings", action="store_true")

    registry_audit_parser = subparsers.add_parser("expression-audit-registry")
    registry_audit_parser.add_argument("--json", action="store_true", dest="json_output")
    registry_audit_parser.add_argument("--fail-on-warnings", action="store_true")

    registry_bootstrap_parser = subparsers.add_parser("expression-bootstrap-registry")
    registry_bootstrap_parser.add_argument("--apply", action="store_true")
    registry_bootstrap_parser.add_argument("--json", action="store_true", dest="json_output")

    coverage_audit_parser = subparsers.add_parser("expression-audit-audience-coverage")
    coverage_audit_parser.add_argument("--json", action="store_true", dest="json_output")
    coverage_audit_parser.add_argument("--fail-on-warnings", action="store_true")

    roadmap_audit_parser = subparsers.add_parser("expression-audit-target-roadmap")
    roadmap_audit_parser.add_argument("--json", action="store_true", dest="json_output")
    roadmap_audit_parser.add_argument("--fail-on-hard-bugs", action="store_true")
    roadmap_audit_parser.add_argument("--fail-on-product-risk", action="store_true")

    eval_readiness_parser = subparsers.add_parser("expression-audit-eval-readiness")
    eval_readiness_parser.add_argument("--json", action="store_true", dest="json_output")
    eval_readiness_parser.add_argument("--fail-on-missing-golden", action="store_true")

    layer_readiness_parser = subparsers.add_parser("expression-audit-layer-readiness")
    layer_readiness_parser.add_argument("--json", action="store_true", dest="json_output")
    layer_readiness_parser.add_argument("--fail-on-product-risk", action="store_true")

    prompt_catalog_parser = subparsers.add_parser("expression-audit-prompt-catalog")
    prompt_catalog_parser.add_argument("--json", action="store_true", dest="json_output")

    prompt_surface_parser = subparsers.add_parser("expression-audit-ai-prompt-surface-coverage")
    prompt_surface_parser.add_argument("--json", action="store_true", dest="json_output")

    photo_prompt_parity_parser = subparsers.add_parser("expression-audit-photo-field-analysis-prompt-parity")
    photo_prompt_parity_parser.add_argument("--json", action="store_true", dest="json_output")

    invoice_prompt_parity_parser = subparsers.add_parser("expression-audit-photo-invoice-analysis-prompt-parity")
    invoice_prompt_parity_parser.add_argument("--json", action="store_true", dest="json_output")

    video_prompt_parity_parser = subparsers.add_parser("expression-audit-video-frame-insight-prompt-parity")
    video_prompt_parity_parser.add_argument("--json", action="store_true", dest="json_output")

    progress_prompt_parity_parser = subparsers.add_parser(
        "expression-audit-progress-report-multi-image-prompt-parity"
    )
    progress_prompt_parity_parser.add_argument("--json", action="store_true", dest="json_output")

    generated_report_prompt_parity_parser = subparsers.add_parser(
        "expression-audit-generated-report-markdown-legacy-prompt-parity"
    )
    generated_report_prompt_parity_parser.add_argument("--json", action="store_true", dest="json_output")

    contract_matrix_parser = subparsers.add_parser("expression-audit-contract-matrix")
    contract_matrix_parser.add_argument("--json", action="store_true", dest="json_output")

    visibility_boundary_parser = subparsers.add_parser("expression-audit-visibility-fact-boundary")
    visibility_boundary_parser.add_argument("--company-id", default=None)
    visibility_boundary_parser.add_argument("--window-days", type=int, default=30)
    visibility_boundary_parser.add_argument("--json", action="store_true", dest="json_output")

    promotion_surface_parser = subparsers.add_parser("expression-audit-promotion-surfaces")
    promotion_surface_parser.add_argument("--json", action="store_true", dest="json_output")
    promotion_surface_parser.add_argument("--fail-on-review", action="store_true")

    promotion_readiness_parser = subparsers.add_parser("expression-audit-promotion-readiness")
    promotion_readiness_parser.add_argument("--artifact-id", default=None)
    promotion_readiness_parser.add_argument("--company-id", default=None)
    promotion_readiness_parser.add_argument("--sample-limit", type=int, default=50)
    promotion_readiness_parser.add_argument("--json", action="store_true", dest="json_output")

    artifact_promotion_parser = subparsers.add_parser("expression-audit-artifact-promotion")
    artifact_promotion_parser.add_argument("--artifact-id", required=True)
    artifact_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    client_promotion_parser = subparsers.add_parser("expression-audit-client-progress-summary-promotion")
    client_promotion_parser.add_argument("--artifact-id", required=True)
    client_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    executive_promotion_parser = subparsers.add_parser("expression-audit-executive-company-health-promotion")
    executive_promotion_parser.add_argument("--artifact-id", required=True)
    executive_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    operations_promotion_parser = subparsers.add_parser("expression-audit-operations-health-summary-promotion")
    operations_promotion_parser.add_argument("--artifact-id", required=True)
    operations_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    finance_promotion_parser = subparsers.add_parser("expression-audit-finance-summary-promotion")
    finance_promotion_parser.add_argument("--artifact-id", required=True)
    finance_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    pm_status_card_contract_parser = subparsers.add_parser("expression-audit-project-manager-status-card-contract")
    pm_status_card_contract_parser.add_argument("--company-id", default=None)
    pm_status_card_contract_parser.add_argument("--sample-limit", type=int, default=50)
    pm_status_card_contract_parser.add_argument("--json", action="store_true", dest="json_output")

    mobile_read_contract_parser = subparsers.add_parser("expression-audit-mobile-promoted-read-contracts")
    mobile_read_contract_parser.add_argument("--company-id", default=None)
    mobile_read_contract_parser.add_argument("--sample-limit", type=int, default=50)
    mobile_read_contract_parser.add_argument("--json", action="store_true", dest="json_output")

    pm_status_card_promotion_parser = subparsers.add_parser(
        "expression-audit-project-manager-status-card-promotion"
    )
    pm_status_card_promotion_parser.add_argument("--artifact-id", required=True)
    pm_status_card_promotion_parser.add_argument("--json", action="store_true", dest="json_output")

    pm_status_card_promote_parser = subparsers.add_parser("expression-promote-project-manager-status-card")
    pm_status_card_promote_parser.add_argument("--artifact-id", required=True)
    pm_status_card_promote_parser.add_argument("--json", action="store_true", dest="json_output")

    golden_replay_parser = subparsers.add_parser("expression-golden-replay")
    golden_replay_parser.add_argument("--company-id", default=None)
    golden_replay_parser.add_argument("--artifact-type", default=None)
    golden_replay_parser.add_argument("--limit", type=int, default=200)
    golden_replay_parser.add_argument("--json", action="store_true", dest="json_output")

    revalidate_parser = subparsers.add_parser("expression-revalidate-artifacts")
    revalidate_parser.add_argument("--artifact-type", required=True)
    revalidate_parser.add_argument("--company-id", default=None)
    revalidate_parser.add_argument("--limit", type=int, default=200)
    revalidate_parser.add_argument("--apply", action="store_true")
    revalidate_parser.add_argument("--json", action="store_true", dest="json_output")

    production_gate_parser = subparsers.add_parser("expression-audit-production-gate")
    production_gate_parser.add_argument("--company-id", default=None)
    production_gate_parser.add_argument("--project-limit", type=int, default=8)
    production_gate_parser.add_argument("--client-limit", type=int, default=8)
    production_gate_parser.add_argument("--operations-limit", type=int, default=8)
    production_gate_parser.add_argument("--finance-limit", type=int, default=8)
    production_gate_parser.add_argument("--executive-limit", type=int, default=20)
    production_gate_parser.add_argument("--window-days", type=int, default=30)
    production_gate_parser.add_argument("--finance-readiness-window-days", type=int, default=365)
    production_gate_parser.add_argument("--operations-window-hours", type=int, default=24)
    production_gate_parser.add_argument("--language", default="en")
    production_gate_parser.add_argument("--min-project-samples", type=int, default=1)
    production_gate_parser.add_argument("--min-client-samples", type=int, default=1)
    production_gate_parser.add_argument("--min-operations-samples", type=int, default=1)
    production_gate_parser.add_argument("--min-finance-samples", type=int, default=0)
    production_gate_parser.add_argument("--min-finance-readiness-samples", type=int, default=1)
    production_gate_parser.add_argument("--json", action="store_true", dest="json_output")
    production_gate_parser.add_argument("--fail-on-warnings", action="store_true")

    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin(
            from_env=args.from_env,
            if_missing=args.if_missing,
            username=args.username,
            password=args.password,
            email=args.email,
            display_name=args.display_name,
        )
    if args.command == "photo-lens-seed-core":
        return photo_lens_seed_core()
    if args.command == "photo-lens-run":
        return photo_lens_run(
            photo_id=args.photo_id,
            lens_key=args.lens_key,
            model=args.model,
            backend_url=args.backend_url,
            force=args.force,
        )
    if args.command == "photo-lens-backfill":
        return photo_lens_backfill(
            company_id=args.company_id,
            project_id=args.project_id,
            lens_keys=args.lens_keys,
            limit=args.limit,
            model=args.model,
            backend_url=args.backend_url,
            sleep_seconds=args.sleep_seconds,
            force=args.force,
            json_output=args.json_output,
        )
    if args.command == "queue-requeue-dead-letter":
        return queue_requeue_dead_letter(
            task_type=args.task_type,
            company_id=args.company_id,
            since_hours=args.since_hours,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    if args.command == "photo-lens-coverage":
        return photo_lens_coverage(
            company_id=args.company_id,
            project_id=args.project_id,
            recent_hours=args.recent_hours,
        )
    if args.command == "project-manager-lens-report":
        return project_manager_lens_report(
            company_id=args.company_id,
            project_id=args.project_id,
            window_days=args.window_days,
            max_photos=args.max_photos,
            max_evidence_per_lens=args.max_evidence_per_lens,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-employee":
        return expression_shadow_employee(
            company_id=args.company_id,
            employee_id=args.employee_id,
            project_id=args.project_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-promote-artifact":
        return expression_promote_artifact(artifact_id=args.artifact_id)
    if args.command == "expression-shadow-progress-report":
        return expression_shadow_progress_report(
            report_id=args.report_id,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-progress-report-batch":
        return expression_shadow_progress_report_batch(
            limit=args.limit,
            company_id=args.company_id,
            project_id=args.project_id,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-progress-report-string":
        return expression_shadow_progress_report_string(
            report_id=args.report_id,
            field_path=args.field_path,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-progress-report-string-batch":
        return expression_shadow_progress_report_string_batch(
            limit=args.limit,
            company_id=args.company_id,
            project_id=args.project_id,
            chunks_per_report=args.chunks_per_report,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-report-markdown":
        return expression_shadow_report_markdown(
            report_id=args.report_id,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-report-markdown-batch":
        return expression_shadow_report_markdown_batch(
            limit=args.limit,
            company_id=args.company_id,
            project_id=args.project_id,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-report-markdown-section":
        return expression_shadow_report_markdown_section(
            report_id=args.report_id,
            section_key=args.section_key,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-report-markdown-section-batch":
        return expression_shadow_report_markdown_section_batch(
            limit=args.limit,
            company_id=args.company_id,
            project_id=args.project_id,
            section_keys=args.section_keys,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-project-manager-brief":
        return expression_shadow_project_manager_brief(
            company_id=args.company_id,
            project_id=args.project_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-project-manager-brief-batch":
        return expression_shadow_project_manager_brief_batch(
            limit=args.limit,
            company_id=args.company_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-client-progress-summary":
        return expression_shadow_client_progress_summary(
            company_id=args.company_id,
            project_id=args.project_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-client-progress-summary-batch":
        return expression_shadow_client_progress_summary_batch(
            limit=args.limit,
            company_id=args.company_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-operations-health-summary":
        return expression_shadow_operations_health_summary(
            company_id=args.company_id,
            window_hours=args.window_hours,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-operations-health-summary-batch":
        return expression_shadow_operations_health_summary_batch(
            limit=args.limit,
            company_id=args.company_id,
            window_hours=args.window_hours,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-finance-summary":
        return expression_shadow_finance_summary(
            company_id=args.company_id,
            project_id=args.project_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
        )
    if args.command == "expression-shadow-finance-summary-batch":
        return expression_shadow_finance_summary_batch(
            limit=args.limit,
            company_id=args.company_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-finance-fact-health":
        return expression_audit_finance_fact_health(
            company_id=args.company_id,
            window_days=args.window_days,
            limit=args.limit,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-finance-fact-attribution":
        return expression_audit_finance_fact_attribution(
            company_id=args.company_id,
            window_days=args.window_days,
            limit=args.limit,
            apply=args.apply,
            rollback_file=args.rollback_file,
            json_output=args.json_output,
        )
    if args.command == "expression-rollback-finance-fact-attribution":
        return expression_rollback_finance_fact_attribution(
            rollback_file=args.rollback_file,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-executive-company-health":
        return expression_shadow_executive_company_health(
            company_id=args.company_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-shadow-executive-company-health-batch":
        return expression_shadow_executive_company_health_batch(
            limit=args.limit,
            company_id=args.company_id,
            window_days=args.window_days,
            language=args.language,
            preferred_model=args.preferred_model,
            no_ai=args.no_ai,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-executive-company-health-shadow":
        return audit_executive_company_health_shadow(
            company_id=args.company_id,
            limit=args.limit,
            json_output=args.json_output,
            fail_on_warnings=args.fail_on_warnings,
        )
    if args.command == "expression-audit-registry":
        return audit_expression_registry(
            json_output=args.json_output,
            fail_on_warnings=args.fail_on_warnings,
        )
    if args.command == "expression-bootstrap-registry":
        return expression_bootstrap_registry(
            apply=args.apply,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-audience-coverage":
        return audit_expression_audience_coverage(
            json_output=args.json_output,
            fail_on_warnings=args.fail_on_warnings,
        )
    if args.command == "expression-audit-target-roadmap":
        return audit_expression_target_roadmap(
            json_output=args.json_output,
            fail_on_hard_bugs=args.fail_on_hard_bugs,
            fail_on_product_risk=args.fail_on_product_risk,
        )
    if args.command == "expression-audit-eval-readiness":
        return expression_audit_eval_readiness(
            json_output=args.json_output,
            fail_on_missing_golden=args.fail_on_missing_golden,
        )
    if args.command == "expression-audit-layer-readiness":
        return expression_audit_layer_readiness(
            json_output=args.json_output,
            fail_on_product_risk=args.fail_on_product_risk,
        )
    if args.command == "expression-audit-prompt-catalog":
        return expression_audit_prompt_catalog(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-ai-prompt-surface-coverage":
        return expression_audit_ai_prompt_surface_coverage(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-photo-field-analysis-prompt-parity":
        return expression_audit_photo_field_analysis_prompt_parity(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-photo-invoice-analysis-prompt-parity":
        return expression_audit_photo_invoice_analysis_prompt_parity(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-video-frame-insight-prompt-parity":
        return expression_audit_video_frame_insight_prompt_parity(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-progress-report-multi-image-prompt-parity":
        return expression_audit_progress_report_multi_image_prompt_parity(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-generated-report-markdown-legacy-prompt-parity":
        return expression_audit_generated_report_markdown_legacy_prompt_parity(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-contract-matrix":
        return expression_audit_contract_matrix(
            json_output=args.json_output,
        )
    if args.command == "expression-audit-visibility-fact-boundary":
        return expression_audit_visibility_fact_boundary(
            company_id=args.company_id,
            window_days=args.window_days,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-promotion-surfaces":
        return expression_audit_promotion_surfaces(
            json_output=args.json_output,
            fail_on_review=args.fail_on_review,
        )
    if args.command == "expression-audit-promotion-readiness":
        return expression_audit_promotion_readiness(
            artifact_id=args.artifact_id,
            company_id=args.company_id,
            sample_limit=args.sample_limit,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-artifact-promotion":
        return expression_audit_artifact_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-client-progress-summary-promotion":
        return expression_audit_client_progress_summary_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-executive-company-health-promotion":
        return expression_audit_executive_company_health_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-operations-health-summary-promotion":
        return expression_audit_operations_health_summary_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-finance-summary-promotion":
        return expression_audit_finance_summary_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-project-manager-status-card-contract":
        return expression_audit_project_manager_status_card_contract(
            company_id=args.company_id,
            sample_limit=args.sample_limit,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-mobile-promoted-read-contracts":
        return expression_audit_mobile_promoted_read_contracts(
            company_id=args.company_id,
            sample_limit=args.sample_limit,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-project-manager-status-card-promotion":
        return expression_audit_project_manager_status_card_promotion(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-promote-project-manager-status-card":
        return expression_promote_project_manager_status_card(
            artifact_id=args.artifact_id,
            json_output=args.json_output,
        )
    if args.command == "expression-golden-replay":
        return expression_golden_replay(
            company_id=args.company_id,
            artifact_type=args.artifact_type,
            limit=args.limit,
            json_output=args.json_output,
        )
    if args.command == "expression-revalidate-artifacts":
        return expression_revalidate_artifacts(
            artifact_type=args.artifact_type,
            company_id=args.company_id,
            limit=args.limit,
            apply=args.apply,
            json_output=args.json_output,
        )
    if args.command == "expression-audit-production-gate":
        return expression_audit_production_gate(
            company_id=args.company_id,
            project_limit=args.project_limit,
            client_limit=args.client_limit,
            operations_limit=args.operations_limit,
            finance_limit=args.finance_limit,
            executive_limit=args.executive_limit,
            window_days=args.window_days,
            finance_readiness_window_days=args.finance_readiness_window_days,
            operations_window_hours=args.operations_window_hours,
            language=args.language,
            json_output=args.json_output,
            fail_on_warnings=args.fail_on_warnings,
            min_project_samples=args.min_project_samples,
            min_client_samples=args.min_client_samples,
            min_operations_samples=args.min_operations_samples,
            min_finance_samples=args.min_finance_samples,
            min_finance_readiness_samples=args.min_finance_readiness_samples,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
