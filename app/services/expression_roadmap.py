from __future__ import annotations

from typing import Any

ROADMAP_SCHEMA_VERSION = "expression_roadmap_gap_v1"

# Declared runtime maturity for registry-backed artifacts (code truth, not DB).
EXPRESSION_ARTIFACT_IMPLEMENTATION: dict[str, dict[str, Any]] = {
    "employee_contribution_narrative": {
        "maturity": "promoted_api",
        "shadow_generator": "generate_employee_contribution_shadow",
        "shadow_batch_cli": "expression-shadow-employee",
        "production_gate_step": None,
        "promoted_read_surfaces": ["mobile"],
        "deterministic_fallback": True,
        "immutability_validator": "employee_chinese_and_schema",
        "ai_polish_allowed": True,
    },
    "progress_report_translation": {
        "maturity": "shadow_cli",
        "shadow_generator": "generate_progress_report_translation_shadow",
        "shadow_batch_cli": "expression-shadow-progress-report-batch",
        "production_gate_step": None,
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "structure_parity",
        "ai_polish_allowed": True,
    },
    "progress_report_string_translation": {
        "maturity": "shadow_cli",
        "shadow_generator": "generate_progress_report_string_translation_shadow",
        "shadow_batch_cli": "expression-shadow-progress-report-string-batch",
        "production_gate_step": None,
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "translation_chunk_quality",
        "ai_polish_allowed": True,
    },
    "generated_report_markdown": {
        "maturity": "shadow_cli",
        "shadow_generator": "generate_report_markdown_shadow",
        "shadow_batch_cli": "expression-shadow-report-markdown-batch",
        "production_gate_step": None,
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "section_refs",
        "ai_polish_allowed": True,
    },
    "generated_report_markdown_section": {
        "maturity": "shadow_cli",
        "shadow_generator": "generate_report_markdown_section_shadow",
        "shadow_batch_cli": "expression-shadow-report-markdown-section-batch",
        "production_gate_step": None,
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "section_refs",
        "ai_polish_allowed": True,
    },
    "project_manager_decision_brief": {
        "maturity": "promoted_api",
        "shadow_generator": "generate_project_manager_decision_brief_shadow",
        "shadow_batch_cli": "expression-shadow-project-manager-brief-batch",
        "production_gate_step": "project_manager_decision_brief_shadow_batch",
        "promoted_read_surfaces": ["mobile_project_manager_status_card"],
        "deterministic_fallback": True,
        "immutability_validator": "decision_surface",
        "ai_polish_allowed": False,
    },
    "client_progress_summary": {
        "maturity": "production_gate",
        "shadow_generator": "generate_client_progress_summary_shadow",
        "shadow_batch_cli": "expression-shadow-client-progress-summary-batch",
        "production_gate_step": "client_progress_summary_shadow_batch",
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "surface_immutability",
        "ai_polish_allowed": True,
    },
    "operations_health_summary": {
        "maturity": "production_gate",
        "shadow_generator": "generate_operations_health_summary_shadow",
        "shadow_batch_cli": "expression-shadow-operations-health-summary-batch",
        "production_gate_step": "operations_health_summary_shadow_batch",
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "surface_immutability",
        "ai_polish_allowed": True,
    },
    "finance_summary": {
        "maturity": "production_gate",
        "shadow_generator": "generate_finance_summary_shadow",
        "shadow_batch_cli": "expression-shadow-finance-summary-batch",
        "production_gate_step": "finance_summary_shadow_batch",
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "surface_immutability",
        "ai_polish_allowed": True,
    },
    "executive_company_health_summary": {
        "maturity": "production_gate",
        "shadow_generator": "generate_executive_company_health_shadow",
        "shadow_batch_cli": "expression-shadow-executive-company-health-batch",
        "production_gate_step": "executive_company_health_shadow_audit",
        "promoted_read_surfaces": [],
        "deterministic_fallback": True,
        "immutability_validator": "surface_immutability",
        "ai_polish_allowed": True,
    },
}

# Target audiences/artifacts/capabilities not yet in EXPRESSION_REGISTRY_EXPECTATIONS.
EXPRESSION_FUTURE_ROADMAP_TARGETS: list[dict[str, Any]] = [
    {
        "target_id": "audience:platform_admin",
        "kind": "audience",
        "audience_id": "platform_admin",
        "phase": "4",
        "priority": 1,
        "risk_class": "future",
        "validation_gate": "Seed expression_audiences row with admin visibility_policy before any registry artifact.",
        "fallback_policy": "Portal admin and diagnostics keep non-registry inline prompts.",
        "promotion_gate": "Admin-only surfaces; never exposed on mobile client routes.",
    },
    {
        "target_id": "artifact:platform_expression_ops_digest",
        "kind": "artifact",
        "artifact_type": "platform_expression_ops_digest",
        "audience_id": "platform_admin",
        "template_id": "platform_expression_ops_digest",
        "stage": "expression",
        "phase": "4b",
        "priority": 2,
        "risk_class": "future",
        "validation_gate": "Roll up operations_health_summary and expression_readiness facts; no secrets or paths in payload.",
        "fallback_policy": "Deterministic digest from locked DB metrics only.",
        "promotion_gate": "Shadow-only until platform_admin audience and contract exist.",
    },
    {
        "target_id": "capability:surface_immutability_validators",
        "kind": "capability",
        "phase": "3b",
        "priority": 1,
        "risk_class": "product_risk",
        "affects_artifact_types": [
            "client_progress_summary",
            "operations_health_summary",
            "finance_summary",
            "executive_company_health_summary",
        ],
        "validation_gate": "AI polish rejected when structured surface/headline_metrics change vs deterministic baseline.",
        "fallback_policy": "Keep --no-ai shadow runs and shadow_fallback_valid as the only path until validators land.",
        "promotion_gate": "No promotion while ai_policy still says AI disabled for immutability.",
    },
    {
        "target_id": "infrastructure:expression_eval_runs",
        "kind": "infrastructure",
        "phase": "5",
        "priority": 3,
        "risk_class": "future",
        "validation_gate": "Golden replay proves validator regressions before prompt version promotion.",
        "fallback_policy": "Continue CLI shadow batches and production gate until golden replay runs are recorded.",
        "promotion_gate": "Prompt version status shadow->active requires eval pass once tables exist.",
        "implemented_by": "ExpressionEvalRun/ExpressionEvalItem + expression-golden-replay + expression-audit-eval-readiness",
    },
    {
        "target_id": "artifact:evidence_copilot_turn_summary",
        "kind": "artifact",
        "artifact_type": "evidence_copilot_turn_summary",
        "audience_id": "project_manager",
        "template_id": "evidence_copilot_turn_summary",
        "stage": "narrative",
        "phase": "6",
        "priority": 4,
        "risk_class": "future",
        "validation_gate": "Source refs must map to copilot_message_sources rows; no new claims beyond facts.",
        "fallback_policy": "Keep evidence_copilot.py inline prompts until shadow parity with registry.",
        "promotion_gate": "Do not retire copilot_messages flow until registry shadow parity gate passes.",
    },
]

PRODUCTION_GATE_STEPS = frozenset(
    {
        "project_manager_decision_brief_shadow_batch",
        "client_progress_summary_shadow_batch",
        "operations_health_summary_shadow_batch",
        "finance_summary_shadow_batch",
        "finance_summary_readiness_shadow_batch",
        "executive_company_health_shadow_audit",
    }
)


def _registry_artifact_types(registry_expectations: list[dict[str, Any]]) -> set[str]:
    return {str(item["artifact_type"]) for item in registry_expectations}


def _implementation_gaps_for_registry(
    *,
    registry_expectations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for expectation in registry_expectations:
        artifact_type = str(expectation["artifact_type"])
        impl = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(artifact_type)
        if impl is None:
            gaps.append(
                {
                    "gap_id": f"registry_missing_implementation_map:{artifact_type}",
                    "artifact_type": artifact_type,
                    "audience_id": str(expectation["audience_id"]),
                    "risk_class": "hard_bug",
                    "reason": "registry_expectation_has_no_implementation_map",
                    "validation_gate": expectation.get("promotion_gate"),
                    "fallback_policy": "deterministic_fallback_required",
                    "promotion_gate": expectation.get("promotion_gate"),
                }
            )
            continue

        ai_policy = str(expectation.get("ai_policy") or "")
        if "AI disabled until" in ai_policy and not impl.get("immutability_validator"):
            gaps.append(
                {
                    "gap_id": f"missing_immutability_validator:{artifact_type}",
                    "artifact_type": artifact_type,
                    "audience_id": str(expectation["audience_id"]),
                    "risk_class": "product_risk",
                    "reason": "ai_policy_blocks_polish_without_validator",
                    "validation_gate": expectation.get("promotion_gate"),
                    "fallback_policy": "shadow_fallback_valid_with_no_ai",
                    "promotion_gate": expectation.get("promotion_gate"),
                }
            )

        if impl.get("maturity") == "registry_only":
            gaps.append(
                {
                    "gap_id": f"shadow_path_not_implemented:{artifact_type}",
                    "artifact_type": artifact_type,
                    "audience_id": str(expectation["audience_id"]),
                    "risk_class": "product_risk",
                    "reason": "registry_seeded_without_shadow_generator",
                    "validation_gate": expectation.get("promotion_gate"),
                    "fallback_policy": "do_not_promote_or_expose",
                    "promotion_gate": expectation.get("promotion_gate"),
                }
            )

        gate_step = impl.get("production_gate_step")
        if gate_step and gate_step not in PRODUCTION_GATE_STEPS:
            gaps.append(
                {
                    "gap_id": f"production_gate_step_unknown:{artifact_type}",
                    "artifact_type": artifact_type,
                    "audience_id": str(expectation["audience_id"]),
                    "risk_class": "hard_bug",
                    "reason": "implementation_map_references_missing_gate",
                    "validation_gate": expectation.get("promotion_gate"),
                    "fallback_policy": "deterministic_fallback_required",
                    "promotion_gate": expectation.get("promotion_gate"),
                }
            )

        if str(expectation.get("stage")) == "expression" and impl.get("maturity") not in {
            "shadow_cli",
            "production_gate",
            "promoted_api",
        }:
            gaps.append(
                {
                    "gap_id": f"expression_stage_below_shadow_cli:{artifact_type}",
                    "artifact_type": artifact_type,
                    "audience_id": str(expectation["audience_id"]),
                    "risk_class": "product_risk",
                    "reason": "expression_artifact_lacks_shadow_cli",
                    "validation_gate": expectation.get("promotion_gate"),
                    "fallback_policy": "registry_only_safe",
                    "promotion_gate": expectation.get("promotion_gate"),
                }
            )

    return gaps


def _future_target_gaps(
    *,
    registry_artifact_types: set[str],
    registry_audience_ids: set[str],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for target in EXPRESSION_FUTURE_ROADMAP_TARGETS:
        entry = dict(target)
        entry_gaps: list[str] = []
        kind = str(target.get("kind"))
        if kind == "audience":
            audience_id = str(target["audience_id"])
            if audience_id not in registry_audience_ids:
                entry_gaps.append("audience_not_in_registry_expectations")
        elif kind == "artifact":
            artifact_type = str(target["artifact_type"])
            if artifact_type not in registry_artifact_types:
                entry_gaps.append("artifact_not_in_registry_expectations")
            audience_id = str(target.get("audience_id") or "")
            if audience_id and audience_id not in registry_audience_ids:
                entry_gaps.append("audience_not_in_registry_expectations")
        elif kind == "capability" and target.get("target_id") == "capability:surface_immutability_validators":
            affected_artifacts = [str(item) for item in target.get("affects_artifact_types") or []]
            missing_validators = [
                artifact_type
                for artifact_type in affected_artifacts
                if not EXPRESSION_ARTIFACT_IMPLEMENTATION.get(artifact_type, {}).get("immutability_validator")
            ]
            if missing_validators:
                entry_gaps.append("not_implemented")
                entry["missing_validators"] = missing_validators
            else:
                entry["implemented_by"] = "surface_immutability"
        elif kind == "infrastructure" and target.get("target_id") == "infrastructure:expression_eval_runs":
            entry["implemented_by"] = target.get("implemented_by")
        elif kind in {"capability", "infrastructure"}:
            entry_gaps.append("not_implemented")
        entry["gaps"] = entry_gaps
        entry["ready"] = not entry_gaps
        entries.append(entry)
        if entry_gaps:
            gaps.append(
                {
                    "gap_id": f"future_target:{target['target_id']}",
                    "target_id": target["target_id"],
                    "kind": kind,
                    "risk_class": str(target.get("risk_class") or "future"),
                    "reasons": entry_gaps,
                    "validation_gate": target.get("validation_gate"),
                    "fallback_policy": target.get("fallback_policy"),
                    "promotion_gate": target.get("promotion_gate"),
                }
            )
    return gaps, entries


def build_expression_target_roadmap_gap_audit(
    *,
    registry_expectations: list[dict[str, Any]],
) -> dict[str, Any]:
    registry_types = _registry_artifact_types(registry_expectations)
    registry_audiences = {str(item["audience_id"]) for item in registry_expectations}
    registry_gaps = _implementation_gaps_for_registry(registry_expectations=registry_expectations)
    future_gaps, future_targets = _future_target_gaps(
        registry_artifact_types=registry_types,
        registry_audience_ids=registry_audiences,
    )
    all_gaps = registry_gaps + future_gaps
    risk_totals = {"hard_bug": 0, "product_risk": 0, "future": 0}
    for gap in all_gaps:
        risk_class = str(gap.get("risk_class") or "future")
        if risk_class in risk_totals:
            risk_totals[risk_class] += 1

    registry_rows: list[dict[str, Any]] = []
    for expectation in sorted(registry_expectations, key=lambda item: str(item["artifact_type"])):
        artifact_type = str(expectation["artifact_type"])
        impl = EXPRESSION_ARTIFACT_IMPLEMENTATION.get(artifact_type, {})
        registry_rows.append(
            {
                "artifact_type": artifact_type,
                "audience_id": str(expectation["audience_id"]),
                "stage": str(expectation.get("stage")),
                "maturity": impl.get("maturity"),
                "shadow_generator": impl.get("shadow_generator"),
                "production_gate_step": impl.get("production_gate_step"),
                "promoted_read_surfaces": list(impl.get("promoted_read_surfaces") or []),
                "promotion_gate": expectation.get("promotion_gate"),
                "ai_policy": expectation.get("ai_policy"),
                "deterministic_fallback": bool(impl.get("deterministic_fallback")),
            }
        )

    return {
        "schema_version": ROADMAP_SCHEMA_VERSION,
        "status": "inform",
        "totals": {
            "registry_artifacts": len(registry_expectations),
            "future_targets": len(EXPRESSION_FUTURE_ROADMAP_TARGETS),
            "gaps": len(all_gaps),
            **risk_totals,
        },
        "registry_implementation": registry_rows,
        "registry_gaps": registry_gaps,
        "future_targets": future_targets,
        "future_gaps": future_gaps,
        "guardrails": {
            "uses_ai": False,
            "writes_database": False,
            "uses_filesystem_scan": False,
        },
    }


def audit_expression_target_roadmap_exit_code(
    summary: dict[str, Any],
    *,
    fail_on_hard_bugs: bool,
    fail_on_product_risk: bool,
) -> int:
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    if fail_on_hard_bugs and int(totals.get("hard_bug") or 0) > 0:
        summary["status"] = "fail"
        return 1
    if fail_on_product_risk and int(totals.get("product_risk") or 0) > 0:
        summary["status"] = "fail"
        return 1
    return 0
