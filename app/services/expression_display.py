from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import to_utc_iso, utc_now
from app.models import Employee, ExpressionArtifact, FactSnapshot, Photo, PhotoType, ProgressReport, ProgressReportStatus, Project


EMPLOYEE_CONTRIBUTION_ARTIFACT = "employee_contribution_narrative"
PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT = "project_manager_decision_brief"
EMPLOYEE_CONTRIBUTION_SCOPE = "employee_project_window"
PROJECT_MANAGER_DECISION_BRIEF_SCOPES = ("project_period", "project_window")
PROMOTED_VISIBLE_STATUS = "promoted_valid"
DISPLAY_CONTRACT_VERSION = "mobile_contribution_dashboard:v1"
PROJECT_MANAGER_STATUS_CARD_CONTRACT_VERSION = "project_manager_status_card:v1"
FUTURE_AI_SLOT_IDS = ("2", "3", "4", "5", "6")


def _artifact_metadata(artifact: ExpressionArtifact, snapshot: FactSnapshot | None = None) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "audience_id": artifact.audience_id,
        "fact_snapshot_id": artifact.fact_snapshot_id,
        "prompt_version_id": artifact.prompt_version_id,
        "contract_id": artifact.contract_id,
        "validation_status": artifact.validation_status,
        "promoted": artifact.promoted,
        "model_used": artifact.model_used,
        "created_at": to_utc_iso(artifact.created_at),
        "snapshot_created_at": to_utc_iso(snapshot.created_at) if snapshot is not None else None,
    }


def _dict_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _employee_contribution_payload(artifact: ExpressionArtifact) -> dict[str, Any]:
    payload = _dict_payload(artifact.structured_json)
    contribution_explanation = payload.get("contribution_explanation")
    strengths = payload.get("strengths")
    suggestions = payload.get("suggestions")
    recent_highlights = payload.get("recent_highlights")
    response = dict(payload)
    response.setdefault("summary", payload.get("summary_line"))
    response.setdefault("comparison", payload.get("comparison_text"))
    response.setdefault("highlights", recent_highlights if isinstance(recent_highlights, list) else [])
    response.setdefault(
        "sections",
        {
            "contribution_explanation": contribution_explanation if isinstance(contribution_explanation, list) else [],
            "strengths": strengths if isinstance(strengths, list) else [],
            "suggestions": suggestions if isinstance(suggestions, list) else [],
        },
    )
    return response


def _latest_promoted_employee_contribution(
    db: Session,
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int,
) -> tuple[ExpressionArtifact, FactSnapshot] | None:
    rows = (
        db.execute(
            select(ExpressionArtifact, FactSnapshot)
            .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id)
            .where(
                ExpressionArtifact.company_id == company_id,
                ExpressionArtifact.artifact_type == EMPLOYEE_CONTRIBUTION_ARTIFACT,
                ExpressionArtifact.audience_id == "employee",
                ExpressionArtifact.promoted.is_(True),
                ExpressionArtifact.validation_status == PROMOTED_VISIBLE_STATUS,
                FactSnapshot.company_id == company_id,
                FactSnapshot.employee_id == employee_id,
                FactSnapshot.project_id == project_id,
                FactSnapshot.scope_type == EMPLOYEE_CONTRIBUTION_SCOPE,
            )
            .order_by(ExpressionArtifact.created_at.desc())
            .limit(20)
        )
        .tuples()
        .all()
    )
    for artifact, snapshot in rows:
        scope_key = snapshot.scope_key_json if isinstance(snapshot.scope_key_json, dict) else {}
        if int(scope_key.get("window_days") or 0) == window_days:
            return artifact, snapshot
    return None


def _latest_promoted_project_manager_status_card(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int,
) -> tuple[ExpressionArtifact, FactSnapshot] | None:
    rows = (
        db.execute(
            select(ExpressionArtifact, FactSnapshot)
            .join(FactSnapshot, FactSnapshot.id == ExpressionArtifact.fact_snapshot_id)
            .where(
                ExpressionArtifact.company_id == company_id,
                ExpressionArtifact.artifact_type == PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
                ExpressionArtifact.audience_id == "project_manager",
                ExpressionArtifact.promoted.is_(True),
                ExpressionArtifact.validation_status == PROMOTED_VISIBLE_STATUS,
                FactSnapshot.company_id == company_id,
                FactSnapshot.project_id == project_id,
                FactSnapshot.scope_type.in_(PROJECT_MANAGER_DECISION_BRIEF_SCOPES),
            )
            .order_by(ExpressionArtifact.created_at.desc())
            .limit(20)
        )
        .tuples()
        .all()
    )
    for artifact, snapshot in rows:
        scope_key = snapshot.scope_key_json if isinstance(snapshot.scope_key_json, dict) else {}
        if int(scope_key.get("window_days") or 0) == window_days:
            return artifact, snapshot
    return None


def _project_manager_status_card_payload(artifact: ExpressionArtifact) -> dict[str, Any]:
    payload = _dict_payload(artifact.structured_json)
    decision_surface = _dict_payload(payload.get("decision_surface"))
    body = _dict_payload(payload.get("body"))
    provenance = _dict_payload(payload.get("provenance"))
    return {
        "artifact_type": PROJECT_MANAGER_DECISION_BRIEF_ARTIFACT,
        "artifact_version": payload.get("artifact_version"),
        "audience": "project_manager",
        "visibility": "promoted",
        "project_id": payload.get("project_id"),
        "as_of": payload.get("as_of"),
        "decision_surface": {
            "computed_at": decision_surface.get("computed_at"),
            "items": decision_surface.get("items") if isinstance(decision_surface.get("items"), list) else [],
        },
        "body": {
            "format": body.get("format"),
            "paragraphs": body.get("paragraphs") if isinstance(body.get("paragraphs"), list) else [],
            "word_count": body.get("word_count"),
            "generation_path": body.get("generation_path"),
        },
        "provenance": {
            "source_tables": provenance.get("source_tables") if isinstance(provenance.get("source_tables"), list) else [],
            "fact_snapshot_id": provenance.get("fact_snapshot_id"),
            "assembler_version": provenance.get("assembler_version"),
        },
    }


def _photo_metric_summary(
    db: Session,
    *,
    company_id: str,
    employee_id: str,
    project_id: str,
    window_days: int,
) -> dict[str, Any]:
    now = utc_now()
    current_start = now - timedelta(days=window_days)
    previous_start = current_start - timedelta(days=window_days)

    def count_between(start: Any, end: Any) -> int:
        return int(
            db.scalar(
                select(func.count(Photo.id)).where(
                    Photo.company_id == company_id,
                    Photo.employee_id == employee_id,
                    Photo.project_id == project_id,
                    Photo.photo_type == PhotoType.project,
                    Photo.deleted.is_(False),
                    Photo.captured_at_utc >= start,
                    Photo.captured_at_utc <= end,
                )
            )
            or 0
        )

    current_count = count_between(current_start, now)
    previous_count = count_between(previous_start, current_start)
    completed_ai = int(
        db.scalar(
            select(func.count(Photo.id)).where(
                Photo.company_id == company_id,
                Photo.employee_id == employee_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= current_start,
                Photo.captured_at_utc <= now,
                Photo.labeling_status == "completed",
            )
        )
        or 0
    )
    active_days = int(
        db.scalar(
            select(func.count(func.distinct(func.date(Photo.captured_at_utc)))).where(
                Photo.company_id == company_id,
                Photo.employee_id == employee_id,
                Photo.project_id == project_id,
                Photo.photo_type == PhotoType.project,
                Photo.deleted.is_(False),
                Photo.captured_at_utc >= current_start,
                Photo.captured_at_utc <= now,
            )
        )
        or 0
    )
    return {
        "window_days": window_days,
        "current_start_utc": to_utc_iso(current_start),
        "current_end_utc": to_utc_iso(now),
        "photos_current": current_count,
        "photos_previous": previous_count,
        "photo_delta": current_count - previous_count,
        "active_days_current": active_days,
        "completed_ai_current": completed_ai,
        "ai_completion_ratio": round(completed_ai / current_count, 4) if current_count else None,
    }


def _project_report_summary(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    rows = db.execute(
        select(ProgressReport.status, func.count(ProgressReport.id))
        .where(ProgressReport.company_id == company_id, ProgressReport.project_id == project_id)
        .group_by(ProgressReport.status)
    ).all()
    counts = {
        (status.value if hasattr(status, "value") else str(status)): int(count)
        for status, count in rows
    }
    latest_completed = db.scalar(
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
        "status_counts": counts,
        "completed_with_content": completed_with_content,
        "latest_completed_report_id": latest_completed.id if latest_completed is not None else None,
        "latest_completed_at": to_utc_iso(latest_completed.completed_at) if latest_completed is not None else None,
    }


def _project_artifact_shadow_summary(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    artifact_type: str,
) -> dict[str, Any]:
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
    counts: dict[str, int] = {}
    promoted_count = 0
    latest_created_at = None
    for artifact in rows:
        counts[artifact.validation_status] = counts.get(artifact.validation_status, 0) + 1
        if artifact.promoted:
            promoted_count += 1
        latest_created_at = latest_created_at or artifact.created_at
    return {
        "artifact_type": artifact_type,
        "total_recent_artifacts": len(rows),
        "validation_status_counts": counts,
        "promoted_count": promoted_count,
        "latest_created_at": to_utc_iso(latest_created_at),
    }


def _future_ai_slots(db: Session, *, company_id: str, project_id: str) -> dict[str, Any]:
    slot_artifacts = {
        "2": "progress_report_translation",
        "3": "generated_report_markdown",
        "4": "project_manager_decision_brief",
        "5": "client_progress_summary",
        "6": "executive_project_brief",
    }
    return {
        slot_id: {
            "slot_id": slot_id,
            "artifact_type": artifact_type,
            "status": "not_promoted",
            "content": None,
            "reason": "awaiting_shadow_promotion",
            "shadow_summary": _project_artifact_shadow_summary(
                db,
                company_id=company_id,
                project_id=project_id,
                artifact_type=artifact_type,
            ),
        }
        for slot_id, artifact_type in slot_artifacts.items()
    }


def build_mobile_contribution_dashboard(
    db: Session,
    *,
    employee: Employee,
    project: Project,
    window_days: int,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    metrics = _photo_metric_summary(
        db,
        company_id=employee.company_id,
        employee_id=employee.employee_id,
        project_id=project.project_id,
        window_days=window_days,
    )
    contribution = _latest_promoted_employee_contribution(
        db,
        company_id=employee.company_id,
        employee_id=employee.employee_id,
        project_id=project.project_id,
        window_days=window_days,
    )
    modules: list[dict[str, Any]] = [
        {
            "module_id": "deterministic_metrics",
            "status": "ready",
            "source": "db_metric",
            "content": metrics,
            "provenance": {"tables": ["photos"]},
        }
    ]
    if contribution is None:
        modules.append(
            {
                "module_id": "employee_contribution",
                "status": "unavailable",
                "source": "promoted_artifact",
                "content": None,
                "provenance": None,
                "reason": "no_promoted_employee_contribution_artifact",
            }
        )
    else:
        artifact, snapshot = contribution
        modules.append(
            {
                "module_id": "employee_contribution",
                "status": "ready",
                "source": "promoted_artifact",
                "content": _employee_contribution_payload(artifact),
                "rendered_markdown": artifact.rendered_markdown,
                "provenance": _artifact_metadata(artifact, snapshot),
            }
        )

    modules.append(
        {
            "module_id": "project_management_status",
            "status": "partial",
            "source": "db_metric",
            "content": {
                "reports": _project_report_summary(db, company_id=employee.company_id, project_id=project.project_id),
                "ai_slots_ready": 0,
                "ai_slots_total": len(FUTURE_AI_SLOT_IDS),
            },
            "provenance": {"tables": ["progress_reports", "expression_artifacts"]},
        }
    )

    return {
        "contract_version": DISPLAY_CONTRACT_VERSION,
        "status": "partial" if contribution is None else "ready",
        "employee": {"employee_id": employee.employee_id, "name": employee.name, "role": employee.role},
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
        },
        "window": {
            "days": window_days,
            "current_start_utc": metrics["current_start_utc"],
            "current_end_utc": metrics["current_end_utc"],
        },
        "modules": modules,
        "ai_slots": _future_ai_slots(db, company_id=employee.company_id, project_id=project.project_id),
        "guardrails": {
            "uses_promoted_artifacts_only": True,
            "exposes_shadow_content": False,
            "client_should_not_compute_metrics": True,
        },
    }


def build_project_manager_status_card(
    db: Session,
    *,
    project: Project,
    window_days: int,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    resolved = _latest_promoted_project_manager_status_card(
        db,
        company_id=project.company_id,
        project_id=project.project_id,
        window_days=window_days,
    )
    base_payload: dict[str, Any] = {
        "contract_version": PROJECT_MANAGER_STATUS_CARD_CONTRACT_VERSION,
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
        },
        "window": {"days": window_days},
        "guardrails": {
            "uses_promoted_artifacts_only": True,
            "exposes_shadow_content": False,
            "client_should_not_compute_metrics": True,
            "model_output_included": False,
        },
    }
    if resolved is None:
        return {
            **base_payload,
            "status": "not_ready",
            "status_card": None,
            "rendered_markdown": None,
            "artifact": None,
            "reason": "no_promoted_project_manager_status_card",
        }

    artifact, snapshot = resolved
    return {
        **base_payload,
        "status": "available",
        "window": {
            "days": window_days,
            "snapshot_created_at": to_utc_iso(snapshot.created_at),
        },
        "status_card": _project_manager_status_card_payload(artifact),
        "rendered_markdown": artifact.rendered_markdown,
        "artifact": _artifact_metadata(artifact, snapshot),
    }
