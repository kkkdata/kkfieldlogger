from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import requests
from requests import RequestException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import utc_now
from app.models import (
    AIAnalysisLog,
    AIAnalysisStatus,
    AIAnalysisType,
    LensDefinition,
    LensDefinitionVersion,
    Photo,
    PhotoLensObservation,
    PhotoLensObservationRun,
)
from app.services.ai_pipeline import AIBackendNode, OLLAMA_TYPE, _build_ollama_endpoint, _read_photo_payload


LENS_MODEL_DEFAULT = "llama3.2-vision:11b"
LENS_VERSION = "v2"
LENS_PROMPT_VERSION = "photo_lens_v2"
VALID_STATES = {"observed", "not_observed", "uncertain"}
VALID_CONFIDENCE = {"low", "medium", "high", "unknown"}
REVIEW_REQUIRED_LENSES = {"defect_surface", "remediation_evidence", "concealed_work", "safety_observables"}
FORBIDDEN_LENS_TERMS = (
    "responsible",
    "blame",
    "fault",
    "must",
    "urgent",
    "priority",
    "recommend",
    "should",
    "unsafe",
    "violation",
    "pass",
    "fail",
    "绩效",
    "责任",
    "必须",
    "应该",
    "紧急",
    "优先",
    "违规",
    "不合格",
)


CORE_LENS_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "lens_key": "scene_context",
        "display_name": "Scene Context",
        "category": "field_memory",
        "priority": 100,
        "description": "General factual scene memory for search and later recall.",
        "items": ["environment_type", "primary_subjects", "visibility_quality", "capture_angle"],
        "intent": "Describe visible scene context, major subjects, visibility quality, and capture angle.",
    },
    {
        "lens_key": "spatial_anchor",
        "display_name": "Spatial Anchor",
        "category": "location_memory",
        "priority": 95,
        "description": "Visible location and structural cues without claiming correctness.",
        "items": ["level_or_floor", "room_or_zone_label", "structure_element", "relative_position_cues", "orientation_cues"],
        "intent": "Extract visible location cues, structural elements, and orientation clues.",
    },
    {
        "lens_key": "visible_text",
        "display_name": "Visible Text",
        "category": "text_memory",
        "priority": 90,
        "description": "Readable text, labels, serial plates, tags, and document-like content.",
        "items": ["text_blocks", "document_detected", "document_type", "identifiers_readable", "language"],
        "intent": "Transcribe only legible text and identify document-like objects when visible.",
    },
    {
        "lens_key": "work_progress",
        "display_name": "Work Progress",
        "category": "progress_memory",
        "priority": 85,
        "description": "Observable construction state without percentages or schedule judgment.",
        "items": ["trade_or_work_type", "activity_observed", "completion_state", "visible_milestones", "surface_finish_state"],
        "intent": "Record observable work state, visible milestones, and surface finish state.",
    },
    {
        "lens_key": "defect_surface",
        "display_name": "Defect Surface",
        "category": "quality_memory",
        "priority": 80,
        "description": "Visible defect types and affected elements without severity or cause.",
        "items": ["defect_present", "defect_types", "affected_element", "defect_extent_visible", "measurement_visible"],
        "intent": "List visible surface defects only when directly visible; avoid severity, cause, and corrective actions.",
        "guardrails": "Ordinary seams, joints, shadows, unfinished surfaces, and color changes are not defects. If no unmistakable physical discontinuity is visible, mark every defect field not_observed or uncertain.",
    },
    {
        "lens_key": "tools_equipment",
        "display_name": "Tools and Equipment",
        "category": "asset_memory",
        "priority": 75,
        "description": "Visible tools/equipment and observable state for later finding.",
        "items": ["equipment_present", "equipment_types", "identifying_marks", "operational_state_visible", "serial_or_asset_tag", "count_estimate"],
        "intent": "Identify visible tools or equipment, marks, tags, and visible state.",
        "guardrails": "Do not infer tools from construction context. List only distinctly visible objects. Operational state is observed only while equipment is visibly operating; never infer good condition or readiness.",
    },
    {
        "lens_key": "materials_visible",
        "display_name": "Visible Materials",
        "category": "material_memory",
        "priority": 70,
        "description": "Visible materials, packaging, labels, quantity cues, and storage state.",
        "items": ["material_categories", "product_labels_visible", "brand_or_product_name", "quantity_cues", "storage_state", "batch_or_lot_readable"],
        "intent": "Identify visible construction materials and readable product/lot cues only when legible.",
    },
    {
        "lens_key": "remediation_evidence",
        "display_name": "Remediation Evidence",
        "category": "correction_memory",
        "priority": 65,
        "description": "Observable signs of repair or correction activity without sign-off.",
        "items": ["remediation_activity_visible", "remediation_types", "materials_for_repair_visible", "before_after_same_frame", "area_prepared", "defect_still_visible"],
        "intent": "Capture visible signs of corrective work or prepared repair areas without judging acceptability.",
        "guardrails": "Ordinary installation or new work is not remediation. Remediation requires visible evidence of a prior defect and visible repair activity or before/after evidence in the same image; otherwise use not_observed or uncertain.",
    },
    {
        "lens_key": "concealed_work",
        "display_name": "Concealed Work",
        "category": "as_built_memory",
        "priority": 60,
        "description": "Rough-in or pre-cover evidence that may be hidden later.",
        "items": ["concealment_imminent", "rough_in_visible", "covering_material_visible", "access_panel_visible", "markings_visible", "as_built_photo_candidate"],
        "intent": "Record visible rough-in or installed elements before cover-up, without inspection approval.",
    },
    {
        "lens_key": "safety_observables",
        "display_name": "Safety Observables",
        "category": "safety_memory",
        "priority": 55,
        "description": "Visible safety-related objects/states, not safety judgments.",
        "items": ["ppe_visible_on_persons", "barrier_or_guarding_visible", "signage_visible", "housekeeping_observable", "elevation_work_visible"],
        "intent": "Record only visible PPE, barriers, signage, housekeeping cues, and elevation work objects.",
    },
)


@dataclass
class LensRunResult:
    run: PhotoLensObservationRun
    observation: PhotoLensObservation | None
    ai_log: AIAnalysisLog
    validation_errors: list[str]


def seed_core_lenses(db: Session) -> int:
    created_or_updated = 0
    now = utc_now()
    for definition in CORE_LENS_DEFINITIONS:
        lens_id = f"core:{definition['lens_key']}"
        version_id = f"{lens_id}:{LENS_VERSION}"
        lens = db.get(LensDefinition, lens_id)
        if lens is None:
            lens = LensDefinition(
                id=lens_id,
                lens_key=definition["lens_key"],
                scope="core",
                company_id=None,
                project_id=None,
                display_name=definition["display_name"],
                description=definition["description"],
                category=definition["category"],
                is_enabled=True,
                priority=int(definition["priority"]),
                current_version_id=version_id,
                created_at=now,
                updated_at=now,
            )
            db.add(lens)
            created_or_updated += 1
        else:
            lens.display_name = definition["display_name"]
            lens.description = definition["description"]
            lens.category = definition["category"]
            lens.priority = int(definition["priority"])
            lens.is_enabled = True
            lens.current_version_id = version_id
            lens.updated_at = now
            db.add(lens)

        version = db.get(LensDefinitionVersion, version_id)
        prompt_template = _lens_prompt_template(definition)
        schema = _lens_schema(definition["lens_key"], list(definition["items"]))
        validation_rules = {
            "allowed_states": sorted(VALID_STATES),
            "forbidden_terms": list(FORBIDDEN_LENS_TERMS),
            "ai_may_not_emit": ["risk", "priority", "action advice", "responsibility", "employee scoring"],
        }
        if version is None:
            db.add(
                LensDefinitionVersion(
                    id=version_id,
                    lens_id=lens_id,
                    version=LENS_VERSION,
                    prompt_template=prompt_template,
                    output_schema_json=schema,
                    validation_rules_json=validation_rules,
                    model_profile_json={"default_model": LENS_MODEL_DEFAULT, "temperature": 0.0},
                    status="active",
                    created_at=now,
                    created_by="system",
                )
            )
            created_or_updated += 1
        else:
            version.prompt_template = prompt_template
            version.output_schema_json = schema
            version.validation_rules_json = validation_rules
            version.model_profile_json = {"default_model": LENS_MODEL_DEFAULT, "temperature": 0.0}
            version.status = "active"
            db.add(version)
    db.flush()
    return created_or_updated


def enabled_core_lenses(db: Session, *, lens_keys: list[str] | None = None) -> list[tuple[LensDefinition, LensDefinitionVersion]]:
    seed_core_lenses(db)
    stmt = (
        select(LensDefinition)
        .where(LensDefinition.scope == "core", LensDefinition.is_enabled.is_(True))
        .order_by(LensDefinition.priority.desc(), LensDefinition.lens_key.asc())
    )
    if lens_keys:
        stmt = stmt.where(LensDefinition.lens_key.in_(lens_keys))
    lenses = list(db.scalars(stmt))
    results: list[tuple[LensDefinition, LensDefinitionVersion]] = []
    for lens in lenses:
        if not lens.current_version_id:
            continue
        version = db.get(LensDefinitionVersion, lens.current_version_id)
        if version is not None and version.status == "active":
            results.append((lens, version))
    return results


def run_photo_lens_shadow(
    db: Session,
    *,
    app_settings: Settings,
    photo: Photo,
    lens: LensDefinition,
    version: LensDefinitionVersion,
    model: str | None = None,
    backend_url: str | None = None,
    batch_id: str | None = None,
    force: bool = False,
) -> LensRunResult | None:
    if not force:
        existing = db.scalar(
            select(PhotoLensObservation).where(
                PhotoLensObservation.photo_id == photo.id,
                PhotoLensObservation.lens_id == lens.id,
                PhotoLensObservation.lens_version_id == version.id,
                PhotoLensObservation.status == "active",
                PhotoLensObservation.validation_status == "shadow_valid",
            )
        )
        if existing is not None:
            return None

    node = _lens_backend(app_settings, model=model, backend_url=backend_url)
    prompt = _format_lens_prompt(photo=photo, lens=lens, version=version)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    image_base64, mime_type = _read_photo_payload(photo)
    input_hash = hashlib.sha256((str(photo.checksum or "") + ":" + version.id + ":" + prompt_hash).encode("utf-8")).hexdigest()
    run_id = str(uuid4())
    raw_text: str | None = None
    payload: dict[str, Any] | None = None
    errors: list[str] = []
    run_status = "completed"
    try:
        schema = version.output_schema_json if isinstance(version.output_schema_json, dict) else {}
        allowed_keys = schema.get("allowed_item_keys") if isinstance(schema.get("allowed_item_keys"), list) else []
        response_schema = _ollama_response_schema(lens_key=lens.lens_key, allowed_item_keys=[str(key) for key in allowed_keys])
        raw_text = _call_lens_ollama(
            node,
            prompt=prompt,
            image_base64=image_base64,
            response_schema=response_schema,
        )
        payload = _extract_json(raw_text)
        payload = _normalize_lens_payload(payload, lens_key=lens.lens_key)
        errors = _validate_lens_payload(
            payload,
            expected_lens_key=lens.lens_key,
            expected_schema_version=f"{lens.lens_key}:{LENS_VERSION}",
            allowed_item_keys=[str(key) for key in allowed_keys],
        )
    except Exception as exc:
        payload = _fallback_uncertain_payload(lens.lens_key, version, str(exc))
        errors = [f"backend:{node.id}:{exc}"]
        run_status = "failed"

    validation_status = _lens_validation_status(
        lens_key=lens.lens_key,
        run_status=run_status,
        errors=errors,
    )
    payload["provenance"] = {
        "source_kind": "vision_model",
        "photo_id": photo.id,
        "run_id": run_id,
        "lens_version_id": version.id,
        "prompt_version": LENS_PROMPT_VERSION,
        "model": node.model,
        "prompt_hash": prompt_hash,
        "input_hash": input_hash,
        "legacy_caption_used": False,
        "validation_status": validation_status,
    }
    output_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    ai_log = AIAnalysisLog(
        id=str(uuid4()),
        photo_id=photo.id,
        media_asset_id=None,
        batch_id=batch_id,
        analysis_type=AIAnalysisType.lens_extraction,
        prompt_used=prompt,
        model_used=node.model,
        result_data={
            "lens_key": lens.lens_key,
            "lens_version_id": version.id,
            "payload": payload,
            "validation_status": validation_status,
            "validation_errors": errors,
        },
        status=AIAnalysisStatus.active if validation_status == "shadow_valid" else AIAnalysisStatus.rejected,
        created_by="photo_lens_shadow",
    )
    db.add(ai_log)
    db.flush()

    run = PhotoLensObservationRun(
        id=run_id,
        photo_id=photo.id,
        company_id=photo.company_id,
        project_id=photo.project_id,
        employee_id=photo.employee_id,
        lens_id=lens.id,
        lens_version_id=version.id,
        ai_analysis_log_id=ai_log.id,
        batch_id=batch_id,
        model_used=node.model,
        backend_profile_json={"backend_id": node.id, "backend_type": node.type, "url": node.url, "model": node.model},
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        output_hash=output_hash,
        run_status=run_status,
        validation_status=validation_status,
        result_json=payload,
        raw_output_ref=None,
        error_json={"errors": errors} if errors else None,
    )
    db.add(run)
    db.flush()

    observation: PhotoLensObservation | None = None
    if validation_status == "shadow_valid":
        observation = _activate_observation(db, photo=photo, lens=lens, version=version, run=run, payload=payload)
    return LensRunResult(run=run, observation=observation, ai_log=ai_log, validation_errors=errors)


def backfill_photo_lenses(
    db: Session,
    *,
    app_settings: Settings,
    company_id: str | None,
    project_id: str | None,
    lens_keys: list[str] | None,
    limit: int,
    model: str | None,
    backend_url: str | None,
    sleep_seconds: float | None = None,
    force: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    batch_id = batch_id or str(uuid4())
    lenses = enabled_core_lenses(db, lens_keys=lens_keys)
    photo_stmt = select(Photo).where(Photo.deleted.is_(False), Photo.soft_deleted_at.is_(None))
    if company_id:
        photo_stmt = photo_stmt.where(Photo.company_id == company_id)
    if project_id:
        photo_stmt = photo_stmt.where(Photo.project_id == project_id)
    photo_stmt = photo_stmt.order_by(Photo.created_at.desc(), Photo.id.desc()).limit(max(1, int(limit)))
    photos = list(db.scalars(photo_stmt))
    counters = {
        "photos_selected": len(photos),
        "lenses_selected": len(lenses),
        "runs_created": 0,
        "skipped_existing": 0,
        "shadow_valid": 0,
        "shadow_fallback_valid": 0,
        "shadow_invalid": 0,
    }
    pause = float(app_settings.lens_backfill_sleep_seconds if sleep_seconds is None else sleep_seconds)

    for photo in photos:
        for lens, version in lenses:
            result = run_photo_lens_shadow(
                db,
                app_settings=app_settings,
                photo=photo,
                lens=lens,
                version=version,
                model=model,
                backend_url=backend_url,
                batch_id=batch_id,
                force=force,
            )
            if result is None:
                counters["skipped_existing"] += 1
                continue
            counters["runs_created"] += 1
            counters[result.run.validation_status] = int(counters.get(result.run.validation_status, 0)) + 1
            db.commit()
            if pause > 0:
                time.sleep(pause)
    counters["batch_id"] = batch_id
    return counters


def _lens_backend(app_settings: Settings, *, model: str | None, backend_url: str | None) -> AIBackendNode:
    url = (backend_url or app_settings.lens_vision_backend_url or "").strip()
    if not url:
        url = (app_settings.expression_text_backend_url or "").strip()
    if not url:
        url = "http://127.0.0.1:11434"
    selected_model = (model or app_settings.lens_vision_backend_model or LENS_MODEL_DEFAULT).strip()
    return AIBackendNode(
        id=f"photo-lens-ollama:{selected_model}",
        type=OLLAMA_TYPE,
        url=url,
        model=selected_model,
        enabled=True,
    )


def _call_lens_ollama(
    node: AIBackendNode,
    *,
    prompt: str,
    image_base64: str,
    response_schema: dict[str, Any],
) -> str:
    response = requests.post(
        _build_ollama_endpoint(node),
        json={
            "model": node.model,
            "prompt": prompt,
            "images": [image_base64],
            "stream": False,
            "format": response_schema,
            "options": {"temperature": 0.0, "top_p": 0.75, "repeat_penalty": 1.05, "num_predict": 1600},
        },
        timeout=180,
    )
    try:
        response.raise_for_status()
    except RequestException as exc:
        raise RuntimeError(str(exc)) from exc
    payload = response.json()
    text = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama lens response missing text")
    return text.strip()


def _lens_prompt_template(definition: dict[str, Any]) -> str:
    items = ", ".join(definition["items"])
    response_schema = _ollama_response_schema(
        lens_key=str(definition["lens_key"]),
        allowed_item_keys=[str(item) for item in definition["items"]],
    )
    return "\n".join(
        [
            "You extract visible construction-photo observations for one fixed lens.",
            f"Lens: {definition['lens_key']}",
            f"Intent: {definition['intent']}",
            f"Lens-specific guardrails: {definition.get('guardrails') or 'No additional guardrails.'}",
            "The server selected this lens. Do not classify the photo or choose another lens.",
            "Return only facts visible in the image. If something is not visible, use state not_observed. If unclear, use uncertain.",
            "Do not output risk, priority, action advice, responsibility, blame, compliance verdicts, pass/fail, schedule impact, productivity, or employee scoring.",
            f"Allowed item keys: {items}",
            "Return exactly one item for every allowed item key, in the listed order. Do not omit, duplicate, rename, or add item keys.",
            "Return valid JSON only with this shape:",
            '{"lens_key":"...", "summary":"short factual summary", "items":[{"key":"allowed_key","state":"observed|not_observed|uncertain","value":"short value or empty","confidence":"low|medium|high|unknown","evidence":"short visible evidence"}], "limitations":["..."]}',
            "Response JSON Schema:",
            json.dumps(response_schema, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def _ollama_response_schema(*, lens_key: str, allowed_item_keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "lens_key", "summary", "items", "limitations"],
        "properties": {
            "schema_version": {"type": "string", "const": f"{lens_key}:{LENS_VERSION}"},
            "lens_key": {"type": "string", "const": lens_key},
            "summary": {"type": "string", "maxLength": 320},
            "items": {
                "type": "array",
                "minItems": len(allowed_item_keys),
                "maxItems": len(allowed_item_keys),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "state", "value", "confidence", "evidence"],
                    "properties": {
                        "key": {"type": "string", "enum": allowed_item_keys},
                        "state": {"type": "string", "enum": sorted(VALID_STATES)},
                        "value": {"type": "string", "maxLength": 160},
                        "confidence": {"type": "string", "enum": sorted(VALID_CONFIDENCE)},
                        "evidence": {"type": "string", "maxLength": 240},
                    },
                },
            },
            "limitations": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "maxLength": 180},
            },
        },
    }


def _lens_schema(lens_key: str, items: list[str]) -> dict[str, Any]:
    return {
        "schema_version": f"{lens_key}:{LENS_VERSION}",
        "required_keys": ["lens_key", "summary", "items", "limitations"],
        "allowed_item_keys": items,
        "require_all_item_keys": True,
        "allow_additional_item_keys": False,
        "allowed_states": sorted(VALID_STATES),
        "allowed_confidence": sorted(VALID_CONFIDENCE),
    }


def _format_lens_prompt(*, photo: Photo, lens: LensDefinition, version: LensDefinitionVersion) -> str:
    context = {
        "photo_id": photo.id,
        "photo_type": photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type),
        "company_id": photo.company_id,
        "project_id": photo.project_id,
        "employee_id": photo.employee_id,
        "captured_at_utc": str(photo.captured_at_utc),
        "note": photo.note,
    }
    return version.prompt_template + "\n\nPhoto metadata:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)


def _extract_json(raw_text: str) -> dict[str, Any]:
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
        raise ValueError("Lens response must be a JSON object")
    return payload


def _normalize_lens_payload(payload: dict[str, Any], *, lens_key: str) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    normalized_items = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        state = str(item.get("state") or "uncertain").strip().lower()
        confidence = str(item.get("confidence") or "unknown").strip().lower()
        normalized_items.append(
            {
                "key": key,
                "state": state if state in VALID_STATES else "uncertain",
                "value": _short_text(item.get("value"), 160),
                "confidence": confidence if confidence in VALID_CONFIDENCE else "unknown",
                "evidence": _short_text(item.get("evidence"), 240),
            }
        )
    limitations = payload.get("limitations") if isinstance(payload.get("limitations"), list) else []
    return {
        "schema_version": f"{lens_key}:{LENS_VERSION}",
        "lens_key": _short_text(payload.get("lens_key"), 80),
        "summary": _short_text(payload.get("summary"), 320),
        "items": normalized_items,
        "limitations": [_short_text(item, 180) for item in limitations[:8] if _short_text(item, 180)],
    }


def _validate_lens_payload(
    payload: dict[str, Any],
    *,
    expected_lens_key: str,
    expected_schema_version: str,
    allowed_item_keys: list[str],
) -> list[str]:
    errors: list[str] = []
    if payload.get("lens_key") != expected_lens_key:
        errors.append("lens_key_mismatch")
    if payload.get("schema_version") != expected_schema_version:
        errors.append("schema_version_mismatch")
    if not isinstance(payload.get("items"), list):
        errors.append("items_missing")
        return errors
    if not str(payload.get("summary") or "").strip():
        errors.append("summary_missing")
    combined = json.dumps(payload, ensure_ascii=False).casefold()
    for term in FORBIDDEN_LENS_TERMS:
        if term.casefold() in combined:
            errors.append(f"forbidden_term:{term}")
    seen_keys: list[str] = []
    allowed = set(allowed_item_keys)
    for index, item in enumerate(payload.get("items") or []):
        if not isinstance(item, dict):
            errors.append(f"items[{index}]:not_object")
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            errors.append(f"items[{index}]:key_missing")
        elif key not in allowed:
            errors.append(f"items[{index}]:unknown_key:{key}")
        elif key in seen_keys:
            errors.append(f"items[{index}]:duplicate_key:{key}")
        seen_keys.append(key)
        if item.get("state") not in VALID_STATES:
            errors.append(f"items[{index}]:invalid_state")
        if item.get("confidence") not in VALID_CONFIDENCE:
            errors.append(f"items[{index}]:invalid_confidence")
        if item.get("state") == "observed" and not str(item.get("evidence") or "").strip():
            errors.append(f"items[{index}]:observed_missing_evidence")
    for key in allowed_item_keys:
        if key not in seen_keys:
            errors.append(f"item_missing:{key}")
    return errors


def _activate_observation(
    db: Session,
    *,
    photo: Photo,
    lens: LensDefinition,
    version: LensDefinitionVersion,
    run: PhotoLensObservationRun,
    payload: dict[str, Any],
) -> PhotoLensObservation:
    existing = list(
        db.scalars(
            select(PhotoLensObservation).where(
                PhotoLensObservation.photo_id == photo.id,
                PhotoLensObservation.lens_id == lens.id,
                PhotoLensObservation.status == "active",
            )
        )
    )
    supersedes_id = existing[0].id if existing else None
    for row in existing:
        row.status = "superseded"
        db.add(row)
    counts = _state_counts(payload)
    observation = PhotoLensObservation(
        id=str(uuid4()),
        photo_id=photo.id,
        company_id=photo.company_id,
        project_id=photo.project_id,
        employee_id=photo.employee_id,
        lens_id=lens.id,
        lens_version_id=version.id,
        active_run_id=run.id,
        observation_json=payload,
        state_summary=_state_summary(counts),
        observed_count=counts["observed"],
        not_observed_count=counts["not_observed"],
        uncertain_count=counts["uncertain"],
        confidence_level=_confidence_summary(payload),
        validation_status=run.validation_status,
        status="active",
        supersedes_id=supersedes_id,
    )
    db.add(observation)
    db.flush()
    return observation


def _state_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts = {"observed": 0, "not_observed": 0, "uncertain": 0}
    for item in payload.get("items") or []:
        if isinstance(item, dict) and item.get("state") in counts:
            counts[str(item["state"])] += 1
    return counts


def _lens_validation_status(*, lens_key: str, run_status: str, errors: list[str]) -> str:
    if run_status != "completed" or errors:
        return "shadow_invalid"
    if lens_key in REVIEW_REQUIRED_LENSES:
        return "shadow_review_required"
    return "shadow_valid"


def _state_summary(counts: dict[str, int]) -> str:
    if counts["observed"] and not counts["uncertain"] and not counts["not_observed"]:
        return "observed"
    if counts["not_observed"] and not counts["observed"] and not counts["uncertain"]:
        return "not_observed"
    if counts["uncertain"] and not counts["observed"]:
        return "uncertain"
    return "mixed"


def _confidence_summary(payload: dict[str, Any]) -> str:
    values = [item.get("confidence") for item in payload.get("items") or [] if isinstance(item, dict)]
    if "high" in values:
        return "high"
    if "medium" in values:
        return "medium"
    if "low" in values:
        return "low"
    return "unknown"


def _short_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit]


def _fallback_uncertain_payload(lens_key: str, version: LensDefinitionVersion, message: str) -> dict[str, Any]:
    schema = version.output_schema_json if isinstance(version.output_schema_json, dict) else {}
    keys = schema.get("allowed_item_keys") if isinstance(schema.get("allowed_item_keys"), list) else []
    return {
        "schema_version": f"{lens_key}:{LENS_VERSION}",
        "lens_key": lens_key,
        "summary": "Lens extraction did not return parseable structured observations.",
        "items": [
            {
                "key": str(key),
                "state": "uncertain",
                "value": "",
                "confidence": "low",
                "evidence": "Model output was not parseable for this lens.",
            }
            for key in keys
            if str(key).strip()
        ],
        "limitations": [f"lens_extraction_unparsed:{message[:160]}"],
    }
