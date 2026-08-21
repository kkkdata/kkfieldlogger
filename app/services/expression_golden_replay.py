from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models import (
    ExpressionArtifact,
    ExpressionAudience,
    ExpressionEvalItem,
    ExpressionEvalRun,
    ExpressionOutputContract,
    FactSnapshot,
)
from app.services.expression import (
    CLIENT_PROGRESS_SUMMARY_ARTIFACT,
    EXECUTIVE_COMPANY_HEALTH_ARTIFACT,
    FINANCE_SUMMARY_ARTIFACT,
    OPERATIONS_HEALTH_SUMMARY_ARTIFACT,
    PROMOTABLE_EXPRESSION_STATUSES,
    _SURFACE_SUMMARY_ARTIFACT_TYPES,
    _body_generation_path,
    _validate_expression_payload,
    build_client_progress_summary_fallback,
    build_executive_company_health_fallback,
    build_finance_summary_fallback,
    build_operations_health_summary_fallback,
)

GOLDEN_REPLAY_SCHEMA_VERSION = "golden_replay_v1"
GOLDEN_REPLAY_RUNNER_VERSION = "golden_replay_v1"
GOLDEN_REPLAY_VALIDATOR_NAME = "expression_payload"

_REPLAYABLE_VALIDATION_STATUSES = frozenset(
    {"shadow_valid", "shadow_invalid", "shadow_fallback_valid", "promoted_valid"}
)

_LOCKED_BASELINE_BUILDERS = {
    CLIENT_PROGRESS_SUMMARY_ARTIFACT: build_client_progress_summary_fallback,
    OPERATIONS_HEALTH_SUMMARY_ARTIFACT: build_operations_health_summary_fallback,
    FINANCE_SUMMARY_ARTIFACT: build_finance_summary_fallback,
    EXECUTIVE_COMPANY_HEALTH_ARTIFACT: build_executive_company_health_fallback,
}


def _artifact_validator_pass(validation_status: str) -> bool:
    return validation_status in PROMOTABLE_EXPRESSION_STATUSES


def _replay_validator_pass(errors: list[str]) -> bool:
    return not errors


def _empty_coverage_counts() -> dict[str, int]:
    return {"cases": 0, "passed": 0, "failed": 0, "skipped": 0}


def _bump_coverage(
    artifact_type_counts: dict[str, dict[str, int]],
    *,
    artifact_type: str,
    status: str,
) -> None:
    counts = artifact_type_counts.setdefault(artifact_type, _empty_coverage_counts())
    counts["cases"] += 1
    if status in {"passed", "failed", "skipped"}:
        counts[status] += 1


def _golden_case_key(artifact: ExpressionArtifact) -> str:
    scope = artifact.scope_key_json if isinstance(artifact.scope_key_json, dict) else {}
    scope_token = (
        scope.get("project_id")
        or scope.get("report_id")
        or scope.get("employee_id")
        or scope.get("company_id")
        or artifact.company_id
    )
    return f"{artifact.artifact_type}:{scope_token}:{artifact.id[:8]}"


def _locked_baseline_for_replay(
    *,
    artifact_type: str,
    snapshot: FactSnapshot,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if artifact_type not in _SURFACE_SUMMARY_ARTIFACT_TYPES:
        return None
    if _body_generation_path(payload) != "ai_polish":
        return None
    builder = _LOCKED_BASELINE_BUILDERS.get(artifact_type)
    if builder is None:
        return None
    return builder(snapshot)


def _resolve_replay_contract(db: Session, artifact: ExpressionArtifact) -> ExpressionOutputContract | None:
    if artifact.contract_id:
        contract = db.get(ExpressionOutputContract, artifact.contract_id)
        if contract is not None:
            return contract
    contract = db.scalar(
        select(ExpressionOutputContract)
        .where(
            ExpressionOutputContract.artifact_type == artifact.artifact_type,
            ExpressionOutputContract.audience_id == artifact.audience_id,
            ExpressionOutputContract.is_active.is_(True),
        )
        .order_by(ExpressionOutputContract.created_at.desc())
    )
    return contract


def _select_shadow_artifacts(
    db: Session,
    *,
    company_id: str | None,
    artifact_type: str | None,
    limit: int,
) -> list[ExpressionArtifact]:
    stmt = (
        select(ExpressionArtifact)
        .where(
            ExpressionArtifact.validation_status.in_(_REPLAYABLE_VALIDATION_STATUSES),
            or_(
                ExpressionArtifact.promoted.is_(False),
                ExpressionArtifact.validation_status == "promoted_valid",
            ),
        )
        .order_by(ExpressionArtifact.created_at.desc(), ExpressionArtifact.id.desc())
        .limit(limit)
    )
    if company_id:
        stmt = stmt.where(ExpressionArtifact.company_id == company_id)
    if artifact_type:
        stmt = stmt.where(ExpressionArtifact.artifact_type == artifact_type)
    return list(db.scalars(stmt))


def run_expression_golden_replay(
    db: Session,
    *,
    company_id: str | None = None,
    artifact_type: str | None = None,
    limit: int = 200,
    trigger_source: str = "cli:expression-golden-replay",
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 2000))
    artifacts = _select_shadow_artifacts(
        db,
        company_id=company_id,
        artifact_type=artifact_type,
        limit=limit,
    )

    now_value = utc_now()
    run = ExpressionEvalRun(
        id=str(uuid4()),
        tenant_id=None,
        company_id=company_id,
        run_type="golden_replay",
        status="running",
        trigger_source=trigger_source,
        artifact_type=artifact_type,
        audience_id=None,
        prompt_version_id=None,
        contract_id=None,
        runner_version=GOLDEN_REPLAY_RUNNER_VERSION,
        input_manifest_json={
            "schema_version": GOLDEN_REPLAY_SCHEMA_VERSION,
            "selection": "expression_artifacts.promoted=false OR validation_status=promoted_valid",
            "company_id": company_id,
            "artifact_type": artifact_type,
            "limit": limit,
        },
        summary_json=None,
        started_at=now_value,
        completed_at=None,
    )
    db.add(run)
    db.flush()

    totals = {"cases": 0, "passed": 0, "failed": 0, "skipped": 0}
    artifact_type_counts: dict[str, dict[str, int]] = {}
    regressions: list[dict[str, Any]] = []
    items: list[ExpressionEvalItem] = []

    for artifact in artifacts:
        totals["cases"] += 1
        payload = artifact.structured_json if isinstance(artifact.structured_json, dict) else None
        if payload is None:
            totals["skipped"] += 1
            _bump_coverage(artifact_type_counts, artifact_type=artifact.artifact_type, status="skipped")
            items.append(
                ExpressionEvalItem(
                    id=str(uuid4()),
                    eval_run_id=run.id,
                    artifact_id=artifact.id,
                    fact_snapshot_id=artifact.fact_snapshot_id,
                    case_key=_golden_case_key(artifact),
                    status="skipped",
                    validator_name=GOLDEN_REPLAY_VALIDATOR_NAME,
                    expected_json={"validation_status": artifact.validation_status},
                    actual_json={"reason": "missing_structured_json"},
                    validation_errors_json=["replay:missing_structured_json"],
                    metrics_json={"skipped": True},
                )
            )
            continue

        snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id)
        audience = db.get(ExpressionAudience, artifact.audience_id)
        contract = _resolve_replay_contract(db, artifact)
        if snapshot is None or audience is None or contract is None:
            totals["skipped"] += 1
            _bump_coverage(artifact_type_counts, artifact_type=artifact.artifact_type, status="skipped")
            missing = []
            if snapshot is None:
                missing.append("fact_snapshot")
            if audience is None:
                missing.append("audience")
            if contract is None:
                missing.append("contract")
            items.append(
                ExpressionEvalItem(
                    id=str(uuid4()),
                    eval_run_id=run.id,
                    artifact_id=artifact.id,
                    fact_snapshot_id=artifact.fact_snapshot_id,
                    case_key=_golden_case_key(artifact),
                    status="skipped",
                    validator_name=GOLDEN_REPLAY_VALIDATOR_NAME,
                    expected_json={"validation_status": artifact.validation_status},
                    actual_json={"missing": missing},
                    validation_errors_json=[f"replay:missing_{part}" for part in missing],
                    metrics_json={"skipped": True},
                )
            )
            continue

        facts = snapshot.facts_json if isinstance(snapshot.facts_json, dict) else {}
        locked_baseline = _locked_baseline_for_replay(
            artifact_type=artifact.artifact_type,
            snapshot=snapshot,
            payload=payload,
        )
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=payload,
            language=artifact.language,
            facts=facts,
            locked_baseline=locked_baseline,
        )
        expected_pass = _artifact_validator_pass(artifact.validation_status)
        actual_pass = _replay_validator_pass(errors)
        item_status = "pass" if expected_pass == actual_pass else "fail"
        if item_status == "pass":
            totals["passed"] += 1
            _bump_coverage(artifact_type_counts, artifact_type=artifact.artifact_type, status="passed")
        else:
            totals["failed"] += 1
            _bump_coverage(artifact_type_counts, artifact_type=artifact.artifact_type, status="failed")
            regressions.append(
                {
                    "artifact_id": artifact.id,
                    "case_key": _golden_case_key(artifact),
                    "expected_pass": expected_pass,
                    "actual_pass": actual_pass,
                    "stored_validation_status": artifact.validation_status,
                    "error_count": len(errors),
                }
            )

        items.append(
            ExpressionEvalItem(
                id=str(uuid4()),
                eval_run_id=run.id,
                artifact_id=artifact.id,
                fact_snapshot_id=snapshot.id,
                case_key=_golden_case_key(artifact),
                status=item_status,
                validator_name=GOLDEN_REPLAY_VALIDATOR_NAME,
                expected_json={
                    "validation_status": artifact.validation_status,
                    "validator_pass": expected_pass,
                },
                actual_json={
                    "validator_pass": actual_pass,
                    "validation_errors": errors,
                },
                validation_errors_json=errors if item_status == "fail" else [],
                metrics_json={
                    "error_count": len(errors),
                    "expected_pass": expected_pass,
                    "actual_pass": actual_pass,
                },
            )
        )

    db.add_all(items)
    run.status = "fail" if totals["failed"] else ("pass" if totals["passed"] else "empty")
    run.completed_at = utc_now()
    run.summary_json = {
        "schema_version": GOLDEN_REPLAY_SCHEMA_VERSION,
        **totals,
        "artifact_type_counts": artifact_type_counts,
        "regression_count": len(regressions),
        "regressions": regressions[:50],
    }
    db.flush()

    return {
        "status": "fail" if totals["failed"] else "pass",
        "schema_version": GOLDEN_REPLAY_SCHEMA_VERSION,
        "guardrails": {
            "uses_ai": False,
            "writes_database": True,
            "writes_tables": ["expression_eval_runs", "expression_eval_items"],
            "mutates_expression_artifacts": False,
            "mutates_promotion": False,
            "raw_output_included": False,
        },
        "eval_run_id": run.id,
        "run_status": run.status,
        "totals": totals,
        "artifact_type_counts": artifact_type_counts,
        "regressions": regressions,
    }
