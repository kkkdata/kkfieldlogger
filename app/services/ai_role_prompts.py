from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.ai_evidence_engine import EVIDENCE_DIMENSIONS, PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, EvidenceDimension


ROLE_OUTPUT_SCHEMA = {
    "role_id": "",
    "priority": "P0|P1|P2|P3",
    "status": "pass|warning|risk|not_applicable|not_enough_evidence",
    "finding": "one practical conclusion, grounded in evidence",
    "evidence": ["visible or metadata facts that support the finding"],
    "missing_evidence": ["facts needed before making a stronger claim"],
    "recommended_action": "one next action, or empty string",
    "confidence": "high|medium|low",
    "should_retry": False,
    "numeric_value": None,
    "count_range": "",
    "is_estimate": False,
}


@dataclass(frozen=True)
class EvidenceRolePrompt:
    role_id: str
    title: str
    role: str
    priority: str
    category: str
    prompt: str
    trigger_terms: tuple[str, ...]
    evidence_fields: tuple[str, ...]
    max_retries: int = 2


ROLE_FOCUS: dict[str, str] = {
    "photo_validity": "Decide whether this photo has field audit value. Reject ordinary, duplicate, blurry, cropped, or context-free images as not_enough_evidence instead of inventing worksite meaning.",
    "scene_type": "Classify the scene conservatively. Use construction/worksite categories only when materials, equipment, PPE, active work, installed systems, or work staging are visible.",
    "evidence_limitations": "List concrete limits: blur, crop, distance, low light, obstruction, no location context, text unreadable, or insufficient visible field evidence.",
    "safety_overall": "Look only for visible safety hazards. Do not report a hazard when the evidence says no visible hazard.",
    "fall_hazard": "Check ladders, roofs, edges, openings, stairs, scaffolds, elevated work, guardrails, harnesses, and fall exposure.",
    "struck_by_hazard": "Check vehicles, moving equipment, traffic, suspended loads, falling objects, and worker proximity.",
    "caught_between_hazard": "Check trenches, excavation, machines, moving parts, pinch points, and workers between heavy objects.",
    "electrical_hazard": "Check cords, temporary power, panels, outlets, generators, exposed wiring, and wet electrical conditions.",
    "ppe_compliance": "Check only visible people. Mention hard hats, vests, gloves, eye protection, harnesses, or lack of PPE only when visible enough to judge.",
    "emergency_access": "Check exits, stairs, doors, walkways, access paths, and emergency routes for visible obstruction.",
    "water_slip_hazard": "Check standing water, mud, wet walking surfaces, hoses, debris, and trip/slip clues.",
    "visible_quality_defect": "Check visible damage, cracks, missing work, misalignment, poor finish, broken material, or incomplete installation.",
    "progress_state": "State whether visible work appears not started, active, staged, partially complete, complete, or impossible to judge.",
    "visible_people_count": "Count visible people in the image for workforce analytics. Count only people with a visible body/head/limb clue. Do not infer hidden people. Use numeric_value for the best visible count, count_range for uncertainty such as 3-5, and is_estimate=true when partially obscured, cropped, or crowded.",
    "worker_activity": "Describe visible worker activity without guessing productivity. Idle/working only if supported by body posture, tools, or task context.",
    "asset_material_equipment_facts": "Extract concrete visible assets, materials, equipment, tools, vehicles, or installed systems. Avoid guesses.",
    "daily_work_inference": "Infer today's likely work from visible evidence only, or return not_enough_evidence.",
    "site_area": "Classify area such as indoor room, exterior yard, road, roof, warehouse, vehicle scene, residential exterior, or unclear.",
    "work_phase": "Infer project phase only if visible evidence supports it: demolition, framing, electrical, plumbing, finish, cleanup, closeout, etc.",
    "material_delivery": "Check whether materials appear delivered, staged, or newly present. Do not count unless visible.",
    "material_shortage": "Look for visible shortage clues only; otherwise not_applicable or not_enough_evidence.",
    "material_storage": "Check material storage: blocked, wet, unprotected, damaged, scattered, or acceptable.",
    "equipment_status": "Check visible equipment status: present, in use, idle, damaged, blocked, leaking, or impossible to judge.",
    "tool_management": "Check tools, ladders, hoses, cords, and hand tools for scatter, trip hazard, or unsafe placement.",
    "housekeeping": "Check trash, debris, clutter, loose material, blocked access, and cleanup condition.",
    "access_logistics": "Check whether people/material/vehicle movement paths are clear or blocked.",
    "weather_environment": "Check visible weather/environment effects: rain, snow, mud, wet surfaces, low light, dust, or no visible effect.",
    "rework_risk": "Look for visible clues that work may need correction or rework. Do not infer rework from uncertainty alone.",
    "finished_work_protection": "Check whether finished/installed surfaces or fixtures appear protected or at risk of damage/contamination.",
    "installation_completeness": "Check visible installation completeness: missing covers, loose parts, unsealed penetrations, incomplete connections. Only apply when installed equipment, fixtures, or building systems are visible.",
    "note_consistency": "Compare note/context to visible evidence. Mark mismatch only when the conflict is clear.",
    "quality_positive": "Identify positive quality evidence only when visible: clean finish, alignment, complete installation, organized work.",
    "defect_severity": "Rate severity only for visible defects. Avoid severity when no specific defect exists.",
    "defect_location_usability": "Judge whether the photo gives enough context to locate a defect or condition later.",
    "missing_photo_evidence": "Name supplemental photos needed: wider context, close-up, label/serial, receipt total, GPS/location, before/after.",
    "client_visibility_risk": "Flag whether photo should be held from client view only when it shows a concrete visible safety, quality, housekeeping, privacy, or embarrassing condition. Ordinary outdoor scenes, vehicles, rocks, gravel, people at a table, or low confidence are not client visibility risks by themselves.",
    "inspection_readiness": "Judge whether this photo supports inspection/approval, or what evidence is missing.",
    "issue_closure_evidence": "Judge whether photo is enough to prove an issue was closed or repair completed.",
    "historical_progress_delta": "Assess whether this photo could support progress comparison. If no baseline, say missing baseline.",
    "duplicate_low_value": "Identify low-value media: duplicate, unclear, no auditable object, generic surface, or casual scene.",
    "stalled_area_signal": "Look for visible stalled/inactive work clues, but require comparison or repeated context for strong claims.",
    "receipt_type": "Classify receipt/invoice type and readable document category.",
    "receipt_readability": "Check readability of vendor, date, amount, tax, payment, line items, and crop/blur.",
    "receipt_amount": "Extract or validate amount confidence from visible receipt facts only.",
    "fuel_evidence_chain": "For fuel, require receipt plus supporting clues such as pump, gallons, vehicle, GPS, or timestamp; otherwise name missing evidence.",
    "receipt_project_employee_match": "Check if receipt can be tied to project/employee/time/location. Do not guess identities.",
    "expense_anomaly": "Look for finance anomalies: unreadable totals, cropped receipt, duplicate-looking document, category mismatch, missing support.",
    "material_purchase_crosscheck": "Compare receipt/material claims against visible materials when possible.",
    "time_gps_reasonableness": "Use metadata to judge whether time/GPS are present and reviewable; do not infer project location without coordinates.",
    "cost_risk_signal": "Identify cost risk from rework, material evidence, receipt anomaly, or missing financial support.",
    "chief_ai_review": "Audit the AI output itself. Reject conclusions that lack evidence, contradict other fields, overstate low confidence, or convert negative observations into risks.",
}


def _role_prompt_from_dimension(dimension: EvidenceDimension) -> EvidenceRolePrompt:
    focus = ROLE_FOCUS.get(dimension.id, f"Review the {dimension.title} dimension using only supplied evidence.")
    prompt = (
        f"You are the {dimension.role} for KK Field Logger.\n"
        f"Dimension: {dimension.title} ({dimension.id}). Priority: {dimension.priority}. Category: {dimension.category}.\n"
        f"Mission: {focus}\n\n"
        "Rules:\n"
        "- Use only the supplied visual facts, AI fields, capture metadata, note, and receipt facts.\n"
        "- Do not invent facts, identities, quantities, defects, hazards, costs, locations, or progress.\n"
        "- If evidence is weak, return not_enough_evidence with concrete missing_evidence.\n"
        "- If the role does not apply to this photo, return not_applicable.\n"
        "- If you report risk/warning, evidence must cite the specific visible or metadata facts.\n"
        "- Do not cite schema field names such as visible_objects, equipment, people_ppe, gps, or empty list as evidence; cite the actual visible values and why they matter.\n"
        "- Empty arrays mean unknown or no visible evidence; they are not evidence of missing PPE, missing safety controls, defects, or risk.\n"
        "- Low confidence, blur, crop, weak context, or model uncertainty cannot support risk/warning by itself; use not_enough_evidence instead.\n"
        "- Do not convert ordinary objects such as cars, rocks, gravel, trees, tables, chairs, tarps, sheds, or residential exteriors into hazards without a specific visible hazard mechanism.\n"
        "- Do not treat sunglasses, casual clothing, hats, standing, sitting, or people talking as PPE compliance, work progress, or safety proof.\n"
        "- For pass, state only that this role found no visible issue; do not praise the scene as safe, compliant, inspection-ready, or proper PPE unless those controls are clearly visible.\n"
        "- Do not repeat uncertain source language such as possibly, probably, may indicate, or appears to be as a stronger conclusion.\n"
        "- Low confidence must include missing_evidence or limitations.\n"
        "- Return strict JSON only. No markdown.\n"
        f"Output schema example: {json.dumps(ROLE_OUTPUT_SCHEMA, ensure_ascii=False)}"
    )
    return EvidenceRolePrompt(
        role_id=dimension.id,
        title=dimension.title,
        role=dimension.role,
        priority=dimension.priority,
        category=dimension.category,
        prompt=prompt,
        trigger_terms=dimension.trigger_terms,
        evidence_fields=dimension.evidence_fields,
        max_retries=2 if dimension.priority in {PRIORITY_P0, PRIORITY_P1} else 1,
    )


ROLE_PROMPTS: tuple[EvidenceRolePrompt, ...] = tuple(_role_prompt_from_dimension(item) for item in EVIDENCE_DIMENSIONS)
ROLE_PROMPT_MAP: dict[str, EvidenceRolePrompt] = {item.role_id: item for item in ROLE_PROMPTS}


def role_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "role_id": item.role_id,
            "title": item.title,
            "role": item.role,
            "priority": item.priority,
            "category": item.category,
            "prompt": item.prompt,
            "trigger_terms": list(item.trigger_terms),
            "evidence_fields": list(item.evidence_fields),
            "max_retries": item.max_retries,
        }
        for item in ROLE_PROMPTS
    ]


def selected_role_prompts(*, priorities: set[str] | None = None, role_ids: set[str] | None = None) -> list[EvidenceRolePrompt]:
    selected = list(ROLE_PROMPTS)
    if priorities:
        selected = [item for item in selected if item.priority in priorities]
    if role_ids:
        selected = [item for item in selected if item.role_id in role_ids]
    return selected


def compact_evidence_context(payload: dict[str, Any], *, photo_context: dict[str, Any] | None = None) -> str:
    context = {
        "photo_context": photo_context or {},
        "ai_summary": payload.get("ai_summary"),
        "confidence_level": payload.get("confidence_level"),
        "scene_type": payload.get("scene_type"),
        "labels": payload.get("labels") or [],
        "defects": payload.get("defects") or [],
        "visible_objects": payload.get("visible_objects") or [],
        "materials": payload.get("materials") or [],
        "equipment": payload.get("equipment") or [],
        "people_ppe": payload.get("people_ppe") or [],
        "safety_observations": payload.get("safety_observations") or [],
        "quality_observations": payload.get("quality_observations") or [],
        "inventory_observations": payload.get("inventory_observations") or [],
        "water_or_housekeeping_observations": payload.get("water_or_housekeeping_observations") or [],
        "financial_anomaly_flags": payload.get("financial_anomaly_flags") or [],
        "missing_evidence": payload.get("missing_evidence") or [],
        "recommended_actions": payload.get("recommended_actions") or [],
        "evidence_limitations": payload.get("evidence_limitations") or [],
        "receipt_facts": payload.get("receipt_facts") or {},
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


def build_role_prompt(role_prompt: EvidenceRolePrompt, payload: dict[str, Any], *, photo_context: dict[str, Any] | None = None) -> str:
    focused_context = {
        "photo_context": photo_context or {},
        "ai_summary": payload.get("ai_summary"),
        "confidence_level": payload.get("confidence_level"),
    }
    for field_name in role_prompt.evidence_fields:
        focused_context[field_name] = payload.get(field_name)
    for field_name in ("defects", "recommended_actions", "evidence_limitations", "missing_evidence"):
        if field_name not in focused_context:
            focused_context[field_name] = payload.get(field_name)
    schema = dict(ROLE_OUTPUT_SCHEMA)
    schema["role_id"] = role_prompt.role_id
    schema["priority"] = role_prompt.priority
    focus = ROLE_FOCUS.get(role_prompt.role_id, role_prompt.title)
    return (
        f"Role: {role_prompt.title} ({role_prompt.role_id}).\n"
        f"Question: {focus}\n"
        "Return strict JSON only. No markdown.\n"
        "Use only the supplied evidence context. Do not invent missing visual facts.\n"
        "Use risk only when the supplied evidence contains a specific problem.\n"
        "Use not_enough_evidence when this role lacks concrete supporting facts.\n"
        "Finding must be 8-22 words. Evidence should contain 1-3 concrete supplied facts.\n"
        f"Output schema example: {json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n"
        "Supplied evidence context:\n"
        + json.dumps(focused_context, ensure_ascii=False, separators=(",", ":"))
    )


def build_role_vision_prompt(role_prompt: EvidenceRolePrompt, *, photo_context: dict[str, Any] | None = None) -> str:
    trace_context = {
        "photo_id": (photo_context or {}).get("photo_id"),
        "photo_type": (photo_context or {}).get("photo_type"),
        "project_id": (photo_context or {}).get("project_id"),
        "employee_id": (photo_context or {}).get("employee_id"),
        "note": (photo_context or {}).get("note") or "",
    }
    if role_prompt.role_id == "visible_people_count":
        return (
            "Count visible people in the attached image for workforce analytics.\n"
            "Return only valid JSON. No markdown. No explanation outside JSON.\n"
            "Count only visible people with a clear head, body, or limb clue. Do not infer hidden people.\n"
            "If people are cropped, partially hidden, or crowded, provide the best count and set is_estimate=true.\n"
            "If no people are visible, numeric_value must be 0.\n"
            "Use this exact JSON shape:\n"
            '{"role_id":"visible_people_count","priority":"P0","status":"pass|not_enough_evidence",'
            '"finding":"","evidence":[],"missing_evidence":[],"recommended_action":"",'
            '"confidence":"high|medium|low","should_retry":false,'
            '"numeric_value":0,"count_range":"","is_estimate":false}\n'
            "Metadata context:\n"
            + json.dumps(trace_context, ensure_ascii=False, separators=(",", ":"))
        )
    focus = ROLE_FOCUS.get(role_prompt.role_id, role_prompt.title)
    default_result = {
        "status": "not_enough_evidence",
        "finding": f"Not enough visible evidence for {role_prompt.title}.",
        "evidence": [],
        "missing_evidence": ["Clear visible evidence for this role is not shown."],
        "recommended_action": "",
        "confidence": "low",
        "should_retry": False,
        "numeric_value": None,
        "count_range": "",
        "is_estimate": False,
    }
    return (
        f"Role: {role_prompt.title} ({role_prompt.role_id}).\n"
        f"Question: {focus}\n"
        "Use only visible image evidence. Metadata is trace context only.\n"
        "Return only valid JSON. No markdown. No text outside JSON.\n"
        "Start from this JSON. Keep it if evidence is not visible. Replace it only when the image clearly supports this role:\n"
        + json.dumps(default_result, ensure_ascii=False, separators=(",", ":"))
        + "\nIf useful evidence is visible, set status to pass, warning, or risk and add 1-3 concrete visible evidence strings."
        + "\nUse risk only for a specific visible problem. Keep finding under 22 words."
        + "\nTrace context:\n"
        + json.dumps(trace_context, ensure_ascii=False, separators=(",", ":"))
    )


def extract_role_json(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    if not text:
        raise ValueError("Role response was empty")
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
        for match in re.finditer(r"\{", candidate):
            try:
                payload, _ = decoder.raw_decode(candidate[match.start() :])
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
    raise ValueError("Role response did not contain valid JSON")


def normalize_role_result(raw_payload: dict[str, Any], role_prompt: EvidenceRolePrompt) -> dict[str, Any]:
    status = str(raw_payload.get("status") or "not_enough_evidence").strip().lower()
    if status not in {"pass", "warning", "risk", "not_applicable", "not_enough_evidence"}:
        status = "not_enough_evidence"
    confidence = str(raw_payload.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    def _list(value: Any) -> list[str]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = " ".join(str(item or "").strip().split())
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text[:260])
        return normalized[:6]

    finding = " ".join(str(raw_payload.get("finding") or "").strip().split())
    numeric_value = _normalize_count_value(raw_payload)
    count_range = " ".join(str(raw_payload.get("count_range") or "").strip().split())[:64]
    is_estimate = bool(raw_payload.get("is_estimate"))
    if role_prompt.role_id == "visible_people_count" and numeric_value is not None and not finding:
        estimate_text = "estimated " if is_estimate else ""
        range_text = f" ({count_range})" if count_range else ""
        finding = f"Visible people count is {estimate_text}{numeric_value}{range_text}."
        status = "pass"
        confidence = confidence if confidence in {"high", "medium"} else "medium"
    if not finding:
        finding = "No reliable role finding was generated."
        status = "not_enough_evidence"
        confidence = "low"

    evidence = _list(raw_payload.get("evidence"))
    if role_prompt.role_id == "visible_people_count" and numeric_value is not None and not evidence:
        evidence = [f"Model counted {numeric_value} visible people in the image."]
    missing_evidence = _list(raw_payload.get("missing_evidence"))
    recommended_action = " ".join(str(raw_payload.get("recommended_action") or "").strip().split())[:320]

    if status in {"risk", "warning"} and not evidence:
        status = "not_enough_evidence"
        confidence = "low"
        missing_evidence = missing_evidence or ["The role did not provide evidence for its finding."]

    if confidence == "low" and not missing_evidence:
        missing_evidence = ["Evidence is insufficient for a high-confidence role conclusion."]

    should_retry = bool(raw_payload.get("should_retry")) and status in {"risk", "warning"}

    return {
        "role_id": role_prompt.role_id,
        "title": role_prompt.title,
        "role": role_prompt.role,
        "priority": role_prompt.priority,
        "category": role_prompt.category,
        "status": status,
        "finding": finding[:320],
        "evidence": evidence,
        "missing_evidence": missing_evidence,
        "recommended_action": recommended_action,
        "confidence": confidence,
        "should_retry": should_retry,
        "numeric_value": numeric_value,
        "count_range": count_range,
        "is_estimate": is_estimate,
    }


def _normalize_count_value(raw_payload: dict[str, Any]) -> int | None:
    for key in ("numeric_value", "visible_person_count", "people_count", "count"):
        value = raw_payload.get(key)
        if isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        return max(0, min(parsed, 200))
    return None
