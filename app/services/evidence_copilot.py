from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import to_utc_iso, utc_now
from app.models import (
    AnnotationStatus,
    AnnotationVisibility,
    CopilotConversation,
    CopilotMessage,
    CopilotMessageSource,
    CopilotMessageStatus,
    Employee,
    EvidenceObservation,
    MediaAnnotation,
    Photo,
    PhotoType,
    ProgressReport,
    ProgressReportStatus,
    Project,
    ReceiptFact,
    User,
)
from app.services.ai_pipeline import (
    AIBackendNode,
    GEMINI_TYPE,
    OLLAMA_TYPE,
    generate_markdown_completion,
    resolve_ai_backends_for_tenant,
    semantic_search_photos,
)
from app.services.audit import log_audit
from app.services.photos import photo_media_kind, serialize_photo

logger = get_logger("kkfieldlogger.evidence_copilot")

COPILOT_CHAT_TASK = "copilot_chat_generation"
COPILOT_MODE_STANDARD = "standard"
COPILOT_MODE_DEEP = "deep"
COPILOT_MODE_EXECUTIVE = "executive"
COPILOT_ALLOWED_MODES = {COPILOT_MODE_STANDARD, COPILOT_MODE_DEEP, COPILOT_MODE_EXECUTIVE}
COPILOT_INTENT_GENERAL = "general"
COPILOT_INTENT_SAFETY = "safety"
COPILOT_INTENT_QUALITY = "quality"
COPILOT_INTENT_INVENTORY = "inventory"
COPILOT_INTENT_WORKFORCE = "workforce"
COPILOT_INTENT_FINANCE = "finance"
COPILOT_MAX_SOURCES = 12
COPILOT_RECENT_DAYS = 30

_FINANCE_KEYWORDS = (
    "receipt",
    "invoice",
    "fuel",
    "gas",
    "diesel",
    "gasoline",
    "corruption",
    "fraud",
    "expense",
    "reimbursement",
    "receipt",
    "加油",
    "汽油",
    "柴油",
    "发票",
    "收据",
    "报销",
    "腐败",
    "fraude",
    "recibo",
    "gasolina",
    "diésel",
    "corrupción",
)
_SAFETY_KEYWORDS = (
    "safety",
    "risk",
    "hazard",
    "helmet",
    "ppe",
    "guardrail",
    "water",
    "flood",
    "slip",
    "fall",
    "积水",
    "安全",
    "风险",
    "安全帽",
    "护栏",
    "riesgo",
    "seguridad",
    "casco",
)
_QUALITY_KEYWORDS = (
    "quality",
    "defect",
    "crack",
    "issue",
    "rework",
    "瑕疵",
    "质量",
    "返工",
    "缺陷",
    "calidad",
    "defecto",
)
_INVENTORY_KEYWORDS = (
    "inventory",
    "material",
    "stock",
    "tool",
    "equipment",
    "screwdriver",
    "库存",
    "材料",
    "工具",
    "物资",
    "inventario",
    "materiales",
    "herramienta",
    "destornillador",
)
_WORKFORCE_KEYWORDS = (
    "employee",
    "worker",
    "onsite",
    "active",
    "attendance",
    "crew",
    "staff",
    "员工",
    "上岗",
    "在岗",
    "现场",
    "trabajador",
    "empleado",
    "asistencia",
)


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _truncate(value: Any, limit: int) -> str:
    cleaned = _normalized_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _normalize_key(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", _normalized_text(value).casefold())
    # Column evidence_observations.normalized_key is VARCHAR(160).
    return normalized.strip("_")[:160].rstrip("_")


def _parse_float(value: Any) -> float | None:
    try:
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return None


def _severity_for_text(value: str) -> str:
    normalized = value.casefold()
    if any(marker in normalized for marker in ("critical", "urgent", "high risk", "严重", "紧急")):
        return "high"
    if any(marker in normalized for marker in ("risk", "warning", "hazard", "defect", "瑕疵", "风险")):
        return "medium"
    return "low"


def _confidence_from_ai_level(value: Any) -> float | None:
    normalized = _normalized_text(value).casefold()
    if normalized == "high":
        return 0.86
    if normalized == "medium":
        return 0.68
    if normalized == "low":
        return 0.42
    return None


def _iter_normalized_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _normalized_text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _receipt_fact_value(facts: dict[str, Any], key: str) -> str | None:
    value = _normalized_text(facts.get(key))
    return value or None


def _severity_for_dimension_status(status: Any) -> str:
    normalized = _normalized_text(status).casefold()
    if normalized == "risk":
        return "high"
    if normalized in {"warning", "not_enough_evidence"}:
        return "medium"
    return "low"


def _conversation_title_from_prompt(prompt: str) -> str:
    cleaned = _truncate(prompt, 72)
    return cleaned or "New Copilot Conversation"


def _message_source_url(source_type: str, source_id: str) -> str | None:
    if source_type == "photo" and source_id.isdigit():
        return f"/portal/photos?focus_photo_id={source_id}"
    if source_type == "progress_report":
        return f"/portal/reports/progress/{source_id}"
    if source_type == "invoice_photo" and source_id.isdigit():
        return f"/portal/invoices?focus_photo_id={source_id}"
    return None


def list_copilot_backend_options(db: Session, app_settings: Settings, company_id: str) -> list[dict[str, str]]:
    backends, config_source = resolve_ai_backends_for_tenant(db, app_settings, company_id)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for backend in backends:
        if not backend.enabled or backend.id in seen:
            continue
        seen.add(backend.id)
        items.append(
            {
                "id": backend.id,
                "label": f"{backend.type.upper()} · {backend.model}",
                "type": backend.type,
                "model": backend.model,
                "source": config_source,
            }
        )
    return items


def serialize_copilot_conversation(conversation: CopilotConversation, latest_message: CopilotMessage | None = None) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "project_id": conversation.project_id,
        "title": conversation.title,
        "preferred_backend_id": conversation.preferred_backend_id,
        "preferred_mode": conversation.preferred_mode,
        "created_at": to_utc_iso(conversation.created_at),
        "updated_at": to_utc_iso(conversation.updated_at),
        "latest_message_excerpt": _truncate(latest_message.content_text, 160) if latest_message is not None else None,
        "latest_message_status": latest_message.status.value if latest_message is not None else None,
    }


def serialize_copilot_message_sources(message: CopilotMessage, db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CopilotMessageSource)
            .where(CopilotMessageSource.message_id == message.id)
            .order_by(CopilotMessageSource.relevance_score.desc(), CopilotMessageSource.id.asc())
        )
    )
    return [
        {
            "id": row.id,
            "source_type": row.source_type,
            "source_id": row.source_id,
            "source_title": row.source_title,
            "source_url": row.source_url,
            "relevance_score": row.relevance_score,
            "metadata_json": row.metadata_json,
        }
        for row in rows
    ]


def serialize_copilot_message(message: CopilotMessage, db: Session) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "role": message.role,
        "status": message.status.value,
        "language": message.language,
        "content_text": message.content_text,
        "selected_backend_id": message.selected_backend_id,
        "selected_backend_type": message.selected_backend_type,
        "selected_model": message.selected_model,
        "context_summary_json": message.context_summary_json,
        "error_message": message.error_message,
        "created_at": to_utc_iso(message.created_at),
        "updated_at": to_utc_iso(message.updated_at),
        "completed_at": to_utc_iso(message.completed_at),
        "sources": serialize_copilot_message_sources(message, db),
    }


def _valid_project_id(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text == "invoice":
        return None
    return text


def _existing_company_project_id(db: Session, *, company_id: str, project_id: str | None) -> str | None:
    normalized = _valid_project_id(project_id)
    if not normalized:
        return None
    return db.scalar(
        select(Project.project_id).where(
            Project.project_id == normalized,
            Project.company_id == company_id,
        )
    )


def _resolve_photo_evidence_project_id(db: Session, photo: Photo) -> str | None:
    photo_project_id = _existing_company_project_id(db, company_id=photo.company_id, project_id=photo.project_id)
    employee_project_id = db.scalar(
        select(Employee.project_id).where(
            Employee.company_id == photo.company_id,
            Employee.employee_id == photo.employee_id,
            Employee.project_id.is_not(None),
        )
    )
    employee_project_id = _existing_company_project_id(
        db,
        company_id=photo.company_id,
        project_id=employee_project_id,
    )
    if photo_project_id and employee_project_id and photo_project_id != employee_project_id:
        return None
    return photo_project_id or employee_project_id


def sync_photo_evidence_bundle(db: Session, photo: Photo) -> None:
    db.execute(delete(EvidenceObservation).where(EvidenceObservation.photo_id == photo.id))
    observations: list[EvidenceObservation] = []
    now = utc_now()
    observed_at = _parse_datetime(photo.captured_at_utc) or _parse_datetime(photo.created_at) or now
    tag_json = photo.tag_json if isinstance(photo.tag_json, dict) else {}
    evidence_project_id = _resolve_photo_evidence_project_id(db, photo)

    def add_observation(
        *,
        source_kind: str,
        observation_type: str,
        title: str,
        content_text: str,
        severity: str,
        confidence: float | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        normalized_content = _normalized_text(content_text)
        if not normalized_content:
            return
        observations.append(
            EvidenceObservation(
                id=str(uuid4()),
                tenant_id=photo.tenant_id or photo.company_id,
                company_id=photo.company_id,
                project_id=evidence_project_id,
                photo_id=photo.id,
                media_asset_id=photo_media_asset_id(photo) if photo_media_kind(photo) == "video" else None,
                source_kind=source_kind,
                observation_type=observation_type,
                normalized_key=_normalize_key(title or content_text),
                title=_truncate(title, 255) or observation_type.replace("_", " ").title(),
                content_text=normalized_content,
                severity=severity,
                confidence=confidence,
                observed_at=observed_at,
                metadata_json=metadata_json,
            )
        )

    summary_text = _normalized_text(tag_json.get("ai_summary"))
    ai_confidence = _confidence_from_ai_level(tag_json.get("confidence_level"))
    if summary_text:
        add_observation(
            source_kind="ai",
            observation_type="summary",
            title="AI Summary",
            content_text=summary_text,
            severity=_severity_for_text(summary_text),
            confidence=ai_confidence or 0.7,
            metadata_json={"confidence_level": tag_json.get("confidence_level")},
        )

    ai_list_observation_specs = (
        ("labels", "label", "Label", "low", 0.66),
        ("visible_objects", "visible_object", "Visible Object", "low", 0.66),
        ("materials", "material", "Material", "low", 0.66),
        ("equipment", "equipment", "Equipment", "low", 0.66),
        ("people_ppe", "people_ppe", "People/PPE", "medium", 0.64),
        ("defects", "defect", "Defect", None, 0.78),
        ("safety_observations", "safety", "Safety Observation", None, 0.74),
        ("quality_observations", "quality", "Quality Observation", None, 0.72),
        ("inventory_observations", "inventory", "Inventory Observation", "low", 0.68),
        ("water_or_housekeeping_observations", "housekeeping", "Water/Housekeeping", None, 0.72),
        ("financial_anomaly_flags", "financial_anomaly", "Financial Anomaly", None, 0.76),
        ("missing_evidence", "missing_evidence", "Missing Evidence", "medium", 0.7),
        ("recommended_actions", "recommended_action", "Recommended Action", "medium", 0.62),
        ("evidence_limitations", "evidence_limitation", "Evidence Limitation", "medium", 0.68),
    )
    for field_name, observation_type, title_prefix, default_severity, default_confidence in ai_list_observation_specs:
        for item in _iter_normalized_list(tag_json.get(field_name)):
            add_observation(
                source_kind="ai",
                observation_type=observation_type,
                title=f"{title_prefix}: {item}",
                content_text=item,
                severity=default_severity or _severity_for_text(item),
                confidence=ai_confidence or default_confidence,
                metadata_json={"ai_field": field_name, "confidence_level": tag_json.get("confidence_level")},
            )

    scene_type = _normalized_text(tag_json.get("scene_type"))
    if scene_type:
        add_observation(
            source_kind="ai",
            observation_type="scene_type",
            title=f"Scene Type: {scene_type}",
            content_text=scene_type,
            severity="low",
            confidence=ai_confidence or 0.62,
            metadata_json={"ai_field": "scene_type", "confidence_level": tag_json.get("confidence_level")},
        )

    receipt_facts = tag_json.get("receipt_facts") if isinstance(tag_json.get("receipt_facts"), dict) else {}
    if receipt_facts:
        for key, value in receipt_facts.items():
            fact_text = _normalized_text(value)
            if not fact_text:
                continue
            add_observation(
                source_kind="ai",
                observation_type=f"receipt_{_normalize_key(key)}",
                title=f"Receipt {str(key).replace('_', ' ').title()}",
                content_text=fact_text,
                severity="low",
                confidence=ai_confidence or 0.7,
                metadata_json={"ai_field": "receipt_facts", "receipt_key": key},
            )

    for dimension in tag_json.get("evidence_dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        status = _normalized_text(dimension.get("status"))
        if not status or status == "not_applicable":
            continue
        title = _normalized_text(dimension.get("title")) or _normalized_text(dimension.get("dimension")) or "AI Dimension"
        finding = _normalized_text(dimension.get("finding"))
        evidence_items = _iter_normalized_list(dimension.get("evidence"))
        missing_items = _iter_normalized_list(dimension.get("missing_evidence"))
        action_text = _normalized_text(dimension.get("recommended_action"))
        content_parts = [f"status={status}"]
        if finding:
            content_parts.append(finding)
        if evidence_items:
            content_parts.append("Evidence: " + "; ".join(evidence_items[:3]))
        if missing_items:
            content_parts.append("Missing: " + "; ".join(missing_items[:3]))
        if action_text:
            content_parts.append("Action: " + action_text)
        confidence_value = _confidence_from_ai_level(dimension.get("confidence")) or ai_confidence
        add_observation(
            source_kind="ai_dimension",
            observation_type=f"dimension_{_normalize_key(dimension.get('dimension'))}",
            title=f"{_normalized_text(dimension.get('priority')) or 'P?'} {title}",
            content_text=" | ".join(content_parts),
            severity=_severity_for_dimension_status(status),
            confidence=confidence_value,
            metadata_json={
                "dimension": dimension.get("dimension"),
                "role": dimension.get("role"),
                "priority": dimension.get("priority"),
                "category": dimension.get("category"),
                "status": status,
                "requires_recheck": bool(dimension.get("requires_recheck")),
                "idle_only": bool(dimension.get("idle_only")),
            },
        )

    evidence_engine = tag_json.get("evidence_engine") if isinstance(tag_json.get("evidence_engine"), dict) else {}
    engine_summary = evidence_engine.get("summary") if isinstance(evidence_engine.get("summary"), dict) else {}
    pollution_flags = _iter_normalized_list(engine_summary.get("pollution_flags"))
    if pollution_flags:
        add_observation(
            source_kind="ai_inspector",
            observation_type="ai_pollution_flag",
            title="AI Inspector Flags",
            content_text="; ".join(pollution_flags),
            severity="medium",
            confidence=0.72,
            metadata_json={"evidence_engine_version": evidence_engine.get("version")},
        )

    ai_role_reviews = tag_json.get("ai_role_reviews") if isinstance(tag_json.get("ai_role_reviews"), dict) else {}
    for role_result in ai_role_reviews.get("results") or []:
        if not isinstance(role_result, dict):
            continue
        status = _normalized_text(role_result.get("status"))
        if not status or status == "not_applicable":
            continue
        title = _normalized_text(role_result.get("title")) or _normalized_text(role_result.get("role_id")) or "AI Role"
        finding = _normalized_text(role_result.get("finding"))
        evidence_items = _iter_normalized_list(role_result.get("evidence"))
        missing_items = _iter_normalized_list(role_result.get("missing_evidence"))
        action_text = _normalized_text(role_result.get("recommended_action"))
        content_parts = [f"status={status}"]
        if role_result.get("role_id") == "visible_people_count" and role_result.get("numeric_value") is not None:
            count_text = f"Visible people count: {role_result.get('numeric_value')}"
            count_range = _normalized_text(role_result.get("count_range"))
            if count_range:
                count_text += f" ({count_range})"
            if role_result.get("is_estimate"):
                count_text += " estimated"
            content_parts.append(count_text)
        if finding:
            content_parts.append(finding)
        if evidence_items:
            content_parts.append("Evidence: " + "; ".join(evidence_items[:3]))
        if missing_items:
            content_parts.append("Missing: " + "; ".join(missing_items[:3]))
        if action_text:
            content_parts.append("Action: " + action_text)
        add_observation(
            source_kind="ai_role",
            observation_type=f"role_{_normalize_key(role_result.get('role_id'))}",
            title=f"{_normalized_text(role_result.get('priority')) or 'P?'} {title}",
            content_text=" | ".join(content_parts),
            severity=_severity_for_dimension_status(status),
            confidence=_confidence_from_ai_level(role_result.get("confidence")) or ai_confidence,
            metadata_json={
                "role_id": role_result.get("role_id"),
                "role": role_result.get("role"),
                "priority": role_result.get("priority"),
                "category": role_result.get("category"),
                "status": status,
                "should_retry": bool(role_result.get("should_retry")),
                "numeric_value": role_result.get("numeric_value"),
                "count_range": role_result.get("count_range"),
                "is_estimate": bool(role_result.get("is_estimate")),
                "role_engine_version": ai_role_reviews.get("version"),
            },
        )

    if photo.note:
        add_observation(
            source_kind="operator",
            observation_type="note",
            title="Field Note",
            content_text=photo.note,
            severity="low",
        )

    if photo.location:
        add_observation(
            source_kind="capture",
            observation_type="location",
            title="Capture Location",
            content_text=photo.location,
            severity="low",
            metadata_json={"gps": photo.gps, "gps_lat": photo.gps_lat, "gps_lon": photo.gps_lon},
        )

    if photo.gps or photo.gps_lat is not None or photo.gps_lon is not None:
        gps_text = f"{photo.gps or ''} lat={photo.gps_lat or 'unknown'} lon={photo.gps_lon or 'unknown'}".strip()
        add_observation(
            source_kind="capture",
            observation_type="gps",
            title="GPS Position",
            content_text=gps_text,
            severity="low",
        )

    for observation in observations:
        db.add(observation)

    if photo.photo_type == PhotoType.invoice:
        note_text = " ".join(filter(None, [summary_text, _normalized_text(photo.note)]))
        amount_match = re.search(r"(?:\\$|USD\\s*)(\\d+(?:\\.\\d{1,2})?)", note_text, re.IGNORECASE)
        gallons_match = re.search(r"(\\d+(?:\\.\\d+)?)\\s*(?:gal|gallon)", note_text, re.IGNORECASE)
        receipt = db.scalar(select(ReceiptFact).where(ReceiptFact.photo_id == photo.id))
        if receipt is None:
            receipt = ReceiptFact(
                id=str(uuid4()),
                tenant_id=photo.tenant_id or photo.company_id,
                company_id=photo.company_id,
                project_id=evidence_project_id,
                photo_id=photo.id,
                employee_id=photo.employee_id,
            )
        elif not _valid_project_id(receipt.project_id) and evidence_project_id:
            receipt.project_id = evidence_project_id
        receipt.summary_text = _truncate(note_text, 1000)
        receipt.vendor_name = _receipt_fact_value(receipt_facts, "vendor") or receipt.vendor_name
        receipt.total_amount = _parse_float(_receipt_fact_value(receipt_facts, "total_amount")) or (
            _parse_float(amount_match.group(1)) if amount_match else receipt.total_amount
        )
        receipt.gallons = _parse_float(_receipt_fact_value(receipt_facts, "fuel_gallons")) or (
            _parse_float(gallons_match.group(1)) if gallons_match else receipt.gallons
        )
        receipt.unit_price = _parse_float(_receipt_fact_value(receipt_facts, "unit_price")) or receipt.unit_price
        receipt.currency_code = _receipt_fact_value(receipt_facts, "currency") or receipt.currency_code
        receipt.has_pump_photo = bool(photo.location or photo.gps)
        receipt.facts_json = {
            "ai_receipt_facts": receipt_facts,
            "financial_anomaly_flags": _iter_normalized_list(tag_json.get("financial_anomaly_flags")),
            "missing_evidence": _iter_normalized_list(tag_json.get("missing_evidence")),
            "gps": photo.gps,
            "gps_lat": photo.gps_lat,
            "gps_lon": photo.gps_lon,
            "captured_at_utc": to_utc_iso(photo.captured_at_utc),
        }
        db.add(receipt)


def create_copilot_conversation(
    db: Session,
    *,
    company_id: str,
    tenant_id: str | None,
    project_id: str | None,
    created_by_user_id: int,
    title: str | None,
    preferred_backend_id: str | None,
    preferred_mode: str,
) -> CopilotConversation:
    conversation = CopilotConversation(
        id=str(uuid4()),
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        project_id=project_id,
        created_by_user_id=created_by_user_id,
        title=_truncate(title or "", 255) or "New Copilot Conversation",
        preferred_backend_id=_normalized_text(preferred_backend_id) or None,
        preferred_mode=preferred_mode if preferred_mode in COPILOT_ALLOWED_MODES else COPILOT_MODE_STANDARD,
    )
    db.add(conversation)
    db.flush()
    return conversation


def list_copilot_conversations(
    db: Session,
    *,
    company_id: str,
    project_ids: list[str] | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    stmt = select(CopilotConversation).where(CopilotConversation.company_id == company_id)
    if project_ids:
        stmt = stmt.where(CopilotConversation.project_id.in_(project_ids))
    conversations = list(db.scalars(stmt.order_by(CopilotConversation.updated_at.desc()).limit(limit)))
    message_map: dict[str, CopilotMessage] = {}
    if conversations:
        message_rows = list(
            db.scalars(
                select(CopilotMessage)
                .where(CopilotMessage.conversation_id.in_([item.id for item in conversations]))
                .order_by(CopilotMessage.created_at.desc())
            )
        )
        for message in message_rows:
            message_map.setdefault(message.conversation_id, message)
    return [serialize_copilot_conversation(item, message_map.get(item.id)) for item in conversations]


def get_copilot_conversation_payload(db: Session, conversation: CopilotConversation) -> dict[str, Any]:
    messages = list(
        db.scalars(
            select(CopilotMessage)
            .where(CopilotMessage.conversation_id == conversation.id)
            .order_by(CopilotMessage.created_at.asc(), CopilotMessage.id.asc())
        )
    )
    return {
        "conversation": serialize_copilot_conversation(conversation, messages[-1] if messages else None),
        "messages": [serialize_copilot_message(message, db) for message in messages],
    }


def _intent_from_question(question: str) -> str:
    normalized = question.casefold()
    if any(keyword in normalized for keyword in _FINANCE_KEYWORDS):
        return COPILOT_INTENT_FINANCE
    if any(keyword in normalized for keyword in _SAFETY_KEYWORDS):
        return COPILOT_INTENT_SAFETY
    if any(keyword in normalized for keyword in _QUALITY_KEYWORDS):
        return COPILOT_INTENT_QUALITY
    if any(keyword in normalized for keyword in _INVENTORY_KEYWORDS):
        return COPILOT_INTENT_INVENTORY
    if any(keyword in normalized for keyword in _WORKFORCE_KEYWORDS):
        return COPILOT_INTENT_WORKFORCE
    return COPILOT_INTENT_GENERAL


def _ordered_backends_for_mode(backends: list[AIBackendNode], *, preferred_mode: str, preferred_backend_id: str | None) -> list[AIBackendNode]:
    filtered = [backend for backend in backends if backend.enabled and backend.weight > 0]
    if preferred_backend_id:
        explicit = [backend for backend in filtered if backend.id == preferred_backend_id]
        if explicit:
            return explicit
    preferred_types = [OLLAMA_TYPE, GEMINI_TYPE]
    if preferred_mode in {COPILOT_MODE_DEEP, COPILOT_MODE_EXECUTIVE}:
        preferred_types = [GEMINI_TYPE, OLLAMA_TYPE]

    def sort_key(node: AIBackendNode) -> tuple[int, int, str]:
        try:
            type_rank = preferred_types.index(node.type)
        except ValueError:
            type_rank = len(preferred_types)
        return (type_rank, -int(node.weight), node.id)

    return sorted(filtered, key=sort_key)


def _collect_available_backends(db: Session, app_settings: Settings, company_id: str) -> list[AIBackendNode]:
    nodes, _ = resolve_ai_backends_for_tenant(db, app_settings, company_id)
    deduped: list[AIBackendNode] = []
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            continue
        deduped.append(node)
        seen.add(node.id)
    return deduped


def _candidate_photos(
    db: Session,
    *,
    company_id: str,
    project_id: str | None,
    days: int = COPILOT_RECENT_DAYS,
    limit: int = 240,
) -> list[Photo]:
    cutoff = utc_now() - timedelta(days=days)
    stmt = (
        select(Photo)
        .where(
            Photo.company_id == company_id,
            Photo.deleted.is_(False),
            Photo.created_at >= cutoff,
        )
        .order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
        .limit(limit)
    )
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)
    return list(db.scalars(stmt))


def _recent_employee_activity(db: Session, *, company_id: str, project_id: str | None, days: int = 7) -> list[dict[str, Any]]:
    cutoff = utc_now() - timedelta(days=days)
    stmt = (
        select(Photo.employee_id, func.count(Photo.id))
        .where(Photo.company_id == company_id, Photo.deleted.is_(False), Photo.created_at >= cutoff)
        .group_by(Photo.employee_id)
        .order_by(func.count(Photo.id).desc(), Photo.employee_id.asc())
        .limit(8)
    )
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)
    return [{"employee_id": employee_id, "upload_count": int(count)} for employee_id, count in db.execute(stmt).all()]


def _recent_progress_report_summaries(db: Session, *, company_id: str, project_id: str | None) -> list[dict[str, Any]]:
    stmt = (
        select(ProgressReport)
        .where(ProgressReport.company_id == company_id, ProgressReport.status == ProgressReportStatus.completed)
        .order_by(ProgressReport.completed_at.desc(), ProgressReport.created_at.desc())
        .limit(3)
    )
    if project_id:
        stmt = stmt.where(ProgressReport.project_id == project_id)
    reports = list(db.scalars(stmt))
    items: list[dict[str, Any]] = []
    for report in reports:
        items.append(
            {
                "report_id": report.id,
                "project_id": report.project_id,
                "completed_at": to_utc_iso(report.completed_at),
                "summary": _truncate(report.report_content, 600),
            }
        )
    return items


def _matching_observations(
    db: Session,
    *,
    company_id: str,
    project_id: str | None,
    intent: str,
    candidate_photo_ids: list[int],
    limit: int = 18,
) -> list[EvidenceObservation]:
    stmt = (
        select(EvidenceObservation)
        .where(EvidenceObservation.company_id == company_id)
        .order_by(EvidenceObservation.observed_at.desc(), EvidenceObservation.created_at.desc())
        .limit(limit * 3)
    )
    if project_id:
        stmt = stmt.where(EvidenceObservation.project_id == project_id)
    if candidate_photo_ids:
        stmt = stmt.where(EvidenceObservation.photo_id.in_(candidate_photo_ids))
    rows = list(db.scalars(stmt))
    if intent == COPILOT_INTENT_SAFETY:
        rows = [row for row in rows if row.observation_type in {"defect", "summary", "label"} and _severity_for_text(row.content_text) != "low"]
    elif intent == COPILOT_INTENT_QUALITY:
        rows = [row for row in rows if row.observation_type in {"defect", "summary"}]
    elif intent == COPILOT_INTENT_INVENTORY:
        rows = [row for row in rows if row.observation_type in {"label", "summary", "note"}]
    elif intent == COPILOT_INTENT_WORKFORCE:
        rows = [row for row in rows if row.observation_type in {"location", "summary", "note"}]
    return rows[:limit]


def _matching_receipts(db: Session, *, company_id: str, project_id: str | None, limit: int = 12) -> list[ReceiptFact]:
    stmt = select(ReceiptFact).where(ReceiptFact.company_id == company_id).order_by(ReceiptFact.receipt_timestamp.desc(), ReceiptFact.created_at.desc()).limit(limit)
    if project_id:
        stmt = stmt.where(ReceiptFact.project_id == project_id)
    return list(db.scalars(stmt))


def _serialize_photo_source(photo: Photo, app_settings: Settings) -> dict[str, Any]:
    serialized = serialize_photo(photo, None, app_settings)
    return {
        "source_type": "photo",
        "source_id": str(photo.id),
        "source_title": f"Photo #{photo.id}",
        "source_url": _message_source_url("photo", str(photo.id)),
        "relevance_score": 1.0,
        "metadata_json": {
            "project_id": photo.project_id,
            "employee_id": photo.employee_id,
            "captured_at_utc": serialized.captured_at_utc,
            "image_url": serialized.image_url,
            "thumb_url": serialized.thumb_url,
            "media_kind": serialized.media_kind,
            "location": photo.location,
            "gps_lat": photo.gps_lat,
            "gps_lon": photo.gps_lon,
        },
    }


def _build_context_package(
    db: Session,
    app_settings: Settings,
    *,
    company_id: str,
    project_id: str | None,
    question: str,
) -> dict[str, Any]:
    intent = _intent_from_question(question)
    photos = _candidate_photos(db, company_id=company_id, project_id=project_id)
    candidate_photo_ids = [photo.id for photo in photos]
    ranked_photos: list[Photo] = []
    try:
        semantic_matches = semantic_search_photos(
            db,
            app_settings,
            tenant_slug=company_id,
            query=question,
            candidate_photo_ids=candidate_photo_ids,
            limit=8,
        )
        ranked_photos = [photo for photo, _ in semantic_matches]
    except Exception as exc:
        logger.warning("copilot_semantic_search_failed", company_id=company_id, project_id=project_id, error=str(exc))

    if not ranked_photos:
        ranked_photos = photos[:8]

    observations = _matching_observations(
        db,
        company_id=company_id,
        project_id=project_id,
        intent=intent,
        candidate_photo_ids=[photo.id for photo in ranked_photos] or candidate_photo_ids,
    )
    receipts = _matching_receipts(db, company_id=company_id, project_id=project_id) if intent == COPILOT_INTENT_FINANCE else []
    employee_activity = _recent_employee_activity(db, company_id=company_id, project_id=project_id)
    progress_reports = _recent_progress_report_summaries(db, company_id=company_id, project_id=project_id)
    recent_public_annotations = list(
        db.scalars(
            select(MediaAnnotation)
            .where(
                MediaAnnotation.status == AnnotationStatus.completed,
                MediaAnnotation.visibility == AnnotationVisibility.public,
                MediaAnnotation.photo_id.in_([photo.id for photo in ranked_photos] or [-1]),
            )
            .order_by(MediaAnnotation.created_at.desc())
            .limit(8)
        )
    )

    evidence_summary = {
        "intent": intent,
        "photo_count": len(ranked_photos),
        "observation_count": len(observations),
        "receipt_count": len(receipts),
        "employee_activity_count": len(employee_activity),
        "progress_report_count": len(progress_reports),
    }
    return {
        "intent": intent,
        "photos": ranked_photos,
        "observations": observations,
        "receipts": receipts,
        "employee_activity": employee_activity,
        "progress_reports": progress_reports,
        "annotations": recent_public_annotations,
        "summary": evidence_summary,
    }


def _build_answer_prompt(
    *,
    question: str,
    language: str,
    project_prompt: str | None,
    context_package: dict[str, Any],
) -> str:
    photos: list[Photo] = context_package["photos"]
    observations: list[EvidenceObservation] = context_package["observations"]
    receipts: list[ReceiptFact] = context_package["receipts"]
    employee_activity: list[dict[str, Any]] = context_package["employee_activity"]
    progress_reports: list[dict[str, Any]] = context_package["progress_reports"]
    annotations: list[MediaAnnotation] = context_package["annotations"]

    lines = [
        "You are KK Field Logger Copilot. Answer like an operations copilot for a project manager or finance director.",
        f"Respond in language code: {language}.",
        "Ground every answer in the supplied evidence only. State uncertainty clearly when evidence is incomplete.",
        "Return strict JSON only with keys: title, executive_brief, answer_markdown, key_points, risk_signals, recommended_actions, follow_up_questions, confidence_level.",
        "confidence_level must be one of high, medium, low.",
        f"Project-specific AI guidance: {project_prompt or 'No project-specific guidance configured.'}",
        f"User question: {question}",
        "",
        "Top media evidence:",
    ]
    for photo in photos[:8]:
        tag_json = photo.tag_json if isinstance(photo.tag_json, dict) else {}
        lines.append(
            f"- Photo #{photo.id}: project={photo.project_id}, employee={photo.employee_id}, captured={to_utc_iso(photo.captured_at_utc)}, "
            f"location={photo.location or 'unknown'}, gps=({photo.gps_lat or 'unknown'},{photo.gps_lon or 'unknown'}), "
            f"angles=heading {photo.heading or 'unknown'}, pitch {photo.pitch or 'unknown'}, roll {photo.roll or 'unknown'}, "
            f"summary={_truncate(tag_json.get('ai_summary'), 320)}, note={_truncate(photo.note, 200)}"
        )
    if observations:
        lines.append("")
        lines.append("Structured observations:")
        for observation in observations[:12]:
            lines.append(
                f"- [{observation.observation_type}] severity={observation.severity or 'unknown'} photo_id={observation.photo_id or 'n/a'} :: {observation.content_text}"
            )
    if annotations:
        lines.append("")
        lines.append("Recent public annotations:")
        for annotation in annotations[:8]:
            lines.append(f"- photo_id={annotation.photo_id or 'n/a'} :: {_truncate(annotation.content_text, 240)}")
    if employee_activity:
        lines.append("")
        lines.append("Employee field activity in the last 7 days:")
        for item in employee_activity:
            lines.append(f"- employee {item['employee_id']} uploaded {item['upload_count']} item(s)")
    if receipts:
        lines.append("")
        lines.append("Recent receipt facts:")
        for receipt in receipts[:10]:
            lines.append(
                f"- photo_id={receipt.photo_id}, vendor={receipt.vendor_name or 'unknown'}, amount={receipt.total_amount or 'unknown'}, "
                f"gallons={receipt.gallons or 'unknown'}, purchaser={receipt.purchaser_name or receipt.employee_id or 'unknown'}, "
                f"has_pump_photo={receipt.has_pump_photo}"
            )
    if progress_reports:
        lines.append("")
        lines.append("Recent progress reports:")
        for report in progress_reports[:3]:
            lines.append(f"- report_id={report['report_id']}, completed_at={report['completed_at']}, summary={report['summary']}")
    return "\n".join(lines)


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None

    # Models often wrap JSON in fenced blocks or append explanatory text before/after it.
    # Normalize those variants first, then scan for the first valid object payload.
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])

    expected_keys = {
        "title",
        "executive_brief",
        "answer_markdown",
        "key_points",
        "risk_signals",
        "recommended_actions",
        "follow_up_questions",
        "confidence_level",
    }

    def _is_valid_payload(payload: Any) -> bool:
        return isinstance(payload, dict) and bool(expected_keys.intersection(payload.keys()))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            payload = None
        if _is_valid_payload(payload):
            return payload
        for match in re.finditer(r"\{", candidate):
            try:
                payload, end_index = decoder.raw_decode(candidate[match.start() :])
            except Exception:
                continue
            if _is_valid_payload(payload):
                return payload
    return None


def _extract_quoted_field(raw_text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', raw_text, flags=re.IGNORECASE)
    if match is None:
        return ""
    index = match.end()
    chars: list[str] = []
    escaped = False
    while index < len(raw_text):
        char = raw_text[index]
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        else:
            chars.append(char)
        index += 1
    return _normalized_text("".join(chars))


def _extract_list_field(raw_text: str, key: str) -> list[str]:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', raw_text, flags=re.IGNORECASE)
    if match is None:
        return []
    index = match.end()
    depth = 1
    chars: list[str] = []
    while index < len(raw_text) and depth > 0:
        char = raw_text[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        chars.append(char)
        index += 1
    body = "".join(chars)
    items = [_normalized_text(item) for item in re.findall(r'"((?:[^"\\]|\\.)*)"', body)]
    return _normalize_structured_list([item for item in items if item and item != "..."])


def _normalize_structured_list(items: list[Any]) -> list[str]:
    normalized_items = [_normalized_text(item) for item in items if _normalized_text(item) and _normalized_text(item) != "..."]
    if normalized_items and sum(1 for item in normalized_items if len(item) <= 1) >= max(3, int(len(normalized_items) * 0.7)):
        merged = _normalized_text("".join(normalized_items))
        if merged and merged != "...":
            normalized_merged = merged.casefold()
            if normalized_merged in {"none", "nonerecommended", "noneavailable"}:
                return []
            if normalized_merged in {"none identified", "noneidentified"}:
                return ["None identified"]
            if " " not in merged and len(merged) > 24:
                return []
            return [merged]
    return normalized_items


def _salvage_answer_structure(raw_text: str) -> dict[str, Any] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    title = _extract_quoted_field(text, "title")
    executive_brief = _extract_quoted_field(text, "executive_brief")
    answer_markdown = _extract_quoted_field(text, "answer_markdown")
    key_points = _extract_list_field(text, "key_points")
    risk_signals = _extract_list_field(text, "risk_signals")
    recommended_actions = _extract_list_field(text, "recommended_actions")
    follow_up_questions = _extract_list_field(text, "follow_up_questions")
    confidence_level = _extract_quoted_field(text, "confidence_level").casefold()
    confidence_level = confidence_level if confidence_level in {"high", "medium", "low"} else "medium"

    if not any([title, executive_brief, answer_markdown, key_points, risk_signals, recommended_actions, follow_up_questions]):
        return None
    return {
        "title": title or "Evidence Copilot Answer",
        "executive_brief": executive_brief,
        "answer_markdown": answer_markdown or executive_brief,
        "key_points": key_points,
        "risk_signals": risk_signals,
        "recommended_actions": recommended_actions,
        "follow_up_questions": follow_up_questions,
        "confidence_level": confidence_level,
    }


def _trim_structured_section_repeats(answer_markdown: str, structured_payload: dict[str, Any]) -> str:
    text = str(answer_markdown or "").strip()
    if not text:
        return ""
    if not any(structured_payload.get(key) for key in ("key_points", "risk_signals", "recommended_actions", "follow_up_questions")):
        return text
    markers = [
        r"\*\*Key Points:\*\*",
        r"\*\*Risk Signals:\*\*",
        r"\*\*Recommended Actions:\*\*",
        r"\*\*Follow-up Questions:\*\*",
        r"###\s+Key Points",
        r"###\s+Risk Signals",
        r"###\s+Recommended Actions",
        r"###\s+Follow-up Questions",
    ]
    cut_indexes = [match.start() for marker in markers for match in re.finditer(marker, text, flags=re.IGNORECASE)]
    if not cut_indexes:
        return text
    trimmed = text[: min(cut_indexes)].strip()
    return trimmed or text


def _normalize_answer_structure(raw_payload: dict[str, Any] | None, fallback_text: str) -> dict[str, Any]:
    raw_payload = raw_payload or _salvage_answer_structure(fallback_text)
    if not isinstance(raw_payload, dict):
        summary = _truncate(fallback_text, 1200) or "No answer was generated."
        return {
            "title": "Evidence Copilot Answer",
            "executive_brief": summary,
            "answer_markdown": summary,
            "key_points": [],
            "risk_signals": [],
            "recommended_actions": [],
            "follow_up_questions": [],
            "confidence_level": "medium",
        }
    normalized = {
        "title": _truncate(raw_payload.get("title") or "Evidence Copilot Answer", 160),
        "executive_brief": _truncate(raw_payload.get("executive_brief"), 600),
        "answer_markdown": _truncate(raw_payload.get("answer_markdown"), 8000) or _truncate(fallback_text, 8000),
        "key_points": [_truncate(item, 240) for item in _normalize_structured_list(list(raw_payload.get("key_points") or []))],
        "risk_signals": [_truncate(item, 240) for item in _normalize_structured_list(list(raw_payload.get("risk_signals") or []))],
        "recommended_actions": [_truncate(item, 240) for item in _normalize_structured_list(list(raw_payload.get("recommended_actions") or []))],
        "follow_up_questions": [_truncate(item, 240) for item in _normalize_structured_list(list(raw_payload.get("follow_up_questions") or []))],
        "confidence_level": str(raw_payload.get("confidence_level") or "medium").strip().lower() if str(raw_payload.get("confidence_level") or "").strip().lower() in {"high", "medium", "low"} else "medium",
    }
    normalized["answer_markdown"] = _trim_structured_section_repeats(normalized["answer_markdown"], normalized)
    return normalized


def _render_structured_answer_markdown(answer: dict[str, Any]) -> str:
    sections = [f"## {answer['title']}", answer.get("executive_brief") or ""]
    if answer.get("answer_markdown"):
        sections.extend(["", answer["answer_markdown"]])
    for heading, key in (
        ("Key Points", "key_points"),
        ("Risk Signals", "risk_signals"),
        ("Recommended Actions", "recommended_actions"),
        ("Follow-up Questions", "follow_up_questions"),
    ):
        items = answer.get(key) or []
        if items:
            sections.append("")
            sections.append(f"### {heading}")
            sections.extend([f"- {item}" for item in items])
    sections.append("")
    sections.append(f"Confidence: {answer.get('confidence_level', 'medium')}")
    return "\n".join([item for item in sections if item is not None]).strip()


def queue_copilot_message_generation(
    db: Session,
    *,
    app_settings: Settings,
    conversation: CopilotConversation,
    prompt_text: str,
    language: str,
    actor_user_id: int,
    preferred_backend_id: str | None = None,
    preferred_mode: str | None = None,
) -> tuple[CopilotMessage, CopilotMessage]:
    from app.services.job_queue import enqueue_copilot_message_task

    cleaned_prompt = _normalized_text(prompt_text)
    if not cleaned_prompt:
        raise ValueError("Question cannot be empty")

    user_message = CopilotMessage(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="user",
        status=CopilotMessageStatus.completed,
        language=language,
        content_text=cleaned_prompt,
        context_summary_json=None,
        completed_at=utc_now(),
    )
    db.add(user_message)
    db.flush()

    assistant_message = CopilotMessage(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        status=CopilotMessageStatus.pending,
        language=language,
        content_text="",
        context_summary_json={
            "prompt_message_id": user_message.id,
            "preferred_backend_id": _normalized_text(preferred_backend_id) or conversation.preferred_backend_id,
            "preferred_mode": preferred_mode if preferred_mode in COPILOT_ALLOWED_MODES else conversation.preferred_mode,
        },
    )
    db.add(assistant_message)

    if not conversation.title or conversation.title == "New Copilot Conversation":
        conversation.title = _conversation_title_from_prompt(cleaned_prompt)
    conversation.preferred_backend_id = _normalized_text(preferred_backend_id) or conversation.preferred_backend_id
    conversation.preferred_mode = preferred_mode if preferred_mode in COPILOT_ALLOWED_MODES else conversation.preferred_mode
    db.add(conversation)
    db.flush()

    enqueue_copilot_message_task(
        db,
        app_settings=app_settings,
        company_id=conversation.company_id,
        tenant_id=conversation.tenant_id,
        message_id=assistant_message.id,
        actor_user_id=actor_user_id,
    )
    return user_message, assistant_message


def process_copilot_assistant_message(
    message_id: str,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    *,
    raise_on_failure: bool = False,
) -> None:
    with session_maker() as db:
        try:
            assistant_message = db.get(CopilotMessage, message_id)
            if assistant_message is None or assistant_message.role != "assistant":
                logger.warning("copilot_message_processing_skipped", message_id=message_id, reason="missing_or_invalid_message")
                return
            conversation = db.get(CopilotConversation, assistant_message.conversation_id)
            if conversation is None:
                raise RuntimeError("Copilot conversation could not be loaded")

            ctx = assistant_message.context_summary_json if isinstance(assistant_message.context_summary_json, dict) else {}
            prompt_message_id = str(ctx.get("prompt_message_id") or "").strip()
            prompt_message = db.get(CopilotMessage, prompt_message_id) if prompt_message_id else None
            if prompt_message is None:
                raise RuntimeError("Original copilot prompt message could not be loaded")

            assistant_message.status = CopilotMessageStatus.processing
            assistant_message.error_message = None
            db.add(assistant_message)
            db.commit()
            db.refresh(assistant_message)

            project_prompt = None
            if conversation.project_id:
                project = db.scalar(select(Project).where(Project.project_id == conversation.project_id, Project.company_id == conversation.company_id))
                if project is not None:
                    project_prompt = project.image_video_ai_prompt

            context_package = _build_context_package(
                db,
                app_settings,
                company_id=conversation.company_id,
                project_id=conversation.project_id,
                question=prompt_message.content_text,
            )

            candidate_backends = _collect_available_backends(db, app_settings, conversation.tenant_id or conversation.company_id)
            if not candidate_backends:
                raise RuntimeError("No enabled AI backends are configured for Copilot")
            ordered_backends = _ordered_backends_for_mode(
                candidate_backends,
                preferred_mode=str(ctx.get("preferred_mode") or conversation.preferred_mode or COPILOT_MODE_STANDARD),
                preferred_backend_id=str(ctx.get("preferred_backend_id") or conversation.preferred_backend_id or "").strip() or None,
            )
            prompt = _build_answer_prompt(
                question=prompt_message.content_text,
                language=assistant_message.language or "en",
                project_prompt=project_prompt,
                context_package=context_package,
            )
            raw_answer, backend_ref, attempts = generate_markdown_completion(backends=ordered_backends, prompt=prompt)
            structured_answer = _normalize_answer_structure(_extract_json_object(raw_answer), raw_answer)
            rendered_answer = _render_structured_answer_markdown(structured_answer)

            assistant_message.status = CopilotMessageStatus.completed
            assistant_message.content_text = rendered_answer
            assistant_message.error_message = None
            assistant_message.completed_at = utc_now()
            selected_backend = ordered_backends[0]
            assistant_message.selected_backend_id = selected_backend.id
            assistant_message.selected_backend_type = selected_backend.type
            assistant_message.selected_model = selected_backend.model
            assistant_message.context_summary_json = {
                "mode": str(ctx.get("preferred_mode") or conversation.preferred_mode or COPILOT_MODE_STANDARD),
                "backend_ref": backend_ref,
                "attempts": attempts,
                "intent": context_package["intent"],
                "summary": context_package["summary"],
                "structured_answer": structured_answer,
            }
            db.add(assistant_message)
            db.execute(delete(CopilotMessageSource).where(CopilotMessageSource.message_id == assistant_message.id))
            for source in [_serialize_photo_source(photo, app_settings) for photo in context_package["photos"][:COPILOT_MAX_SOURCES]]:
                db.add(
                    CopilotMessageSource(
                        message_id=assistant_message.id,
                        source_type=source["source_type"],
                        source_id=source["source_id"],
                        source_title=source["source_title"],
                        source_url=source["source_url"],
                        relevance_score=source["relevance_score"],
                        metadata_json=source["metadata_json"],
                    )
                )
            for report in context_package["progress_reports"][:3]:
                db.add(
                    CopilotMessageSource(
                        message_id=assistant_message.id,
                        source_type="progress_report",
                        source_id=report["report_id"],
                        source_title=f"Progress Report {report['report_id'][:8]}",
                        source_url=_message_source_url("progress_report", report["report_id"]),
                        relevance_score=0.74,
                        metadata_json=report,
                    )
                )
            db.commit()

            log_audit(
                db,
                action="copilot_message_completed",
                target_type="copilot_message",
                target_id=assistant_message.id,
                actor_user_id=conversation.created_by_user_id,
                tenant_id=conversation.tenant_id or conversation.company_id,
                company_id=conversation.company_id,
                project_id=conversation.project_id,
                detail_json={
                    "intent": context_package["intent"],
                    "backend_ref": backend_ref,
                    "source_count": len(context_package["photos"]),
                },
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            failed_message = db.get(CopilotMessage, message_id)
            if failed_message is not None:
                failed_message.status = CopilotMessageStatus.failed
                failed_message.error_message = str(exc)
                failed_message.completed_at = utc_now()
                db.add(failed_message)
                db.commit()
            logger.warning("copilot_message_processing_failed", message_id=message_id, error=str(exc))
            if raise_on_failure:
                raise
