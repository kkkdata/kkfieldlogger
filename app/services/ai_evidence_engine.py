from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STATUS_PASS = "pass"
STATUS_WARNING = "warning"
STATUS_RISK = "risk"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_NOT_ENOUGH_EVIDENCE = "not_enough_evidence"

PRIORITY_P0 = "P0"
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"

MAX_DIMENSION_EVIDENCE_ITEMS = 5
MAX_ENGINE_FINDING_CHARS = 260

LOW_CONFIDENCE_ASSERTIVE_MARKERS = (
    "clearly",
    "definitely",
    "confirms",
    "must",
    "requires",
    "is present",
)
UNCERTAINTY_MARKERS = (
    "unclear",
    "limited",
    "not visible",
    "cannot",
    "may",
    "appears",
    "possible",
    "ambiguous",
)


@dataclass(frozen=True)
class EvidenceDimension:
    id: str
    title: str
    role: str
    priority: str
    category: str
    always_run: bool
    idle_only: bool
    evidence_fields: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    risk_terms: tuple[str, ...]
    positive_terms: tuple[str, ...] = ()


def _dimension(
    id: str,
    title: str,
    role: str,
    priority: str,
    category: str,
    *,
    evidence_fields: tuple[str, ...],
    trigger_terms: tuple[str, ...] = (),
    risk_terms: tuple[str, ...] = (),
    positive_terms: tuple[str, ...] = (),
    always_run: bool = False,
    idle_only: bool = False,
) -> EvidenceDimension:
    return EvidenceDimension(
        id=id,
        title=title,
        role=role,
        priority=priority,
        category=category,
        always_run=always_run,
        idle_only=idle_only,
        evidence_fields=evidence_fields,
        trigger_terms=trigger_terms,
        risk_terms=risk_terms,
        positive_terms=positive_terms,
    )


EVIDENCE_DIMENSIONS: tuple[EvidenceDimension, ...] = (
    _dimension("photo_validity", "Photo validity", "intake_triage", PRIORITY_P0, "intake", always_run=True, evidence_fields=("ai_summary", "visible_objects", "evidence_limitations", "labels"), risk_terms=("blurry", "unclear", "too close", "too dark", "no auditable", "not visible")),
    _dimension("scene_type", "Scene type", "scene_classifier", PRIORITY_P0, "intake", always_run=True, evidence_fields=("scene_type", "labels", "visible_objects", "ai_summary"), risk_terms=("unknown", "unclear")),
    _dimension("evidence_limitations", "Evidence limitations", "evidence_librarian", PRIORITY_P0, "intake", always_run=True, evidence_fields=("evidence_limitations", "ai_summary"), risk_terms=("blur", "unclear", "limited", "not visible", "cropped", "obstructed")),
    _dimension("safety_overall", "Overall safety screen", "safety_manager", PRIORITY_P0, "safety", always_run=True, evidence_fields=("safety_observations", "defects", "water_or_housekeeping_observations", "people_ppe"), trigger_terms=("safety", "risk", "hazard", "ppe"), risk_terms=("risk", "hazard", "unsafe", "missing", "blocked", "fall", "electrical")),
    _dimension("fall_hazard", "Fall hazard", "fall_protection_checker", PRIORITY_P0, "safety", evidence_fields=("safety_observations", "people_ppe", "visible_objects", "equipment", "defects"), trigger_terms=("ladder", "scaffold", "roof", "edge", "opening", "stair", "elevated", "fall"), risk_terms=("fall", "unprotected", "edge", "opening", "guardrail", "ladder", "harness")),
    _dimension("struck_by_hazard", "Struck-by hazard", "struck_by_checker", PRIORITY_P0, "safety", evidence_fields=("safety_observations", "equipment", "visible_objects"), trigger_terms=("vehicle", "crane", "forklift", "loader", "traffic", "suspended", "moving"), risk_terms=("struck", "moving", "falling object", "traffic", "suspended load", "vehicle")),
    _dimension("caught_between_hazard", "Caught-in/between hazard", "caught_between_checker", PRIORITY_P0, "safety", evidence_fields=("safety_observations", "equipment", "visible_objects"), trigger_terms=("trench", "excavation", "machine", "moving part", "pinch", "between"), risk_terms=("caught", "between", "trench", "excavation", "unguarded", "pinch")),
    _dimension("electrical_hazard", "Electrical hazard", "electrical_safety_checker", PRIORITY_P0, "safety", evidence_fields=("safety_observations", "equipment", "visible_objects", "water_or_housekeeping_observations"), trigger_terms=("wire", "cable", "electrical", "panel", "cord", "generator", "outlet"), risk_terms=("electrical", "exposed wire", "temporary power", "wet", "cord", "panel", "shock")),
    _dimension("ppe_compliance", "PPE compliance", "ppe_checker", PRIORITY_P0, "safety", evidence_fields=("people_ppe", "safety_observations", "visible_objects"), trigger_terms=("person", "worker", "people", "hard hat", "vest", "gloves", "ppe"), risk_terms=("missing", "lack", "without", "no hard hat", "no vest", "no ppe"), positive_terms=("hard hat", "vest", "helmet", "gloves", "eye protection")),
    _dimension("emergency_access", "Emergency access", "access_checker", PRIORITY_P0, "safety", evidence_fields=("water_or_housekeeping_observations", "safety_observations", "visible_objects"), trigger_terms=("exit", "door", "stair", "access", "path", "walkway"), risk_terms=("blocked", "obstructed", "clutter", "narrow", "access")),
    _dimension("water_slip_hazard", "Water and slip hazard", "housekeeping_checker", PRIORITY_P0, "safety", evidence_fields=("water_or_housekeeping_observations", "safety_observations", "defects"), trigger_terms=("water", "wet", "mud", "slip", "standing water"), risk_terms=("standing water", "wet", "mud", "slip", "trip")),
    _dimension("visible_quality_defect", "Visible quality defect", "quality_manager", PRIORITY_P0, "quality", always_run=True, evidence_fields=("quality_observations", "defects", "ai_summary"), risk_terms=("crack", "damage", "defect", "broken", "missing", "misaligned", "poor")),
    _dimension("progress_state", "Progress state", "scheduler", PRIORITY_P0, "progress", always_run=True, evidence_fields=("ai_summary", "scene_type", "visible_objects", "materials", "equipment"), risk_terms=("inactive", "not started", "incomplete", "unfinished", "delay")),
    _dimension("visible_people_count", "Visible people count", "workforce_counter", PRIORITY_P0, "workforce", always_run=True, evidence_fields=("people_ppe", "visible_objects", "ai_summary"), trigger_terms=("person", "people", "worker", "crew", "employee"), risk_terms=("unclear", "obstructed", "cropped")),
    _dimension("worker_activity", "Worker activity", "workforce_checker", PRIORITY_P0, "workforce", evidence_fields=("people_ppe", "ai_summary", "visible_objects"), trigger_terms=("worker", "person", "people", "crew", "employee"), risk_terms=("idle", "waiting", "unsafe", "inactive"), positive_terms=("working", "installing", "carrying", "active")),
    _dimension("asset_material_equipment_facts", "Asset/material/equipment facts", "field_inventory_clerk", PRIORITY_P0, "inventory", always_run=True, evidence_fields=("visible_objects", "materials", "equipment", "labels"), risk_terms=("unknown", "unclear")),
    _dimension("daily_work_inference", "Daily work inference", "daily_report_writer", PRIORITY_P1, "progress", evidence_fields=("ai_summary", "scene_type", "visible_objects", "materials", "equipment"), trigger_terms=("install", "repair", "pour", "frame", "paint", "clean", "demo", "work"), risk_terms=("unclear", "inactive", "incomplete")),
    _dimension("site_area", "Site area", "location_context_checker", PRIORITY_P1, "progress", evidence_fields=("scene_type", "labels", "ai_summary"), trigger_terms=("indoor", "outdoor", "roof", "road", "room", "warehouse", "yard", "mechanical"), risk_terms=("unknown", "unclear")),
    _dimension("work_phase", "Work phase", "phase_classifier", PRIORITY_P1, "progress", evidence_fields=("scene_type", "ai_summary", "materials", "equipment"), trigger_terms=("demolition", "foundation", "framing", "electrical", "plumbing", "finish", "closeout"), risk_terms=("unclear", "not started", "incomplete")),
    _dimension("material_delivery", "Material delivery", "materials_manager", PRIORITY_P1, "inventory", evidence_fields=("materials", "inventory_observations", "visible_objects"), trigger_terms=("material", "delivery", "pallet", "box", "pipe", "lumber", "stock"), risk_terms=("missing", "shortage", "damaged", "wet", "blocked")),
    _dimension("material_shortage", "Material shortage", "materials_shortage_checker", PRIORITY_P1, "inventory", evidence_fields=("inventory_observations", "materials", "missing_evidence"), trigger_terms=("shortage", "missing", "empty", "not enough", "material"), risk_terms=("shortage", "missing", "empty", "not enough")),
    _dimension("material_storage", "Material storage", "storage_checker", PRIORITY_P1, "inventory", evidence_fields=("inventory_observations", "water_or_housekeeping_observations", "materials"), trigger_terms=("storage", "stacked", "pallet", "material", "stock"), risk_terms=("wet", "blocked", "clutter", "unprotected", "damaged")),
    _dimension("equipment_status", "Equipment status", "equipment_manager", PRIORITY_P1, "equipment", evidence_fields=("equipment", "ai_summary", "visible_objects"), trigger_terms=("equipment", "machine", "vehicle", "generator", "pump", "lift"), risk_terms=("damaged", "idle", "blocked", "unsafe", "leak")),
    _dimension("tool_management", "Tool management", "tool_checker", PRIORITY_P1, "equipment", evidence_fields=("equipment", "water_or_housekeeping_observations", "visible_objects"), trigger_terms=("tool", "drill", "saw", "ladder", "cord", "hose"), risk_terms=("scattered", "trip", "blocked", "unsecured")),
    _dimension("housekeeping", "Housekeeping", "housekeeping_checker", PRIORITY_P1, "site_control", evidence_fields=("water_or_housekeeping_observations", "visible_objects", "defects"), trigger_terms=("trash", "debris", "clutter", "waste", "housekeeping"), risk_terms=("trash", "debris", "clutter", "blocked", "waste")),
    _dimension("access_logistics", "Access and logistics", "logistics_checker", PRIORITY_P1, "site_control", evidence_fields=("water_or_housekeeping_observations", "safety_observations", "visible_objects"), trigger_terms=("access", "path", "road", "parking", "gate", "staging"), risk_terms=("blocked", "narrow", "obstructed", "mud", "traffic")),
    _dimension("weather_environment", "Weather/environment impact", "weather_context_checker", PRIORITY_P1, "site_control", evidence_fields=("water_or_housekeeping_observations", "ai_summary", "evidence_limitations"), trigger_terms=("rain", "snow", "mud", "wet", "dark", "low light", "weather"), risk_terms=("rain", "snow", "mud", "wet", "low light", "delay")),
    _dimension("rework_risk", "Rework risk", "rework_checker", PRIORITY_P1, "quality", evidence_fields=("quality_observations", "defects", "recommended_actions"), trigger_terms=("rework", "repair", "fix", "correct", "defect"), risk_terms=("rework", "repair", "fix", "incorrect", "defect", "missing")),
    _dimension("finished_work_protection", "Finished work protection", "finish_protection_checker", PRIORITY_P1, "quality", evidence_fields=("quality_observations", "water_or_housekeeping_observations", "visible_objects"), trigger_terms=("finished", "installed", "surface", "floor", "wall", "fixture"), risk_terms=("scratch", "damage", "dirty", "unprotected", "stain")),
    _dimension("installation_completeness", "Installation completeness", "installation_checker", PRIORITY_P1, "quality", evidence_fields=("quality_observations", "visible_objects", "equipment", "materials"), trigger_terms=("installed", "mounted", "connected", "fixture", "panel", "pipe"), risk_terms=("missing", "incomplete", "loose", "unsealed", "misaligned")),
    _dimension("note_consistency", "Note consistency", "note_reviewer", PRIORITY_P1, "evidence", evidence_fields=("ai_summary", "visible_objects", "materials", "equipment"), trigger_terms=("note", "annotation", "operator"), risk_terms=("mismatch", "unclear", "not visible")),
    _dimension("quality_positive", "Positive quality evidence", "quality_positive_checker", PRIORITY_P2, "quality", evidence_fields=("quality_observations", "ai_summary", "visible_objects"), trigger_terms=("complete", "clean", "installed", "finished", "aligned"), positive_terms=("complete", "clean", "installed", "finished", "aligned"), risk_terms=("defect", "damage")),
    _dimension("defect_severity", "Defect severity", "defect_severity_checker", PRIORITY_P2, "quality", evidence_fields=("defects", "quality_observations", "safety_observations"), trigger_terms=("defect", "issue", "damage", "crack", "missing"), risk_terms=("critical", "urgent", "hazard", "high risk", "severe")),
    _dimension("defect_location_usability", "Defect location usability", "defect_location_checker", PRIORITY_P2, "evidence", evidence_fields=("evidence_limitations", "ai_summary", "scene_type"), trigger_terms=("defect", "location", "where", "area"), risk_terms=("unclear", "not visible", "limited", "cropped")),
    _dimension("missing_photo_evidence", "Needed supplemental photo evidence", "evidence_gap_checker", PRIORITY_P2, "evidence", evidence_fields=("missing_evidence", "evidence_limitations", "recommended_actions"), trigger_terms=("missing", "not visible", "need", "request", "photo"), risk_terms=("missing", "not visible", "need", "request")),
    _dimension("client_visibility_risk", "Client visibility risk", "client_visibility_reviewer", PRIORITY_P2, "quality", evidence_fields=("defects", "safety_observations", "quality_observations", "ai_summary"), trigger_terms=("client", "visible", "customer", "defect", "safety"), risk_terms=("defect", "unsafe", "dirty", "incomplete", "risk")),
    _dimension("inspection_readiness", "Inspection readiness", "inspection_readiness_checker", PRIORITY_P2, "quality", evidence_fields=("quality_observations", "defects", "recommended_actions", "missing_evidence"), trigger_terms=("inspection", "ready", "complete", "approve", "accept"), risk_terms=("missing", "incomplete", "defect", "not visible")),
    _dimension("issue_closure_evidence", "Issue closure evidence", "closure_evidence_checker", PRIORITY_P2, "evidence", evidence_fields=("quality_observations", "ai_summary", "visible_objects", "missing_evidence"), trigger_terms=("fixed", "repair", "closed", "resolved", "complete"), risk_terms=("not visible", "unclear", "missing", "incomplete")),
    _dimension("historical_progress_delta", "Historical progress delta", "progress_delta_checker", PRIORITY_P2, "progress", evidence_fields=("ai_summary", "scene_type", "visible_objects"), trigger_terms=("progress", "change", "before", "after"), risk_terms=("no progress", "same", "stalled", "inactive"), idle_only=True),
    _dimension("duplicate_low_value", "Duplicate or low-value media", "media_value_checker", PRIORITY_P2, "intake", evidence_fields=("evidence_limitations", "ai_summary", "labels"), trigger_terms=("duplicate", "same", "unclear", "surface", "texture"), risk_terms=("duplicate", "same", "no auditable", "unclear", "surface")),
    _dimension("stalled_area_signal", "Stalled area signal", "schedule_risk_checker", PRIORITY_P2, "progress", evidence_fields=("ai_summary", "progress_state", "evidence_limitations"), trigger_terms=("inactive", "stalled", "delay", "no progress", "not started"), risk_terms=("inactive", "stalled", "delay", "not started"), idle_only=True),
    _dimension("receipt_type", "Receipt type", "finance_document_classifier", PRIORITY_P1, "finance", evidence_fields=("receipt_facts", "labels", "ai_summary"), trigger_terms=("receipt", "invoice", "fuel", "total", "vendor"), risk_terms=("unknown", "unreadable", "cropped")),
    _dimension("receipt_readability", "Receipt readability", "finance_readability_checker", PRIORITY_P1, "finance", evidence_fields=("receipt_facts", "evidence_limitations", "defects"), trigger_terms=("receipt", "invoice", "vendor", "amount", "date"), risk_terms=("unreadable", "blurry", "cropped", "missing", "not readable")),
    _dimension("receipt_amount", "Receipt amount confidence", "finance_amount_checker", PRIORITY_P1, "finance", evidence_fields=("receipt_facts", "financial_anomaly_flags", "missing_evidence"), trigger_terms=("amount", "total", "tax", "currency", "receipt"), risk_terms=("missing", "unreadable", "unclear", "suspicious")),
    _dimension("fuel_evidence_chain", "Fuel evidence chain", "fuel_audit_checker", PRIORITY_P1, "finance", evidence_fields=("receipt_facts", "financial_anomaly_flags", "missing_evidence", "visible_objects"), trigger_terms=("fuel", "gas", "diesel", "gallon", "pump"), risk_terms=("missing pump", "missing vehicle", "no supporting", "unreadable")),
    _dimension("receipt_project_employee_match", "Receipt project/employee match", "finance_match_checker", PRIORITY_P1, "finance", evidence_fields=("receipt_facts", "missing_evidence", "ai_summary"), trigger_terms=("employee", "project", "receipt", "invoice"), risk_terms=("missing", "mismatch", "unknown", "not visible")),
    _dimension("expense_anomaly", "Expense anomaly", "finance_anomaly_checker", PRIORITY_P1, "finance", evidence_fields=("financial_anomaly_flags", "defects", "missing_evidence"), trigger_terms=("receipt", "invoice", "expense", "reimbursement", "fuel"), risk_terms=("suspicious", "duplicate", "cropped", "unreadable", "mismatch", "missing")),
    _dimension("material_purchase_crosscheck", "Material purchase cross-check", "finance_material_checker", PRIORITY_P2, "finance", evidence_fields=("receipt_facts", "materials", "inventory_observations", "missing_evidence"), trigger_terms=("material", "receipt", "invoice", "purchase"), risk_terms=("mismatch", "missing", "not visible", "unreadable"), idle_only=True),
    _dimension("time_gps_reasonableness", "Time/GPS reasonableness", "geo_time_checker", PRIORITY_P1, "evidence", evidence_fields=("ai_summary", "evidence_limitations"), trigger_terms=("gps", "location", "time", "date"), risk_terms=("missing", "unknown", "mismatch", "outside")),
    _dimension("cost_risk_signal", "Cost risk signal", "cost_risk_checker", PRIORITY_P2, "finance", evidence_fields=("financial_anomaly_flags", "missing_evidence", "recommended_actions", "defects"), trigger_terms=("cost", "change order", "rework", "receipt", "expense", "t&m"), risk_terms=("rework", "missing", "anomaly", "unreadable", "change order"), idle_only=True),
    _dimension("chief_ai_review", "Chief AI review", "ai_quality_inspector", PRIORITY_P0, "governance", always_run=True, evidence_fields=("ai_summary", "defects", "recommended_actions", "evidence_limitations"), risk_terms=("risk", "hazard", "defect", "missing", "unclear")),
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _truncate(value: Any, limit: int = MAX_ENGINE_FINDING_CHARS) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = [f"{key}: {item}" for key, item in value.items() if _text(item)]
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _field_items(payload: dict[str, Any], field_name: str) -> list[str]:
    if field_name == "progress_state":
        return []
    return _items(payload.get(field_name))


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(marker in normalized for marker in markers)


def _confidence_level(payload: dict[str, Any]) -> str:
    normalized = _text(payload.get("confidence_level")).casefold()
    return normalized if normalized in {"high", "medium", "low"} else "medium"


def _profile_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for field_name in (
        "ai_summary",
        "scene_type",
        "confidence_level",
        "labels",
        "defects",
        "visible_objects",
        "materials",
        "equipment",
        "people_ppe",
        "safety_observations",
        "quality_observations",
        "inventory_observations",
        "water_or_housekeeping_observations",
        "financial_anomaly_flags",
        "missing_evidence",
        "recommended_actions",
        "evidence_limitations",
        "receipt_facts",
    ):
        parts.extend(_items(payload.get(field_name)))
    return " ".join(parts)


def _collect_evidence(payload: dict[str, Any], dimension: EvidenceDimension) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for field_name in dimension.evidence_fields:
        for item in _field_items(payload, field_name):
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            evidence.append(item)
            if len(evidence) >= MAX_DIMENSION_EVIDENCE_ITEMS:
                return evidence
    return evidence


def _dimension_is_triggered(payload: dict[str, Any], dimension: EvidenceDimension, profile_text: str, photo_context: dict[str, Any]) -> bool:
    if dimension.always_run:
        return True
    if dimension.category == "finance" and str(photo_context.get("photo_type") or "").casefold() == "invoice":
        return True
    if any(_field_items(payload, field_name) for field_name in dimension.evidence_fields):
        return True
    return _has_any(profile_text, dimension.trigger_terms)


def _status_for_dimension(payload: dict[str, Any], dimension: EvidenceDimension, evidence: list[str], profile_text: str) -> str:
    confidence = _confidence_level(payload)
    evidence_text = " ".join(evidence) or profile_text

    if dimension.id == "photo_validity":
        if confidence == "low" and _has_any(evidence_text, ("blurry", "unclear", "no auditable", "not visible", "too close", "too dark")):
            return STATUS_NOT_ENOUGH_EVIDENCE
        return STATUS_PASS if evidence else STATUS_NOT_ENOUGH_EVIDENCE

    if dimension.id == "chief_ai_review":
        flags = inspect_ai_pollution(payload, [])
        return STATUS_WARNING if flags else STATUS_PASS

    if not evidence:
        return STATUS_NOT_ENOUGH_EVIDENCE
    if dimension.risk_terms and _has_any(evidence_text, dimension.risk_terms):
        return STATUS_RISK
    if dimension.positive_terms and _has_any(evidence_text, dimension.positive_terms):
        return STATUS_PASS
    if dimension.priority in {PRIORITY_P0, PRIORITY_P1} and dimension.category in {"safety", "quality", "finance"}:
        return STATUS_WARNING
    return STATUS_PASS


def inspect_ai_pollution(payload: dict[str, Any], dimensions: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    summary = _text(payload.get("ai_summary"))
    confidence = _confidence_level(payload)
    defects = _items(payload.get("defects"))
    actions = _items(payload.get("recommended_actions"))
    limitations = _items(payload.get("evidence_limitations"))
    profile_text = _profile_text(payload)

    if _has_any(summary, ("risk", "hazard", "unsafe", "defect", "issue", "concern")) and not defects:
        flags.append("summary_risk_without_defect")
    if confidence == "low" and _has_any(summary, LOW_CONFIDENCE_ASSERTIVE_MARKERS) and not _has_any(summary, UNCERTAINTY_MARKERS):
        flags.append("low_confidence_overassertive")
    if confidence == "low" and not limitations:
        flags.append("low_confidence_without_limitations")
    if "construction site" in summary.casefold() and not any(_items(payload.get(field)) for field in ("materials", "equipment", "people_ppe")):
        flags.append("construction_overclaim")
    if defects and not actions:
        flags.append("defect_without_action")

    for dimension in dimensions:
        status = dimension.get("status")
        evidence = dimension.get("evidence") or []
        if status in {STATUS_WARNING, STATUS_RISK} and not evidence:
            flags.append(f"{dimension.get('dimension')}_finding_without_evidence")
        if status == STATUS_RISK and confidence == "low":
            flags.append(f"{dimension.get('dimension')}_low_confidence_risk_needs_recheck")

    if not profile_text:
        flags.append("empty_structured_profile")
    return sorted(set(flags))


def evaluate_dimension(payload: dict[str, Any], dimension: EvidenceDimension, *, photo_context: dict[str, Any], profile_text: str) -> dict[str, Any]:
    if not _dimension_is_triggered(payload, dimension, profile_text, photo_context):
        return {
            "dimension": dimension.id,
            "title": dimension.title,
            "role": dimension.role,
            "priority": dimension.priority,
            "category": dimension.category,
            "status": STATUS_NOT_APPLICABLE,
            "finding": "No trigger evidence for this dimension.",
            "evidence": [],
            "missing_evidence": [],
            "recommended_action": "",
            "confidence": _confidence_level(payload),
            "requires_recheck": False,
            "idle_only": dimension.idle_only,
        }

    evidence = _collect_evidence(payload, dimension)
    status = _status_for_dimension(payload, dimension, evidence, profile_text)
    missing_evidence = _items(payload.get("missing_evidence"))
    actions = _items(payload.get("recommended_actions"))
    limitations = _items(payload.get("evidence_limitations"))
    confidence = _confidence_level(payload)
    requires_recheck = status == STATUS_RISK and confidence == "low"
    if status == STATUS_NOT_ENOUGH_EVIDENCE and dimension.priority in {PRIORITY_P0, PRIORITY_P1}:
        requires_recheck = True

    if status == STATUS_NOT_ENOUGH_EVIDENCE:
        finding = "Not enough reliable evidence for this dimension."
    elif status == STATUS_RISK:
        finding = f"{dimension.title} needs attention based on visible evidence."
    elif status == STATUS_WARNING:
        finding = f"{dimension.title} has reviewable evidence but needs manager confirmation."
    else:
        finding = f"{dimension.title} has no obvious issue in the available evidence."

    if limitations and confidence == "low":
        missing_evidence = list(dict.fromkeys([*missing_evidence, *limitations]))

    return {
        "dimension": dimension.id,
        "title": dimension.title,
        "role": dimension.role,
        "priority": dimension.priority,
        "category": dimension.category,
        "status": status,
        "finding": _truncate(finding),
        "evidence": evidence,
        "missing_evidence": missing_evidence[:MAX_DIMENSION_EVIDENCE_ITEMS],
        "recommended_action": actions[0] if actions else "",
        "confidence": confidence,
        "requires_recheck": requires_recheck,
        "idle_only": dimension.idle_only,
    }


def _priority_queue(dimensions: list[dict[str, Any]]) -> dict[str, list[str]]:
    queue: dict[str, list[str]] = {PRIORITY_P0: [], PRIORITY_P1: [], PRIORITY_P2: [], PRIORITY_P3: [], "idle": []}
    for item in dimensions:
        if item.get("status") == STATUS_NOT_APPLICABLE:
            continue
        dimension_id = str(item.get("dimension") or "")
        if item.get("idle_only"):
            queue["idle"].append(dimension_id)
        else:
            priority = str(item.get("priority") or PRIORITY_P3)
            queue.setdefault(priority, []).append(dimension_id)
    return queue


def _summary(dimensions: list[dict[str, Any]], pollution_flags: list[str]) -> dict[str, Any]:
    actionable = [item for item in dimensions if item.get("status") in {STATUS_WARNING, STATUS_RISK, STATUS_NOT_ENOUGH_EVIDENCE}]
    risk_dimensions = [item["dimension"] for item in dimensions if item.get("status") == STATUS_RISK]
    recheck_dimensions = [item["dimension"] for item in dimensions if item.get("requires_recheck")]
    completed_dimensions = [item for item in dimensions if item.get("status") != STATUS_NOT_APPLICABLE]
    return {
        "dimension_count": len(dimensions),
        "completed_dimension_count": len(completed_dimensions),
        "actionable_dimension_count": len(actionable),
        "risk_dimensions": risk_dimensions[:12],
        "recheck_dimensions": recheck_dimensions[:12],
        "pollution_flags": pollution_flags,
        "confidence_gate": "needs_review" if pollution_flags or recheck_dimensions else "usable",
    }


def build_evidence_engine_result(payload: dict[str, Any], *, photo_context: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_payload = dict(payload or {})
    context = dict(photo_context or {})
    profile_text = _profile_text(normalized_payload)
    dimensions = [
        evaluate_dimension(normalized_payload, dimension, photo_context=context, profile_text=profile_text)
        for dimension in EVIDENCE_DIMENSIONS
    ]
    pollution_flags = inspect_ai_pollution(normalized_payload, dimensions)

    normalized_payload["evidence_dimensions"] = dimensions
    normalized_payload["evidence_engine"] = {
        "version": "2026-05-04-v1",
        "strategy": "visual_facts_then_dimension_review",
        "dimension_priorities": _priority_queue(dimensions),
        "summary": _summary(dimensions, pollution_flags),
    }
    return normalized_payload


def dimension_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "dimension": item.id,
            "title": item.title,
            "role": item.role,
            "priority": item.priority,
            "category": item.category,
            "always_run": item.always_run,
            "idle_only": item.idle_only,
            "trigger_terms": list(item.trigger_terms),
            "evidence_fields": list(item.evidence_fields),
        }
        for item in EVIDENCE_DIMENSIONS
    ]
