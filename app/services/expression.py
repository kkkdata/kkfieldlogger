from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import requests
from requests import RequestException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import to_utc_iso, utc_now
from app.models import (
    ApprovalStatus,
    AIAnalysisLog,
    AIAnalysisStatus,
    Company,
    EvidenceObservation,
    ExpressionArtifact,
    ExpressionAudience,
    ExpressionForbiddenPhrase,
    ExpressionOutputContract,
    ExpressionPromptBinding,
    ExpressionPromptVersion,
    FactSnapshot,
    Photo,
    PhotoType,
    PhotoVisibility,
    ProgressReportStatus,
    ProgressReport,
    Project,
    ProjectStatus,
    ReceiptFact,
    TaskJob,
    TaskStatus,
)
from app.services.ai_pipeline import (
    AIBackendError,
    AIBackendNode,
    GEMINI_TYPE,
    OLLAMA_TYPE,
    call_text_backend,
    resolve_ai_backends_for_tenant,
)

EMPLOYEE_CONTRIBUTION_ARTIFACT = "employee_contribution_narrative"
EMPLOYEE_CONTRIBUTION_SCOPE = "employee_project_window"
EMPLOYEE_CONTRIBUTION_ASSEMBLER_VERSION = "employee_contribution_v1"
PROGRESS_REPORT_TRANSLATION_ARTIFACT = "progress_report_translation"
PROGRESS_REPORT_TRANSLATION_SCOPE = "progress_report"
PROGRESS_REPORT_TRANSLATION_ASSEMBLER_VERSION = "progress_report_translation_v1"
PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT = "progress_report_string_translation"
PROGRESS_REPORT_STRING_TRANSLATION_SCOPE = "progress_report_string"
PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION = "pr_string_translation_v1"
GENERATED_REPORT_MARKDOWN_ARTIFACT = "generated_report_markdown"
GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT = "generated_report_markdown_section"
GENERATED_REPORT_MARKDOWN_SECTION_KEYS = frozenset(
    {"summary", "completed_work", "risks", "next_steps", "timeline"},
)
GENERATED_REPORT_MARKDOWN_ASSEMBLER_VERSION = "generated_report_markdown_v1"
PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT = "project_manager_decision_brief"
PROJECT_MANAGER_DECISION_BRIEF_SCOPE = "project_period"
PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION = "pm_decision_brief_v1"
CLIENT_PROGRESS_SUMMARY_ARTIFACT = "client_progress_summary"
CLIENT_PROGRESS_SUMMARY_SCOPE = "client_project_period"
CLIENT_PROGRESS_SUMMARY_ASSEMBLER_VERSION = "client_progress_v1"
CLIENT_PROGRESS_DISCLAIMER = "This summary is based only on approved client-visible records and is not a contract commitment or final acceptance decision."
OPERATIONS_HEALTH_SUMMARY_ARTIFACT = "operations_health_summary"
OPERATIONS_HEALTH_SUMMARY_SCOPE = "company_operations"
OPERATIONS_HEALTH_SUMMARY_ASSEMBLER_VERSION = "ops_health_v1"
FINANCE_SUMMARY_ARTIFACT = "finance_summary"
FINANCE_SUMMARY_SCOPE = "project_finance_period"
FINANCE_SUMMARY_ASSEMBLER_VERSION = "finance_summary_v1"
EXECUTIVE_COMPANY_HEALTH_ARTIFACT = "executive_company_health_summary"
EXECUTIVE_COMPANY_HEALTH_SCOPE = "company_health_period"
EXECUTIVE_COMPANY_HEALTH_ASSEMBLER_VERSION = "exec_company_health_v1"
FIXED_EMPLOYEE_DISCLAIMER = "该说明仅用于现场记录参考，不是绩效评分。"
DEFAULT_EXPRESSION_TEXT_MODEL = "qwen2.5:7b-instruct"
_EMPLOYEE_ZERO_CURRENT_PHOTO_PHRASES = (
    "这些照片",
    "这批记录",
    "连续记录",
    "这批照片",
)
EXPRESSION_GENERATION_TIMEOUT_SECONDS = 180
EXPRESSION_GENERATION_NUM_PREDICT = 4096
PROMOTABLE_EXPRESSION_STATUSES = {"shadow_valid", "shadow_fallback_valid", "promoted_valid"}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z0-9_.-]+\}")
_SPANISH_LOW_QUALITY_PATTERNS = (
    re.compile(r"\bel\s+roca\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class ExpressionRunResult:
    snapshot: FactSnapshot
    artifact: ExpressionArtifact
    used_fallback: bool
    errors: list[str]


def _safe_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_list(value: Any, *, limit: int = 6, item_limit: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _safe_text(item, limit=item_limit)
        key = text.casefold()
        if not text or key in seen:
            continue
        result.append(text)
        seen.add(key)
        if len(result) >= limit:
            break
    return result


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _count_facts(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_count_facts(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_facts(item) for item in value)
    return 1 if value not in (None, "", []) else 0


def _photo_highlight(photo: Photo) -> dict[str, Any] | None:
    tag_json = photo.tag_json if isinstance(photo.tag_json, dict) else {}
    summary = _safe_text(
        (tag_json.get("ai_summary_translations") or {}).get("zh")
        if isinstance(tag_json.get("ai_summary_translations"), dict)
        else None,
        limit=180,
    )
    if not summary:
        summary = _safe_text(tag_json.get("ai_summary"), limit=180)
    labels = _safe_list(tag_json.get("labels"), limit=4, item_limit=60)
    defects = _safe_list(tag_json.get("defects"), limit=3, item_limit=80)
    if not summary and not labels and not defects:
        return None
    return {
        "photo_id": photo.id,
        "captured_at_utc": to_utc_iso(photo.captured_at_utc),
        "summary": summary,
        "labels": labels,
        "defects": defects,
        "approval_status": photo.approval_status.value if hasattr(photo.approval_status, "value") else photo.approval_status,
    }


def _has_cjk(text: Any) -> bool:
    return bool(_CJK_RE.search(str(text or "")))


def _active_day_count(photos: list[Photo]) -> int:
    return len({photo.captured_at_utc.date().isoformat() for photo in photos if photo.captured_at_utc is not None})


def _completed_ai_count(photos: list[Photo]) -> int:
    return sum(1 for photo in photos if photo.labeling_status == "completed")


def _approved_count(photos: list[Photo]) -> int:
    return sum(1 for photo in photos if photo.approval_status == ApprovalStatus.approved)


def _observation_summary(db: Session, *, photo_ids: list[int]) -> dict[str, Any]:
    if not photo_ids:
        return {"by_type": {}, "by_severity": {}, "examples": []}
    by_type_rows = db.execute(
        select(EvidenceObservation.observation_type, func.count())
        .where(EvidenceObservation.photo_id.in_(photo_ids))
        .group_by(EvidenceObservation.observation_type)
    ).all()
    by_severity_rows = db.execute(
        select(EvidenceObservation.severity, func.count())
        .where(EvidenceObservation.photo_id.in_(photo_ids))
        .group_by(EvidenceObservation.severity)
    ).all()
    examples = list(
        db.scalars(
            select(EvidenceObservation)
            .where(
                EvidenceObservation.photo_id.in_(photo_ids),
                EvidenceObservation.observation_type.in_(["summary", "quality", "safety", "material", "equipment"]),
            )
            .order_by(EvidenceObservation.observed_at.desc(), EvidenceObservation.created_at.desc())
            .limit(8)
        )
    )
    return {
        "by_type": {str(key or "unknown"): int(count) for key, count in by_type_rows},
        "by_severity": {str(key or "unknown"): int(count) for key, count in by_severity_rows},
        "examples": [
            {
                "photo_id": row.photo_id,
                "type": row.observation_type,
                "title": _safe_text(row.title, limit=120),
                "content": _safe_text(row.content_text, limit=180),
                "severity": row.severity,
            }
            for row in examples
        ],
    }


def build_employee_contribution_facts(
    db: Session,
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int = 30,
    now: Any | None = None,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    now_value = now or utc_now()
    current_start = now_value - timedelta(days=window_days)
    previous_start = current_start - timedelta(days=window_days)

    current_photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == company_id,
                Photo.employee_id == employee_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= current_start,
                Photo.captured_at_utc <= now_value,
            )
            .order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
        )
    )
    previous_photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == company_id,
                Photo.employee_id == employee_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= previous_start,
                Photo.captured_at_utc < current_start,
            )
        )
    )
    current_photo_ids = [photo.id for photo in current_photos]
    recent_highlights = [item for item in (_photo_highlight(photo) for photo in current_photos[:12]) if item is not None][:5]
    useful_photo_count = sum(1 for item in recent_highlights if item.get("summary") or item.get("labels"))

    return {
        "scope": {
            "company_id": company_id,
            "employee_id": employee_id,
            "project_id": project_id,
            "scope_type": EMPLOYEE_CONTRIBUTION_SCOPE,
        },
        "window": {
            "current": {
                "days": window_days,
                "start": to_utc_iso(current_start),
                "end": to_utc_iso(now_value),
            },
            "previous": {
                "days": window_days,
                "start": to_utc_iso(previous_start),
                "end": to_utc_iso(current_start),
            },
        },
        "counts": {
            "photos_current": len(current_photos),
            "photos_previous": len(previous_photos),
            "active_days_current": _active_day_count(current_photos),
            "active_days_previous": _active_day_count(previous_photos),
            "completed_ai_current": _completed_ai_count(current_photos),
            "completed_ai_previous": _completed_ai_count(previous_photos),
            "approved_current": _approved_count(current_photos),
            "approved_previous": _approved_count(previous_photos),
            "recent_highlight_count": len(recent_highlights),
            "useful_photo_proxy_current": useful_photo_count,
        },
        "trend": {
            "photo_delta": len(current_photos) - len(previous_photos),
            "active_day_delta": _active_day_count(current_photos) - _active_day_count(previous_photos),
            "completed_ai_delta": _completed_ai_count(current_photos) - _completed_ai_count(previous_photos),
        },
        "coverage": {
            "has_current_photos": bool(current_photos),
            "has_completed_ai": _completed_ai_count(current_photos) > 0,
            "has_recent_highlights": bool(recent_highlights),
            "completed_ai_ratio": round(_completed_ai_count(current_photos) / len(current_photos), 3) if current_photos else 0,
        },
        "recent_highlights": recent_highlights,
        "observations": _observation_summary(db, photo_ids=current_photo_ids),
        "guardrails": {
            "do_not_show_score": True,
            "do_not_rank_against_others": True,
            "do_not_claim_performance": True,
            "missing_coverage_is_not_employee_fault": True,
            "fixed_disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
        },
    }


def create_or_get_employee_fact_snapshot(
    db: Session,
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int = 30,
) -> FactSnapshot:
    scope_key = {
        "company_id": company_id,
        "employee_id": employee_id,
        "project_id": project_id,
        "window_days": window_days,
    }
    scope_key_hash = _json_hash(scope_key)
    existing = db.scalar(
        select(FactSnapshot).where(
            FactSnapshot.company_id == company_id,
            FactSnapshot.scope_type == EMPLOYEE_CONTRIBUTION_SCOPE,
            FactSnapshot.scope_key_hash == scope_key_hash,
            FactSnapshot.assembler_version == EMPLOYEE_CONTRIBUTION_ASSEMBLER_VERSION,
        )
    )
    if existing is not None:
        return existing

    facts = build_employee_contribution_facts(
        db,
        company_id=company_id,
        employee_id=employee_id,
        project_id=project_id,
        window_days=window_days,
    )
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=employee_id,
        project_id=project_id,
        scope_type=EMPLOYEE_CONTRIBUTION_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=EMPLOYEE_CONTRIBUTION_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["photos", "evidence_observations"],
            "photo_filters": {
                "company_id": company_id,
                "employee_id": employee_id,
                "project_id": project_id,
                "photo_type": PhotoType.project.value,
                "deleted": False,
            },
        },
        facts_json=facts,
        fact_count=_count_facts(facts),
        coverage_json=facts.get("coverage") if isinstance(facts.get("coverage"), dict) else None,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _count_fact_leaves(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_count_fact_leaves(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_fact_leaves(item) for item in value)
    return 1 if value is not None else 0


def build_progress_report_translation_facts(db: Session, *, report: ProgressReport) -> dict[str, Any]:
    from app.services.reports import (
        PROGRESS_REPORT_PAYLOAD_VERSION,
        _extract_json_object,
        _extract_progress_report_payload_marker,
        _normalize_progress_report_structure,
    )

    source_photo_ids = [int(item) for item in (report.source_photo_ids or []) if str(item).isdigit()]
    photos = (
        list(db.scalars(select(Photo).where(Photo.company_id == report.company_id, Photo.id.in_(source_photo_ids))))
        if source_photo_ids
        else []
    )
    _, embedded_payload = _extract_progress_report_payload_marker(report.report_content)
    payload_envelope = embedded_payload if isinstance(embedded_payload, dict) else None
    raw_structured_payload: Any = embedded_payload
    if raw_structured_payload is None and report.report_content:
        raw_structured_payload = _extract_json_object(report.report_content)
    raw_translations: dict[str, Any] = {}
    if payload_envelope and (
        "structured_report" in payload_envelope
        or "translations" in payload_envelope
        or payload_envelope.get("version") == PROGRESS_REPORT_PAYLOAD_VERSION
    ):
        raw_structured_payload = payload_envelope.get("structured_report")
        translations = payload_envelope.get("translations")
        raw_translations = translations if isinstance(translations, dict) else {}
    structured_report = _normalize_progress_report_structure(
        raw_structured_payload if isinstance(raw_structured_payload, dict) else {},
        photos,
    )
    return {
        "scope": {
            "company_id": report.company_id,
            "project_id": report.project_id,
            "report_id": report.id,
            "scope_type": PROGRESS_REPORT_TRANSLATION_SCOPE,
        },
        "source_report": {
            "status": report.status.value if hasattr(report.status, "value") else str(report.status),
            "source_photo_ids": source_photo_ids,
            "created_at": to_utc_iso(report.created_at),
            "completed_at": to_utc_iso(report.completed_at),
            "has_embedded_payload": payload_envelope is not None,
            "existing_translation_languages": sorted(raw_translations.keys()),
        },
        "structured_report": structured_report,
        "guardrails": {
            "target_languages": ["zh", "en", "es"],
            "preserve_keys": True,
            "preserve_enums": {
                "overall_status": ["on_track", "at_risk", "blocked", "unknown"],
                "confidence_level": ["high", "medium", "low"],
            },
            "preserve_photo_ids": True,
        },
    }


def create_or_get_progress_report_translation_snapshot(db: Session, *, report_id: str) -> FactSnapshot:
    report = db.get(ProgressReport, report_id)
    if report is None:
        raise ValueError(f"Progress report {report_id} was not found")
    facts = build_progress_report_translation_facts(db, report=report)
    scope_key = {
        "company_id": report.company_id,
        "project_id": report.project_id,
        "report_id": report.id,
    }
    scope_key_hash = _json_hash(scope_key)
    existing = db.scalar(
        select(FactSnapshot).where(
            FactSnapshot.company_id == report.company_id,
            FactSnapshot.scope_type == PROGRESS_REPORT_TRANSLATION_SCOPE,
            FactSnapshot.scope_key_hash == scope_key_hash,
            FactSnapshot.assembler_version == PROGRESS_REPORT_TRANSLATION_ASSEMBLER_VERSION,
        )
    )
    if existing is not None:
        return existing

    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=report.tenant_id or report.company_id,
        company_id=report.company_id,
        employee_id=None,
        project_id=report.project_id,
        scope_type=PROGRESS_REPORT_TRANSLATION_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=PROGRESS_REPORT_TRANSLATION_ASSEMBLER_VERSION,
        source_manifest_json={"tables": ["progress_reports", "photos"], "report_id": report.id},
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_structured_report": bool(facts.get("structured_report")),
            "source_photo_count": len(facts["source_report"]["source_photo_ids"]),
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _latest_valid_progress_translation_artifact(db: Session, *, report: ProgressReport) -> ExpressionArtifact | None:
    scope_key = {
        "company_id": report.company_id,
        "project_id": report.project_id,
        "report_id": report.id,
    }
    rows = list(
        db.scalars(
            select(ExpressionArtifact)
            .where(
                ExpressionArtifact.company_id == report.company_id,
                ExpressionArtifact.scope_type == PROGRESS_REPORT_TRANSLATION_SCOPE,
                ExpressionArtifact.artifact_type == PROGRESS_REPORT_TRANSLATION_ARTIFACT,
                ExpressionArtifact.validation_status.in_(["shadow_valid", "promoted_valid"]),
            )
            .order_by(ExpressionArtifact.promoted.desc(), ExpressionArtifact.created_at.desc())
            .limit(20)
        )
    )
    for row in rows:
        if row.scope_key_json == scope_key and isinstance(row.structured_json, dict):
            return row
    return None


def build_generated_report_markdown_facts(db: Session, *, report: ProgressReport) -> dict[str, Any]:
    facts = build_progress_report_translation_facts(db, report=report)
    translation_artifact = _latest_valid_progress_translation_artifact(db, report=report)
    translations = translation_artifact.structured_json if translation_artifact and isinstance(translation_artifact.structured_json, dict) else {}
    facts["source_artifacts"] = {
        "progress_report_translation": {
            "artifact_id": translation_artifact.id if translation_artifact else None,
            "validation_status": translation_artifact.validation_status if translation_artifact else None,
            "promoted": bool(translation_artifact.promoted) if translation_artifact else False,
            "languages": sorted(translations.keys()) if isinstance(translations, dict) else [],
        }
    }
    facts["translations"] = translations
    facts["guardrails"] = {
        **(facts.get("guardrails") if isinstance(facts.get("guardrails"), dict) else {}),
        "markdown": {
            "no_raw_html": True,
            "fixed_sections": ["summary", "completed_work", "risks", "next_steps", "timeline"],
            "source_refs_required": True,
        },
    }
    return facts


def create_or_get_generated_report_markdown_snapshot(db: Session, *, report_id: str) -> FactSnapshot:
    report = db.get(ProgressReport, report_id)
    if report is None:
        raise ValueError(f"Progress report {report_id} was not found")
    facts = build_generated_report_markdown_facts(db, report=report)
    scope_key = {
        "company_id": report.company_id,
        "project_id": report.project_id,
        "report_id": report.id,
    }
    scope_key_hash = _json_hash(scope_key)
    existing = db.scalar(
        select(FactSnapshot).where(
            FactSnapshot.company_id == report.company_id,
            FactSnapshot.scope_type == PROGRESS_REPORT_TRANSLATION_SCOPE,
            FactSnapshot.scope_key_hash == scope_key_hash,
            FactSnapshot.assembler_version == GENERATED_REPORT_MARKDOWN_ASSEMBLER_VERSION,
        )
    )
    if existing is not None:
        return existing

    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=report.tenant_id or report.company_id,
        company_id=report.company_id,
        employee_id=None,
        project_id=report.project_id,
        scope_type=PROGRESS_REPORT_TRANSLATION_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=GENERATED_REPORT_MARKDOWN_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["progress_reports", "photos", "expression_artifacts"],
            "report_id": report.id,
            "source_artifact_types": [PROGRESS_REPORT_TRANSLATION_ARTIFACT],
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_structured_report": bool(facts.get("structured_report")),
            "has_translation_artifact": bool(
                (facts.get("source_artifacts") or {}).get("progress_report_translation", {}).get("artifact_id")
            ),
            "source_photo_count": len(facts["source_report"]["source_photo_ids"]),
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _project_photo_metrics(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    current_start: Any,
    previous_start: Any,
    now_value: Any,
) -> dict[str, Any]:
    current_photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == company_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= current_start,
                Photo.captured_at_utc <= now_value,
            )
            .order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
        )
    )
    previous_count = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                Photo.company_id == company_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= previous_start,
                Photo.captured_at_utc < current_start,
            )
        )
        or 0
    )
    by_employee_rows = db.execute(
        select(Photo.employee_id, func.count(Photo.id), func.count(func.distinct(func.date(Photo.captured_at_utc))))
        .where(
            Photo.company_id == company_id,
            Photo.project_id == project_id,
            Photo.photo_type == PhotoType.project,
            Photo.deleted.is_(False),
            Photo.captured_at_utc >= current_start,
            Photo.captured_at_utc <= now_value,
        )
        .group_by(Photo.employee_id)
        .order_by(func.count(Photo.id).desc(), Photo.employee_id.asc())
        .limit(20)
    ).all()
    current_count = len(current_photos)
    completed_ai = _completed_ai_count(current_photos)
    approved_count = _approved_count(current_photos)
    return {
        "photos_current": current_count,
        "photos_previous": previous_count,
        "photo_delta": current_count - previous_count,
        "active_days_current": _active_day_count(current_photos),
        "completed_ai_current": completed_ai,
        "approved_current": approved_count,
        "ai_completion_ratio": round(completed_ai / current_count, 4) if current_count else None,
        "approval_ratio": round(approved_count / current_count, 4) if current_count else None,
        "employee_contributors_current": len(by_employee_rows),
        "employee_contributor_summary": [
            {"employee_id": str(employee_id), "photo_count": int(photo_count), "active_days": int(active_days)}
            for employee_id, photo_count, active_days in by_employee_rows
        ],
    }


def _progress_report_metrics(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    status_rows = db.execute(
        select(ProgressReport.status, func.count(ProgressReport.id))
        .where(ProgressReport.company_id == company_id, ProgressReport.project_id == project_id)
        .group_by(ProgressReport.status)
    ).all()
    latest_report = db.scalar(
        select(ProgressReport)
        .where(
            ProgressReport.company_id == company_id,
            ProgressReport.project_id == project_id,
            ProgressReport.status == ProgressReportStatus.completed,
            ProgressReport.report_content.is_not(None),
        )
        .order_by(
            ProgressReport.completed_at.is_(None),
            ProgressReport.completed_at.desc(),
            ProgressReport.created_at.desc(),
        )
        .limit(1)
    )
    structured_summary: dict[str, Any] = {}
    if latest_report is not None:
        latest_facts = build_progress_report_translation_facts(db, report=latest_report)
        structured_report = latest_facts.get("structured_report") if isinstance(latest_facts.get("structured_report"), dict) else {}
        structured_summary = {
            "overall_progress_percent": structured_report.get("overall_progress_percent"),
            "overall_status": structured_report.get("overall_status"),
            "confidence_level": structured_report.get("confidence_level"),
            "safety_risk_count": len(structured_report.get("safety_risks") or []),
            "quality_risk_count": len(structured_report.get("quality_risks") or []),
            "work_remaining_count": len(structured_report.get("work_remaining") or []),
            "evidence_limitation_count": len(structured_report.get("evidence_limitations") or []),
        }
    completed_with_content = int(
        db.scalar(
            select(func.count(ProgressReport.id)).where(
                ProgressReport.company_id == company_id,
                ProgressReport.project_id == project_id,
                ProgressReport.status == ProgressReportStatus.completed,
                ProgressReport.report_content.is_not(None),
            )
        )
        or 0
    )
    return {
        "status_counts": {
            (status.value if hasattr(status, "value") else str(status)): int(count)
            for status, count in status_rows
        },
        "completed_with_content": completed_with_content,
        "latest_completed_report_id": latest_report.id if latest_report is not None else None,
        "latest_completed_at": to_utc_iso(latest_report.completed_at) if latest_report is not None else None,
        "latest_structured_summary": structured_summary,
    }


def _project_expression_metrics(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    artifact_types = (
        EMPLOYEE_CONTRIBUTION_ARTIFACT,
        PROGRESS_REPORT_TRANSLATION_ARTIFACT,
        GENERATED_REPORT_MARKDOWN_ARTIFACT,
    )
    result: dict[str, Any] = {}
    for artifact_type in artifact_types:
        rows = list(
            db.scalars(
                select(ExpressionArtifact)
                .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id)
                .where(
                    ExpressionArtifact.company_id == company_id,
                    ExpressionArtifact.artifact_type == artifact_type,
                    FactSnapshot.project_id == project_id,
                )
                .order_by(ExpressionArtifact.created_at.desc())
                .limit(50)
            )
        )
        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row.validation_status] = status_counts.get(row.validation_status, 0) + 1
        result[artifact_type] = {
            "recent_artifacts": len(rows),
            "promoted_artifacts": sum(1 for row in rows if row.promoted),
            "validation_status_counts": status_counts,
            "latest_created_at": to_utc_iso(rows[0].created_at) if rows else None,
        }
    return result


def build_project_manager_decision_brief_facts(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    now: Any | None = None,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    now_value = now or utc_now()
    current_start = now_value - timedelta(days=window_days)
    previous_start = current_start - timedelta(days=window_days)
    project = db.scalar(select(Project).where(Project.company_id == company_id, Project.project_id == project_id))
    if project is None:
        raise ValueError(f"Project {project_id} was not found for company {company_id}")
    return {
        "scope": {
            "company_id": company_id,
            "project_id": project_id,
            "scope_type": PROJECT_MANAGER_DECISION_BRIEF_SCOPE,
        },
        "window": {
            "days": window_days,
            "current_start_utc": to_utc_iso(current_start),
            "current_end_utc": to_utc_iso(now_value),
            "previous_start_utc": to_utc_iso(previous_start),
        },
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
            "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        },
        "photo_metrics": _project_photo_metrics(
            db,
            company_id=company_id,
            project_id=project_id,
            current_start=current_start,
            previous_start=previous_start,
            now_value=now_value,
        ),
        "progress_report_metrics": _progress_report_metrics(db, company_id=company_id, project_id=project_id),
        "expression_metrics": _project_expression_metrics(db, company_id=company_id, project_id=project_id),
        "guardrails": {
            "db_only": True,
            "no_disk_file_reads": True,
            "ai_may_polish_only": True,
            "ai_may_not_change_decision_surface": True,
            "shadow_only_until_promoted": True,
        },
    }


def create_or_get_project_manager_decision_brief_snapshot(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
) -> FactSnapshot:
    window_days = max(1, min(int(window_days), 365))
    scope_key = {
        "company_id": company_id,
        "project_id": project_id,
        "window_days": window_days,
        "snapshot_run_id": str(uuid4()),
    }
    scope_key_hash = _json_hash(scope_key)
    facts = build_project_manager_decision_brief_facts(
        db,
        company_id=company_id,
        project_id=project_id,
        window_days=window_days,
    )
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=None,
        project_id=project_id,
        scope_type=PROJECT_MANAGER_DECISION_BRIEF_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["projects", "photos", "progress_reports", "expression_artifacts", "fact_snapshots"],
            "photo_filters": {
                "company_id": company_id,
                "project_id": project_id,
                "photo_type": PhotoType.project.value,
                "deleted": False,
            },
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_current_photos": int(facts["photo_metrics"]["photos_current"]) > 0,
            "has_completed_progress_report": int(facts["progress_report_metrics"]["completed_with_content"]) > 0,
            "has_promoted_employee_contribution": int(
                facts["expression_metrics"][EMPLOYEE_CONTRIBUTION_ARTIFACT]["promoted_artifacts"]
            )
            > 0,
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


CLIENT_PROGRESS_DENIED_FACT_KEYS = {
    "employee_id",
    "employee_contributor_summary",
    "file_path",
    "storage_path",
    "image_url",
    "thumb_url",
    "original_file_name",
    "manager_brief",
    "safety_risks",
    "quality_risks",
    "immediate_decisions",
    "recommended_actions",
    "work_remaining",
    "material_inventory_signals",
    "water_housekeeping_signals",
    "uncertain_items",
    "timeline_observations",
    "expression_metrics",
    "ai_completion_ratio",
    "labeling_status",
    "prompt_used",
    "raw_model_output",
    "api_key",
    "secret",
    "password",
    "token",
    "receipt_facts",
    "purchaser_name",
}


_CLIENT_PROGRESS_DENIED_TEXT_RE = re.compile(
    r"(https?://|/opt/|/app/|/tmp/|[A-Za-z]:\\|file_path|storage_path|image_url|thumb_url|"
    r"api_key|secret|password|token|prompt_used|raw_model_output|\bemployee_id\b|(?<![A-Za-z0-9-])(?-i:E\d{2,})(?![A-Za-z0-9-])|"
    r"\binternal\b|\bcost\b|\bpenalty\b|\bbreach\b)",
    flags=re.IGNORECASE,
)


def _client_safe_text(value: Any, *, limit: int = 240) -> str:
    text = _safe_text(value, limit=limit)
    if not text or _CLIENT_PROGRESS_DENIED_TEXT_RE.search(text):
        return ""
    return text


def _client_safe_list(value: Any, *, limit: int = 6, item_limit: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _client_safe_text(item, limit=item_limit)
        key = text.casefold()
        if not text or key in seen:
            continue
        result.append(text)
        seen.add(key)
        if len(result) >= limit:
            break
    return result


def _client_visible_photo_metrics(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    current_start: Any,
    previous_start: Any,
    now_value: Any,
) -> dict[str, Any]:
    current_photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == company_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.approval_status == ApprovalStatus.approved,
                Photo.visibility == PhotoVisibility.client_visible,
                Photo.captured_at_utc >= current_start,
                Photo.captured_at_utc <= now_value,
            )
            .order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
        )
    )
    previous_count = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                Photo.company_id == company_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.approval_status == ApprovalStatus.approved,
                Photo.visibility == PhotoVisibility.client_visible,
                Photo.captured_at_utc >= previous_start,
                Photo.captured_at_utc < current_start,
            )
        )
        or 0
    )
    return {
        "photos_current": len(current_photos),
        "photos_previous": previous_count,
        "photo_delta": len(current_photos) - previous_count,
        "active_days_current": _active_day_count(current_photos),
        "latest_photo_at": to_utc_iso(current_photos[0].captured_at_utc) if current_photos else None,
    }


def _latest_client_progress_report(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    report = db.scalar(
        select(ProgressReport)
        .where(
            ProgressReport.company_id == company_id,
            ProgressReport.project_id == project_id,
            ProgressReport.status == ProgressReportStatus.completed,
            ProgressReport.report_content.is_not(None),
        )
        .order_by(
            ProgressReport.completed_at.is_(None),
            ProgressReport.completed_at.desc(),
            ProgressReport.created_at.desc(),
        )
        .limit(1)
    )
    if report is None:
        return {
            "report_id": None,
            "completed_at": None,
            "has_completed_report": False,
            "client_report": {},
        }
    source_facts = build_progress_report_translation_facts(db, report=report)
    structured = source_facts.get("structured_report") if isinstance(source_facts.get("structured_report"), dict) else {}
    client_report = {
        "executive_summary": _client_safe_text(structured.get("executive_summary"), limit=500),
        "overall_progress_percent": structured.get("overall_progress_percent"),
        "overall_status": structured.get("overall_status"),
        "confidence_level": structured.get("confidence_level"),
        "key_changes": _client_safe_list(structured.get("key_changes"), limit=4, item_limit=240),
        "work_completed": _client_safe_list(structured.get("work_completed"), limit=4, item_limit=240),
        "evidence_limitations": _client_safe_list(structured.get("evidence_limitations"), limit=3, item_limit=220),
    }
    return {
        "report_id": report.id,
        "completed_at": to_utc_iso(report.completed_at),
        "has_completed_report": True,
        "client_report": client_report,
    }


def build_client_progress_summary_facts(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    now: Any | None = None,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    now_value = now or utc_now()
    current_start = now_value - timedelta(days=window_days)
    previous_start = current_start - timedelta(days=window_days)
    project = db.scalar(select(Project).where(Project.company_id == company_id, Project.project_id == project_id))
    if project is None:
        raise ValueError(f"Project {project_id} was not found for company {company_id}")
    return {
        "scope": {
            "company_id": company_id,
            "project_id": project_id,
            "scope_type": CLIENT_PROGRESS_SUMMARY_SCOPE,
        },
        "window": {
            "days": window_days,
            "current_start_utc": to_utc_iso(current_start),
            "current_end_utc": to_utc_iso(now_value),
            "previous_start_utc": to_utc_iso(previous_start),
        },
        "project": {
            "project_id": project.project_id,
            "project_name": _client_safe_text(project.project_name, limit=240),
            "client_name": _client_safe_text(project.client_name, limit=240),
            "location": _client_safe_text(project.location, limit=240),
            "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        },
        "client_visible_photo_metrics": _client_visible_photo_metrics(
            db,
            company_id=company_id,
            project_id=project_id,
            current_start=current_start,
            previous_start=previous_start,
            now_value=now_value,
        ),
        "latest_progress_report": _latest_client_progress_report(db, company_id=company_id, project_id=project_id),
        "guardrails": {
            "db_only": True,
            "redaction_profile": "client_progress_v1",
            "approved_client_visible_photos_only": True,
            "denied_fact_keys": sorted(CLIENT_PROGRESS_DENIED_FACT_KEYS),
            "shadow_only_until_promoted": True,
        },
    }


def create_or_get_client_progress_summary_snapshot(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
) -> FactSnapshot:
    window_days = max(1, min(int(window_days), 365))
    scope_key = {
        "company_id": company_id,
        "project_id": project_id,
        "window_days": window_days,
        "snapshot_run_id": str(uuid4()),
    }
    scope_key_hash = _json_hash(scope_key)
    facts = build_client_progress_summary_facts(
        db,
        company_id=company_id,
        project_id=project_id,
        window_days=window_days,
    )
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=None,
        project_id=project_id,
        scope_type=CLIENT_PROGRESS_SUMMARY_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=CLIENT_PROGRESS_SUMMARY_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["projects", "photos", "progress_reports"],
            "redaction_profile": "client_progress_v1",
            "photo_filters": {
                "company_id": company_id,
                "project_id": project_id,
                "photo_type": PhotoType.project.value,
                "deleted": False,
                "approval_status": ApprovalStatus.approved.value,
                "visibility": PhotoVisibility.client_visible.value,
            },
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_client_visible_photos": int(facts["client_visible_photo_metrics"]["photos_current"]) > 0,
            "has_completed_progress_report": bool(facts["latest_progress_report"]["has_completed_report"]),
            "redaction_profile": "client_progress_v1",
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


OPERATIONS_DENIED_FACT_KEYS = {
    "file_path",
    "storage_path",
    "image_url",
    "thumb_url",
    "original_file_name",
    "gps",
    "gps_lat",
    "gps_lon",
    "gps_lng",
    "prompt_used",
    "raw_model_output",
    "api_key",
    "secret",
    "password",
    "token",
    "receipt_facts",
    "purchaser_name",
}


def _seconds_since(value: Any, now_value: Any) -> int | None:
    if value is None:
        return None
    try:
        delta = now_value - value
    except TypeError:
        return None
    return max(0, int(delta.total_seconds()))


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _task_job_counts(db: Session, *, company_id: str, since: Any, now_value: Any) -> dict[str, Any]:
    status_counts = {
        _enum_value(status): 0
        for status in (TaskStatus.queued, TaskStatus.running, TaskStatus.completed, TaskStatus.failed, TaskStatus.dead_letter)
    }
    for status, count in db.execute(
        select(TaskJob.status, func.count(TaskJob.id))
        .where(TaskJob.company_id == company_id, TaskJob.created_at >= since)
        .group_by(TaskJob.status)
    ):
        status_counts[_enum_value(status)] = int(count or 0)
    queued_oldest = db.scalar(
        select(func.min(TaskJob.available_at)).where(TaskJob.company_id == company_id, TaskJob.status == TaskStatus.queued)
    )
    running_oldest = db.scalar(
        select(func.min(TaskJob.started_at)).where(TaskJob.company_id == company_id, TaskJob.status == TaskStatus.running)
    )
    last_completed = db.scalar(
        select(func.max(TaskJob.completed_at)).where(TaskJob.company_id == company_id, TaskJob.status == TaskStatus.completed)
    )
    return {
        "window_status_counts": status_counts,
        "queued_backlog": int(
            db.scalar(select(func.count(TaskJob.id)).where(TaskJob.company_id == company_id, TaskJob.status == TaskStatus.queued))
            or 0
        ),
        "running_jobs": int(
            db.scalar(select(func.count(TaskJob.id)).where(TaskJob.company_id == company_id, TaskJob.status == TaskStatus.running))
            or 0
        ),
        "oldest_queued_age_seconds": _seconds_since(queued_oldest, now_value),
        "oldest_running_age_seconds": _seconds_since(running_oldest, now_value),
        "last_completed_at": to_utc_iso(last_completed) if last_completed is not None else None,
    }


def _photo_processing_counts(db: Session, *, company_id: str, since: Any) -> dict[str, Any]:
    total_photos = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                Photo.company_id == company_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.created_at >= since,
            )
        )
        or 0
    )
    completed_labeling = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                Photo.company_id == company_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.created_at >= since,
                Photo.labeling_status == "completed",
            )
        )
        or 0
    )
    pending_labeling = max(0, total_photos - completed_labeling)
    latest_photo = db.scalar(
        select(func.max(Photo.created_at)).where(
            Photo.company_id == company_id,
            Photo.photo_type == PhotoType.project,
            Photo.deleted.is_(False),
        )
    )
    return {
        "project_photos_current": total_photos,
        "labeling_completed_current": completed_labeling,
        "labeling_pending_current": pending_labeling,
        "latest_project_photo_created_at": to_utc_iso(latest_photo) if latest_photo is not None else None,
    }


def _ai_analysis_counts(db: Session, *, since: Any) -> dict[str, Any]:
    status_counts = {_enum_value(AIAnalysisStatus.active): 0, _enum_value(AIAnalysisStatus.rejected): 0}
    for status, count in db.execute(
        select(AIAnalysisLog.status, func.count(AIAnalysisLog.id))
        .where(AIAnalysisLog.created_at >= since)
        .group_by(AIAnalysisLog.status)
    ):
        status_counts[_enum_value(status)] = int(count or 0)
    latest_log = db.scalar(select(func.max(AIAnalysisLog.created_at)))
    model_rows = db.execute(
        select(AIAnalysisLog.model_used, func.count(AIAnalysisLog.id))
        .where(AIAnalysisLog.created_at >= since)
        .group_by(AIAnalysisLog.model_used)
        .order_by(func.count(AIAnalysisLog.id).desc())
        .limit(5)
    ).all()
    return {
        "window_status_counts": status_counts,
        "logs_current": sum(status_counts.values()),
        "latest_log_created_at": to_utc_iso(latest_log) if latest_log is not None else None,
        "top_models": [{"model": str(model or "unknown"), "count": int(count or 0)} for model, count in model_rows],
    }


def _expression_artifact_counts(db: Session, *, company_id: str, since: Any) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(ExpressionArtifact)
            .where(ExpressionArtifact.company_id == company_id, ExpressionArtifact.created_at >= since)
            .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
            .limit(1000)
        )
    )
    latest_by_scope: dict[tuple[str, str, str], ExpressionArtifact] = {}
    for artifact in rows:
        scope_hash = _json_hash(artifact.scope_key_json or {})
        key = (artifact.artifact_type, artifact.scope_type, scope_hash)
        latest_by_scope.setdefault(key, artifact)

    by_artifact: dict[str, dict[str, Any]] = {}
    total = len(latest_by_scope)
    invalid = 0
    fallback = 0
    for artifact in latest_by_scope.values():
        status = artifact.validation_status
        if status == "shadow_invalid":
            invalid += 1
        if status == "shadow_fallback_valid":
            fallback += 1
        entry = by_artifact.setdefault(artifact.artifact_type, {"total": 0, "validation_status_counts": {}})
        entry["total"] += 1
        entry["validation_status_counts"][status] = entry["validation_status_counts"].get(status, 0) + 1
    latest_artifact = db.scalar(
        select(func.max(ExpressionArtifact.created_at)).where(ExpressionArtifact.company_id == company_id)
    )
    return {
        "artifacts_current": total,
        "artifact_rows_current": len(rows),
        "shadow_invalid_current": invalid,
        "shadow_fallback_valid_current": fallback,
        "latest_artifact_created_at": to_utc_iso(latest_artifact) if latest_artifact is not None else None,
        "by_artifact_type": by_artifact,
    }


def build_operations_health_summary_facts(
    db: Session,
    *,
    company_id: str,
    window_hours: int = 24,
    now: Any | None = None,
) -> dict[str, Any]:
    window_hours = max(1, min(int(window_hours), 24 * 30))
    now_value = now or utc_now()
    since = now_value - timedelta(hours=window_hours)
    return {
        "scope": {
            "company_id": company_id,
            "scope_type": OPERATIONS_HEALTH_SUMMARY_SCOPE,
        },
        "window": {
            "hours": window_hours,
            "current_start_utc": to_utc_iso(since),
            "current_end_utc": to_utc_iso(now_value),
        },
        "task_jobs": _task_job_counts(db, company_id=company_id, since=since, now_value=now_value),
        "photo_processing": _photo_processing_counts(db, company_id=company_id, since=since),
        "ai_analysis": _ai_analysis_counts(db, since=since),
        "expression_artifacts": _expression_artifact_counts(db, company_id=company_id, since=since),
        "guardrails": {
            "db_only": True,
            "no_disk_file_reads": True,
            "redaction_profile": "operations_health_v1",
            "denied_fact_keys": sorted(OPERATIONS_DENIED_FACT_KEYS),
            "shadow_only_until_promoted": True,
        },
    }


def create_or_get_operations_health_summary_snapshot(
    db: Session,
    *,
    company_id: str,
    window_hours: int = 24,
) -> FactSnapshot:
    window_hours = max(1, min(int(window_hours), 24 * 30))
    scope_key = {
        "company_id": company_id,
        "window_hours": window_hours,
        "snapshot_run_id": str(uuid4()),
    }
    facts = build_operations_health_summary_facts(db, company_id=company_id, window_hours=window_hours)
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=None,
        project_id=None,
        scope_type=OPERATIONS_HEALTH_SUMMARY_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=_json_hash(scope_key),
        assembler_version=OPERATIONS_HEALTH_SUMMARY_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["task_jobs", "photos", "ai_analysis_logs", "expression_artifacts", "fact_snapshots"],
            "redaction_profile": "operations_health_v1",
            "window_hours": window_hours,
            "excluded_fields": sorted(OPERATIONS_DENIED_FACT_KEYS),
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_task_job_window": sum(facts["task_jobs"]["window_status_counts"].values()) > 0,
            "has_expression_artifact_window": int(facts["expression_artifacts"]["artifacts_current"]) > 0,
            "has_ai_analysis_window": int(facts["ai_analysis"]["logs_current"]) > 0,
            "redaction_profile": "operations_health_v1",
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


FINANCE_DENIED_FACT_KEYS = {
    "employee_id",
    "purchaser_name",
    "file_path",
    "storage_path",
    "image_url",
    "thumb_url",
    "original_file_name",
    "summary_text",
    "facts_json",
    "prompt_used",
    "raw_model_output",
    "api_key",
    "secret",
    "password",
    "token",
}


def _finance_receipt_metrics(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    current_start: Any,
    now_value: Any,
) -> dict[str, Any]:
    receipt_observed_at = func.coalesce(ReceiptFact.receipt_timestamp, ReceiptFact.created_at)
    base_filters = (
        ReceiptFact.company_id == company_id,
        ReceiptFact.project_id == project_id,
        receipt_observed_at >= current_start,
        receipt_observed_at <= now_value,
    )
    receipt_count = int(db.scalar(select(func.count(ReceiptFact.id)).where(*base_filters)) or 0)
    with_amount = int(
        db.scalar(select(func.count(ReceiptFact.id)).where(*base_filters, ReceiptFact.total_amount.is_not(None))) or 0
    )
    total_amount = db.scalar(select(func.sum(ReceiptFact.total_amount)).where(*base_filters))
    gallons_total = db.scalar(select(func.sum(ReceiptFact.gallons)).where(*base_filters))
    missing_amount = max(0, receipt_count - with_amount)
    pump_yes = int(
        db.scalar(select(func.count(ReceiptFact.id)).where(*base_filters, ReceiptFact.has_pump_photo.is_(True))) or 0
    )
    pump_no = int(
        db.scalar(select(func.count(ReceiptFact.id)).where(*base_filters, ReceiptFact.has_pump_photo.is_(False))) or 0
    )
    pump_unknown = int(
        db.scalar(select(func.count(ReceiptFact.id)).where(*base_filters, ReceiptFact.has_pump_photo.is_(None))) or 0
    )
    latest_timestamp = db.scalar(select(func.max(ReceiptFact.receipt_timestamp)).where(*base_filters))
    currency_rows = db.execute(
        select(ReceiptFact.currency_code, func.count(ReceiptFact.id))
        .where(*base_filters, ReceiptFact.currency_code.is_not(None))
        .group_by(ReceiptFact.currency_code)
        .order_by(func.count(ReceiptFact.id).desc())
        .limit(5)
    ).all()
    vendor_rows = db.execute(
        select(ReceiptFact.vendor_name, func.count(ReceiptFact.id), func.sum(ReceiptFact.total_amount))
        .where(*base_filters, ReceiptFact.vendor_name.is_not(None))
        .group_by(ReceiptFact.vendor_name)
        .order_by(func.count(ReceiptFact.id).desc())
        .limit(5)
    ).all()
    currency_counts = [{"currency_code": str(code), "count": int(count or 0)} for code, count in currency_rows]
    vendor_summaries = []
    for index, (_vendor, count, amount) in enumerate(vendor_rows):
        vendor_summaries.append(
            {
                "vendor_label": f"Vendor {index + 1}",
                "receipt_count": int(count or 0),
                "total_amount": round(float(amount or 0.0), 2),
            }
        )
    return {
        "receipt_count": receipt_count,
        "receipts_with_amount": with_amount,
        "receipts_missing_amount": missing_amount,
        "total_amount": round(float(total_amount or 0.0), 2),
        "fuel_gallons_total": round(float(gallons_total or 0.0), 3),
        "has_pump_photo_true": pump_yes,
        "has_pump_photo_false": pump_no,
        "has_pump_photo_unknown": pump_unknown,
        "latest_receipt_timestamp": to_utc_iso(latest_timestamp) if latest_timestamp is not None else None,
        "currency_counts": currency_counts,
        "mixed_currency": len(currency_counts) > 1,
        "top_vendors": vendor_summaries,
    }


def build_finance_summary_facts(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    now: Any | None = None,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    now_value = now or utc_now()
    current_start = now_value - timedelta(days=window_days)
    project = db.scalar(select(Project).where(Project.company_id == company_id, Project.project_id == project_id))
    if project is None:
        raise ValueError(f"Project {project_id} was not found for company {company_id}")
    return {
        "scope": {
            "company_id": company_id,
            "project_id": project_id,
            "scope_type": FINANCE_SUMMARY_SCOPE,
        },
        "window": {
            "days": window_days,
            "current_start_utc": to_utc_iso(current_start),
            "current_end_utc": to_utc_iso(now_value),
        },
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        },
        "receipt_metrics": _finance_receipt_metrics(
            db,
            company_id=company_id,
            project_id=project_id,
            current_start=current_start,
            now_value=now_value,
        ),
        "guardrails": {
            "db_only": True,
            "redaction_profile": "finance_summary_v1",
            "shadow_only_until_promoted": True,
        },
    }


def create_or_get_finance_summary_snapshot(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int = 30,
) -> FactSnapshot:
    window_days = max(1, min(int(window_days), 365))
    scope_key = {
        "company_id": company_id,
        "project_id": project_id,
        "window_days": window_days,
        "snapshot_run_id": str(uuid4()),
    }
    facts = build_finance_summary_facts(db, company_id=company_id, project_id=project_id, window_days=window_days)
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=None,
        project_id=project_id,
        scope_type=FINANCE_SUMMARY_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=_json_hash(scope_key),
        assembler_version=FINANCE_SUMMARY_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["projects", "receipt_facts"],
            "redaction_profile": "finance_summary_v1",
            "excluded_fields": sorted(FINANCE_DENIED_FACT_KEYS),
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_receipts": int(facts["receipt_metrics"]["receipt_count"]) > 0,
            "has_amounts": int(facts["receipt_metrics"]["receipts_with_amount"]) > 0,
            "redaction_profile": "finance_summary_v1",
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


EXECUTIVE_DENIED_FACT_KEYS = {
    "employee_id",
    "uploaded_by_user_id",
    "created_by_user_id",
    "created_by",
    "file_path",
    "storage_path",
    "image_url",
    "thumb_url",
    "original_file_name",
    "prompt",
    "prompt_used",
    "raw_model_output",
    "api_key",
    "secret",
    "password",
    "token",
}


def _company_project_portfolio_counts(db: Session, *, company_id: str) -> dict[str, Any]:
    status_counts: dict[str, int] = {_enum_value(ProjectStatus.active): 0}
    for status, count in db.execute(
        select(Project.status, func.count(Project.id)).where(Project.company_id == company_id).group_by(Project.status)
    ):
        status_counts[_enum_value(status)] = int(count or 0)
    return {
        "projects_total": sum(status_counts.values()),
        "status_counts": status_counts,
    }


def _company_photo_activity_counts(db: Session, *, company_id: str, since: Any) -> dict[str, Any]:
    current_filters = (
        Photo.company_id == company_id,
        Photo.photo_type == PhotoType.project,
        Photo.deleted.is_(False),
        Photo.created_at >= since,
    )
    photos_current = int(db.scalar(select(func.count(Photo.id)).where(*current_filters)) or 0)
    active_projects = int(
        db.scalar(select(func.count(func.distinct(Photo.project_id))).where(*current_filters, Photo.project_id.is_not(None)))
        or 0
    )
    active_days = int(
        db.scalar(select(func.count(func.distinct(func.date(Photo.created_at)))).where(*current_filters)) or 0
    )
    client_visible_current = int(
        db.scalar(select(func.count(Photo.id)).where(*current_filters, Photo.visibility == PhotoVisibility.client_visible))
        or 0
    )
    approved_current = int(
        db.scalar(select(func.count(Photo.id)).where(*current_filters, Photo.approval_status == ApprovalStatus.approved))
        or 0
    )
    latest_photo = db.scalar(
        select(func.max(Photo.created_at)).where(
            Photo.company_id == company_id,
            Photo.photo_type == PhotoType.project,
            Photo.deleted.is_(False),
        )
    )
    return {
        "photos_current": photos_current,
        "active_project_count": active_projects,
        "active_day_count": active_days,
        "approved_photos_current": approved_current,
        "client_visible_photos_current": client_visible_current,
        "latest_project_photo_created_at": to_utc_iso(latest_photo) if latest_photo is not None else None,
    }


def _company_progress_report_counts(db: Session, *, company_id: str, since: Any) -> dict[str, Any]:
    status_counts: dict[str, int] = {_enum_value(ProgressReportStatus.completed): 0}
    for status, count in db.execute(
        select(ProgressReport.status, func.count(ProgressReport.id))
        .where(ProgressReport.company_id == company_id, ProgressReport.created_at >= since)
        .group_by(ProgressReport.status)
    ):
        status_counts[_enum_value(status)] = int(count or 0)
    latest_completed = db.scalar(
        select(func.max(ProgressReport.completed_at)).where(
            ProgressReport.company_id == company_id,
            ProgressReport.status == ProgressReportStatus.completed,
        )
    )
    return {
        "reports_current": sum(status_counts.values()),
        "status_counts": status_counts,
        "completed_current": int(status_counts.get(ProgressReportStatus.completed.value, 0) or 0),
        "latest_completed_at": to_utc_iso(latest_completed) if latest_completed is not None else None,
    }


def _company_finance_receipt_coverage(db: Session, *, company_id: str, since: Any, now_value: Any) -> dict[str, Any]:
    receipt_observed_at = func.coalesce(ReceiptFact.receipt_timestamp, ReceiptFact.created_at)
    current_filters = (
        ReceiptFact.company_id == company_id,
        receipt_observed_at >= since,
        receipt_observed_at <= now_value,
    )
    receipt_count = int(db.scalar(select(func.count(ReceiptFact.id)).where(*current_filters)) or 0)
    projects_with_receipts = int(
        db.scalar(
            select(func.count(func.distinct(ReceiptFact.project_id))).where(
                *current_filters,
                ReceiptFact.project_id.is_not(None),
            )
        )
        or 0
    )
    receipts_with_amount = int(
        db.scalar(select(func.count(ReceiptFact.id)).where(*current_filters, ReceiptFact.total_amount.is_not(None))) or 0
    )
    return {
        "receipt_count": receipt_count,
        "projects_with_receipts": projects_with_receipts,
        "receipts_with_amount": receipts_with_amount,
        "receipts_missing_amount": max(0, receipt_count - receipts_with_amount),
    }


def build_executive_company_health_facts(
    db: Session,
    *,
    company_id: str,
    window_days: int = 30,
    now: Any | None = None,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    now_value = now or utc_now()
    current_start = now_value - timedelta(days=window_days)
    company = db.scalar(select(Company).where(Company.company_id == company_id))
    if company is None:
        raise ValueError(f"Company {company_id} was not found")
    return {
        "scope": {
            "company_id": company_id,
            "scope_type": EXECUTIVE_COMPANY_HEALTH_SCOPE,
        },
        "window": {
            "days": window_days,
            "current_start_utc": to_utc_iso(current_start),
            "current_end_utc": to_utc_iso(now_value),
        },
        "company_profile": {
            "company_id": company.company_id,
            "active": bool(company.active),
        },
        "project_portfolio": _company_project_portfolio_counts(db, company_id=company_id),
        "photo_activity": _company_photo_activity_counts(db, company_id=company_id, since=current_start),
        "progress_reports": _company_progress_report_counts(db, company_id=company_id, since=current_start),
        "expression_readiness": _expression_artifact_counts(db, company_id=company_id, since=current_start),
        "finance_receipt_coverage": _company_finance_receipt_coverage(
            db,
            company_id=company_id,
            since=current_start,
            now_value=now_value,
        ),
        "guardrails": {
            "db_only": True,
            "no_disk_file_reads": True,
            "redaction_profile": "executive_company_health_v1",
            "denied_fact_keys": sorted(EXECUTIVE_DENIED_FACT_KEYS),
            "no_employee_ranking": True,
            "no_performance_scoring": True,
            "shadow_only_until_promoted": True,
        },
    }


def create_or_get_executive_company_health_snapshot(
    db: Session,
    *,
    company_id: str,
    window_days: int = 30,
) -> FactSnapshot:
    window_days = max(1, min(int(window_days), 365))
    scope_key = {
        "company_id": company_id,
        "window_days": window_days,
        "snapshot_run_id": str(uuid4()),
    }
    facts = build_executive_company_health_facts(db, company_id=company_id, window_days=window_days)
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=company_id,
        company_id=company_id,
        employee_id=None,
        project_id=None,
        scope_type=EXECUTIVE_COMPANY_HEALTH_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=_json_hash(scope_key),
        assembler_version=EXECUTIVE_COMPANY_HEALTH_ASSEMBLER_VERSION,
        source_manifest_json={
            "tables": ["companies", "projects", "photos", "progress_reports", "expression_artifacts", "receipt_facts"],
            "redaction_profile": "executive_company_health_v1",
            "excluded_fields": sorted(EXECUTIVE_DENIED_FACT_KEYS),
            "window_days": window_days,
        },
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={
            "has_projects": int(facts["project_portfolio"]["projects_total"]) > 0,
            "has_photo_activity": int(facts["photo_activity"]["photos_current"]) > 0,
            "has_progress_reports": int(facts["progress_reports"]["reports_current"]) > 0,
            "has_expression_artifacts": int(facts["expression_readiness"]["artifacts_current"]) > 0,
            "has_receipt_facts": int(facts["finance_receipt_coverage"]["receipt_count"]) > 0,
            "redaction_profile": "executive_company_health_v1",
        },
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _resolve_prompt_version(db: Session, *, company_id: str, project_id: str | None, template_id: str) -> ExpressionPromptVersion:
    scope_candidates = [("project", project_id), ("company", company_id), ("global", None)]
    for scope_type, scope_id in scope_candidates:
        if scope_type == "project" and not scope_id:
            continue
        binding = db.scalar(
            select(ExpressionPromptBinding)
            .where(
                ExpressionPromptBinding.template_id == template_id,
                ExpressionPromptBinding.scope_type == scope_type,
                ExpressionPromptBinding.scope_id == scope_id,
            )
            .order_by(ExpressionPromptBinding.priority.desc(), ExpressionPromptBinding.created_at.desc())
        )
        if binding is None:
            continue
        version = db.get(ExpressionPromptVersion, binding.prompt_version_id)
        if version is not None and version.status == "active":
            return version
    raise ValueError(f"No active expression prompt version bound for template {template_id}")


def _resolve_contract(db: Session, *, artifact_type: str, audience_id: str) -> ExpressionOutputContract:
    contract = db.scalar(
        select(ExpressionOutputContract)
        .where(
            ExpressionOutputContract.artifact_type == artifact_type,
            ExpressionOutputContract.audience_id == audience_id,
            ExpressionOutputContract.is_active.is_(True),
        )
        .order_by(ExpressionOutputContract.created_at.desc())
    )
    if contract is None:
        raise ValueError(f"No active expression output contract for {artifact_type}/{audience_id}")
    return contract


def _format_prompt(
    version: ExpressionPromptVersion,
    *,
    facts: dict[str, Any],
    contract: ExpressionOutputContract,
    extra_vars: dict[str, Any] | None = None,
) -> str:
    facts_json = json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2)
    contract_json = json.dumps(contract.json_schema, ensure_ascii=False, sort_keys=True, indent=2)
    variables = {
        "facts_json": facts_json,
        "contract_schema_json": contract_json,
    }
    if extra_vars:
        variables.update({key: str(value) for key, value in extra_vars.items()})
    return (
        version.system_prompt.strip()
        + "\n\n"
        + version.user_prompt_template.format(**variables)
    )


def _ollama_endpoint(node: AIBackendNode) -> str:
    return node.url.rstrip("/") + "/api/generate"


def _call_ollama_expression_backend(node: AIBackendNode, *, prompt: str) -> str:
    try:
        response = requests.post(
            _ollama_endpoint(node),
            json={
                "model": node.model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "top_p": 0.75,
                    "repeat_penalty": 1.1,
                    "num_predict": EXPRESSION_GENERATION_NUM_PREDICT,
                },
            },
            timeout=EXPRESSION_GENERATION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ollama expression response must be a JSON object")
        text = payload.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Ollama expression response missing text")
        return text.strip()
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def _expression_override_backend(
    app_settings: Settings,
    *,
    preferred_model: str | None,
) -> AIBackendNode | None:
    url = (app_settings.expression_text_backend_url or "").strip()
    if not url:
        return None
    model = (
        (app_settings.expression_text_backend_model or "").strip()
        or (preferred_model or "").strip()
        or DEFAULT_EXPRESSION_TEXT_MODEL
    )
    return AIBackendNode(
        id="expression-text-override",
        type=OLLAMA_TYPE,
        url=url,
        model=model,
        api_key=None,
        embedding_model=None,
        weight=100,
        enabled=True,
    )


def _expression_backend_candidates(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    preferred_model: str | None,
) -> list[AIBackendNode]:
    candidates: list[AIBackendNode] = []
    override = _expression_override_backend(app_settings, preferred_model=preferred_model)
    if override is not None:
        candidates.append(override)

    backends, _ = resolve_ai_backends_for_tenant(db, app_settings, company_id)
    model_override = (preferred_model or DEFAULT_EXPRESSION_TEXT_MODEL).strip()
    for backend in backends:
        if not backend.enabled:
            continue
        if backend.type == OLLAMA_TYPE:
            candidates.append(
                AIBackendNode(
                    id=f"{backend.id}:expression:{model_override}",
                    type=backend.type,
                    url=backend.url,
                    model=model_override or backend.model,
                    api_key=backend.api_key,
                    embedding_model=backend.embedding_model,
                    weight=backend.weight,
                    enabled=True,
                )
            )
        elif backend.type == GEMINI_TYPE:
            candidates.append(backend)
    return candidates


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Expression output must be a JSON object")
    return payload


def _schema_type_ok(value: Any, expected: Any) -> bool:
    expected_values = expected if isinstance(expected, list) else [expected]
    for expected_type in expected_values:
        if expected_type == "object" and isinstance(value, dict):
            return True
        if expected_type == "array" and isinstance(value, list):
            return True
        if expected_type == "string" and isinstance(value, str):
            return True
        if expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if expected_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if expected_type == "boolean" and isinstance(value, bool):
            return True
        if expected_type == "null" and value is None:
            return True
    return False


def _validate_schema_value(value: Any, rules: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "type" in rules and not _schema_type_ok(value, rules["type"]):
        return [f"type:{path}"]
    if "const" in rules and value != rules["const"]:
        errors.append(f"const:{path}")
    if "enum" in rules and isinstance(rules["enum"], list) and value not in rules["enum"]:
        errors.append(f"enum:{path}")
    if isinstance(value, str):
        if rules.get("minLength") is not None and len(value) < int(rules["minLength"]):
            errors.append(f"min_length:{path}")
        if rules.get("maxLength") is not None and len(value) > int(rules["maxLength"]):
            errors.append(f"max_length:{path}")
    if isinstance(value, list):
        if rules.get("minItems") is not None and len(value) < int(rules["minItems"]):
            errors.append(f"min_items:{path}")
        if rules.get("maxItems") is not None and len(value) > int(rules["maxItems"]):
            errors.append(f"max_items:{path}")
        item_rules = rules.get("items") if isinstance(rules.get("items"), dict) else {}
        for index, item in enumerate(value):
            if item_rules:
                errors.extend(_validate_schema_value(item, item_rules, f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = rules.get("properties") if isinstance(rules.get("properties"), dict) else {}
        for required_key in rules.get("required") or []:
            if required_key not in value:
                errors.append(f"missing_required:{path}.{required_key}")
        if rules.get("additionalProperties") is False:
            allowed = set(properties.keys())
            for key in value:
                if key not in allowed:
                    errors.append(f"unexpected_property:{path}.{key}")
        for key, child_rules in properties.items():
            if key in value and isinstance(child_rules, dict):
                errors.extend(_validate_schema_value(value[key], child_rules, f"{path}.{key}"))
    return errors


def _validate_against_contract(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in schema.get("required") or []:
        if key not in payload:
            errors.append(f"missing_required:{key}")
    if schema.get("additionalProperties") is False:
        allowed = set((schema.get("properties") or {}).keys())
        for key in payload:
            if key not in allowed:
                errors.append(f"unexpected_property:{key}")
    for key, rules in (schema.get("properties") or {}).items():
        if key not in payload or not isinstance(rules, dict):
            continue
        errors.extend(_validate_schema_value(payload[key], rules, key))
    return errors


def _path_exists(payload: Any, path: str) -> bool:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return False
    return current is not None


def _value_at_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return None
    return current


def _required_fact_path_errors(contract: ExpressionOutputContract, facts: dict[str, Any]) -> list[str]:
    paths = contract.required_fact_paths_json
    if not isinstance(paths, list):
        return []
    errors: list[str] = []
    for path in paths:
        if isinstance(path, str) and path and not _path_exists(facts, path):
            errors.append(f"missing_fact_path:{path}")
    return errors


def _forbidden_claim_errors(contract: ExpressionOutputContract, payload: dict[str, Any]) -> list[str]:
    rules = contract.forbidden_claims_json
    if not isinstance(rules, list):
        return []
    flattened = " ".join(_flatten_strings(payload)).casefold()
    errors: list[str] = []
    for rule in rules:
        if isinstance(rule, dict):
            phrase = str(rule.get("phrase") or "").casefold()
            pattern = str(rule.get("pattern") or "")
            if phrase and phrase in flattened:
                errors.append(f"forbidden_claim:{phrase}")
            elif pattern and re.search(pattern, flattened, flags=re.IGNORECASE):
                errors.append(f"forbidden_claim:{pattern}")
        elif isinstance(rule, str):
            claim_text = rule.removeprefix("Do not claim").strip(" .").casefold()
            candidates = [claim_text]
            if "," in claim_text:
                candidates.extend(part.strip(" .") for part in claim_text.split(","))
            for candidate in candidates:
                if candidate and len(candidate) >= 4 and candidate in flattened:
                    errors.append(f"forbidden_claim:{candidate}")
                    break
    return errors


def _progress_report_translation_preservation_errors(payload: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    source = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    source_timeline = source.get("timeline_observations") if isinstance(source.get("timeline_observations"), list) else []
    errors: list[str] = []
    for language in ("zh", "en", "es"):
        candidate = payload.get(language)
        if not isinstance(candidate, dict):
            continue
        for field_name in ("overall_progress_percent", "overall_status", "confidence_level"):
            if candidate.get(field_name) != source.get(field_name):
                errors.append(f"preserve:{language}.{field_name}")
        candidate_timeline = (
            candidate.get("timeline_observations")
            if isinstance(candidate.get("timeline_observations"), list)
            else []
        )
        if len(candidate_timeline) != len(source_timeline):
            errors.append(f"preserve:{language}.timeline_observations.length")
            continue
        for index, source_item in enumerate(source_timeline):
            candidate_item = candidate_timeline[index] if isinstance(candidate_timeline[index], dict) else {}
            if not isinstance(source_item, dict):
                continue
            for field_name in ("photo_id", "captured_at"):
                if candidate_item.get(field_name) != source_item.get(field_name):
                    errors.append(f"preserve:{language}.timeline_observations[{index}].{field_name}")
    return errors


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(_flatten_strings(item))
        return result
    return []


def _mostly_same_text(left: str, right: str) -> bool:
    left_normalized = " ".join(left.casefold().split())
    right_normalized = " ".join(right.casefold().split())
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    shorter = min(len(left_normalized), len(right_normalized))
    longer = max(len(left_normalized), len(right_normalized))
    return shorter >= 40 and shorter / longer >= 0.96 and (
        left_normalized in right_normalized or right_normalized in left_normalized
    )


def _translation_chunk_quality_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    source_text = ""
    if isinstance(facts, dict):
        source_text = _safe_text(facts.get("source_text"), limit=4000)
    errors: list[str] = []
    for language in ("zh", "en", "es"):
        value = payload.get(language)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"translation_chunk:{language}:empty")
            continue
        if _PLACEHOLDER_RE.search(value):
            errors.append(f"translation_chunk:{language}:placeholder")
        if language == "zh":
            if source_text and not _has_cjk(value):
                errors.append("translation_chunk:zh:missing_cjk")
            if source_text and not _has_cjk(source_text) and _mostly_same_text(value, source_text):
                errors.append("translation_chunk:zh:untranslated")
        elif _has_cjk(value) and not _has_cjk(source_text):
            errors.append(f"translation_chunk:{language}:unexpected_cjk")
    es_text = str(payload.get("es") or "")
    for pattern in _SPANISH_LOW_QUALITY_PATTERNS:
        if pattern.search(es_text):
            errors.append(f"translation_chunk:es:low_quality:{pattern.pattern}")
    en_text = str(payload.get("en") or "")
    if source_text and not _has_cjk(source_text) and _mostly_same_text(es_text, source_text):
        errors.append("translation_chunk:es:untranslated_source")
    if en_text and _mostly_same_text(es_text, en_text):
        errors.append("translation_chunk:es:untranslated_en")
    return errors


def _progress_report_translation_language_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for language in ("zh", "en", "es"):
        candidate = payload.get(language)
        if not isinstance(candidate, dict):
            continue
        strings = [text for text in _flatten_strings(candidate) if text and text not in {"unknown", "on_track", "at_risk", "blocked", "high", "medium", "low"}]
        if not strings:
            continue
        joined = "\n".join(strings)
        if language == "zh" and not _has_cjk(joined):
            errors.append("language:zh:missing_cjk")
        if language in {"en", "es"} and _has_cjk(joined):
            errors.append(f"language:{language}:unexpected_cjk")
        if language == "es":
            for pattern in _SPANISH_LOW_QUALITY_PATTERNS:
                if pattern.search(joined):
                    errors.append(f"language:es:low_quality:{pattern.pattern}")
                    break
    return errors


def _generated_report_allowed_source_refs(facts: dict[str, Any]) -> set[str]:
    structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    allowed: set[str] = set()
    for key, value in structured_report.items():
        if isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    for child_key, child_value in item.items():
                        if child_value not in (None, "", []):
                            allowed.add(f"structured_report.{key}.{index}.{child_key}")
                elif item not in (None, "", []):
                    allowed.add(f"structured_report.{key}.{index}")
        elif value not in (None, "", []):
            allowed.add(f"structured_report.{key}")
    return allowed


def _generated_report_markdown_errors(
    payload: dict[str, Any],
    facts: dict[str, Any] | None,
    *,
    require_all_sections: bool = True,
) -> list[str]:
    errors: list[str] = []
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    allowed_refs = _generated_report_allowed_source_refs(facts or {})
    html_pattern = re.compile(r"</?[a-zA-Z][^>]*>")
    seen_keys: set[str] = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        key = str(section.get("key") or "")
        if key:
            seen_keys.add(key)
        markdown = str(section.get("markdown") or "")
        if html_pattern.search(markdown):
            errors.append(f"markdown:sections[{index}]:raw_html")
        if re.search(r"\bshould\b", markdown, flags=re.IGNORECASE):
            errors.append(f"markdown:sections[{index}]:unsupported_recommendation:should")
        if re.search(r"\bpriority\b", markdown, flags=re.IGNORECASE):
            errors.append(f"markdown:sections[{index}]:unsupported_recommendation:priority")
        refs = section.get("source_refs") if isinstance(section.get("source_refs"), list) else []
        if not refs:
            errors.append(f"markdown:sections[{index}]:missing_source_refs")
        for ref in refs:
            if ref not in allowed_refs:
                errors.append(f"markdown:sections[{index}]:unknown_source_ref:{ref}")
    if require_all_sections:
        for required_key in ("summary", "completed_work", "risks", "next_steps", "timeline"):
            if required_key not in seen_keys:
                errors.append(f"markdown:missing_section:{required_key}")
    return errors


_PROJECT_MANAGER_IMPERATIVE_RE = re.compile(
    r"\b(should|must|priority|prioritize|recommend|recommended|ensure|secure|need to|needs to|escalate|urgent|guaranteed)\b",
    flags=re.IGNORECASE,
)
_NUMERIC_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_PM_POLISH_STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "and",
    "are",
    "available",
    "brief",
    "completed",
    "current",
    "database",
    "evidence",
    "for",
    "from",
    "has",
    "have",
    "into",
    "is",
    "linked",
    "manager",
    "project",
    "record",
    "report",
    "reports",
    "shows",
    "stored",
    "structured",
    "than",
    "that",
    "the",
    "this",
    "window",
    "with",
}


def _pm_polish_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z_-]{3,}", text.casefold())
        if token not in _PM_POLISH_STOPWORDS
    }


_SURFACE_IMMUTABILITY_LOCKED_KEYS: dict[str, tuple[str, ...]] = {
    CLIENT_PROGRESS_SUMMARY_ARTIFACT: ("summary_surface",),
    OPERATIONS_HEALTH_SUMMARY_ARTIFACT: ("health_surface", "overall_status"),
    FINANCE_SUMMARY_ARTIFACT: ("finance_surface", "headline_metrics", "status_flags"),
    EXECUTIVE_COMPANY_HEALTH_ARTIFACT: ("company_health_surface", "headline_metrics", "overall_status"),
}

_SURFACE_SUMMARY_ARTIFACT_TYPES = frozenset(_SURFACE_IMMUTABILITY_LOCKED_KEYS)

_SURFACE_IMMUTABILITY_ERROR_PREFIX: dict[str, str] = {
    CLIENT_PROGRESS_SUMMARY_ARTIFACT: "client_summary",
    OPERATIONS_HEALTH_SUMMARY_ARTIFACT: "operations_health",
    FINANCE_SUMMARY_ARTIFACT: "finance_summary",
    EXECUTIVE_COMPANY_HEALTH_ARTIFACT: "executive_company_health",
}


def _canonical_json_for_immutability(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _collect_surface_fact_refs(payload: dict[str, Any], artifact_type: str) -> list[Any]:
    refs: list[Any] = []
    if artifact_type == CLIENT_PROGRESS_SUMMARY_ARTIFACT:
        surface = payload.get("summary_surface") if isinstance(payload.get("summary_surface"), dict) else {}
        items = surface.get("items") if isinstance(surface.get("items"), list) else []
        for item in items:
            if isinstance(item, dict):
                refs.extend(item.get("fact_refs") if isinstance(item.get("fact_refs"), list) else [])
    elif artifact_type == OPERATIONS_HEALTH_SUMMARY_ARTIFACT:
        surface = payload.get("health_surface") if isinstance(payload.get("health_surface"), dict) else {}
        dimensions = surface.get("dimensions") if isinstance(surface.get("dimensions"), list) else []
        for dimension in dimensions:
            if isinstance(dimension, dict):
                refs.extend(dimension.get("fact_refs") if isinstance(dimension.get("fact_refs"), list) else [])
    elif artifact_type == FINANCE_SUMMARY_ARTIFACT:
        surface = payload.get("finance_surface") if isinstance(payload.get("finance_surface"), dict) else {}
        sections = surface.get("sections") if isinstance(surface.get("sections"), list) else []
        for section in sections:
            if isinstance(section, dict):
                refs.extend(section.get("fact_refs") if isinstance(section.get("fact_refs"), list) else [])
    elif artifact_type == EXECUTIVE_COMPANY_HEALTH_ARTIFACT:
        surface = payload.get("company_health_surface") if isinstance(payload.get("company_health_surface"), dict) else {}
        cards = surface.get("cards") if isinstance(surface.get("cards"), list) else []
        for card in cards:
            if isinstance(card, dict):
                refs.extend(card.get("fact_refs") if isinstance(card.get("fact_refs"), list) else [])
    return refs


def _surface_immutability_errors(
    *,
    artifact_type: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    partial_candidate: bool = False,
) -> list[str]:
    locked_keys = _SURFACE_IMMUTABILITY_LOCKED_KEYS.get(artifact_type)
    if not locked_keys:
        return []
    prefix = _SURFACE_IMMUTABILITY_ERROR_PREFIX.get(artifact_type, artifact_type)
    errors: list[str] = []
    for key in locked_keys:
        if partial_candidate and key not in candidate:
            continue
        baseline_value = baseline.get(key)
        candidate_value = candidate.get(key)
        if _canonical_json_for_immutability(baseline_value) != _canonical_json_for_immutability(candidate_value):
            errors.append(f"{prefix}:immutability:{key}:changed")
    baseline_refs = _collect_surface_fact_refs(baseline, artifact_type)
    if not partial_candidate or any(
        key in candidate for key in ("summary_surface", "health_surface", "finance_surface", "company_health_surface")
    ):
        candidate_refs = _collect_surface_fact_refs(candidate, artifact_type)
        if _canonical_json_for_immutability(baseline_refs) != _canonical_json_for_immutability(candidate_refs):
            errors.append(f"{prefix}:immutability:fact_refs:changed")
    return errors


def _body_generation_path(payload: dict[str, Any]) -> str | None:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    generation_path = body.get("generation_path")
    return str(generation_path) if generation_path is not None else None


def _surface_summary_polished_payload(*, fallback_payload: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    artifact_type = str(fallback_payload.get("artifact_type") or "")
    candidate_body = candidate.get("body") if isinstance(candidate.get("body"), dict) else {}
    paragraphs = candidate_body.get("paragraphs") if isinstance(candidate_body.get("paragraphs"), list) else []
    cleaned_paragraphs: list[str] = []
    max_paragraphs = 8
    if artifact_type == CLIENT_PROGRESS_SUMMARY_ARTIFACT:
        max_paragraphs = 6
    elif artifact_type == OPERATIONS_HEALTH_SUMMARY_ARTIFACT:
        max_paragraphs = 8
    elif artifact_type == EXECUTIVE_COMPANY_HEALTH_ARTIFACT:
        max_paragraphs = 8
    for paragraph in paragraphs[:max_paragraphs]:
        text = _safe_text(paragraph, limit=600)
        if text:
            cleaned_paragraphs.append(text)
    if not cleaned_paragraphs:
        raise ValueError(f"{artifact_type}:polish:body.paragraphs:empty")
    payload = _copy_json_dict(fallback_payload)
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in cleaned_paragraphs)
    payload["body"] = {
        "format": "markdown",
        "paragraphs": cleaned_paragraphs,
        "word_count": word_count,
        "generation_path": "ai_polish",
    }
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    validation["ai_polish_attempted"] = True
    payload["validation"] = validation
    return payload


def _project_manager_polish_grounding_errors(paragraphs: list[Any], summaries: set[str]) -> list[str]:
    source_text = " ".join(summaries)
    source_numbers = set(_NUMERIC_TOKEN_RE.findall(source_text))
    source_terms = _pm_polish_terms(source_text)
    errors: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        paragraph_numbers = set(_NUMERIC_TOKEN_RE.findall(text))
        unknown_numbers = sorted(paragraph_numbers - source_numbers)
        if unknown_numbers:
            errors.append(f"decision_brief:paragraphs[{index}]:ai_polish_unknown_numbers:{','.join(unknown_numbers)}")
        paragraph_terms = _pm_polish_terms(text)
        if paragraph_terms and source_terms:
            overlap = len(paragraph_terms & source_terms) / len(paragraph_terms)
            if overlap < 0.45:
                errors.append(f"decision_brief:paragraphs[{index}]:ai_polish_low_grounding")
    return errors


def _decision_surface_ref_errors(
    *,
    prefix: str,
    snapshot_items: list[Any],
    facts: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for index, item in enumerate(snapshot_items):
        if not isinstance(item, dict):
            continue
        refs = item.get("fact_refs") if isinstance(item.get("fact_refs"), list) else []
        if not refs:
            errors.append(f"{prefix}:items[{index}]:missing_fact_refs")
        for ref_index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                errors.append(f"{prefix}:items[{index}].fact_refs[{ref_index}]:type")
                continue
            path = str(ref.get("field_path") or "")
            observed = ref.get("observed_value")
            actual = _value_at_path(facts, path)
            if not path or actual is None:
                errors.append(f"{prefix}:items[{index}].fact_refs[{ref_index}]:unknown_path:{path}")
            elif observed != actual:
                errors.append(f"{prefix}:items[{index}].fact_refs[{ref_index}]:value_mismatch:{path}")
    return errors


def _project_manager_decision_brief_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    facts = facts or {}
    errors: list[str] = []
    decision_surface = payload.get("decision_surface") if isinstance(payload.get("decision_surface"), dict) else {}
    items = decision_surface.get("items") if isinstance(decision_surface.get("items"), list) else []
    summaries: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        summary = _safe_text(item.get("deterministic_summary"), limit=600)
        if summary:
            summaries.add(summary)
    errors.extend(_decision_surface_ref_errors(prefix="decision_surface", snapshot_items=items, facts=facts))
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    generation_path = body.get("generation_path")
    total_words = 0
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        total_words += len(re.findall(r"\w+", text))
        if _PROJECT_MANAGER_IMPERATIVE_RE.search(text):
            errors.append(f"decision_brief:paragraphs[{index}]:imperative_language")
        if generation_path != "ai_polish" and summaries and text not in summaries:
            errors.append(f"decision_brief:paragraphs[{index}]:not_from_decision_surface")
    declared_word_count = body.get("word_count")
    if isinstance(declared_word_count, int) and declared_word_count > 120:
        errors.append("decision_brief:word_count:max_120")
    if total_words > 120:
        errors.append("decision_brief:paragraphs:word_count:max_120")
    if generation_path == "ai_polish":
        errors.extend(_project_manager_polish_grounding_errors(paragraphs, summaries))
    return errors


def _contains_denied_fact_key(value: Any, denied_keys: set[str] | None = None) -> bool:
    denied_keys = denied_keys or CLIENT_PROGRESS_DENIED_FACT_KEYS
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "guardrails":
                continue
            if str(key) in denied_keys:
                return True
            if _contains_denied_fact_key(child, denied_keys):
                return True
    if isinstance(value, list):
        return any(_contains_denied_fact_key(item, denied_keys) for item in value)
    return False


def _flatten_strings_excluding_keys(value: Any, excluded_keys: set[str]) -> list[str]:
    if isinstance(value, dict):
        strings: list[str] = []
        for key, child in value.items():
            if str(key) in excluded_keys:
                continue
            strings.extend(_flatten_strings_excluding_keys(child, excluded_keys))
        return strings
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(_flatten_strings_excluding_keys(item, excluded_keys))
        return strings
    if isinstance(value, str):
        return [value]
    return []


def _client_progress_summary_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    facts = facts or {}
    errors: list[str] = []
    if _contains_denied_fact_key(facts):
        errors.append("client_visibility:facts:denied_key")
    if _CLIENT_PROGRESS_DENIED_TEXT_RE.search(" ".join(_flatten_strings_excluding_keys(facts, {"guardrails"}))):
        errors.append("client_visibility:facts:denied_text")
    surface = payload.get("summary_surface") if isinstance(payload.get("summary_surface"), dict) else {}
    items = surface.get("items") if isinstance(surface.get("items"), list) else []
    summaries: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        summary = _safe_text(item.get("deterministic_summary"), limit=600)
        if summary:
            summaries.add(summary)
    errors.extend(_decision_surface_ref_errors(prefix="summary_surface", snapshot_items=items, facts=facts))
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    generation_path = _body_generation_path(payload)
    total_words = 0
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        total_words += len(re.findall(r"\w+", text))
        if _PROJECT_MANAGER_IMPERATIVE_RE.search(text):
            errors.append(f"client_summary:paragraphs[{index}]:imperative_language")
        if generation_path != "ai_polish" and summaries and text not in summaries:
            errors.append(f"client_summary:paragraphs[{index}]:not_from_summary_surface")
    if isinstance(body.get("word_count"), int) and body["word_count"] > 120:
        errors.append("client_summary:word_count:max_120")
    if total_words > 120:
        errors.append("client_summary:paragraphs:word_count:max_120")
    flattened = " ".join(_flatten_strings(payload))
    denied_patterns = [
        r"\bemployee_id\b",
        r"\bmanager_brief\b",
        r"\bsafety_risks\b",
        r"\bquality_risks\b",
        r"\brecommended_actions\b",
        r"\bimmediate_decisions\b",
        r"(?<![A-Za-z0-9-])E\d{2,}(?![A-Za-z0-9-])",
        r"\binternal\b",
        r"\bcost\b",
        r"\bpenalty\b",
        r"\bbreach\b",
    ]
    for pattern in denied_patterns:
        if re.search(pattern, flattened, flags=re.IGNORECASE):
            errors.append(f"client_visibility:payload:{pattern}")
    if _CLIENT_PROGRESS_DENIED_TEXT_RE.search(flattened):
        errors.append("client_visibility:payload:denied_text")
    return errors


_OPERATIONS_PATH_OR_SECRET_RE = re.compile(
    r"(https?://|/opt/|/app/|/tmp/|[A-Za-z]:\\|file_path|storage_path|image_url|thumb_url|api_key|secret|password|token|prompt_used|raw_model_output)",
    flags=re.IGNORECASE,
)


_FINANCE_SECRET_OR_PII_RE = re.compile(
    r"(https?://|/opt/|/app/|/tmp/|[A-Za-z]:\\|file_path|storage_path|image_url|thumb_url|employee_id|purchaser_name|api_key|secret|password|token|prompt_used|raw_model_output)",
    flags=re.IGNORECASE,
)

_EXECUTIVE_DENIED_RE = re.compile(
    r"(https?://|/opt/|/app/|/tmp/|[A-Za-z]:\\|file_path|storage_path|image_url|thumb_url|employee_id|uploaded_by_user_id|created_by_user_id|api_key|secret|password|token|prompt_used|raw_model_output|\bshould\b|\bmust\b|\bpriority\b|\brecommend\b|\bensure\b|\burgent\b|\brank(?:ing)?\b|\bperformance score\b)",
    flags=re.IGNORECASE,
)


_HEALTH_SEVERITY_RANK = {"green": 0, "amber": 1, "red": 2}


def _derived_overall_health_status(dimensions: list[Any]) -> str:
    max_rank = 0
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        max_rank = max(max_rank, _HEALTH_SEVERITY_RANK.get(str(dimension.get("status") or "green"), 0))
    for status, rank in _HEALTH_SEVERITY_RANK.items():
        if rank == max_rank:
            return status
    return "green"


def _operations_health_summary_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    facts = facts or {}
    errors: list[str] = []
    if _contains_denied_fact_key(facts, OPERATIONS_DENIED_FACT_KEYS):
        errors.append("operations_visibility:facts:denied_key")
    health_surface = payload.get("health_surface") if isinstance(payload.get("health_surface"), dict) else {}
    dimensions = health_surface.get("dimensions") if isinstance(health_surface.get("dimensions"), list) else []
    errors.extend(_decision_surface_ref_errors(prefix="health_surface", snapshot_items=dimensions, facts=facts))
    expected_status = _derived_overall_health_status(dimensions)
    if payload.get("overall_status") != expected_status:
        errors.append(f"operations_health:overall_status:mismatch:{expected_status}")
    summaries: set[str] = set()
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, dict):
            continue
        status = str(dimension.get("status") or "")
        if status not in _HEALTH_SEVERITY_RANK:
            errors.append(f"operations_health:dimensions[{index}]:unknown_status:{status}")
        summary = _safe_text(dimension.get("deterministic_summary"), limit=600)
        if summary:
            summaries.add(summary)
        text_blob = " ".join(_flatten_strings(dimension))
        if _OPERATIONS_PATH_OR_SECRET_RE.search(text_blob):
            errors.append(f"operations_visibility:dimensions[{index}]:path_or_secret")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    generation_path = _body_generation_path(payload)
    total_words = 0
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        total_words += len(re.findall(r"\w+", text))
        if generation_path != "ai_polish" and summaries and text not in summaries:
            errors.append(f"operations_health:paragraphs[{index}]:not_from_health_surface")
        if _OPERATIONS_PATH_OR_SECRET_RE.search(text):
            errors.append(f"operations_visibility:paragraphs[{index}]:path_or_secret")
    if isinstance(body.get("word_count"), int) and body["word_count"] > 140:
        errors.append("operations_health:word_count:max_140")
    if total_words > 140:
        errors.append("operations_health:paragraphs:word_count:max_140")
    if _OPERATIONS_PATH_OR_SECRET_RE.search(" ".join(_flatten_strings(payload))):
        errors.append("operations_visibility:payload:path_or_secret")
    return errors


def _finance_summary_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    facts = facts or {}
    errors: list[str] = []
    if _contains_denied_fact_key(facts, FINANCE_DENIED_FACT_KEYS):
        errors.append("finance_visibility:facts:denied_key")
    surface = payload.get("finance_surface") if isinstance(payload.get("finance_surface"), dict) else {}
    sections = surface.get("sections") if isinstance(surface.get("sections"), list) else []
    errors.extend(_decision_surface_ref_errors(prefix="finance_surface", snapshot_items=sections, facts=facts))
    lines: set[str] = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_lines = section.get("lines") if isinstance(section.get("lines"), list) else []
        for line in section_lines:
            text = _safe_text(line, limit=600)
            if text:
                lines.add(text)
        if _FINANCE_SECRET_OR_PII_RE.search(" ".join(_flatten_strings(section))):
            errors.append(f"finance_visibility:sections[{index}]:pii_or_secret")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    generation_path = _body_generation_path(payload)
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        if generation_path != "ai_polish" and lines and text not in lines:
            errors.append(f"finance_summary:paragraphs[{index}]:not_from_finance_surface")
        if _FINANCE_SECRET_OR_PII_RE.search(text):
            errors.append(f"finance_visibility:paragraphs[{index}]:pii_or_secret")
    headline = payload.get("headline_metrics") if isinstance(payload.get("headline_metrics"), dict) else {}
    receipt_metrics = facts.get("receipt_metrics") if isinstance(facts.get("receipt_metrics"), dict) else {}
    expected_pairs = {
        "receipt_count": receipt_metrics.get("receipt_count"),
        "total_amount": receipt_metrics.get("total_amount"),
        "receipts_missing_amount": receipt_metrics.get("receipts_missing_amount"),
        "fuel_gallons_total": receipt_metrics.get("fuel_gallons_total"),
    }
    for key, expected in expected_pairs.items():
        if key not in headline:
            errors.append(f"finance_summary:headline_metrics:missing:{key}")
        elif headline.get(key) != expected:
            errors.append(f"finance_summary:headline_metrics:value_mismatch:{key}")
    for key in ("receipt_count", "total_amount", "receipts_missing_amount", "fuel_gallons_total"):
        value = headline.get(key)
        if isinstance(value, (int, float)) and value < 0:
            errors.append(f"finance_summary:headline_metrics:negative:{key}")
    if _FINANCE_SECRET_OR_PII_RE.search(" ".join(_flatten_strings(payload))):
        errors.append("finance_visibility:payload:pii_or_secret")
    return errors


def _executive_company_health_errors(payload: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    facts = facts or {}
    errors: list[str] = []
    if _contains_denied_fact_key(facts, EXECUTIVE_DENIED_FACT_KEYS):
        errors.append("executive_visibility:facts:denied_key")
    surface = payload.get("company_health_surface") if isinstance(payload.get("company_health_surface"), dict) else {}
    cards = surface.get("cards") if isinstance(surface.get("cards"), list) else []
    errors.extend(_decision_surface_ref_errors(prefix="company_health_surface", snapshot_items=cards, facts=facts))
    expected_status = _derived_overall_health_status(cards)
    if payload.get("overall_status") != expected_status:
        errors.append(f"executive_company_health:overall_status:mismatch:{expected_status}")
    card_lines: set[str] = set()
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        status = str(card.get("status") or "")
        if status not in _HEALTH_SEVERITY_RANK:
            errors.append(f"executive_company_health:cards[{index}]:unknown_status:{status}")
        for line in card.get("lines") or []:
            text = _safe_text(line, limit=600)
            if text:
                card_lines.add(text)
        if _EXECUTIVE_DENIED_RE.search(" ".join(_flatten_strings(card))):
            errors.append(f"executive_visibility:cards[{index}]:denied_text")
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    generation_path = _body_generation_path(payload)
    total_words = 0
    for index, paragraph in enumerate(paragraphs):
        text = _safe_text(paragraph, limit=1000)
        total_words += len(re.findall(r"\w+", text))
        if generation_path != "ai_polish" and card_lines and text not in card_lines:
            errors.append(f"executive_company_health:paragraphs[{index}]:not_from_health_surface")
        if _EXECUTIVE_DENIED_RE.search(text):
            errors.append(f"executive_visibility:paragraphs[{index}]:denied_text")
    if isinstance(body.get("word_count"), int) and body["word_count"] > 180:
        errors.append("executive_company_health:word_count:max_180")
    if total_words > 180:
        errors.append("executive_company_health:paragraphs:word_count:max_180")
    if _EXECUTIVE_DENIED_RE.search(" ".join(_flatten_strings(payload))):
        errors.append("executive_visibility:payload:denied_text")
    expected_metrics = {
        "projects_total": (facts.get("project_portfolio") or {}).get("projects_total"),
        "photos_current": (facts.get("photo_activity") or {}).get("photos_current"),
        "completed_reports_current": (facts.get("progress_reports") or {}).get("completed_current"),
        "shadow_invalid_current": (facts.get("expression_readiness") or {}).get("shadow_invalid_current"),
        "receipt_count": (facts.get("finance_receipt_coverage") or {}).get("receipt_count"),
    }
    headline = payload.get("headline_metrics") if isinstance(payload.get("headline_metrics"), dict) else {}
    for key, expected in expected_metrics.items():
        if key not in headline:
            errors.append(f"executive_company_health:headline_metrics:missing:{key}")
        elif headline.get(key) != expected:
            errors.append(f"executive_company_health:headline_metrics:value_mismatch:{key}")
    return errors


def _forbidden_phrase_errors(db: Session, *, audience: ExpressionAudience, language: str, payload: dict[str, Any]) -> list[str]:
    rows = list(
        db.scalars(
            select(ExpressionForbiddenPhrase).where(
                ExpressionForbiddenPhrase.is_active.is_(True),
                ExpressionForbiddenPhrase.language == language,
                ExpressionForbiddenPhrase.set_id == audience.forbidden_phrase_set_id,
            )
        )
    )
    strings = [text for text in _flatten_strings(payload) if text != FIXED_EMPLOYEE_DISCLAIMER]
    errors: list[str] = []
    for row in rows:
        for text in strings:
            if row.match_type == "regex":
                if re.search(row.phrase, text):
                    errors.append(f"forbidden_phrase:{row.phrase}")
                    break
            elif row.phrase in text:
                errors.append(f"forbidden_phrase:{row.phrase}")
                break
    return errors


def _employee_contribution_zero_current_photo_errors(
    payload: dict[str, Any],
    facts: dict[str, Any] | None,
) -> list[str]:
    if facts is None:
        return []
    counts = facts.get("counts") if isinstance(facts.get("counts"), dict) else {}
    photos_current = int(counts.get("photos_current") or 0)
    if photos_current > 0:
        return []
    combined = " ".join(_flatten_strings(payload))
    return [
        f"employee_contribution:zero_current_photos:{phrase}"
        for phrase in _EMPLOYEE_ZERO_CURRENT_PHOTO_PHRASES
        if phrase in combined
    ]


def _language_quality_errors(*, payload: dict[str, Any], language: str) -> list[str]:
    if language != "zh":
        return []
    errors: list[str] = []
    text_fields = ("summary_line", "comparison_text")
    for field_name in text_fields:
        if field_name in payload and not _has_cjk(payload.get(field_name)):
            errors.append(f"language:{field_name}:missing_cjk")
    for field_name in ("contribution_explanation", "strengths", "suggestions"):
        value = payload.get(field_name)
        if not isinstance(value, list):
            continue
        for index, item in enumerate(value):
            if not _has_cjk(item):
                errors.append(f"language:{field_name}[{index}]:missing_cjk")
    highlights = payload.get("recent_highlights")
    if isinstance(highlights, list):
        for index, item in enumerate(highlights):
            text = item.get("text") if isinstance(item, dict) else None
            if text and not _has_cjk(text):
                errors.append(f"language:recent_highlights[{index}].text:missing_cjk")
    return errors


def _validate_expression_payload(
    db: Session,
    *,
    audience: ExpressionAudience,
    contract: ExpressionOutputContract,
    payload: dict[str, Any],
    language: str,
    facts: dict[str, Any] | None = None,
    locked_baseline: dict[str, Any] | None = None,
) -> list[str]:
    schema = contract.json_schema if isinstance(contract.json_schema, dict) else {}
    errors = _validate_against_contract(payload, schema)
    if facts is not None:
        errors.extend(_required_fact_path_errors(contract, facts))
        if contract.artifact_type == PROGRESS_REPORT_TRANSLATION_ARTIFACT:
            errors.extend(_progress_report_translation_preservation_errors(payload, facts))
        if contract.artifact_type == PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT:
            errors.extend(_translation_chunk_quality_errors(payload, facts))
    if contract.artifact_type == PROGRESS_REPORT_TRANSLATION_ARTIFACT:
        errors.extend(_progress_report_translation_language_errors(payload))
    if contract.artifact_type == GENERATED_REPORT_MARKDOWN_ARTIFACT:
        errors.extend(_generated_report_markdown_errors(payload, facts))
    if contract.artifact_type == GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT:
        errors.extend(_generated_report_markdown_errors({"sections": [payload]}, facts, require_all_sections=False))
    if contract.artifact_type == PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT:
        errors.extend(_project_manager_decision_brief_errors(payload, facts))
    if contract.artifact_type == CLIENT_PROGRESS_SUMMARY_ARTIFACT:
        errors.extend(_client_progress_summary_errors(payload, facts))
    if contract.artifact_type == OPERATIONS_HEALTH_SUMMARY_ARTIFACT:
        errors.extend(_operations_health_summary_errors(payload, facts))
    if contract.artifact_type == FINANCE_SUMMARY_ARTIFACT:
        errors.extend(_finance_summary_errors(payload, facts))
    if contract.artifact_type == EXECUTIVE_COMPANY_HEALTH_ARTIFACT:
        errors.extend(_executive_company_health_errors(payload, facts))
    if contract.artifact_type == EMPLOYEE_CONTRIBUTION_ARTIFACT:
        errors.extend(_employee_contribution_zero_current_photo_errors(payload, facts))
    if locked_baseline is not None and _body_generation_path(payload) == "ai_polish":
        errors.extend(
            _surface_immutability_errors(
                artifact_type=contract.artifact_type,
                baseline=locked_baseline,
                candidate=payload,
                partial_candidate=False,
            )
        )
    errors.extend(_forbidden_claim_errors(contract, payload))
    errors.extend(_forbidden_phrase_errors(db, audience=audience, language=language, payload=payload))
    errors.extend(_language_quality_errors(payload=payload, language=language))
    return errors


def build_employee_contribution_fallback(facts: dict[str, Any]) -> dict[str, Any]:
    counts = facts.get("counts") if isinstance(facts.get("counts"), dict) else {}
    trend = facts.get("trend") if isinstance(facts.get("trend"), dict) else {}
    highlights = facts.get("recent_highlights") if isinstance(facts.get("recent_highlights"), list) else []
    photos_current = int(counts.get("photos_current") or 0)
    photos_previous = int(counts.get("photos_previous") or 0)
    active_days = int(counts.get("active_days_current") or 0)
    completed_ai = int(counts.get("completed_ai_current") or 0)
    photo_delta = int(trend.get("photo_delta") or 0)
    if photos_current:
        summary = f"最近记录窗口内共有 {photos_current} 张现场照片，覆盖 {active_days} 个记录日。"
    else:
        summary = "最近记录窗口内暂未看到新的项目现场照片。"
    if photos_current:
        if photo_delta > 0:
            comparison = "与上一记录窗口相比，本窗口的现场照片数量有所增加。"
        elif photo_delta < 0:
            comparison = "与上一记录窗口相比，本窗口的现场照片数量较少，可结合实际施工安排理解。"
        else:
            comparison = "与上一记录窗口相比，本窗口的现场照片数量基本持平。"
    elif photos_previous > 0:
        comparison = "与上一记录窗口相比，本窗口未见新的现场照片。"
    else:
        comparison = "当前与上一记录窗口均未看到新的项目现场照片。"

    recent_highlights = []
    for item in highlights[:5]:
        if not isinstance(item, dict):
            continue
        text = _safe_text(item.get("summary"), limit=180)
        if not text:
            labels = item.get("labels") if isinstance(item.get("labels"), list) else []
            text = "、".join(str(label) for label in labels[:3])
        if text and not _has_cjk(text):
            text = f"照片 {item.get('photo_id')} 已记录现场材料、设备或环境信息。"
        if text:
            recent_highlights.append({"photo_id": item.get("photo_id"), "text": text})

    if photos_current:
        contribution_explanation = [
            f"系统已完成 {completed_ai} 张照片的 AI 识别，可用于后续现场记录回看。",
            "这些内容来自已入库照片和结构化观察，不包含硬盘文件扫描结果。",
        ]
    else:
        contribution_explanation = [
            "当前记录窗口内暂未看到新的项目现场照片，可结合施工安排理解记录节奏。",
            "说明仅基于已入库记录统计，不包含硬盘文件扫描结果。",
        ]

    return {
        "summary_line": summary,
        "contribution_explanation": contribution_explanation,
        "strengths": ["有助于项目成员了解近期现场变化。"] if photos_current else [],
        "suggestions": ["后续可继续补充关键工序、材料到场、隐蔽工程等节点照片。"],
        "comparison_text": comparison,
        "recent_highlights": recent_highlights,
        "disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
    }


def render_employee_contribution_markdown(payload: dict[str, Any]) -> str:
    parts = [_safe_text(payload.get("summary_line"), limit=360)]
    for key in ("contribution_explanation", "strengths", "suggestions"):
        items = payload.get(key)
        if isinstance(items, list):
            parts.extend(f"- {_safe_text(item, limit=260)}" for item in items if _safe_text(item, limit=260))
    comparison = _safe_text(payload.get("comparison_text"), limit=300)
    if comparison:
        parts.append(comparison)
    disclaimer = _safe_text(payload.get("disclaimer"), limit=120)
    if disclaimer:
        parts.append(disclaimer)
    return "\n".join(part for part in parts if part).strip()


def render_json_artifact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def run_expression_shadow(
    db: Session,
    *,
    app_settings: Settings,
    snapshot: FactSnapshot,
    artifact_type: str,
    audience_id: str,
    language: str,
    preferred_model: str | None = None,
    use_ai: bool = True,
    fallback_payload: dict[str, Any] | None = None,
    render_payload=None,
) -> ExpressionRunResult:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    audience = db.get(ExpressionAudience, audience_id)
    if audience is None:
        raise ValueError(f"Expression audience '{audience_id}' is not seeded")
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=artifact_type,
    )
    contract = _resolve_contract(db, artifact_type=artifact_type, audience_id=audience.id)
    prompt = _format_prompt(prompt_version, facts=facts, contract=contract)

    errors: list[str] = []
    raw_model_output: str | None = None
    payload: dict[str, Any] | None = None
    model_used: str | None = None
    backend_profile: dict[str, Any] | None = None
    if use_ai:
        for backend in _expression_backend_candidates(
            db,
            app_settings=app_settings,
            company_id=snapshot.company_id,
            preferred_model=preferred_model,
        )[:3]:
            try:
                raw_model_output = (
                    _call_ollama_expression_backend(backend, prompt=prompt)
                    if backend.type == OLLAMA_TYPE
                    else call_text_backend(backend, prompt=prompt)
                )
                candidate = _extract_json_object(raw_model_output)
                candidate_payload = candidate
                if artifact_type in _SURFACE_SUMMARY_ARTIFACT_TYPES and fallback_payload:
                    tamper_errors = _surface_immutability_errors(
                        artifact_type=artifact_type,
                        baseline=fallback_payload,
                        candidate=candidate,
                        partial_candidate=True,
                    )
                    if tamper_errors:
                        errors.extend(tamper_errors)
                        continue
                    candidate_payload = _surface_summary_polished_payload(
                        fallback_payload=fallback_payload,
                        candidate=candidate,
                    )
                candidate_errors = _validate_expression_payload(
                    db,
                    audience=audience,
                    contract=contract,
                    payload=candidate_payload,
                    language=language,
                    facts=facts,
                    locked_baseline=fallback_payload if artifact_type in _SURFACE_SUMMARY_ARTIFACT_TYPES else None,
                )
                model_used = backend.model
                backend_profile = {"backend_id": backend.id, "backend_type": backend.type, "model": backend.model}
                if candidate_errors:
                    errors.extend(candidate_errors)
                    continue
                payload = candidate_payload
                break
            except Exception as exc:
                errors.append(f"backend:{backend.id}:{exc}")

    used_fallback = payload is None
    final_validation_errors: list[str] = []
    if payload is None:
        payload = fallback_payload or {}
        final_validation_errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=payload,
            language=language,
            facts=facts,
            locked_baseline=fallback_payload if artifact_type in _SURFACE_SUMMARY_ARTIFACT_TYPES else None,
        )
        errors.extend(final_validation_errors)
        model_used = model_used or "deterministic_fallback"
        backend_profile = backend_profile or {"backend_id": "deterministic_fallback", "backend_type": "local"}

    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    rendered = render_payload(payload) if render_payload is not None else render_json_artifact(payload)
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=snapshot.scope_key_json,
        artifact_type=artifact_type,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=raw_model_output,
        rendered_markdown=rendered,
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json=backend_profile,
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)


def promote_expression_artifact(db: Session, *, artifact_id: str) -> ExpressionArtifact:
    artifact = db.get(ExpressionArtifact, artifact_id)
    if artifact is None:
        raise ValueError(f"Expression artifact {artifact_id} was not found")
    if artifact.validation_status not in PROMOTABLE_EXPRESSION_STATUSES:
        raise ValueError(f"Expression artifact {artifact_id} is not valid for promotion")
    if not isinstance(artifact.structured_json, dict):
        raise ValueError(f"Expression artifact {artifact_id} has no structured payload")
    if artifact.promoted and artifact.validation_status == "promoted_valid":
        return artifact

    superseded_id: str | None = None
    candidates = list(
        db.scalars(
            select(ExpressionArtifact)
            .where(
                ExpressionArtifact.company_id == artifact.company_id,
                ExpressionArtifact.scope_type == artifact.scope_type,
                ExpressionArtifact.artifact_type == artifact.artifact_type,
                ExpressionArtifact.audience_id == artifact.audience_id,
                ExpressionArtifact.language == artifact.language,
                ExpressionArtifact.contract_id == artifact.contract_id,
                ExpressionArtifact.promoted.is_(True),
                ExpressionArtifact.id != artifact.id,
            )
            .order_by(ExpressionArtifact.created_at.desc())
        )
    )
    target_scope = artifact.scope_key_json if isinstance(artifact.scope_key_json, dict) else artifact.scope_key_json
    for existing in candidates:
        existing_scope = existing.scope_key_json if isinstance(existing.scope_key_json, dict) else existing.scope_key_json
        if existing_scope != target_scope:
            continue
        if superseded_id is None:
            superseded_id = existing.id
        existing.promoted = False
        db.add(existing)

    artifact.promoted = True
    artifact.validation_status = "promoted_valid"
    if superseded_id is not None:
        artifact.supersedes_artifact_id = superseded_id
    db.add(artifact)
    db.flush()
    return artifact


def generate_employee_contribution_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int = 30,
    language: str = "zh",
    preferred_model: str | None = None,
    use_ai: bool = True,
) -> ExpressionRunResult:
    snapshot = create_or_get_employee_fact_snapshot(
        db,
        company_id=company_id,
        employee_id=employee_id,
        project_id=project_id,
        window_days=window_days,
    )
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    return run_expression_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        artifact_type=EMPLOYEE_CONTRIBUTION_ARTIFACT,
        audience_id="employee",
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_employee_contribution_fallback(facts),
        render_payload=render_employee_contribution_markdown,
    )


def _decision_fact_ref(snapshot: FactSnapshot, field_path: str, observed_value: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.id,
        "field_path": field_path,
        "observed_value": observed_value,
    }


def _decision_item(
    *,
    item_id: str,
    category: str,
    severity: str,
    summary: str,
    fact_refs: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": category,
        "severity": severity,
        "deterministic_summary": summary,
        "fact_refs": fact_refs,
        "metrics": metrics,
    }


def build_project_manager_decision_brief_fallback(snapshot: FactSnapshot) -> dict[str, Any]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    photo_metrics = facts.get("photo_metrics") if isinstance(facts.get("photo_metrics"), dict) else {}
    report_metrics = facts.get("progress_report_metrics") if isinstance(facts.get("progress_report_metrics"), dict) else {}
    expression_metrics = facts.get("expression_metrics") if isinstance(facts.get("expression_metrics"), dict) else {}
    latest_report = (
        report_metrics.get("latest_structured_summary")
        if isinstance(report_metrics.get("latest_structured_summary"), dict)
        else {}
    )

    items: list[dict[str, Any]] = []
    photos_current = int(photo_metrics.get("photos_current") or 0)
    photos_previous = int(photo_metrics.get("photos_previous") or 0)
    photo_delta = int(photo_metrics.get("photo_delta") or 0)
    active_days = int(photo_metrics.get("active_days_current") or 0)
    completed_ai = int(photo_metrics.get("completed_ai_current") or 0)
    contributors = int(photo_metrics.get("employee_contributors_current") or 0)
    completed_reports = int(report_metrics.get("completed_with_content") or 0)

    if photos_current == 0:
        items.append(
            _decision_item(
                item_id="evidence_volume_empty",
                category="contribution_gap",
                severity="watch",
                summary="The current project window has 0 database photos.",
                fact_refs=[_decision_fact_ref(snapshot, "photo_metrics.photos_current", photos_current)],
                metrics={"photos_current": photos_current, "window_days": facts.get("window", {}).get("days")},
            )
        )
    else:
        items.append(
            _decision_item(
                item_id="evidence_volume_current",
                category="evidence_coverage",
                severity="info",
                summary=(
                    f"The current project window has {photos_current} database photos across "
                    f"{active_days} active record days and {contributors} employee contributors."
                ),
                fact_refs=[
                    _decision_fact_ref(snapshot, "photo_metrics.photos_current", photos_current),
                    _decision_fact_ref(snapshot, "photo_metrics.active_days_current", active_days),
                    _decision_fact_ref(snapshot, "photo_metrics.employee_contributors_current", contributors),
                ],
                metrics={
                    "photos_current": photos_current,
                    "active_days_current": active_days,
                    "employee_contributors_current": contributors,
                },
            )
        )

    if photos_previous > 0 and photo_delta < 0:
        items.append(
            _decision_item(
                item_id="evidence_volume_change",
                category="evidence_coverage",
                severity="watch",
                summary=(
                    f"The current project window has {abs(photo_delta)} fewer database photos "
                    "than the previous comparable window."
                ),
                fact_refs=[
                    _decision_fact_ref(snapshot, "photo_metrics.photos_current", photos_current),
                    _decision_fact_ref(snapshot, "photo_metrics.photos_previous", photos_previous),
                    _decision_fact_ref(snapshot, "photo_metrics.photo_delta", photo_delta),
                ],
                metrics={"photos_current": photos_current, "photos_previous": photos_previous, "photo_delta": photo_delta},
            )
        )

    if photos_current > completed_ai:
        items.append(
            _decision_item(
                item_id="ai_processing_coverage",
                category="evidence_processing",
                severity="watch",
                summary=f"AI labeling is completed for {completed_ai} of {photos_current} current project photos.",
                fact_refs=[
                    _decision_fact_ref(snapshot, "photo_metrics.completed_ai_current", completed_ai),
                    _decision_fact_ref(snapshot, "photo_metrics.photos_current", photos_current),
                ],
                metrics={"completed_ai_current": completed_ai, "photos_current": photos_current},
            )
        )

    if completed_reports == 0:
        items.append(
            _decision_item(
                item_id="progress_report_absent",
                category="reporting_coverage",
                severity="watch",
                summary="No completed progress report with structured content is currently stored for this project.",
                fact_refs=[_decision_fact_ref(snapshot, "progress_report_metrics.completed_with_content", completed_reports)],
                metrics={"completed_with_content": completed_reports},
            )
        )
    else:
        items.append(
            _decision_item(
                item_id="progress_report_available",
                category="reporting_coverage",
                severity="info",
                summary=(
                    f"The project has {completed_reports} completed progress reports with structured content; "
                    f"the latest completed report is {report_metrics.get('latest_completed_report_id')}."
                ),
                fact_refs=[
                    _decision_fact_ref(snapshot, "progress_report_metrics.completed_with_content", completed_reports),
                    _decision_fact_ref(snapshot, "progress_report_metrics.latest_completed_report_id", report_metrics.get("latest_completed_report_id")),
                ],
                metrics={
                    "completed_with_content": completed_reports,
                    "latest_completed_report_id": report_metrics.get("latest_completed_report_id"),
                },
            )
        )

    latest_status = latest_report.get("overall_status")
    if latest_status in {"at_risk", "blocked"}:
        items.append(
            _decision_item(
                item_id="latest_report_status",
                category="reporting_signal",
                severity="watch" if latest_status == "at_risk" else "action_required",
                summary=f"The latest structured progress report has overall_status={latest_status}.",
                fact_refs=[
                    _decision_fact_ref(snapshot, "progress_report_metrics.latest_structured_summary.overall_status", latest_status),
                    _decision_fact_ref(
                        snapshot,
                        "progress_report_metrics.latest_structured_summary.confidence_level",
                        latest_report.get("confidence_level"),
                    ),
                ],
                metrics={
                    "overall_status": latest_status,
                    "confidence_level": latest_report.get("confidence_level"),
                    "overall_progress_percent": latest_report.get("overall_progress_percent"),
                },
            )
        )

    employee_expression = (
        expression_metrics.get(EMPLOYEE_CONTRIBUTION_ARTIFACT)
        if isinstance(expression_metrics.get(EMPLOYEE_CONTRIBUTION_ARTIFACT), dict)
        else {}
    )
    if int(employee_expression.get("promoted_artifacts") or 0) == 0:
        items.append(
            _decision_item(
                item_id="employee_contribution_not_promoted",
                category="expression_readiness",
                severity="info",
                summary="No promoted employee contribution expression artifact is currently linked to this project.",
                fact_refs=[
                    _decision_fact_ref(
                        snapshot,
                        f"expression_metrics.{EMPLOYEE_CONTRIBUTION_ARTIFACT}.promoted_artifacts",
                        employee_expression.get("promoted_artifacts"),
                    )
                ],
                metrics={"promoted_artifacts": employee_expression.get("promoted_artifacts") or 0},
            )
        )

    paragraphs = [item["deterministic_summary"] for item in items[:5]]
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in paragraphs)
    return {
        "artifact_type": PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
        "artifact_version": "1.0.0",
        "audience": "project_manager",
        "visibility": "shadow",
        "project_id": facts.get("scope", {}).get("project_id"),
        "as_of": facts.get("window", {}).get("current_end_utc"),
        "fact_snapshot_ids": [snapshot.id],
        "decision_surface": {
            "computed_at": to_utc_iso(utc_now()),
            "items": items,
        },
        "body": {
            "format": "markdown",
            "paragraphs": paragraphs,
            "word_count": word_count,
            "generation_path": "deterministic_fallback",
        },
        "provenance": {
            "source_tables": ["projects", "photos", "progress_reports", "expression_artifacts", "fact_snapshots"],
            "fact_snapshot_id": snapshot.id,
            "assembler_version": PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION,
        },
        "validation": {
            "decision_surface_locked": True,
            "ai_changed_decision_surface": False,
            "shadow_only": True,
        },
    }


def render_project_manager_decision_brief(payload: dict[str, Any]) -> str:
    project_id = _safe_text(payload.get("project_id"), limit=80) or "Project"
    parts = [f"# Project Manager Decision Brief: {project_id}"]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        text = _safe_text(paragraph, limit=500)
        if text:
            parts.append(f"- {text}")
    return "\n".join(parts).strip()


def _build_project_manager_polish_prompt(
    *,
    fallback_payload: dict[str, Any],
    facts: dict[str, Any],
) -> str:
    items = (fallback_payload.get("decision_surface") or {}).get("items")
    body = fallback_payload.get("body") if isinstance(fallback_payload.get("body"), dict) else {}
    source_paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    return "\n".join(
        [
            "You are polishing a shadow-only project manager brief.",
            "The server owns all facts, categories, severities, metrics, and decision_surface items.",
            "Return strict JSON only with this exact shape: {\"body\":{\"paragraphs\":[\"...\"]}}.",
            "Do not return decision_surface, facts, provenance, raw model output, prompts, secrets, employee rankings, or recommendations.",
            "Do not use these words or meanings: should, must, priority, prioritize, recommend, ensure, need to, escalate, urgent, guaranteed.",
            "Do not invent causes, dates, risks, people, amounts, or action steps.",
            "Keep at most 5 short factual paragraphs and at most 120 words total.",
            "Use only these deterministic source paragraphs and keep the same factual meaning:",
            json.dumps(source_paragraphs, ensure_ascii=False, indent=2),
            "Locked decision_surface item ids and fact refs for context:",
            json.dumps(items if isinstance(items, list) else [], ensure_ascii=False, indent=2),
            "Database fact summary for context only:",
            json.dumps(
                {
                    "scope": facts.get("scope"),
                    "window": facts.get("window"),
                    "photo_metrics": facts.get("photo_metrics"),
                    "progress_report_metrics": facts.get("progress_report_metrics"),
                    "expression_metrics": facts.get("expression_metrics"),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        ]
    )


def _build_project_manager_copyedit_retry_prompt(
    *,
    fallback_payload: dict[str, Any],
    previous_errors: list[str],
) -> str:
    body = fallback_payload.get("body") if isinstance(fallback_payload.get("body"), dict) else {}
    source_paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    return "\n".join(
        [
            "Retry as a strict copy editor for a shadow-only project manager brief.",
            "Return strict JSON only with this exact shape: {\"body\":{\"paragraphs\":[\"...\"]}}.",
            "Keep the same number of paragraphs in the same order.",
            "Copy each source paragraph exactly unless grammar or spacing is clearly wrong.",
            "Do not add, remove, replace, summarize, or soften any facts, numbers, ids, statuses, categories, causes, risks, or actions.",
            "Do not use these words or meanings: should, must, priority, prioritize, recommend, ensure, need to, escalate, urgent, guaranteed.",
            "If uncertain, return the source paragraphs unchanged.",
            "Previous validation errors:",
            json.dumps(previous_errors, ensure_ascii=False, indent=2),
            "Source paragraphs:",
            json.dumps(source_paragraphs, ensure_ascii=False, indent=2),
        ]
    )


def _project_manager_polished_payload(
    *,
    fallback_payload: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    candidate_body = candidate.get("body") if isinstance(candidate.get("body"), dict) else {}
    paragraphs = candidate_body.get("paragraphs") if isinstance(candidate_body.get("paragraphs"), list) else []
    cleaned_paragraphs = []
    for paragraph in paragraphs[:5]:
        text = _safe_text(paragraph, limit=600)
        if text:
            cleaned_paragraphs.append(text)
    if not cleaned_paragraphs:
        raise ValueError("project_manager_polish:body.paragraphs:empty")
    payload = json.loads(json.dumps(fallback_payload, ensure_ascii=False))
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in cleaned_paragraphs)
    payload["body"] = {
        "format": "markdown",
        "paragraphs": cleaned_paragraphs,
        "word_count": word_count,
        "generation_path": "ai_polish",
    }
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
    validation.update(
        {
            "decision_surface_locked": True,
            "ai_changed_decision_surface": False,
            "ai_polish_attempted": True,
        }
    )
    payload["validation"] = validation
    return payload


def summarize_project_manager_decision_brief_errors(errors: list[str]) -> dict[str, int]:
    reason_counts = {
        "forbidden_language": 0,
        "unknown_numbers": 0,
        "low_grounding": 0,
        "backend_or_json": 0,
        "schema_or_contract": 0,
        "other": 0,
    }
    for error in errors:
        text = str(error)
        if "imperative_language" in text or text.startswith("forbidden_claim:"):
            reason_counts["forbidden_language"] += 1
        elif "ai_polish_unknown_numbers" in text:
            reason_counts["unknown_numbers"] += 1
        elif "ai_polish_low_grounding" in text:
            reason_counts["low_grounding"] += 1
        elif text.startswith("backend:") or "project_manager_polish:" in text:
            reason_counts["backend_or_json"] += 1
        elif text.startswith("schema:") or text.startswith("contract:") or "json_schema" in text:
            reason_counts["schema_or_contract"] += 1
        else:
            reason_counts["other"] += 1
    return {reason: count for reason, count in reason_counts.items() if count}


def run_project_manager_decision_brief_shadow(
    db: Session,
    *,
    app_settings: Settings,
    snapshot: FactSnapshot,
    language: str,
    preferred_model: str | None,
    use_ai: bool,
    fallback_payload: dict[str, Any],
) -> ExpressionRunResult:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
    )
    contract = _resolve_contract(
        db,
        artifact_type=PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
        audience_id=audience.id,
    )
    errors: list[str] = []
    raw_model_output: str | None = None
    payload: dict[str, Any] | None = None
    model_used: str | None = None
    backend_profile: dict[str, Any] | None = None
    if use_ai:
        prompt = _build_project_manager_polish_prompt(fallback_payload=fallback_payload, facts=facts)
        for backend in _expression_backend_candidates(
            db,
            app_settings=app_settings,
            company_id=snapshot.company_id,
            preferred_model=preferred_model,
        )[:3]:
            try:
                raw_model_output = (
                    _call_ollama_expression_backend(backend, prompt=prompt)
                    if backend.type == OLLAMA_TYPE
                    else call_text_backend(backend, prompt=prompt)
                )
                candidate = _extract_json_object(raw_model_output)
                candidate_payload = _project_manager_polished_payload(
                    fallback_payload=fallback_payload,
                    candidate=candidate,
                )
                candidate_errors = _validate_expression_payload(
                    db,
                    audience=audience,
                    contract=contract,
                    payload=candidate_payload,
                    language=language,
                    facts=facts,
                )
                model_used = backend.model
                backend_profile = {"backend_id": backend.id, "backend_type": backend.type, "model": backend.model}
                if candidate_errors:
                    try:
                        retry_prompt = _build_project_manager_copyedit_retry_prompt(
                            fallback_payload=fallback_payload,
                            previous_errors=candidate_errors,
                        )
                        raw_model_output = (
                            _call_ollama_expression_backend(backend, prompt=retry_prompt)
                            if backend.type == OLLAMA_TYPE
                            else call_text_backend(backend, prompt=retry_prompt)
                        )
                        retry_candidate = _extract_json_object(raw_model_output)
                        retry_payload = _project_manager_polished_payload(
                            fallback_payload=fallback_payload,
                            candidate=retry_candidate,
                        )
                        retry_errors = _validate_expression_payload(
                            db,
                            audience=audience,
                            contract=contract,
                            payload=retry_payload,
                            language=language,
                            facts=facts,
                        )
                        if not retry_errors:
                            payload = retry_payload
                            backend_profile["retry_used"] = True
                            backend_profile["retry_reason_counts"] = summarize_project_manager_decision_brief_errors(candidate_errors)
                            break
                        errors.extend(retry_errors)
                    except Exception as exc:
                        errors.append(f"backend:{backend.id}:retry:{exc}")
                    errors.extend(candidate_errors)
                    continue
                payload = candidate_payload
                break
            except Exception as exc:
                errors.append(f"backend:{backend.id}:{exc}")

    used_fallback = payload is None
    final_validation_errors: list[str] = []
    if payload is None:
        payload = fallback_payload
        final_validation_errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=payload,
            language=language,
            facts=facts,
        )
        errors.extend(final_validation_errors)
        model_used = model_used or "deterministic_fallback"
        backend_profile = backend_profile or {"backend_id": "deterministic_fallback", "backend_type": "local"}

    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=snapshot.scope_key_json,
        artifact_type=PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=raw_model_output,
        rendered_markdown=render_project_manager_decision_brief(payload),
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json=backend_profile,
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)


def _summary_item(
    *,
    item_id: str,
    category: str,
    summary: str,
    fact_refs: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": category,
        "deterministic_summary": summary,
        "fact_refs": fact_refs,
        "metrics": metrics,
    }


def build_client_progress_summary_fallback(snapshot: FactSnapshot) -> dict[str, Any]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    photo_metrics = facts.get("client_visible_photo_metrics") if isinstance(facts.get("client_visible_photo_metrics"), dict) else {}
    latest = facts.get("latest_progress_report") if isinstance(facts.get("latest_progress_report"), dict) else {}
    report = latest.get("client_report") if isinstance(latest.get("client_report"), dict) else {}
    items: list[dict[str, Any]] = []

    photos_current = int(photo_metrics.get("photos_current") or 0)
    active_days = int(photo_metrics.get("active_days_current") or 0)
    if photos_current:
        items.append(
            _summary_item(
                item_id="client_visible_activity",
                category="recent_activity",
                summary=(
                    f"The current project window has {photos_current} approved client-visible photos "
                    f"across {active_days} active record days."
                ),
                fact_refs=[
                    _decision_fact_ref(snapshot, "client_visible_photo_metrics.photos_current", photos_current),
                    _decision_fact_ref(snapshot, "client_visible_photo_metrics.active_days_current", active_days),
                ],
                metrics={"photos_current": photos_current, "active_days_current": active_days},
            )
        )
    else:
        items.append(
            _summary_item(
                item_id="client_visible_activity_empty",
                category="recent_activity",
                summary="No approved client-visible photos are currently stored for this project window.",
                fact_refs=[_decision_fact_ref(snapshot, "client_visible_photo_metrics.photos_current", photos_current)],
                metrics={"photos_current": photos_current},
            )
        )

    if latest.get("has_completed_report"):
        status_value = report.get("overall_status")
        progress_percent = report.get("overall_progress_percent")
        confidence_value = report.get("confidence_level")
        status_text = status_value or "unknown"
        confidence_text = confidence_value or "unknown"
        progress_text = f"{progress_percent}%" if isinstance(progress_percent, (int, float)) else "not specified"
        items.append(
            _summary_item(
                item_id="latest_client_progress_status",
                category="progress_status",
                summary=(
                    f"The latest completed progress report lists overall_status={status_text}, "
                    f"overall_progress_percent={progress_text}, and confidence_level={confidence_text}."
                ),
                fact_refs=[
                    _decision_fact_ref(snapshot, "latest_progress_report.client_report.overall_status", status_value),
                    _decision_fact_ref(snapshot, "latest_progress_report.client_report.overall_progress_percent", progress_percent),
                    _decision_fact_ref(snapshot, "latest_progress_report.client_report.confidence_level", confidence_value),
                ],
                metrics={
                    "overall_status": status_value,
                    "overall_progress_percent": progress_percent,
                    "confidence_level": confidence_value,
                },
            )
        )
        completed = report.get("work_completed") if isinstance(report.get("work_completed"), list) else []
        changes = report.get("key_changes") if isinstance(report.get("key_changes"), list) else []
        for index, text in enumerate((completed + changes)[:3]):
            source_path = (
                f"latest_progress_report.client_report.work_completed.{index}"
                if index < len(completed)
                else f"latest_progress_report.client_report.key_changes.{index - len(completed)}"
            )
            summary = _client_safe_text(text, limit=360)
            if summary:
                items.append(
                    _summary_item(
                        item_id=f"client_progress_detail_{index + 1}",
                        category="work_completed",
                        summary=summary,
                        fact_refs=[_decision_fact_ref(snapshot, source_path, summary)],
                        metrics={},
                    )
                )
        limitations = report.get("evidence_limitations") if isinstance(report.get("evidence_limitations"), list) else []
        safe_limitation = _client_safe_text(limitations[0], limit=360) if limitations else ""
        if safe_limitation:
            items.append(
                _summary_item(
                    item_id="client_evidence_limitations",
                    category="limitations",
                    summary=safe_limitation,
                    fact_refs=[
                        _decision_fact_ref(
                            snapshot,
                            "latest_progress_report.client_report.evidence_limitations.0",
                            safe_limitation,
                        )
                    ],
                    metrics={"evidence_limitation_count": len(limitations)},
                )
            )
    else:
        items.append(
            _summary_item(
                item_id="client_progress_report_absent",
                category="evidence_confidence",
                summary="No completed progress report is currently stored for this client-safe summary.",
                fact_refs=[_decision_fact_ref(snapshot, "latest_progress_report.has_completed_report", False)],
                metrics={"has_completed_report": False},
            )
        )

    paragraphs = [item["deterministic_summary"] for item in items[:5]]
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in paragraphs)
    return {
        "artifact_type": CLIENT_PROGRESS_SUMMARY_ARTIFACT,
        "artifact_version": "1.0.0",
        "audience": "client",
        "visibility": "shadow",
        "project_id": facts.get("scope", {}).get("project_id"),
        "as_of": facts.get("window", {}).get("current_end_utc"),
        "fact_snapshot_ids": [snapshot.id],
        "summary_surface": {
            "computed_at": to_utc_iso(utc_now()),
            "items": items,
        },
        "body": {
            "format": "markdown",
            "paragraphs": paragraphs,
            "word_count": word_count,
            "generation_path": "deterministic_fallback",
        },
        "disclaimer": CLIENT_PROGRESS_DISCLAIMER,
        "provenance": {
            "source_tables": ["projects", "photos", "progress_reports"],
            "fact_snapshot_id": snapshot.id,
            "assembler_version": CLIENT_PROGRESS_SUMMARY_ASSEMBLER_VERSION,
            "redaction_profile": "client_progress_v1",
        },
        "validation": {
            "summary_surface_locked": True,
            "ai_changed_summary_surface": False,
            "shadow_only": True,
        },
    }


def render_client_progress_summary(payload: dict[str, Any]) -> str:
    project_id = _safe_text(payload.get("project_id"), limit=80) or "Project"
    parts = [f"# Client Progress Summary: {project_id}"]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        text = _safe_text(paragraph, limit=500)
        if text:
            parts.append(f"- {text}")
    disclaimer = _safe_text(payload.get("disclaimer"), limit=240)
    if disclaimer:
        parts.append(disclaimer)
    return "\n".join(parts).strip()


def _health_dimension(
    *,
    dimension_id: str,
    status: str,
    summary: str,
    fact_refs: list[dict[str, Any]],
    metrics: dict[str, Any],
    threshold_breaches: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": dimension_id,
        "dimension_id": dimension_id,
        "status": status,
        "deterministic_summary": summary,
        "fact_refs": fact_refs,
        "metrics": metrics,
        "threshold_breaches": threshold_breaches or [],
    }


def build_operations_health_summary_fallback(snapshot: FactSnapshot) -> dict[str, Any]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    task_jobs = facts.get("task_jobs") if isinstance(facts.get("task_jobs"), dict) else {}
    photo_processing = facts.get("photo_processing") if isinstance(facts.get("photo_processing"), dict) else {}
    ai_analysis = facts.get("ai_analysis") if isinstance(facts.get("ai_analysis"), dict) else {}
    expression_artifacts = facts.get("expression_artifacts") if isinstance(facts.get("expression_artifacts"), dict) else {}
    dimensions: list[dict[str, Any]] = []

    queued_backlog = int(task_jobs.get("queued_backlog") or 0)
    oldest_queued_age = task_jobs.get("oldest_queued_age_seconds")
    task_status_counts = task_jobs.get("window_status_counts") if isinstance(task_jobs.get("window_status_counts"), dict) else {}
    failed_window = int(task_status_counts.get(TaskStatus.failed.value, 0) or 0)
    dead_letter_window = int(task_status_counts.get(TaskStatus.dead_letter.value, 0) or 0)
    task_status = "green"
    task_breaches: list[str] = []
    if dead_letter_window > 0 or failed_window >= 5:
        task_status = "red"
        task_breaches.extend(["dead_letter_window", "failed_window"])
    elif queued_backlog >= 20 or (isinstance(oldest_queued_age, int) and oldest_queued_age >= 3600) or failed_window > 0:
        task_status = "amber"
        if queued_backlog >= 20:
            task_breaches.append("queued_backlog")
        if isinstance(oldest_queued_age, int) and oldest_queued_age >= 3600:
            task_breaches.append("oldest_queued_age_seconds")
        if failed_window > 0:
            task_breaches.append("failed_window")
    dimensions.append(
        _health_dimension(
            dimension_id="task_jobs",
            status=task_status,
            summary=(
                f"Task jobs show {queued_backlog} queued jobs, {failed_window} failed jobs in the window, "
                f"and {dead_letter_window} dead-letter jobs in the window."
            ),
            fact_refs=[
                _decision_fact_ref(snapshot, "task_jobs.queued_backlog", queued_backlog),
                _decision_fact_ref(snapshot, f"task_jobs.window_status_counts.{TaskStatus.failed.value}", failed_window),
                _decision_fact_ref(snapshot, f"task_jobs.window_status_counts.{TaskStatus.dead_letter.value}", dead_letter_window),
            ],
            metrics={
                "queued_backlog": queued_backlog,
                "failed_window": failed_window,
                "dead_letter_window": dead_letter_window,
                "oldest_queued_age_seconds": oldest_queued_age,
            },
            threshold_breaches=task_breaches,
        )
    )

    photos_current = int(photo_processing.get("project_photos_current") or 0)
    labeling_completed = int(photo_processing.get("labeling_completed_current") or 0)
    labeling_pending = int(photo_processing.get("labeling_pending_current") or 0)
    photo_status = "green"
    photo_breaches: list[str] = []
    if photos_current and labeling_pending >= max(25, int(photos_current * 0.5)):
        photo_status = "red"
        photo_breaches.append("labeling_pending_current")
    elif labeling_pending > 0:
        photo_status = "amber"
        photo_breaches.append("labeling_pending_current")
    dimensions.append(
        _health_dimension(
            dimension_id="photo_processing",
            status=photo_status,
            summary=(
                f"Photo processing shows {photos_current} project photos in the window with "
                f"{labeling_completed} labeling-completed and {labeling_pending} pending labeling."
            ),
            fact_refs=[
                _decision_fact_ref(snapshot, "photo_processing.project_photos_current", photos_current),
                _decision_fact_ref(snapshot, "photo_processing.labeling_completed_current", labeling_completed),
                _decision_fact_ref(snapshot, "photo_processing.labeling_pending_current", labeling_pending),
            ],
            metrics={
                "project_photos_current": photos_current,
                "labeling_completed_current": labeling_completed,
                "labeling_pending_current": labeling_pending,
            },
            threshold_breaches=photo_breaches,
        )
    )

    ai_status_counts = ai_analysis.get("window_status_counts") if isinstance(ai_analysis.get("window_status_counts"), dict) else {}
    ai_logs = int(ai_analysis.get("logs_current") or 0)
    rejected_logs = int(ai_status_counts.get(AIAnalysisStatus.rejected.value, 0) or 0)
    ai_status = "green"
    ai_breaches: list[str] = []
    if ai_logs and rejected_logs >= max(5, int(ai_logs * 0.25)):
        ai_status = "red"
        ai_breaches.append("rejected_logs_current")
    elif rejected_logs > 0:
        ai_status = "amber"
        ai_breaches.append("rejected_logs_current")
    dimensions.append(
        _health_dimension(
            dimension_id="ai_analysis",
            status=ai_status,
            summary=f"AI analysis logs show {ai_logs} records in the window and {rejected_logs} rejected records.",
            fact_refs=[
                _decision_fact_ref(snapshot, "ai_analysis.logs_current", ai_logs),
                _decision_fact_ref(snapshot, f"ai_analysis.window_status_counts.{AIAnalysisStatus.rejected.value}", rejected_logs),
            ],
            metrics={"logs_current": ai_logs, "rejected_logs_current": rejected_logs},
            threshold_breaches=ai_breaches,
        )
    )

    artifacts_current = int(expression_artifacts.get("artifacts_current") or 0)
    shadow_invalid = int(expression_artifacts.get("shadow_invalid_current") or 0)
    fallback_valid = int(expression_artifacts.get("shadow_fallback_valid_current") or 0)
    expression_status = "green"
    expression_breaches: list[str] = []
    if shadow_invalid > 0:
        expression_status = "red"
        expression_breaches.append("shadow_invalid_current")
    elif artifacts_current and fallback_valid >= max(5, int(artifacts_current * 0.75)):
        expression_status = "amber"
        expression_breaches.append("shadow_fallback_valid_current")
    dimensions.append(
        _health_dimension(
            dimension_id="expression_artifacts",
            status=expression_status,
            summary=(
                f"Expression artifacts show {artifacts_current} recent artifacts, "
                f"{shadow_invalid} invalid shadow artifacts, and {fallback_valid} fallback-valid artifacts."
            ),
            fact_refs=[
                _decision_fact_ref(snapshot, "expression_artifacts.artifacts_current", artifacts_current),
                _decision_fact_ref(snapshot, "expression_artifacts.shadow_invalid_current", shadow_invalid),
                _decision_fact_ref(snapshot, "expression_artifacts.shadow_fallback_valid_current", fallback_valid),
            ],
            metrics={
                "artifacts_current": artifacts_current,
                "shadow_invalid_current": shadow_invalid,
                "shadow_fallback_valid_current": fallback_valid,
            },
            threshold_breaches=expression_breaches,
        )
    )

    overall_status = _derived_overall_health_status(dimensions)
    paragraphs = [dimension["deterministic_summary"] for dimension in dimensions]
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in paragraphs)
    return {
        "artifact_type": OPERATIONS_HEALTH_SUMMARY_ARTIFACT,
        "artifact_version": "1.0.0",
        "audience": "operations",
        "visibility": "shadow",
        "company_id": facts.get("scope", {}).get("company_id"),
        "scope_type": OPERATIONS_HEALTH_SUMMARY_SCOPE,
        "as_of": facts.get("window", {}).get("current_end_utc"),
        "overall_status": overall_status,
        "fact_snapshot_ids": [snapshot.id],
        "health_surface": {
            "computed_at": to_utc_iso(utc_now()),
            "dimensions": dimensions,
        },
        "body": {
            "format": "markdown",
            "paragraphs": paragraphs,
            "word_count": word_count,
            "generation_path": "deterministic_fallback",
        },
        "provenance": {
            "source_tables": ["task_jobs", "photos", "ai_analysis_logs", "expression_artifacts", "fact_snapshots"],
            "fact_snapshot_id": snapshot.id,
            "assembler_version": OPERATIONS_HEALTH_SUMMARY_ASSEMBLER_VERSION,
            "redaction_profile": "operations_health_v1",
        },
        "validation": {
            "health_surface_locked": True,
            "ai_changed_health_surface": False,
            "shadow_only": True,
        },
    }


def render_operations_health_summary(payload: dict[str, Any]) -> str:
    company_id = _safe_text(payload.get("company_id"), limit=80) or "Company"
    parts = [f"# Operations Health Summary: {company_id}", f"Overall status: {payload.get('overall_status') or 'unknown'}"]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        text = _safe_text(paragraph, limit=500)
        if text:
            parts.append(f"- {text}")
    return "\n".join(parts).strip()


def _finance_section(
    *,
    section_id: str,
    title: str,
    lines: list[str],
    fact_refs: list[dict[str, Any]],
    metrics: dict[str, Any],
    flags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": section_id,
        "section_id": section_id,
        "title": title,
        "lines": lines,
        "fact_refs": fact_refs,
        "metrics": metrics,
        "flags": flags or [],
    }


def build_finance_summary_fallback(snapshot: FactSnapshot) -> dict[str, Any]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    metrics = facts.get("receipt_metrics") if isinstance(facts.get("receipt_metrics"), dict) else {}
    receipt_count = int(metrics.get("receipt_count") or 0)
    with_amount = int(metrics.get("receipts_with_amount") or 0)
    missing_amount = int(metrics.get("receipts_missing_amount") or 0)
    total_amount = metrics.get("total_amount") or 0.0
    gallons_total = metrics.get("fuel_gallons_total") or 0.0
    pump_true = int(metrics.get("has_pump_photo_true") or 0)
    pump_false = int(metrics.get("has_pump_photo_false") or 0)
    pump_unknown = int(metrics.get("has_pump_photo_unknown") or 0)
    mixed_currency = bool(metrics.get("mixed_currency"))
    sections: list[dict[str, Any]] = []
    amount_flags = []
    if missing_amount:
        amount_flags.append("missing_amount")
    if mixed_currency:
        amount_flags.append("mixed_currency")
    sections.append(
        _finance_section(
            section_id="receipt_amounts",
            title="Receipt Amounts",
            lines=[
                f"The window has {receipt_count} receipt facts, {with_amount} with total_amount, and total_amount={total_amount}."
            ],
            fact_refs=[
                _decision_fact_ref(snapshot, "receipt_metrics.receipt_count", receipt_count),
                _decision_fact_ref(snapshot, "receipt_metrics.receipts_with_amount", with_amount),
                _decision_fact_ref(snapshot, "receipt_metrics.total_amount", total_amount),
            ],
            metrics={
                "receipt_count": receipt_count,
                "receipts_with_amount": with_amount,
                "receipts_missing_amount": missing_amount,
                "total_amount": total_amount,
            },
            flags=amount_flags,
        )
    )
    sections.append(
        _finance_section(
            section_id="fuel_evidence",
            title="Fuel Evidence",
            lines=[
                (
                    f"Fuel receipt facts show fuel_gallons_total={gallons_total}, "
                    f"pump_photo_yes={pump_true}, pump_photo_no={pump_false}, and pump_photo_unknown={pump_unknown}."
                )
            ],
            fact_refs=[
                _decision_fact_ref(snapshot, "receipt_metrics.fuel_gallons_total", gallons_total),
                _decision_fact_ref(snapshot, "receipt_metrics.has_pump_photo_true", pump_true),
                _decision_fact_ref(snapshot, "receipt_metrics.has_pump_photo_false", pump_false),
                _decision_fact_ref(snapshot, "receipt_metrics.has_pump_photo_unknown", pump_unknown),
            ],
            metrics={
                "fuel_gallons_total": gallons_total,
                "has_pump_photo_true": pump_true,
                "has_pump_photo_false": pump_false,
                "has_pump_photo_unknown": pump_unknown,
            },
            flags=["missing_pump_photo"] if pump_false or pump_unknown else [],
        )
    )
    top_vendors = metrics.get("top_vendors") if isinstance(metrics.get("top_vendors"), list) else []
    vendor_lines = []
    for index, vendor in enumerate(top_vendors[:3]):
        if not isinstance(vendor, dict):
            continue
        vendor_lines.append(
            f"{vendor.get('vendor_label') or f'Vendor {index + 1}'} has {vendor.get('receipt_count') or 0} receipts and total_amount={vendor.get('total_amount') or 0.0}."
        )
    if not vendor_lines:
        vendor_lines = ["No vendor summary is available in the current receipt fact window."]
    sections.append(
        _finance_section(
            section_id="vendor_distribution",
            title="Vendor Distribution",
            lines=vendor_lines,
            fact_refs=[_decision_fact_ref(snapshot, "receipt_metrics.top_vendors", top_vendors)],
            metrics={"top_vendor_count": len(top_vendors)},
        )
    )
    data_gaps = []
    if missing_amount:
        data_gaps.append("receipts_missing_amount")
    if receipt_count == 0:
        data_gaps.append("no_receipt_facts")
    paragraphs = [line for section in sections for line in section["lines"][:3]]
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in paragraphs)
    return {
        "artifact_type": FINANCE_SUMMARY_ARTIFACT,
        "artifact_version": "1.0.0",
        "audience": "finance",
        "visibility": "shadow",
        "project_id": facts.get("scope", {}).get("project_id"),
        "company_id": facts.get("scope", {}).get("company_id"),
        "scope_type": FINANCE_SUMMARY_SCOPE,
        "as_of": facts.get("window", {}).get("current_end_utc"),
        "headline_metrics": {
            "receipt_count": receipt_count,
            "total_amount": total_amount,
            "receipts_missing_amount": missing_amount,
            "fuel_gallons_total": gallons_total,
        },
        "status_flags": sorted({flag for section in sections for flag in section.get("flags", [])}),
        "data_gaps": data_gaps,
        "fact_snapshot_ids": [snapshot.id],
        "finance_surface": {
            "computed_at": to_utc_iso(utc_now()),
            "sections": sections,
        },
        "body": {
            "format": "markdown",
            "paragraphs": paragraphs[:8],
            "word_count": word_count,
            "generation_path": "deterministic_fallback",
        },
        "provenance": {
            "source_tables": ["projects", "receipt_facts"],
            "fact_snapshot_id": snapshot.id,
            "assembler_version": FINANCE_SUMMARY_ASSEMBLER_VERSION,
            "redaction_profile": "finance_summary_v1",
        },
        "validation": {
            "finance_surface_locked": True,
            "ai_changed_finance_surface": False,
            "shadow_only": True,
        },
    }


def render_finance_summary(payload: dict[str, Any]) -> str:
    project_id = _safe_text(payload.get("project_id"), limit=80) or "Project"
    parts = [f"# Finance Summary: {project_id}"]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        text = _safe_text(paragraph, limit=500)
        if text:
            parts.append(f"- {text}")
    return "\n".join(parts).strip()


def _company_health_card(
    *,
    card_id: str,
    status: str,
    title: str,
    lines: list[str],
    fact_refs: list[dict[str, Any]],
    metrics: dict[str, Any],
    rule_flags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": card_id,
        "card_id": card_id,
        "status": status,
        "title": title,
        "lines": lines,
        "fact_refs": fact_refs,
        "metrics": metrics,
        "rule_flags": rule_flags or [],
    }


def build_executive_company_health_fallback(snapshot: FactSnapshot) -> dict[str, Any]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    projects = facts.get("project_portfolio") if isinstance(facts.get("project_portfolio"), dict) else {}
    photo_activity = facts.get("photo_activity") if isinstance(facts.get("photo_activity"), dict) else {}
    reports = facts.get("progress_reports") if isinstance(facts.get("progress_reports"), dict) else {}
    expressions = facts.get("expression_readiness") if isinstance(facts.get("expression_readiness"), dict) else {}
    finance = facts.get("finance_receipt_coverage") if isinstance(facts.get("finance_receipt_coverage"), dict) else {}

    project_total = int(projects.get("projects_total") or 0)
    active_projects = int((projects.get("status_counts") or {}).get("active", 0) or 0)
    photos_current = int(photo_activity.get("photos_current") or 0)
    active_photo_projects = int(photo_activity.get("active_project_count") or 0)
    active_days = int(photo_activity.get("active_day_count") or 0)
    client_visible_photos = int(photo_activity.get("client_visible_photos_current") or 0)
    reports_current = int(reports.get("reports_current") or 0)
    completed_reports = int(reports.get("completed_current") or 0)
    artifacts_current = int(expressions.get("artifacts_current") or 0)
    shadow_invalid = int(expressions.get("shadow_invalid_current") or 0)
    fallback_valid = int(expressions.get("shadow_fallback_valid_current") or 0)
    receipt_count = int(finance.get("receipt_count") or 0)
    projects_with_receipts = int(finance.get("projects_with_receipts") or 0)
    receipts_missing_amount = int(finance.get("receipts_missing_amount") or 0)

    cards: list[dict[str, Any]] = []
    portfolio_flags = []
    portfolio_status = "green"
    if project_total == 0:
        portfolio_status = "amber"
        portfolio_flags.append("no_projects_in_company")
    cards.append(
        _company_health_card(
            card_id="project_portfolio",
            status=portfolio_status,
            title="Project Portfolio",
            lines=[f"Project portfolio shows {project_total} projects and {active_projects} active projects."],
            fact_refs=[
                _decision_fact_ref(snapshot, "project_portfolio.projects_total", project_total),
                _decision_fact_ref(snapshot, "project_portfolio.status_counts.active", active_projects),
            ],
            metrics={"projects_total": project_total, "active_projects": active_projects},
            rule_flags=portfolio_flags,
        )
    )

    photo_flags = []
    photo_status = "green"
    if active_projects and active_photo_projects == 0:
        photo_status = "amber"
        photo_flags.append("no_recent_project_photo_activity")
    cards.append(
        _company_health_card(
            card_id="photo_activity",
            status=photo_status,
            title="Photo Activity",
            lines=[
                (
                    f"Photo activity shows {photos_current} project photos across {active_photo_projects} projects "
                    f"and {active_days} active days in the window."
                )
            ],
            fact_refs=[
                _decision_fact_ref(snapshot, "photo_activity.photos_current", photos_current),
                _decision_fact_ref(snapshot, "photo_activity.active_project_count", active_photo_projects),
                _decision_fact_ref(snapshot, "photo_activity.active_day_count", active_days),
            ],
            metrics={
                "photos_current": photos_current,
                "active_project_count": active_photo_projects,
                "active_day_count": active_days,
                "client_visible_photos_current": client_visible_photos,
            },
            rule_flags=photo_flags,
        )
    )

    report_flags = []
    report_status = "green"
    if reports_current and completed_reports == 0:
        report_status = "amber"
        report_flags.append("reports_without_completed_output")
    cards.append(
        _company_health_card(
            card_id="progress_reports",
            status=report_status,
            title="Progress Reports",
            lines=[f"Progress reports show {reports_current} report records and {completed_reports} completed reports in the window."],
            fact_refs=[
                _decision_fact_ref(snapshot, "progress_reports.reports_current", reports_current),
                _decision_fact_ref(snapshot, "progress_reports.completed_current", completed_reports),
            ],
            metrics={"reports_current": reports_current, "completed_current": completed_reports},
            rule_flags=report_flags,
        )
    )

    expression_status = "green"
    expression_flags = []
    if shadow_invalid > 0:
        expression_status = "red"
        expression_flags.append("shadow_invalid_present")
    elif artifacts_current and fallback_valid >= max(5, int(artifacts_current * 0.75)):
        expression_status = "amber"
        expression_flags.append("high_fallback_ratio")
    cards.append(
        _company_health_card(
            card_id="expression_readiness",
            status=expression_status,
            title="Expression Readiness",
            lines=[
                (
                    f"Expression readiness shows {artifacts_current} current artifacts, "
                    f"{shadow_invalid} invalid shadow artifacts, and {fallback_valid} fallback-valid artifacts."
                )
            ],
            fact_refs=[
                _decision_fact_ref(snapshot, "expression_readiness.artifacts_current", artifacts_current),
                _decision_fact_ref(snapshot, "expression_readiness.shadow_invalid_current", shadow_invalid),
                _decision_fact_ref(snapshot, "expression_readiness.shadow_fallback_valid_current", fallback_valid),
            ],
            metrics={
                "artifacts_current": artifacts_current,
                "shadow_invalid_current": shadow_invalid,
                "shadow_fallback_valid_current": fallback_valid,
            },
            rule_flags=expression_flags,
        )
    )

    finance_flags = []
    finance_status = "green"
    if receipt_count and receipts_missing_amount:
        finance_status = "amber"
        finance_flags.append("receipts_missing_amount")
    cards.append(
        _company_health_card(
            card_id="finance_receipt_coverage",
            status=finance_status,
            title="Finance Receipt Coverage",
            lines=[
                (
                    f"Finance receipt coverage shows {receipt_count} receipt facts across "
                    f"{projects_with_receipts} projects and {receipts_missing_amount} receipts missing total_amount."
                )
            ],
            fact_refs=[
                _decision_fact_ref(snapshot, "finance_receipt_coverage.receipt_count", receipt_count),
                _decision_fact_ref(snapshot, "finance_receipt_coverage.projects_with_receipts", projects_with_receipts),
                _decision_fact_ref(snapshot, "finance_receipt_coverage.receipts_missing_amount", receipts_missing_amount),
            ],
            metrics={
                "receipt_count": receipt_count,
                "projects_with_receipts": projects_with_receipts,
                "receipts_missing_amount": receipts_missing_amount,
            },
            rule_flags=finance_flags,
        )
    )

    overall_status = _derived_overall_health_status(cards)
    paragraphs = [line for card in cards for line in card["lines"][:1]]
    word_count = sum(len(re.findall(r"\w+", paragraph)) for paragraph in paragraphs)
    return {
        "artifact_type": EXECUTIVE_COMPANY_HEALTH_ARTIFACT,
        "artifact_version": "1.0.0",
        "audience": "executive",
        "visibility": "shadow",
        "company_id": facts.get("scope", {}).get("company_id"),
        "scope_type": EXECUTIVE_COMPANY_HEALTH_SCOPE,
        "as_of": facts.get("window", {}).get("current_end_utc"),
        "overall_status": overall_status,
        "headline_metrics": {
            "projects_total": project_total,
            "photos_current": photos_current,
            "completed_reports_current": completed_reports,
            "shadow_invalid_current": shadow_invalid,
            "receipt_count": receipt_count,
        },
        "fact_snapshot_ids": [snapshot.id],
        "company_health_surface": {
            "computed_at": to_utc_iso(utc_now()),
            "cards": cards,
        },
        "body": {
            "format": "markdown",
            "paragraphs": paragraphs,
            "word_count": word_count,
            "generation_path": "deterministic_fallback",
        },
        "provenance": {
            "source_tables": ["companies", "projects", "photos", "progress_reports", "expression_artifacts", "receipt_facts"],
            "fact_snapshot_id": snapshot.id,
            "assembler_version": EXECUTIVE_COMPANY_HEALTH_ASSEMBLER_VERSION,
            "redaction_profile": "executive_company_health_v1",
        },
        "validation": {
            "company_health_surface_locked": True,
            "ai_changed_company_health_surface": False,
            "shadow_only": True,
            "employee_ordering_disabled": True,
            "performance_scoring_disabled": True,
        },
    }


def render_executive_company_health(payload: dict[str, Any]) -> str:
    company_id = _safe_text(payload.get("company_id"), limit=80) or "Company"
    parts = [f"# Company Health Summary: {company_id}", f"Overall status: {payload.get('overall_status') or 'unknown'}"]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    paragraphs = body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else []
    for paragraph in paragraphs:
        text = _safe_text(paragraph, limit=500)
        if text:
            parts.append(f"- {text}")
    return "\n".join(parts).strip()


def generate_project_manager_decision_brief_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_project_manager_decision_brief_snapshot(
        db,
        company_id=company_id,
        project_id=project_id,
        window_days=window_days,
    )
    return run_project_manager_decision_brief_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_project_manager_decision_brief_fallback(snapshot),
    )


def generate_client_progress_summary_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_client_progress_summary_snapshot(
        db,
        company_id=company_id,
        project_id=project_id,
        window_days=window_days,
    )
    return run_expression_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        artifact_type=CLIENT_PROGRESS_SUMMARY_ARTIFACT,
        audience_id="client",
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_client_progress_summary_fallback(snapshot),
        render_payload=render_client_progress_summary,
    )


def generate_operations_health_summary_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    window_hours: int = 24,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_operations_health_summary_snapshot(
        db,
        company_id=company_id,
        window_hours=window_hours,
    )
    return run_expression_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        artifact_type=OPERATIONS_HEALTH_SUMMARY_ARTIFACT,
        audience_id="operations",
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_operations_health_summary_fallback(snapshot),
        render_payload=render_operations_health_summary,
    )


def generate_finance_summary_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    project_id: str,
    window_days: int = 30,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_finance_summary_snapshot(
        db,
        company_id=company_id,
        project_id=project_id,
        window_days=window_days,
    )
    return run_expression_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        artifact_type=FINANCE_SUMMARY_ARTIFACT,
        audience_id="finance",
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_finance_summary_fallback(snapshot),
        render_payload=render_finance_summary,
    )


def generate_executive_company_health_shadow(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    window_days: int = 30,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_executive_company_health_snapshot(
        db,
        company_id=company_id,
        window_days=window_days,
    )
    return run_expression_shadow(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        artifact_type=EXECUTIVE_COMPANY_HEALTH_ARTIFACT,
        audience_id="executive",
        language=language,
        preferred_model=preferred_model,
        use_ai=use_ai,
        fallback_payload=build_executive_company_health_fallback(snapshot),
        render_payload=render_executive_company_health,
    )


def build_progress_report_translation_fallback(facts: dict[str, Any]) -> dict[str, Any]:
    structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    return {
        language: dict(structured_report)
        for language in ("zh", "en", "es")
    }


def _copy_json_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _progress_translation_shell(structured_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {language: _copy_json_dict(structured_report) for language in ("zh", "en", "es")}


def _set_report_translation_value(
    payload: dict[str, dict[str, Any]],
    *,
    field_path: str,
    translations: dict[str, str],
) -> None:
    for language in ("zh", "en", "es"):
        parts = field_path.split(".")
        current: Any = payload[language]
        for part in parts[:-1]:
            if isinstance(current, list) and part.isdigit():
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current[part]
        last = parts[-1]
        if isinstance(current, list) and last.isdigit():
            current[int(last)] = translations.get(language, "")
        elif isinstance(current, dict):
            current[last] = translations.get(language, "")


def _is_translatable_report_text(text: str) -> bool:
    normalized = text.strip().casefold()
    if not normalized:
        return False
    if normalized in {"none", "null", "nil", "n/a", "na", "-", "--"}:
        return False
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%?", normalized):
        return False
    return True


def _progress_report_translatable_strings(structured_report: dict[str, Any]) -> list[tuple[str, str]]:
    scalar_fields = ("executive_summary", "manager_brief")
    list_fields = (
        "angle_bias_notes",
        "key_changes",
        "work_completed",
        "work_remaining",
        "safety_risks",
        "quality_risks",
        "material_inventory_signals",
        "water_housekeeping_signals",
        "uncertain_items",
        "evidence_limitations",
        "immediate_decisions",
        "recommended_actions",
    )
    result: list[tuple[str, str]] = []
    for field_name in scalar_fields:
        text = _safe_text(structured_report.get(field_name), limit=4000)
        if _is_translatable_report_text(text):
            result.append((field_name, text))
    for field_name in list_fields:
        items = structured_report.get(field_name) if isinstance(structured_report.get(field_name), list) else []
        for index, item in enumerate(items):
            text = _safe_text(item, limit=4000)
            if _is_translatable_report_text(text):
                result.append((f"{field_name}.{index}", text))
    timeline = (
        structured_report.get("timeline_observations")
        if isinstance(structured_report.get("timeline_observations"), list)
        else []
    )
    for index, item in enumerate(timeline):
        if not isinstance(item, dict):
            continue
        for field_name in ("observation", "progress_signal", "risk_signal"):
            text = _safe_text(item.get(field_name), limit=4000)
            if _is_translatable_report_text(text):
                result.append((f"timeline_observations.{index}.{field_name}", text))
    return result


def _call_expression_translation_chunk(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str,
    project_id: str | None,
    field_path: str,
    source_text: str,
    preferred_model: str | None,
    use_ai: bool,
) -> tuple[dict[str, str], bool, list[str], str, dict[str, Any]]:
    fallback = {"zh": source_text, "en": source_text, "es": source_text}
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    prompt_version = _resolve_prompt_version(
        db,
        company_id=company_id,
        project_id=project_id,
        template_id=PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT,
    )
    contract = _resolve_contract(
        db,
        artifact_type=PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT,
        audience_id=audience.id,
    )
    facts = {"field_label": field_path, "source_text": source_text}
    if not use_ai:
        return fallback, True, [], "deterministic_fallback", {"backend_id": "deterministic_fallback", "backend_type": "local"}

    errors: list[str] = []
    for backend in _expression_backend_candidates(
        db,
        app_settings=app_settings,
        company_id=company_id,
        preferred_model=preferred_model,
    )[:3]:
        prompt = _format_prompt(
            prompt_version,
            facts=facts,
            contract=contract,
            extra_vars={"field_label": field_path, "source_text": source_text},
        )
        for attempt in range(2):
            try:
                raw_model_output = (
                    _call_ollama_expression_backend(backend, prompt=prompt)
                    if backend.type == OLLAMA_TYPE
                    else call_text_backend(backend, prompt=prompt)
                )
                candidate = _extract_json_object(raw_model_output)
                candidate_errors = _validate_expression_payload(
                    db,
                    audience=audience,
                    contract=contract,
                    payload=candidate,
                    language="multi",
                    facts=facts,
                )
                if candidate_errors:
                    errors.extend(f"{field_path}:{error}" for error in candidate_errors)
                    prompt = "\n\n".join(
                        [
                            prompt,
                            "The previous JSON failed validation.",
                            f"Validation errors: {json.dumps(candidate_errors, ensure_ascii=False)}",
                            f"Previous output: {raw_model_output[:2000]}",
                            "Return corrected strict JSON only. Do not copy English into zh or es unless the source text is already Chinese or Spanish.",
                        ]
                    )
                    continue
                return (
                    {language: str(candidate.get(language) or "") for language in ("zh", "en", "es")},
                    False,
                    errors,
                    backend.model,
                    {
                        "backend_id": backend.id,
                        "backend_type": backend.type,
                        "model": backend.model,
                        "repair_attempts": attempt,
                    },
                )
            except Exception as exc:
                errors.append(f"{field_path}:backend:{backend.id}:attempt:{attempt + 1}:{exc}")
                break
    return fallback, True, errors, "deterministic_fallback", {"backend_id": "deterministic_fallback", "backend_type": "local"}


def _fallback_progress_report_string_translation(source_text: str) -> dict[str, str]:
    english = _safe_text(source_text, limit=1000) or "Translation source text was empty."
    return {
        "zh": "该字段的机器翻译待复核。",
        "en": english,
        "es": "La traducción de este campo está pendiente de revisión.",
    }


def create_or_get_progress_report_string_translation_snapshot(
    db: Session,
    *,
    report_id: str,
    field_path: str,
) -> FactSnapshot:
    report = db.get(ProgressReport, report_id)
    if report is None:
        raise ValueError(f"Progress report {report_id} was not found")
    report_facts = build_progress_report_translation_facts(db, report=report)
    structured_report = report_facts.get("structured_report") if isinstance(report_facts.get("structured_report"), dict) else {}
    source_text = _safe_text(_value_at_path(structured_report, field_path), limit=4000)
    if not _is_translatable_report_text(source_text):
        raise ValueError(f"Progress report field '{field_path}' is not translatable")
    scope_key = {
        "company_id": report.company_id,
        "project_id": report.project_id,
        "report_id": report.id,
        "field_label": field_path,
        "source_text_hash": _json_hash({"source_text": source_text}),
    }
    scope_key_hash = _json_hash(scope_key)
    existing = db.scalar(
        select(FactSnapshot).where(
            FactSnapshot.company_id == report.company_id,
            FactSnapshot.scope_type == PROGRESS_REPORT_STRING_TRANSLATION_SCOPE,
            FactSnapshot.scope_key_hash == scope_key_hash,
            FactSnapshot.assembler_version == PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION,
        )
    )
    if existing is not None:
        return existing
    facts = {
        "scope": {
            "company_id": report.company_id,
            "project_id": report.project_id,
            "report_id": report.id,
        },
        "field_label": field_path,
        "source_text": source_text,
        "guardrails": {
            "target_languages": ["zh", "en", "es"],
            "standalone_chunk_shadow": True,
            "not_user_visible": True,
        },
    }
    snapshot = FactSnapshot(
        id=str(uuid4()),
        tenant_id=report.tenant_id or report.company_id,
        company_id=report.company_id,
        employee_id=None,
        project_id=report.project_id,
        scope_type=PROGRESS_REPORT_STRING_TRANSLATION_SCOPE,
        scope_key_json=scope_key,
        scope_key_hash=scope_key_hash,
        assembler_version=PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION,
        source_manifest_json={"tables": ["progress_reports"], "report_id": report.id, "field_label": field_path},
        facts_json=facts,
        fact_count=_count_fact_leaves(facts),
        coverage_json={"has_source_text": bool(source_text), "source_text_length": len(source_text)},
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def generate_progress_report_string_translation_shadow(
    db: Session,
    *,
    app_settings: Settings,
    report_id: str,
    field_path: str,
    language: str = "multi",
    preferred_model: str | None = None,
    use_ai: bool = False,
) -> ExpressionRunResult:
    snapshot = create_or_get_progress_report_string_translation_snapshot(
        db,
        report_id=report_id,
        field_path=field_path,
    )
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    source_text = _safe_text(facts.get("source_text"), limit=4000)
    field_label = _safe_text(facts.get("field_label"), limit=500)
    if use_ai:
        payload, used_fallback, errors, model_used, backend_profile = _call_expression_translation_chunk(
            db,
            app_settings=app_settings,
            company_id=snapshot.company_id,
            project_id=snapshot.project_id,
            field_path=field_label,
            source_text=source_text,
            preferred_model=preferred_model,
            use_ai=True,
        )
    else:
        payload = _fallback_progress_report_string_translation(source_text)
        used_fallback = True
        errors = []
        model_used = "deterministic_fallback"
        backend_profile = {"backend_id": "deterministic_fallback", "backend_type": "local"}

    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    contract = _resolve_contract(
        db,
        artifact_type=PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT,
        audience_id=audience.id,
    )
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT,
    )
    final_validation_errors = _validate_expression_payload(
        db,
        audience=audience,
        contract=contract,
        payload=payload,
        language=language,
        facts=facts,
    )
    errors.extend(final_validation_errors)
    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    backend_profile = {
        **backend_profile,
        "strategy": "progress_report_string_translation_chunk",
        "field_label": field_label,
        "standalone_chunk_shadow": True,
    }
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=snapshot.scope_key_json,
        artifact_type=PROGRESS_REPORT_STRING_TRANSLATION_ARTIFACT,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=None,
        rendered_markdown=render_json_artifact(payload),
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json=backend_profile,
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)


def build_progress_report_translation_with_string_chunks(
    db: Session,
    *,
    app_settings: Settings,
    snapshot: FactSnapshot,
    preferred_model: str | None,
    use_ai: bool,
) -> tuple[dict[str, Any], bool, list[str], str, dict[str, Any]]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    payload = _progress_translation_shell(structured_report)
    used_fallback = False
    errors: list[str] = []
    model_used = "deterministic_fallback"
    backend_profile: dict[str, Any] = {"backend_id": "deterministic_fallback", "backend_type": "local"}
    translated_count = 0
    fallback_count = 0

    for field_path, source_text in _progress_report_translatable_strings(structured_report):
        translations, chunk_fallback, chunk_errors, chunk_model, chunk_backend = _call_expression_translation_chunk(
            db,
            app_settings=app_settings,
            company_id=snapshot.company_id,
            project_id=snapshot.project_id,
            field_path=field_path,
            source_text=source_text,
            preferred_model=preferred_model,
            use_ai=use_ai,
        )
        _set_report_translation_value(payload, field_path=field_path, translations=translations)
        used_fallback = used_fallback or chunk_fallback
        errors.extend(chunk_errors)
        if chunk_fallback:
            fallback_count += 1
        else:
            translated_count += 1
            model_used = chunk_model
            backend_profile = chunk_backend

    backend_profile = {
        **backend_profile,
        "strategy": "progress_report_string_chunks",
        "translated_chunks": translated_count,
        "fallback_chunks": fallback_count,
        "total_chunks": translated_count + fallback_count,
    }
    return payload, used_fallback, errors, model_used, backend_profile


def generate_progress_report_translation_shadow(
    db: Session,
    *,
    app_settings: Settings,
    report_id: str,
    language: str = "multi",
    preferred_model: str | None = None,
    use_ai: bool = True,
) -> ExpressionRunResult:
    snapshot = create_or_get_progress_report_translation_snapshot(db, report_id=report_id)
    payload, used_fallback, errors, model_used, backend_profile = build_progress_report_translation_with_string_chunks(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        preferred_model=preferred_model,
        use_ai=use_ai,
    )
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    contract = _resolve_contract(db, artifact_type=PROGRESS_REPORT_TRANSLATION_ARTIFACT, audience_id=audience.id)
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=PROGRESS_REPORT_TRANSLATION_ARTIFACT,
    )
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    final_validation_errors = _validate_expression_payload(
        db,
        audience=audience,
        contract=contract,
        payload=payload,
        language=language,
        facts=facts,
    )
    errors.extend(final_validation_errors)
    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=snapshot.scope_key_json,
        artifact_type=PROGRESS_REPORT_TRANSLATION_ARTIFACT,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=None,
        rendered_markdown=render_json_artifact(payload),
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json=backend_profile,
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)


def _source_ref_value(facts: dict[str, Any], source_ref: str) -> Any:
    return _value_at_path(facts, source_ref)


def _markdown_section_spec_for_key(facts: dict[str, Any], section_key: str) -> dict[str, Any]:
    normalized = str(section_key or "").strip()
    if normalized not in GENERATED_REPORT_MARKDOWN_SECTION_KEYS:
        allowed = ", ".join(sorted(GENERATED_REPORT_MARKDOWN_SECTION_KEYS))
        raise ValueError(f"Unknown markdown section key '{section_key}'. Expected one of: {allowed}")
    for spec in _markdown_section_specs(facts):
        if spec.get("key") == normalized:
            return spec
    raise ValueError(f"Markdown section '{normalized}' has no spec for this report snapshot")


def _markdown_section_validation_facts(facts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    source_refs = [ref for ref in spec.get("source_refs", []) if isinstance(ref, str)]
    section_facts = {
        "section_key": spec.get("key"),
        "heading": spec.get("heading"),
        "allowed_source_refs": source_refs,
        "source_values": {ref: _source_ref_value(facts, ref) for ref in source_refs},
    }
    return {**facts, **section_facts}


def _markdown_section_specs(facts: dict[str, Any]) -> list[dict[str, Any]]:
    structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    timeline = structured_report.get("timeline_observations") if isinstance(structured_report.get("timeline_observations"), list) else []
    timeline_refs: list[str] = []
    for index, item in enumerate(timeline[:6]):
        if not isinstance(item, dict):
            continue
        for key in ("observation", "progress_signal", "risk_signal"):
            if _safe_text(item.get(key), limit=4000):
                timeline_refs.append(f"structured_report.timeline_observations.{index}.{key}")

    return [
        {
            "key": "summary",
            "heading": "Progress Summary",
            "source_refs": [
                ref
                for ref in [
                    "structured_report.executive_summary",
                    "structured_report.manager_brief",
                    "structured_report.overall_progress_percent",
                    "structured_report.overall_status",
                    "structured_report.confidence_level",
                ]
                if _source_ref_value(facts, ref) not in (None, "", [])
            ],
        },
        {
            "key": "completed_work",
            "heading": "Completed Work",
            "source_refs": [
                ref
                for ref in [
                    *[f"structured_report.key_changes.{index}" for index in range(8)],
                    *[f"structured_report.work_completed.{index}" for index in range(8)],
                ]
                if _source_ref_value(facts, ref) not in (None, "", [])
            ],
        },
        {
            "key": "risks",
            "heading": "Risks And Limits",
            "source_refs": [
                ref
                for ref in [
                    *[f"structured_report.safety_risks.{index}" for index in range(10)],
                    *[f"structured_report.quality_risks.{index}" for index in range(10)],
                    *[f"structured_report.uncertain_items.{index}" for index in range(10)],
                    *[f"structured_report.evidence_limitations.{index}" for index in range(10)],
                ]
                if _source_ref_value(facts, ref) not in (None, "", [])
            ],
        },
        {
            "key": "next_steps",
            "heading": "Next Steps",
            "source_refs": [
                ref
                for ref in [
                    *[f"structured_report.work_remaining.{index}" for index in range(8)],
                    *[f"structured_report.immediate_decisions.{index}" for index in range(10)],
                    *[f"structured_report.recommended_actions.{index}" for index in range(10)],
                ]
                if _source_ref_value(facts, ref) not in (None, "", [])
            ],
        },
        {
            "key": "timeline",
            "heading": "Evidence Timeline",
            "source_refs": timeline_refs,
        },
    ]


def _fallback_markdown_section(facts: dict[str, Any], *, spec: dict[str, Any]) -> dict[str, Any]:
    source_refs = [ref for ref in spec.get("source_refs", []) if isinstance(ref, str)]
    lines: list[str] = []
    for ref in source_refs[:8]:
        value = _source_ref_value(facts, ref)
        if isinstance(value, (int, float)):
            lines.append(f"- {ref.rsplit('.', 1)[-1]}: {value}")
        else:
            text = _safe_text(value, limit=500)
            if text:
                lines.append(f"- {text}")
    if not lines:
        lines.append("- No database-backed detail is available for this section.")
    return {
        "key": str(spec.get("key") or "section"),
        "heading": str(spec.get("heading") or "Section"),
        "markdown": "\n".join(lines),
        "source_refs": source_refs[:8] or ["structured_report.executive_summary"],
        "generation_mode": "deterministic_fallback",
    }


def _call_generated_markdown_section(
    db: Session,
    *,
    app_settings: Settings,
    snapshot: FactSnapshot,
    spec: dict[str, Any],
    preferred_model: str | None,
    use_ai: bool,
) -> tuple[dict[str, Any], bool, list[str], str, dict[str, Any]]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    fallback = _fallback_markdown_section(facts, spec=spec)
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT,
    )
    contract = _resolve_contract(db, artifact_type=GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT, audience_id=audience.id)
    source_refs = [ref for ref in spec.get("source_refs", []) if isinstance(ref, str)]
    validation_facts = _markdown_section_validation_facts(facts, spec)
    if not use_ai or not source_refs:
        return fallback, True, [], "deterministic_fallback", {"backend_id": "deterministic_fallback", "backend_type": "local"}

    errors: list[str] = []
    for backend in _expression_backend_candidates(
        db,
        app_settings=app_settings,
        company_id=snapshot.company_id,
        preferred_model=preferred_model,
    )[:3]:
        prompt = _format_prompt(
            prompt_version,
            facts=validation_facts,
            contract=contract,
            extra_vars={
                "section_key": spec.get("key"),
                "heading": spec.get("heading"),
                "allowed_source_refs_json": json.dumps(source_refs, ensure_ascii=False),
            },
        )
        for attempt in range(2):
            try:
                raw_model_output = (
                    _call_ollama_expression_backend(backend, prompt=prompt)
                    if backend.type == OLLAMA_TYPE
                    else call_text_backend(backend, prompt=prompt)
                )
                candidate = _extract_json_object(raw_model_output)
                candidate["key"] = str(spec.get("key") or candidate.get("key") or "section")
                candidate["heading"] = str(spec.get("heading") or candidate.get("heading") or "Section")
                candidate_errors = _validate_expression_payload(
                    db,
                    audience=audience,
                    contract=contract,
                    payload=candidate,
                    language="en",
                    facts=validation_facts,
                )
                if candidate_errors:
                    errors.extend(f"{spec.get('key')}:{error}" for error in candidate_errors)
                    prompt = "\n\n".join(
                        [
                            prompt,
                            "The previous JSON failed validation.",
                            f"Validation errors: {json.dumps(candidate_errors, ensure_ascii=False)}",
                            f"Previous output: {raw_model_output[:2000]}",
                            "Return corrected strict JSON only. Use only allowed_source_refs.",
                        ]
                    )
                    continue
                candidate["generation_mode"] = "ai"
                return (
                    candidate,
                    False,
                    errors,
                    backend.model,
                    {
                        "backend_id": backend.id,
                        "backend_type": backend.type,
                        "model": backend.model,
                        "repair_attempts": attempt,
                    },
                )
            except Exception as exc:
                errors.append(f"{spec.get('key')}:backend:{backend.id}:attempt:{attempt + 1}:{exc}")
                break
    return fallback, True, errors, "deterministic_fallback", {"backend_id": "deterministic_fallback", "backend_type": "local"}


def render_generated_report_markdown_section(payload: dict[str, Any]) -> str:
    heading = _safe_text(payload.get("heading"), limit=120)
    markdown = str(payload.get("markdown") or "").strip()
    if heading and markdown:
        return f"## {heading}\n{markdown}"
    return markdown or render_json_artifact(payload)


def render_generated_report_markdown(payload: dict[str, Any]) -> str:
    title = _safe_text(payload.get("title"), limit=200) or "Progress Report"
    parts = [f"# {title}"]
    summary = _safe_text(payload.get("summary"), limit=800)
    if summary:
        parts.append(summary)
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    metric_lines = []
    for key in ("overall_progress_percent", "overall_status", "confidence_level"):
        if metrics.get(key) is not None:
            metric_lines.append(f"- {key}: {metrics.get(key)}")
    if metric_lines:
        parts.append("## Metrics\n" + "\n".join(metric_lines))
    sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = _safe_text(section.get("heading"), limit=120)
        markdown = str(section.get("markdown") or "").strip()
        if heading and markdown:
            parts.append(f"## {heading}\n{markdown}")
    return "\n\n".join(parts).strip()


def build_generated_report_markdown_with_sections(
    db: Session,
    *,
    app_settings: Settings,
    snapshot: FactSnapshot,
    preferred_model: str | None,
    use_ai: bool,
) -> tuple[dict[str, Any], bool, list[str], str, dict[str, Any]]:
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    structured_report = facts.get("structured_report") if isinstance(facts.get("structured_report"), dict) else {}
    sections: list[dict[str, Any]] = []
    used_fallback = False
    errors: list[str] = []
    model_used = "deterministic_fallback"
    backend_profile: dict[str, Any] = {"backend_id": "deterministic_fallback", "backend_type": "local"}
    ai_sections = 0
    fallback_sections = 0
    for spec in _markdown_section_specs(facts):
        section, section_fallback, section_errors, section_model, section_backend = _call_generated_markdown_section(
            db,
            app_settings=app_settings,
            snapshot=snapshot,
            spec=spec,
            preferred_model=preferred_model,
            use_ai=use_ai,
        )
        sections.append(section)
        used_fallback = used_fallback or section_fallback
        errors.extend(section_errors)
        if section_fallback:
            fallback_sections += 1
        else:
            ai_sections += 1
            model_used = section_model
            backend_profile = section_backend
    payload = {
        "title": f"Progress Report {facts.get('scope', {}).get('report_id', '')}".strip(),
        "summary": _safe_text(structured_report.get("executive_summary"), limit=1000),
        "metrics": {
            "overall_progress_percent": structured_report.get("overall_progress_percent"),
            "overall_status": structured_report.get("overall_status"),
            "confidence_level": structured_report.get("confidence_level"),
        },
        "sections": sections,
        "source_artifacts": facts.get("source_artifacts") if isinstance(facts.get("source_artifacts"), dict) else {},
        "disclaimer": "Shadow-only generated markdown. Do not show to users until promoted.",
    }
    backend_profile = {
        **backend_profile,
        "strategy": "generated_report_markdown_sections",
        "ai_sections": ai_sections,
        "fallback_sections": fallback_sections,
        "total_sections": ai_sections + fallback_sections,
    }
    return payload, used_fallback, errors, model_used, backend_profile


def generate_report_markdown_shadow(
    db: Session,
    *,
    app_settings: Settings,
    report_id: str,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = True,
) -> ExpressionRunResult:
    snapshot = create_or_get_generated_report_markdown_snapshot(db, report_id=report_id)
    payload, used_fallback, errors, model_used, backend_profile = build_generated_report_markdown_with_sections(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        preferred_model=preferred_model,
        use_ai=use_ai,
    )
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    contract = _resolve_contract(db, artifact_type=GENERATED_REPORT_MARKDOWN_ARTIFACT, audience_id=audience.id)
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=GENERATED_REPORT_MARKDOWN_ARTIFACT,
    )
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    final_validation_errors = _validate_expression_payload(
        db,
        audience=audience,
        contract=contract,
        payload=payload,
        language=language,
        facts=facts,
    )
    errors.extend(final_validation_errors)
    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=snapshot.scope_key_json,
        artifact_type=GENERATED_REPORT_MARKDOWN_ARTIFACT,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=None,
        rendered_markdown=render_generated_report_markdown(payload),
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json=backend_profile,
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)


def generate_report_markdown_section_shadow(
    db: Session,
    *,
    app_settings: Settings,
    report_id: str,
    section_key: str,
    language: str = "en",
    preferred_model: str | None = None,
    use_ai: bool = True,
) -> ExpressionRunResult:
    snapshot = create_or_get_generated_report_markdown_snapshot(db, report_id=report_id)
    facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
    spec = _markdown_section_spec_for_key(facts, section_key)
    payload, used_fallback, errors, model_used, backend_profile = _call_generated_markdown_section(
        db,
        app_settings=app_settings,
        snapshot=snapshot,
        spec=spec,
        preferred_model=preferred_model,
        use_ai=use_ai,
    )
    audience = db.get(ExpressionAudience, "project_manager")
    if audience is None:
        raise ValueError("Expression audience 'project_manager' is not seeded")
    contract = _resolve_contract(db, artifact_type=GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT, audience_id=audience.id)
    prompt_version = _resolve_prompt_version(
        db,
        company_id=snapshot.company_id,
        project_id=snapshot.project_id,
        template_id=GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT,
    )
    validation_facts = _markdown_section_validation_facts(facts, spec)
    final_validation_errors = _validate_expression_payload(
        db,
        audience=audience,
        contract=contract,
        payload=payload,
        language=language,
        facts=validation_facts,
    )
    errors.extend(final_validation_errors)
    validation_status = (
        "shadow_fallback_valid"
        if used_fallback and not final_validation_errors
        else "shadow_valid"
        if not errors
        else "shadow_invalid"
    )
    scope_key_json = dict(snapshot.scope_key_json) if isinstance(snapshot.scope_key_json, dict) else {}
    scope_key_json["section_key"] = str(spec.get("key") or section_key)
    artifact = ExpressionArtifact(
        id=str(uuid4()),
        tenant_id=snapshot.tenant_id or snapshot.company_id,
        company_id=snapshot.company_id,
        fact_snapshot_id=snapshot.id,
        prompt_version_id=prompt_version.id,
        contract_id=contract.id,
        scope_type=snapshot.scope_type,
        scope_key_json=scope_key_json,
        artifact_type=GENERATED_REPORT_MARKDOWN_SECTION_ARTIFACT,
        audience_id=audience.id,
        language=language,
        structured_json=payload,
        raw_model_output=None,
        rendered_markdown=render_generated_report_markdown_section(payload),
        validation_status=validation_status,
        validation_errors_json=errors,
        model_used=model_used,
        backend_profile_json={**backend_profile, "section_key": spec.get("key")},
        promoted=False,
    )
    db.add(artifact)
    db.flush()
    return ExpressionRunResult(snapshot=snapshot, artifact=artifact, used_fallback=used_fallback, errors=errors)
