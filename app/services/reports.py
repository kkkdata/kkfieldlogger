from __future__ import annotations

import base64
import html
import json
import mimetypes
from pathlib import Path
import re
from time import perf_counter
import textwrap
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import to_utc_iso, utc_now
from app.models import AIAnalysisStatus, GeneratedReport, Photo, ProgressReport, ProgressReportStatus, Project, ReportStatus
from app.services.ai_pipeline import (
    VOICE_TRANSLATION_LANGUAGES,
    generate_markdown_completion,
    generate_multi_image_completion,
    list_photo_ai_logs,
    merge_active_ai_analysis_logs,
    resolve_ai_backends_for_tenant,
    sync_ai_runtime_settings,
    translate_text_with_ollama_backends,
)
from app.services.audit import log_audit
from app.services.i18n import get_language, translator
from app.services.photos import build_thumb_filename, create_video_thumbnail_file, photo_media_kind, serialize_photo

logger = get_logger("kkfieldlogger.reports")
PROGRESS_REPORT_DEFAULT_PROMPT = "重点核查施工进度变化、遗漏工序、脚手架、防护栏、安全帽、作业面清理与明显安全隐患。"
PROGRESS_REPORT_EMBED_PREFIX = "<!-- progress_report_payload:"
PROGRESS_REPORT_EMBED_SUFFIX = " -->"
PROGRESS_REPORT_PAYLOAD_VERSION = 2
PROGRESS_REPORT_GENERIC_LABELS = {
    "",
    "none",
    "person",
    "people",
    "room",
    "door",
    "white door",
    "floor",
    "leg",
    "black device",
    "device",
}


def _sanitize_report_filename(title: str, public_id: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in title.lower()).strip("-")
    return f"{safe or 'report'}-{public_id[:8]}.pdf"


def _report_directory(app_settings: Settings, report: GeneratedReport) -> Path:
    return app_settings.reports_root / report.company_id / report.public_id


def _report_file_path(app_settings: Settings, report: GeneratedReport) -> Path:
    return _report_directory(app_settings, report) / "report.pdf"


def build_report_title(photos: list[Photo]) -> str:
    project_ids = sorted({photo.project_id for photo in photos if photo.project_id})
    if len(project_ids) == 1:
        prefix = f"{project_ids[0]} Inspection Report"
    else:
        prefix = "Multi-Project Inspection Report"
    return f"{prefix} {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"


def build_progress_report_title(project: Project) -> str:
    return f"{project.project_id} Progress Compare {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"


def _truncate_text(value: str | None, limit: int) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def _normalize_string_list(value: Any, *, limit: int, max_items: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value[:max_items]:
        text = _truncate_text(str(item), limit)
        if text:
            normalized.append(text)
    return normalized


def _normalize_int(value: Any, *, minimum: int = 0, maximum: int = 100) -> int | None:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _normalize_choice(value: Any, *, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    return default


def _normalize_text(value: Any, *, limit: int) -> str:
    return _truncate_text(str(value or ""), limit).strip()


def _serialize_photo_for_report(photo: Photo) -> dict[str, Any]:
    tag_json = photo.tag_json if isinstance(photo.tag_json, dict) else {}
    return {
        "photo_id": photo.id,
        "project_id": photo.project_id,
        "employee_id": photo.employee_id,
        "photo_type": photo.photo_type.value,
        "original_file_name": photo.original_file_name,
        "captured_at_utc": to_utc_iso(photo.captured_at_utc),
        "location": _truncate_text(photo.location, 200),
        "gps": _truncate_text(photo.gps, 120),
        "note": _truncate_text(photo.note, 600),
        "ai_summary": _truncate_text(str(tag_json.get("ai_summary") or ""), 400),
        "labels": _normalize_string_list(tag_json.get("labels"), limit=80),
        "defects": _normalize_string_list(tag_json.get("defects"), limit=160),
        "visibility": photo.visibility.value,
        "approval_status": photo.approval_status.value,
    }


def _progress_prompt_text(project: Project) -> str:
    cleaned = " ".join(str(project.image_video_ai_prompt or "").strip().split())
    return cleaned or PROGRESS_REPORT_DEFAULT_PROMPT


def _normalize_progress_custom_prompt(custom_prompt: str | None) -> str | None:
    cleaned = " ".join(str(custom_prompt or "").strip().split())
    return cleaned or None


def _format_optional_number(value: Any, *, precision: int = 6) -> str:
    if value in {None, ""}:
        return "unknown"
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return "unknown"


def _format_angle_metadata(photo: Photo) -> str:
    return (
        f"heading={_format_optional_number(photo.heading, precision=2)}, "
        f"pitch={_format_optional_number(photo.pitch, precision=2)}, "
        f"roll={_format_optional_number(photo.roll, precision=2)}"
    )


def _existing_progress_ai_contexts(db: Session, photos: list[Photo]) -> list[str]:
    contexts: list[str] = []
    for photo in photos:
        active_logs = [
            log
            for log in list_photo_ai_logs(db, photo.id)
            if getattr(log.status, "value", log.status) == AIAnalysisStatus.active.value
        ]
        if not active_logs:
            continue
        snapshot = merge_active_ai_analysis_logs(active_logs)
        parts: list[str] = []
        ai_summary = _truncate_text(snapshot.get("ai_summary"), 240)
        if ai_summary:
            parts.append(f"summary={ai_summary}")
        ai_summary_zh = _truncate_text((snapshot.get("ai_summary_translations") or {}).get("zh"), 240)
        if ai_summary_zh:
            parts.append(f"summary_zh={ai_summary_zh}")
        labels = snapshot.get("labels") or []
        if labels:
            normalized_labels = [str(label).strip() for label in labels[:8] if str(label).strip()]
            if normalized_labels:
                parts.append(f"labels={', '.join(normalized_labels)}")
        defects = snapshot.get("defects") or []
        if defects:
            normalized_defects = [str(defect).strip() for defect in defects[:6] if str(defect).strip()]
            if normalized_defects:
                parts.append(f"defects={', '.join(normalized_defects)}")
        for field_name in (
            "visible_objects",
            "equipment",
            "materials",
            "safety_observations",
            "quality_observations",
            "inventory_observations",
            "water_or_housekeeping_observations",
        ):
            values = snapshot.get(field_name) or []
            if isinstance(values, list):
                normalized_values = [str(value).strip() for value in values[:4] if str(value).strip()]
                if normalized_values:
                    parts.append(f"{field_name}={'; '.join(normalized_values)}")
        if parts:
            contexts.append(f"[existing_single_image_ai photo_id={photo.id}] " + " | ".join(parts))
    return contexts


def _preferred_sequence_ai_hints(db: Session, photos: list[Photo]) -> list[str]:
    label_counts: dict[str, set[int]] = {}
    label_zh_examples: dict[str, list[str]] = {}
    for photo in photos:
        active_logs = [
            log
            for log in list_photo_ai_logs(db, photo.id)
            if getattr(log.status, "value", log.status) == AIAnalysisStatus.active.value
        ]
        for log in active_logs:
            result_data = log.result_data if isinstance(log.result_data, dict) else {}
            labels = result_data.get("labels") if isinstance(result_data.get("labels"), list) else []
            zh_summary = _truncate_text((result_data.get("ai_summary_translations") or {}).get("zh"), 120)
            seen_for_log: set[str] = set()
            for raw_label in labels:
                normalized_label = str(raw_label or "").strip()
                label_key = normalized_label.casefold()
                if (
                    not normalized_label
                    or label_key in PROGRESS_REPORT_GENERIC_LABELS
                    or label_key in seen_for_log
                ):
                    continue
                seen_for_log.add(label_key)
                label_counts.setdefault(normalized_label, set()).add(photo.id)
                if zh_summary:
                    examples = label_zh_examples.setdefault(normalized_label, [])
                    if zh_summary not in examples:
                        examples.append(zh_summary)

    hints: list[str] = []
    ranked_labels = sorted(
        (
            (label, len(photo_ids))
            for label, photo_ids in label_counts.items()
            if len(photo_ids) >= 2
        ),
        key=lambda item: (-item[1], item[0].casefold()),
    )
    for label, count in ranked_labels[:6]:
        examples = label_zh_examples.get(label, [])[:2]
        suffix = f"；可参考中文既有描述：{' / '.join(examples)}" if examples else ""
        hints.append(f"[preferred_sequence_label] {label} appears consistently across {count} selected photos{suffix}")
    return hints


def build_multi_image_progress_prompt(
    project: Project,
    photos: list[Photo],
    *,
    custom_prompt: str | None = None,
    existing_ai_contexts: list[str] | None = None,
    preferred_ai_hints: list[str] | None = None,
) -> str:
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
    prompt_lines = [
        "你是一个资深的工程监理，正在为项目经理生成正式的进度演变报告。",
        f"本项目的最高审查标准是：{_progress_prompt_text(project)}",
        (
            f"操作员追加说明：{_normalize_progress_custom_prompt(custom_prompt)}"
            if _normalize_progress_custom_prompt(custom_prompt)
            else "如果操作员没有追加说明，就严格依据项目审查标准生成报告。"
        ),
        "以下是同一施工位置按时间顺序拍摄的多张现场照片。请结合拍摄角度差异、拍摄距离变化和现场遮挡，尽量消除视觉误差。",
        "请只基于这些原始图片和元数据进行判断，不要引用图片外的信息，不要臆测看不见的施工内容。",
        "报告目标不是简单描述图片，而是帮助项目经理判断：进度是否推进、风险是否变大、材料/工具/库存是否支持下一步施工、现场是否有积水或整理问题、哪些事项需要马上安排人员跟进。",
        "每条重要判断尽量写清楚依据来自哪些 photo_id；如果只能从单张图推断，必须说明证据范围有限。",
        "如果下面附带了既有的单图 AI 识别结果，请把它们当作弱约束参考：当前图像与既有结论一致时，优先沿用已有的对象命名。",
        "如果跨图片的一致性提示中已经反复把同一对象命名为某个设备，除非当前图片有明显相反证据，否则不要重新换成别的具体机器名称。",
        "如果你无法高把握确认某个设备或物体的具体类型，请使用保守描述，例如“黑色矩形设备”或“地面设备”，不要仅凭猜测就写成咖啡机、碎纸机或取暖器。",
        "你必须输出一个严格合法的 JSON 对象，不要输出 Markdown，不要包裹 ```json 代码块，不要输出任何 JSON 之外的解释文字。",
        "JSON 字段要求：",
        '- overall_progress_percent 必须是 0 到 100 的整数。',
        '- overall_status 只能是 "on_track"、"at_risk"、"blocked"、"unknown" 之一。',
        '- confidence_level 只能是 "high"、"medium"、"low" 之一。',
        "- 所有列表字段都必须返回数组，没有内容时返回空数组。",
        "- timeline_observations 必须按时间顺序返回，并尽量覆盖每一张图片。",
        "- timeline_observations[].photo_id 必须引用下面提供的真实 photo_id。",
        "- manager_brief 要让项目经理一眼看懂是否需要立即介入。",
        "- material_inventory_signals 要区分“可见库存线索”和“无法确认数量”，不要编造精确数量。",
        "- water_housekeeping_signals 要专门记录积水、泥泞、垃圾、通道遮挡和场地整理情况。",
        "- uncertain_items 用于承认无法可靠识别的设备/材料，并说明为什么不确定。",
        "- immediate_decisions 必须是经理可以立刻执行或安排的事项，不要写空泛建议。",
        "- evidence_limitations 必须明确影响结论可靠性的限制。",
        "输出 JSON 结构示例：",
        json.dumps(example_schema, ensure_ascii=False, indent=2),
        "",
    ]
    if existing_ai_contexts:
        prompt_lines.extend(
            [
                "既有单图 AI 识别结果（仅作弱约束参考）：",
                *existing_ai_contexts,
                "",
            ]
        )
    if preferred_ai_hints:
        prompt_lines.extend(
            [
                "跨图片既有 AI 一致性提示（若当前图像没有明显相反证据，请优先沿用这些命名）：",
                *preferred_ai_hints,
                "",
            ]
        )
    prompt_lines.extend(
        [
        "照片元数据：",
        ]
    )
    for index, photo in enumerate(photos, start=1):
        prompt_lines.append(
            (
                f"[图片{index}] photo_id: {photo.id}, 时间: {to_utc_iso(photo.captured_at_utc) or 'unknown'}, "
                f"GPS: {_format_optional_number(photo.gps_lat)},{_format_optional_number(photo.gps_lon)}, "
                f"相机角度: {_format_angle_metadata(photo)}, "
                f"位置描述: {photo.location or 'unknown'}, "
                f"文件名: {photo.original_file_name or 'unknown'}"
            )
        )
    return "\n".join(prompt_lines)


def _progress_report_input_path(photo: Photo) -> tuple[Path | None, str]:
    media_kind = photo_media_kind(photo)
    file_path = Path(photo.file_path)
    if media_kind == "video":
        thumb_filename = build_thumb_filename(file_path.name, extension_override=".jpg")
        thumb_path = file_path.with_name(thumb_filename)
        if not thumb_path.is_file() and file_path.is_file():
            create_video_thumbnail_file(file_path, thumb_path)
        return (thumb_path if thumb_path.is_file() else None), "image/jpeg"
    if not file_path.is_file():
        return None, photo.mime_type or "image/jpeg"
    mime_type = photo.mime_type or mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    return file_path, mime_type


def _load_progress_report_images(photos: list[Photo]) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    for photo in photos:
        image_path, mime_type = _progress_report_input_path(photo)
        if image_path is None or not image_path.is_file():
            raise RuntimeError(f"Photo {photo.id} file is missing for progress comparison")
        images.append((mime_type, base64.b64encode(image_path.read_bytes()).decode("ascii")))
    return images


def _extract_progress_report_payload_marker(content: str | None) -> tuple[str, dict[str, Any] | None]:
    normalized_content = str(content or "")
    pattern = re.compile(
        rf"{re.escape(PROGRESS_REPORT_EMBED_PREFIX)}(?P<payload>[A-Za-z0-9+/=]+){re.escape(PROGRESS_REPORT_EMBED_SUFFIX)}",
        re.DOTALL,
    )
    match = pattern.search(normalized_content)
    if not match:
        return normalized_content.strip(), None
    payload_json = match.group("payload")
    try:
        decoded = base64.b64decode(payload_json.encode("ascii")).decode("utf-8")
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            payload = None
    except Exception:
        payload = None
    cleaned = pattern.sub("", normalized_content).strip()
    return cleaned, payload


def _extract_json_object(raw_text: str | None) -> dict[str, Any] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text, count=1)
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return None


def _normalize_progress_language(raw_value: str | None) -> str:
    normalized_value = str(raw_value or "").strip().lower()
    if normalized_value.startswith("zh"):
        return "zh"
    if normalized_value.startswith("es"):
        return "es"
    return "en"


def _fallback_progress_report_structure(raw_text: str, photos: list[Photo]) -> dict[str, Any]:
    summary = _normalize_text(raw_text, limit=2400) or "No structured progress summary was generated."
    return {
        "executive_summary": summary,
        "overall_progress_percent": None,
        "overall_status": "unknown",
        "confidence_level": "medium",
        "manager_brief": summary,
        "angle_bias_notes": [],
        "key_changes": [],
        "work_completed": [],
        "work_remaining": [],
        "safety_risks": [],
        "quality_risks": [],
        "material_inventory_signals": [],
        "water_housekeeping_signals": [],
        "uncertain_items": [],
        "evidence_limitations": [],
        "immediate_decisions": [],
        "recommended_actions": [],
        "timeline_observations": [
            {
                "photo_id": photo.id,
                "captured_at": to_utc_iso(photo.captured_at_utc) or "unknown",
                "observation": "See raw report narrative for the full comparison context.",
                "progress_signal": "",
                "risk_signal": "",
            }
            for photo in photos
        ],
    }


def _normalize_progress_report_structure(payload: dict[str, Any] | None, photos: list[Photo]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _fallback_progress_report_structure("", photos)

    photos_by_id = {photo.id: photo for photo in photos}
    ordered_photo_ids = [photo.id for photo in photos]
    timeline_items: list[dict[str, Any]] = []
    raw_timeline = payload.get("timeline_observations") or payload.get("timeline") or []
    if isinstance(raw_timeline, list):
        for index, item in enumerate(raw_timeline[: max(4, len(ordered_photo_ids) + 2)]):
            if not isinstance(item, dict):
                continue
            photo_id = item.get("photo_id")
            if str(photo_id).isdigit():
                resolved_photo_id = int(photo_id)
                if resolved_photo_id not in photos_by_id and index < len(ordered_photo_ids):
                    resolved_photo_id = ordered_photo_ids[index]
            elif index < len(ordered_photo_ids):
                resolved_photo_id = ordered_photo_ids[index]
            else:
                resolved_photo_id = None
            photo = photos_by_id.get(resolved_photo_id) if resolved_photo_id is not None else None
            timeline_items.append(
                {
                    "photo_id": resolved_photo_id,
                    "captured_at": _normalize_text(item.get("captured_at") or (to_utc_iso(photo.captured_at_utc) if photo else ""), limit=80)
                    or "unknown",
                    "observation": _normalize_text(item.get("observation"), limit=1200),
                    "progress_signal": _normalize_text(item.get("progress_signal"), limit=400),
                    "risk_signal": _normalize_text(item.get("risk_signal"), limit=400),
                }
            )
    if not timeline_items:
        timeline_items = _fallback_progress_report_structure("", photos)["timeline_observations"]

    structured = {
        "executive_summary": _normalize_text(payload.get("executive_summary") or payload.get("summary"), limit=2400),
        "overall_progress_percent": _normalize_int(payload.get("overall_progress_percent")),
        "overall_status": _normalize_choice(
            payload.get("overall_status"),
            allowed={"on_track", "at_risk", "blocked", "unknown"},
            default="unknown",
        ),
        "confidence_level": _normalize_choice(
            payload.get("confidence_level"),
            allowed={"high", "medium", "low"},
            default="medium",
        ),
        "manager_brief": _normalize_text(payload.get("manager_brief"), limit=1600),
        "angle_bias_notes": _normalize_string_list(payload.get("angle_bias_notes"), limit=300, max_items=8),
        "key_changes": _normalize_string_list(payload.get("key_changes"), limit=500, max_items=8),
        "work_completed": _normalize_string_list(payload.get("work_completed"), limit=500, max_items=8),
        "work_remaining": _normalize_string_list(payload.get("work_remaining"), limit=500, max_items=8),
        "safety_risks": _normalize_string_list(payload.get("safety_risks"), limit=500, max_items=10),
        "quality_risks": _normalize_string_list(payload.get("quality_risks"), limit=500, max_items=10),
        "material_inventory_signals": _normalize_string_list(payload.get("material_inventory_signals"), limit=500, max_items=10),
        "water_housekeeping_signals": _normalize_string_list(payload.get("water_housekeeping_signals"), limit=500, max_items=10),
        "uncertain_items": _normalize_string_list(payload.get("uncertain_items"), limit=500, max_items=10),
        "evidence_limitations": _normalize_string_list(payload.get("evidence_limitations"), limit=500, max_items=10),
        "immediate_decisions": _normalize_string_list(payload.get("immediate_decisions"), limit=500, max_items=10),
        "recommended_actions": _normalize_string_list(payload.get("recommended_actions"), limit=500, max_items=10),
        "timeline_observations": timeline_items,
    }

    if not structured["executive_summary"]:
        fallback = _normalize_text(payload.get("manager_brief") or payload.get("narrative"), limit=2400)
        structured["executive_summary"] = fallback or "No structured progress summary was generated."
    if not structured["manager_brief"]:
        structured["manager_brief"] = structured["executive_summary"]
    return structured


def _render_progress_report_markdown(structured: dict[str, Any]) -> str:
    lines = [
        "# Progress Evolution Report",
        "",
        "## Executive Summary",
        structured.get("executive_summary") or "No summary available.",
        "",
        "## Manager Brief",
        structured.get("manager_brief") or structured.get("executive_summary") or "No manager brief available.",
        "",
        "## Overall Assessment",
    ]
    overall_progress_percent = structured.get("overall_progress_percent")
    if overall_progress_percent is not None:
        lines.append(f"- Estimated completion: {overall_progress_percent}%")
    lines.append(f"- Overall status: {structured.get('overall_status') or 'unknown'}")
    lines.append(f"- Confidence: {structured.get('confidence_level') or 'medium'}")

    def add_bullet_section(title: str, items: list[str]) -> None:
        if not items:
            return
        lines.extend(["", f"## {title}"])
        for item in items:
            lines.append(f"- {item}")

    add_bullet_section("Key Changes", structured.get("key_changes") or [])
    add_bullet_section("Completed Work", structured.get("work_completed") or [])
    add_bullet_section("Remaining Work", structured.get("work_remaining") or [])
    add_bullet_section("Safety Risks", structured.get("safety_risks") or [])
    add_bullet_section("Quality Risks", structured.get("quality_risks") or [])
    add_bullet_section("Material and Inventory Signals", structured.get("material_inventory_signals") or [])
    add_bullet_section("Water and Housekeeping Signals", structured.get("water_housekeeping_signals") or [])
    add_bullet_section("Uncertain Items", structured.get("uncertain_items") or [])
    add_bullet_section("Evidence Limitations", structured.get("evidence_limitations") or [])
    add_bullet_section("Immediate Decisions", structured.get("immediate_decisions") or [])
    add_bullet_section("Recommended Actions", structured.get("recommended_actions") or [])
    add_bullet_section("Angle Bias Notes", structured.get("angle_bias_notes") or [])

    timeline_observations = structured.get("timeline_observations") or []
    if timeline_observations:
        lines.extend(["", "## Timeline Observations"])
        for item in timeline_observations:
            photo_label = f"Photo #{item.get('photo_id')}" if item.get("photo_id") is not None else "Photo"
            lines.extend(
                [
                    "",
                    f"### {photo_label} · {item.get('captured_at') or 'unknown'}",
                    item.get("observation") or "No observation available.",
                ]
            )
            if item.get("progress_signal"):
                lines.append(f"- Progress signal: {item['progress_signal']}")
            if item.get("risk_signal"):
                lines.append(f"- Risk signal: {item['risk_signal']}")
    return "\n".join(lines).strip()


PROGRESS_REPORT_EXTRA_SECTION_TITLES = {
    "material_inventory_signals": {
        "zh": "材料与库存线索",
        "en": "Material and Inventory Signals",
        "es": "Senales de materiales e inventario",
    },
    "water_housekeeping_signals": {
        "zh": "积水与现场整理",
        "en": "Water and Housekeeping Signals",
        "es": "Senales de agua y orden del sitio",
    },
    "uncertain_items": {
        "zh": "不确定对象",
        "en": "Uncertain Items",
        "es": "Elementos inciertos",
    },
    "evidence_limitations": {
        "zh": "证据限制",
        "en": "Evidence Limitations",
        "es": "Limitaciones de evidencia",
    },
    "immediate_decisions": {
        "zh": "可立即决策事项",
        "en": "Immediate Decisions",
        "es": "Decisiones inmediatas",
    },
}


def _progress_extra_section_title(field_name: str, language: str) -> str:
    titles = PROGRESS_REPORT_EXTRA_SECTION_TITLES.get(field_name) or {}
    return titles.get(_normalize_progress_language(language)) or titles.get("en") or field_name


def _render_progress_report_markdown_localized(
    structured: dict[str, Any],
    *,
    language: str,
) -> str:
    translate_fn = translator(_normalize_progress_language(language))
    lines = [
        f"# {translate_fn('progress_report_result_title')}",
        "",
        f"## {translate_fn('progress_report_executive_summary')}",
        structured.get("executive_summary") or "No summary available.",
        "",
        f"## {translate_fn('progress_report_manager_brief')}",
        structured.get("manager_brief") or structured.get("executive_summary") or "No manager brief available.",
        "",
        f"## {translate_fn('progress_report_overall_status')}",
    ]
    overall_progress_percent = structured.get("overall_progress_percent")
    if overall_progress_percent is not None:
        lines.append(f"- {translate_fn('progress_report_overall_progress')}: {overall_progress_percent}%")
    overall_status_key = f"progress_status_{structured.get('overall_status') or 'unknown'}"
    confidence_key = f"progress_confidence_{structured.get('confidence_level') or 'medium'}"
    lines.append(
        f"- {translate_fn('progress_report_overall_status')}: "
        f"{translate_fn(overall_status_key)}"
    )
    lines.append(
        f"- {translate_fn('progress_report_confidence')}: "
        f"{translate_fn(confidence_key)}"
    )

    def add_bullet_section(title_key: str, items: list[str]) -> None:
        if not items:
            return
        lines.extend(["", f"## {translate_fn(title_key)}"])
        for item in items:
            lines.append(f"- {item}")

    add_bullet_section("progress_report_key_changes", structured.get("key_changes") or [])
    add_bullet_section("progress_report_work_completed", structured.get("work_completed") or [])
    add_bullet_section("progress_report_work_remaining", structured.get("work_remaining") or [])
    add_bullet_section("progress_report_safety_risks", structured.get("safety_risks") or [])
    add_bullet_section("progress_report_quality_risks", structured.get("quality_risks") or [])
    for extra_field in (
        "material_inventory_signals",
        "water_housekeeping_signals",
        "uncertain_items",
        "evidence_limitations",
        "immediate_decisions",
    ):
        items = structured.get(extra_field) or []
        if items:
            lines.extend(["", f"## {_progress_extra_section_title(extra_field, language)}"])
            for item in items:
                lines.append(f"- {item}")
    add_bullet_section("progress_report_recommended_actions", structured.get("recommended_actions") or [])
    add_bullet_section("progress_report_angle_notes", structured.get("angle_bias_notes") or [])

    timeline_observations = structured.get("timeline_observations") or []
    if timeline_observations:
        lines.extend(["", f"## {translate_fn('progress_report_timeline')}"])
        for item in timeline_observations:
            photo_label = f"Photo #{item.get('photo_id')}" if item.get("photo_id") is not None else "Photo"
            lines.extend(
                [
                    "",
                    f"### {photo_label} · {item.get('captured_at') or 'unknown'}",
                    item.get("observation") or "No observation available.",
                ]
            )
            if item.get("progress_signal"):
                lines.append(f"- {translate_fn('progress_report_progress_signal')}: {item['progress_signal']}")
            if item.get("risk_signal"):
                lines.append(f"- {translate_fn('progress_report_risk_signal')}: {item['risk_signal']}")
    return "\n".join(lines).strip()


def _embed_structured_progress_report(markdown_text: str, structured: dict[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(structured, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return "\n".join(
        [
            markdown_text.strip(),
            "",
            f"{PROGRESS_REPORT_EMBED_PREFIX}{payload}{PROGRESS_REPORT_EMBED_SUFFIX}",
        ]
    ).strip()


def _build_progress_report_translation_prompt(structured: dict[str, Any]) -> str:
    return "\n".join(
        [
            "You are a multilingual construction progress report translator.",
            "Translate the string values in the JSON object below into Simplified Chinese, English, and Spanish.",
            "Preserve keys, arrays, object structure, integers, timestamps, and photo_id values exactly.",
            "Do not translate enum values.",
            '- overall_status must stay one of "on_track", "at_risk", "blocked", "unknown".',
            '- confidence_level must stay one of "high", "medium", "low".',
            'Return one JSON object with exactly these top-level keys: "zh", "en", "es".',
            "Each of those keys must contain the translated report object.",
            "Return JSON only. Do not wrap in markdown fences.",
            "",
            json.dumps(structured, ensure_ascii=False, indent=2),
        ]
    )


def _looks_like_embedded_json_text(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text or not text.startswith("{"):
        return False
    return any(
        marker in text
        for marker in ('"executive_summary"', '"overall_progress_percent"', '"timeline_observations"')
    )


def _progress_structure_is_usable(structured: dict[str, Any]) -> bool:
    if not isinstance(structured, dict):
        return False
    if _looks_like_embedded_json_text(structured.get("executive_summary")):
        return False
    if _looks_like_embedded_json_text(structured.get("manager_brief")):
        return False
    for field in (
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
    ):
        for item in structured.get(field) or []:
            if _looks_like_embedded_json_text(item):
                return False
    for item in structured.get("timeline_observations") or []:
        if not isinstance(item, dict):
            continue
        for field in ("observation", "progress_signal", "risk_signal"):
            if _looks_like_embedded_json_text(item.get(field)):
                return False
    return True


def _translate_progress_text_value(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    source_text: str,
    content_label: str,
) -> tuple[dict[str, str], str | None, list[dict[str, Any]]]:
    normalized_source = _normalize_text(source_text, limit=4000)
    if not normalized_source:
        return {language: "" for language in VOICE_TRANSLATION_LANGUAGES}, None, []
    try:
        translations, backend_ref, attempts = translate_text_with_ollama_backends(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            source_text=normalized_source,
            content_label=content_label,
        )
        return translations, backend_ref, attempts
    except Exception:
        fallback = {language: normalized_source for language in VOICE_TRANSLATION_LANGUAGES}
        return fallback, None, []


def _translate_progress_report_localizations_fallback(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    structured_report: dict[str, Any],
    photos: list[Photo],
) -> tuple[dict[str, dict[str, Any]], str | None, list[dict[str, Any]]]:
    localized: dict[str, dict[str, Any]] = {
        language: _normalize_progress_report_structure(dict(structured_report), photos)
        for language in VOICE_TRANSLATION_LANGUAGES
    }
    backend_ref: str | None = None
    attempts: list[dict[str, Any]] = []

    scalar_fields = (
        ("executive_summary", "progress report executive summary"),
        ("manager_brief", "progress report manager brief"),
    )
    list_fields = (
        ("angle_bias_notes", "progress report angle bias note"),
        ("key_changes", "progress report key change"),
        ("work_completed", "progress report completed work item"),
        ("work_remaining", "progress report remaining work item"),
        ("safety_risks", "progress report safety risk"),
        ("quality_risks", "progress report quality risk"),
        ("material_inventory_signals", "progress report material and inventory signal"),
        ("water_housekeeping_signals", "progress report water and housekeeping signal"),
        ("uncertain_items", "progress report uncertain item"),
        ("evidence_limitations", "progress report evidence limitation"),
        ("immediate_decisions", "progress report immediate manager decision"),
        ("recommended_actions", "progress report recommended action"),
    )

    for field_name, content_label in scalar_fields:
        source_value = _normalize_text(structured_report.get(field_name), limit=4000)
        if not source_value:
            continue
        translations, current_backend_ref, current_attempts = _translate_progress_text_value(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            source_text=source_value,
            content_label=content_label,
        )
        backend_ref = backend_ref or current_backend_ref
        attempts.extend(current_attempts)
        for language in VOICE_TRANSLATION_LANGUAGES:
            localized[language][field_name] = _normalize_text(translations.get(language), limit=4000)

    for field_name, content_label in list_fields:
        source_items = list(structured_report.get(field_name) or [])
        translated_lists = {language: [] for language in VOICE_TRANSLATION_LANGUAGES}
        for item in source_items:
            source_value = _normalize_text(item, limit=4000)
            if not source_value:
                continue
            translations, current_backend_ref, current_attempts = _translate_progress_text_value(
                db,
                app_settings,
                tenant_slug=tenant_slug,
                source_text=source_value,
                content_label=content_label,
            )
            backend_ref = backend_ref or current_backend_ref
            attempts.extend(current_attempts)
            for language in VOICE_TRANSLATION_LANGUAGES:
                translated_lists[language].append(_normalize_text(translations.get(language), limit=500))
        for language in VOICE_TRANSLATION_LANGUAGES:
            localized[language][field_name] = translated_lists[language]

    source_timeline = structured_report.get("timeline_observations") or []
    for language in VOICE_TRANSLATION_LANGUAGES:
        localized[language]["timeline_observations"] = []
    for item in source_timeline:
        if not isinstance(item, dict):
            continue
        translated_item = {
            language: {
                "photo_id": item.get("photo_id"),
                "captured_at": item.get("captured_at"),
                "observation": "",
                "progress_signal": "",
                "risk_signal": "",
            }
            for language in VOICE_TRANSLATION_LANGUAGES
        }
        for field_name, content_label in (
            ("observation", "progress report timeline observation"),
            ("progress_signal", "progress report timeline progress signal"),
            ("risk_signal", "progress report timeline risk signal"),
        ):
            source_value = _normalize_text(item.get(field_name), limit=4000)
            if not source_value:
                continue
            translations, current_backend_ref, current_attempts = _translate_progress_text_value(
                db,
                app_settings,
                tenant_slug=tenant_slug,
                source_text=source_value,
                content_label=content_label,
            )
            backend_ref = backend_ref or current_backend_ref
            attempts.extend(current_attempts)
            for language in VOICE_TRANSLATION_LANGUAGES:
                translated_item[language][field_name] = _normalize_text(
                    translations.get(language),
                    limit=1200 if field_name == "observation" else 400,
                )
        for language in VOICE_TRANSLATION_LANGUAGES:
            localized[language]["timeline_observations"].append(translated_item[language])

    for language in VOICE_TRANSLATION_LANGUAGES:
        localized[language] = _normalize_progress_report_structure(localized[language], photos)
    return localized, backend_ref, attempts


def _translate_progress_report_localizations(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    backends: list[Any],
    structured_report: dict[str, Any],
    photos: list[Photo],
) -> tuple[dict[str, dict[str, Any]], str | None, list[dict[str, Any]]]:
    fallback = {
        language: _normalize_progress_report_structure(dict(structured_report), photos)
        for language in VOICE_TRANSLATION_LANGUAGES
    }
    if not backends:
        return _translate_progress_report_localizations_fallback(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            structured_report=structured_report,
            photos=photos,
        )
    try:
        raw_translations, backend_ref, attempts = generate_markdown_completion(
            backends=backends,
            prompt=_build_progress_report_translation_prompt(structured_report),
        )
        translation_payload = _extract_json_object(raw_translations)
        if not isinstance(translation_payload, dict):
            return fallback, backend_ref, attempts
        localized: dict[str, dict[str, Any]] = {}
        for language in VOICE_TRANSLATION_LANGUAGES:
            candidate = translation_payload.get(language)
            if isinstance(candidate, dict):
                localized[language] = _normalize_progress_report_structure(candidate, photos)
            else:
                localized[language] = fallback[language]
        if all(_progress_structure_is_usable(localized[language]) for language in VOICE_TRANSLATION_LANGUAGES):
            return localized, backend_ref, attempts
        fallback_localized, fallback_backend_ref, fallback_attempts = _translate_progress_report_localizations_fallback(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            structured_report=structured_report,
            photos=photos,
        )
        return fallback_localized, fallback_backend_ref or backend_ref, attempts + fallback_attempts
    except Exception:
        return _translate_progress_report_localizations_fallback(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            structured_report=structured_report,
            photos=photos,
        )


def _summary_excerpt_from_structured(structured: dict[str, Any]) -> str:
    return _truncate_text(
        structured.get("manager_brief") or structured.get("executive_summary") or "",
        180,
    )


def _decorate_progress_report_timeline(
    structured: dict[str, Any],
    photo_items: list[dict[str, Any]],
) -> dict[str, Any]:
    photo_by_id = {item["id"]: item for item in photo_items}
    timeline = []
    for item in structured.get("timeline_observations") or []:
        timeline_item = dict(item)
        photo = photo_by_id.get(item.get("photo_id"))
        if photo is not None:
            timeline_item["photo"] = {
                "id": photo["id"],
                "thumb_url": photo["thumb_url"],
                "image_url": photo["image_url"],
                "media_kind": photo["media_kind"],
                "captured_at_utc": photo["captured_at_utc"],
                "original_file_name": photo["original_file_name"],
            }
        timeline.append(timeline_item)
    decorated = dict(structured)
    decorated["timeline_observations"] = timeline
    return decorated


def serialize_progress_report(
    report: ProgressReport,
    *,
    settings: Settings,
    request=None,
    photos: list[Photo] | None = None,
) -> dict[str, Any]:
    photo_items: list[dict[str, Any]] = []
    if photos:
        for photo in photos:
            serialized = serialize_photo(photo, request, settings)
            photo_items.append(
                {
                    "id": serialized.id,
                    "project_id": serialized.project_id,
                    "employee_id": serialized.employee_id,
                    "original_file_name": serialized.original_file_name,
                    "thumb_url": serialized.thumb_url,
                    "image_url": serialized.image_url,
                    "media_kind": serialized.media_kind,
                    "captured_at_utc": serialized.captured_at_utc,
                }
            )
    selected_language = _normalize_progress_language(get_language(request) if request is not None else "en")
    rendered_report_content, embedded_payload = _extract_progress_report_payload_marker(report.report_content)
    payload_envelope = embedded_payload if isinstance(embedded_payload, dict) else None
    prompt_used = None
    project_prompt = None
    custom_prompt = None
    localized_structures: dict[str, dict[str, Any]] = {}
    raw_structured_payload = embedded_payload or _extract_json_object(report.report_content)
    if payload_envelope and (
        "structured_report" in payload_envelope
        or "translations" in payload_envelope
        or payload_envelope.get("version") == PROGRESS_REPORT_PAYLOAD_VERSION
    ):
        raw_structured_payload = payload_envelope.get("structured_report")
        prompt_used = payload_envelope.get("prompt_used")
        project_prompt = payload_envelope.get("project_prompt")
        custom_prompt = payload_envelope.get("custom_prompt")
        raw_translations = payload_envelope.get("translations")
        if isinstance(raw_translations, dict):
            for language in VOICE_TRANSLATION_LANGUAGES:
                candidate = raw_translations.get(language)
                if isinstance(candidate, dict):
                    localized_structures[language] = _normalize_progress_report_structure(candidate, photos or [])
    base_structured_report = (
        _normalize_progress_report_structure(raw_structured_payload, photos or [])
        if isinstance(raw_structured_payload, dict)
        else _fallback_progress_report_structure(rendered_report_content or report.report_content or "", photos or [])
    )
    for language in VOICE_TRANSLATION_LANGUAGES:
        localized_structures.setdefault(language, _normalize_progress_report_structure(dict(base_structured_report), photos or []))
    structured_report = _decorate_progress_report_timeline(
        localized_structures.get(selected_language) or base_structured_report,
        photo_items,
    )
    rendered_report_content = _render_progress_report_markdown_localized(
        structured_report,
        language=selected_language,
    )
    return {
        "report_id": report.id,
        "title": f"{report.project_id} Progress Compare",
        "project_id": report.project_id,
        "status": report.status.value,
        "source_photo_ids": report.source_photo_ids or [],
        "report_content": rendered_report_content,
        "raw_report_content": report.report_content,
        "error_message": report.error_message,
        "created_at": to_utc_iso(report.created_at),
        "updated_at": to_utc_iso(report.updated_at),
        "completed_at": to_utc_iso(report.completed_at),
        "photos": photo_items,
        "photo_count": len(report.source_photo_ids or []),
        "summary_excerpt": _summary_excerpt_from_structured(structured_report),
        "structured_report": structured_report,
        "selected_language": selected_language,
        "available_languages": list(VOICE_TRANSLATION_LANGUAGES),
        "prompt_used": prompt_used,
        "project_prompt": project_prompt,
        "custom_prompt": custom_prompt,
        "detail_url": f"/api/v2/reports/compare-progress/{report.id}",
        "portal_url": f"/portal/reports/progress/{report.id}",
    }


def build_report_markdown_prompt(photos: list[Photo], custom_prompt: str) -> str:
    photo_records = [_serialize_photo_for_report(photo) for photo in photos]
    return "\n".join(
        [
            "You are a construction reporting assistant.",
            "Generate a professional Markdown report only.",
            "Do not return JSON, code fences, or commentary outside the Markdown report.",
            "The report must include these sections when supported by the evidence:",
            "# Title",
            "## Executive Summary",
            "## Key Findings",
            "## Defects and Risks",
            "## Recommended Actions",
            "## Photo Evidence",
            "Rules:",
            "- Use only the evidence provided in the photo dataset.",
            "- Cite photo IDs in the Photo Evidence section.",
            "- Keep the tone factual and professional.",
            "- If evidence is missing, say that clearly instead of inventing details.",
            "User instruction:",
            custom_prompt.strip(),
            "Photo dataset (JSON):",
            json.dumps(photo_records, ensure_ascii=False, indent=2),
        ]
    )


def trim_markdown_report(markdown_text: str, max_chars: int) -> str:
    normalized = markdown_text.strip()
    if len(normalized) <= max_chars:
        return normalized
    logger.warning(
        "report_markdown_truncated",
        original_chars=len(normalized),
        max_chars=max_chars,
    )
    truncated = normalized[: max_chars - 120].rstrip()
    return "\n".join(
        [
            truncated,
            "",
            "## Output Truncated",
            "The generated report exceeded the configured size limit and was truncated before PDF rendering.",
        ]
    )


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _render_plain_pdf_fallback(markdown_text: str, title: str) -> bytes:
    wrapped_lines = [title, ""]
    for raw_line in markdown_text.splitlines():
        candidate = raw_line.strip() or " "
        wrapped_lines.extend(textwrap.wrap(candidate, width=92) or [" "])
    wrapped_lines = wrapped_lines[:60]

    content_lines = ["BT", "/F1 11 Tf", "50 790 Td", "14 TL"]
    for line in wrapped_lines:
        content_lines.append(f"({_pdf_escape(line)}) Tj")
        content_lines.append("T*")
    content_lines.append("ET")
    content_stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(content_stream), content_stream),
    ]

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def render_markdown_report_pdf(markdown_text: str, title: str) -> bytes:
    try:
        import markdown as markdown_lib
        from weasyprint import HTML
    except Exception as exc:  # pragma: no cover - runtime fallback
        logger.warning("report_pdf_fallback_renderer", reason="import_failed", error=str(exc))
        return _render_plain_pdf_fallback(markdown_text, title)

    body_html = markdown_lib.markdown(
        markdown_text,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
    )
    document_html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
    <style>
      @page {{
        margin: 18mm 14mm;
        size: A4;
      }}
      body {{
        font-family: "DejaVu Sans", sans-serif;
        color: #172033;
        font-size: 12px;
        line-height: 1.55;
      }}
      main {{
        padding: 0;
      }}
      h1, h2, h3 {{
        color: #0f172a;
        margin: 1.1em 0 0.45em;
      }}
      h1 {{
        font-size: 24px;
        border-bottom: 2px solid #d5dce6;
        padding-bottom: 8px;
      }}
      h2 {{
        font-size: 18px;
      }}
      h3 {{
        font-size: 14px;
      }}
      p, ul, ol {{
        margin: 0.45em 0;
      }}
      ul, ol {{
        padding-left: 20px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        margin: 14px 0;
      }}
      th, td {{
        border: 1px solid #d5dce6;
        padding: 8px 10px;
        vertical-align: top;
        text-align: left;
      }}
      th {{
        background: #eef2f7;
      }}
      code, pre {{
        background: #f4f6fa;
      }}
      pre {{
        padding: 10px;
        overflow: hidden;
      }}
      .meta {{
        color: #5b6472;
        font-size: 11px;
        margin-bottom: 18px;
      }}
    </style>
  </head>
  <body>
    <main>
      <div class="meta">Generated by KK Field Logger</div>
      {body_html}
    </main>
  </body>
</html>"""
    try:
        return HTML(string=document_html).write_pdf()
    except Exception as exc:  # pragma: no cover - runtime fallback
        logger.warning("report_pdf_fallback_renderer", reason="render_failed", error=str(exc))
        return _render_plain_pdf_fallback(markdown_text, title)


def serialize_generated_report(
    report: GeneratedReport,
    *,
    api_base: str = "/api/v2/reports",
    portal_download_base: str = "/portal/reports",
) -> dict[str, Any]:
    download_url = None
    portal_download_url = None
    if report.status == ReportStatus.completed and report.file_path:
        download_url = f"{api_base}/{report.public_id}/download"
        portal_download_url = f"{portal_download_base}/{report.public_id}/download"
    return {
        "report_id": report.public_id,
        "title": report.title,
        "status": report.status.value,
        "prompt": report.prompt,
        "error_message": report.error_message,
        "source_photo_ids": report.source_photo_ids or [],
        "created_at": to_utc_iso(report.created_at),
        "updated_at": to_utc_iso(report.updated_at),
        "completed_at": to_utc_iso(report.completed_at),
        "status_url": f"{api_base}/{report.public_id}",
        "download_url": download_url,
        "portal_download_url": portal_download_url,
    }


def queue_report_generation(
    db: Session,
    *,
    photos: list[Photo],
    prompt: str,
    schedule_task: Callable[..., None],
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    actor_user_id: int,
    company_id: str,
    tenant_id: str | None,
    source: str,
    ip_address: str | None = None,
) -> GeneratedReport:
    from app.services.job_queue import enqueue_report_task, schedule_job_worker

    normalized_prompt = prompt.strip()
    if not photos:
        raise ValueError("At least one photo is required")
    if not normalized_prompt:
        raise ValueError("Report prompt cannot be empty")
    if len(photos) > app_settings.report_max_selected_photos:
        raise ValueError(f"Too many photos selected for one report. Limit: {app_settings.report_max_selected_photos}")

    report = GeneratedReport(
        public_id=str(uuid4()),
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        created_by_user_id=actor_user_id,
        status=ReportStatus.queued,
        title=build_report_title(photos),
        prompt=normalized_prompt,
        source_photo_ids=[photo.id for photo in photos],
        mime_type="application/pdf",
    )
    db.add(report)
    db.flush()

    log_audit(
        db,
        action="report_generation_requested",
        target_type="report",
        target_id=report.public_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        ip_address=ip_address,
        detail_json={
            "source": source,
            "photo_ids": report.source_photo_ids,
            "photo_count": len(report.source_photo_ids or []),
            "title": report.title,
        },
    )

    enqueue_report_task(db, app_settings=app_settings, report=report)
    logger.info(
        "report_generation_requested",
        report_id=report.public_id,
        company_id=company_id,
        tenant_id=tenant_id or company_id,
        actor_user_id=actor_user_id,
        source=source,
        photo_count=len(report.source_photo_ids or []),
    )
    schedule_job_worker(schedule_task, session_maker, app_settings)
    return report


def retry_report_generation(
    db: Session,
    *,
    report: GeneratedReport,
    schedule_task: Callable[..., None],
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    actor_user_id: int,
    source: str,
    ip_address: str | None = None,
) -> GeneratedReport:
    from app.services.job_queue import enqueue_report_task, schedule_job_worker

    report.status = ReportStatus.queued
    report.error_message = None
    report.completed_at = None
    db.add(report)
    enqueue_report_task(db, app_settings=app_settings, report=report)
    log_audit(
        db,
        action="report_generation_retried",
        target_type="report",
        target_id=report.public_id,
        actor_user_id=actor_user_id,
        tenant_id=report.tenant_id or report.company_id,
        company_id=report.company_id,
        ip_address=ip_address,
        detail_json={
            "source": source,
            "photo_ids": report.source_photo_ids,
        },
    )
    logger.info(
        "report_generation_retried",
        report_id=report.public_id,
        company_id=report.company_id,
        tenant_id=report.tenant_id or report.company_id,
        actor_user_id=actor_user_id,
        source=source,
        photo_count=len(report.source_photo_ids or []),
    )
    schedule_job_worker(schedule_task, session_maker, app_settings)
    return report


def queue_progress_report_generation(
    db: Session,
    *,
    photos: list[Photo],
    custom_prompt: str | None = None,
    schedule_task: Callable[..., None],
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    actor_user_id: int,
    company_id: str,
    tenant_id: str | None,
    source: str,
    ip_address: str | None = None,
) -> ProgressReport:
    from app.services.job_queue import enqueue_progress_report_task, schedule_job_worker

    if len(photos) < 2:
        raise ValueError("At least two photos are required for a progress comparison report")
    if len(photos) > app_settings.progress_report_max_selected_photos:
        raise ValueError(
            f"Too many photos selected for one progress comparison. Limit: {app_settings.progress_report_max_selected_photos}"
        )

    requested_project_ids = {str(photo.project_id or "").strip() for photo in photos}
    if len(requested_project_ids) != 1 or "" in requested_project_ids:
        raise ValueError("Progress comparison requires photos from exactly one project")

    project_id = next(iter(requested_project_ids))
    project = db.scalar(select(Project).where(Project.project_id == project_id, Project.company_id == company_id))
    if project is None:
        raise ValueError("Selected photos do not belong to a valid project")
    normalized_custom_prompt = _normalize_progress_custom_prompt(custom_prompt)

    report = ProgressReport(
        id=str(uuid4()),
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        created_by_user_id=actor_user_id,
        project_id=project.project_id,
        status=ProgressReportStatus.pending,
        source_photo_ids=[photo.id for photo in photos],
    )
    db.add(report)
    db.flush()

    log_audit(
        db,
        action="progress_report_requested",
        target_type="progress_report",
        target_id=report.id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id or company_id,
        company_id=company_id,
        project_id=project.project_id,
        ip_address=ip_address,
        detail_json={
            "source": source,
            "photo_ids": report.source_photo_ids,
            "photo_count": len(report.source_photo_ids or []),
            "project_prompt_present": bool(str(project.image_video_ai_prompt or "").strip()),
            "custom_prompt_present": bool(normalized_custom_prompt),
        },
    )

    enqueue_progress_report_task(
        db,
        app_settings=app_settings,
        report=report,
        custom_prompt=normalized_custom_prompt,
    )
    logger.info(
        "progress_report_requested",
        report_id=report.id,
        company_id=company_id,
        tenant_id=tenant_id or company_id,
        actor_user_id=actor_user_id,
        project_id=project.project_id,
        source=source,
        photo_count=len(report.source_photo_ids or []),
    )
    schedule_job_worker(schedule_task, session_maker, app_settings)
    return report


def process_progress_report(
    report_id: str,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    *,
    custom_prompt: str | None = None,
    raise_on_failure: bool = False,
) -> None:
    started = perf_counter()
    with session_maker() as db:
        try:
            report = db.get(ProgressReport, report_id)
            if report is None:
                logger.warning("progress_report_generation_skipped", report_id=report_id, reason="missing_report")
                return

            report.status = ProgressReportStatus.processing
            report.error_message = None
            db.add(report)
            db.commit()
            db.refresh(report)

            source_ids = [int(photo_id) for photo_id in (report.source_photo_ids or [])]
            if len(source_ids) < 2:
                raise RuntimeError("Progress comparison requires at least two source photos")

            project = db.scalar(
                select(Project).where(
                    Project.project_id == report.project_id,
                    Project.company_id == report.company_id,
                )
            )
            if project is None:
                raise RuntimeError("Project settings for the progress report could not be loaded")

            photo_rows = list(
                db.scalars(
                    select(Photo).where(
                        Photo.company_id == report.company_id,
                        Photo.id.in_(source_ids),
                    )
                )
            )
            photo_map = {photo.id: photo for photo in photo_rows}
            photos = [photo_map[photo_id] for photo_id in source_ids if photo_id in photo_map]
            if len(photos) < 2:
                raise RuntimeError("Selected progress report photos could not be loaded")

            ordered_photos = sorted(
                photos,
                key=lambda item: (
                    item.captured_at_utc or item.created_at or utc_now(),
                    item.id,
                ),
            )
            images = _load_progress_report_images(ordered_photos)

            backends, config_source = resolve_ai_backends_for_tenant(db, app_settings, report.tenant_id or report.company_id)
            if not backends:
                raise RuntimeError("No enabled AI backends are configured")

            sync_ai_runtime_settings(db, app_settings)
            normalized_custom_prompt = _normalize_progress_custom_prompt(custom_prompt)
            generation_prompt = build_multi_image_progress_prompt(
                project,
                ordered_photos,
                custom_prompt=normalized_custom_prompt,
                existing_ai_contexts=_existing_progress_ai_contexts(db, ordered_photos),
                preferred_ai_hints=_preferred_sequence_ai_hints(db, ordered_photos),
            )
            raw_report_content, backend_ref, attempts = generate_multi_image_completion(
                backends=backends,
                prompt=generation_prompt,
                images=images,
            )
            raw_structured_payload = _extract_json_object(raw_report_content)
            structured_report = (
                _normalize_progress_report_structure(raw_structured_payload, ordered_photos)
                if raw_structured_payload is not None
                else _fallback_progress_report_structure(raw_report_content, ordered_photos)
            )
            localized_structures, translation_backend_ref, translation_attempts = _translate_progress_report_localizations(
                db,
                app_settings,
                tenant_slug=report.tenant_id or report.company_id,
                backends=backends,
                structured_report=structured_report,
                photos=ordered_photos,
            )
            payload_object = {
                "version": PROGRESS_REPORT_PAYLOAD_VERSION,
                "prompt_used": generation_prompt,
                "project_prompt": _progress_prompt_text(project),
                "custom_prompt": normalized_custom_prompt,
                "structured_report": structured_report,
                "translations": localized_structures,
                "translation_backend_ref": translation_backend_ref,
                "translation_attempts": translation_attempts,
            }
            report_content = _embed_structured_progress_report(
                _render_progress_report_markdown(structured_report),
                payload_object,
            )

            report.status = ProgressReportStatus.completed
            report.report_content = report_content.strip()
            report.error_message = None
            report.completed_at = utc_now()
            db.add(report)
            log_audit(
                db,
                action="progress_report_completed",
                target_type="progress_report",
                target_id=report.id,
                actor_user_id=report.created_by_user_id,
                tenant_id=report.tenant_id or report.company_id,
                company_id=report.company_id,
                project_id=report.project_id,
                detail_json={
                    "config_source": config_source,
                    "backend_ref": backend_ref,
                    "photo_ids": report.source_photo_ids,
                    "attempts": attempts,
                },
            )
            db.commit()
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.info(
                "progress_report_completed",
                report_id=report.id,
                company_id=report.company_id,
                tenant_id=report.tenant_id or report.company_id,
                project_id=report.project_id,
                duration_ms=duration_ms,
                photo_count=len(report.source_photo_ids or []),
                fallback_count=len([attempt for attempt in attempts if attempt["status"] == "failed"]),
                backend_ref=backend_ref,
                config_source=config_source,
            )
        except Exception as exc:
            db.rollback()
            report = db.get(ProgressReport, report_id)
            if report is not None:
                report.status = ProgressReportStatus.failed
                report.error_message = str(exc)
                db.add(report)
                log_audit(
                    db,
                    action="progress_report_failed",
                    target_type="progress_report",
                    target_id=report.id,
                    actor_user_id=report.created_by_user_id,
                    tenant_id=report.tenant_id or report.company_id,
                    company_id=report.company_id,
                    project_id=report.project_id,
                    detail_json={"error": str(exc), "photo_ids": report.source_photo_ids},
                )
                db.commit()
            logger.warning("progress_report_failed", report_id=report_id, error=str(exc))
            if raise_on_failure:
                raise


def process_generated_report(
    report_id: int,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    raise_on_failure: bool = False,
) -> None:
    started = perf_counter()
    with session_maker() as db:
        try:
            report = db.get(GeneratedReport, report_id)
            if report is None:
                logger.warning(
                    "report_generation_skipped",
                    report_id=report_id,
                    reason="missing_report",
                )
                return

            source_ids = [int(photo_id) for photo_id in (report.source_photo_ids or [])]
            if not source_ids:
                raise RuntimeError("No source photos were provided for report generation")

            photo_rows = list(
                db.scalars(
                    select(Photo).where(
                        Photo.company_id == report.company_id,
                        Photo.id.in_(source_ids),
                    )
                )
            )
            photo_map = {photo.id: photo for photo in photo_rows}
            photos = [photo_map[photo_id] for photo_id in source_ids if photo_id in photo_map]
            if not photos:
                raise RuntimeError("Selected report photos could not be loaded")

            backends, config_source = resolve_ai_backends_for_tenant(db, app_settings, report.tenant_id or report.company_id)
            if not backends:
                raise RuntimeError("No enabled AI backends are configured")

            sync_ai_runtime_settings(db, app_settings)
            generation_prompt = build_report_markdown_prompt(photos, report.prompt)
            markdown_content, backend_ref, attempts = generate_markdown_completion(
                backends=backends,
                prompt=generation_prompt,
            )
            markdown_content = trim_markdown_report(markdown_content, app_settings.report_max_markdown_chars)
            pdf_bytes = render_markdown_report_pdf(markdown_content, report.title)

            report_dir = _report_directory(app_settings, report)
            report_dir.mkdir(parents=True, exist_ok=True)
            file_path = report_dir / _sanitize_report_filename(report.title, report.public_id)
            file_path.write_bytes(pdf_bytes)

            report.status = ReportStatus.completed
            report.markdown_content = markdown_content
            report.file_path = str(file_path)
            report.error_message = None
            report.completed_at = utc_now()
            db.add(report)

            log_audit(
                db,
                action="report_generation_completed",
                target_type="report",
                target_id=report.public_id,
                actor_user_id=report.created_by_user_id,
                tenant_id=report.tenant_id or report.company_id,
                company_id=report.company_id,
                detail_json={
                    "config_source": config_source,
                    "backend_ref": backend_ref,
                    "photo_ids": report.source_photo_ids,
                    "file_path": str(file_path),
                    "attempts": attempts,
                },
            )
            db.commit()
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.info(
                "report_generation_completed",
                report_id=report.public_id,
                company_id=report.company_id,
                tenant_id=report.tenant_id or report.company_id,
                file_path=str(file_path),
                duration_ms=duration_ms,
                photo_count=len(report.source_photo_ids or []),
                fallback_count=len([attempt for attempt in attempts if attempt["status"] == "failed"]),
                backend_ref=backend_ref,
                config_source=config_source,
            )
        except Exception as exc:
            db.rollback()
            report = db.get(GeneratedReport, report_id)
            if report is not None:
                report.status = ReportStatus.failed
                report.error_message = str(exc)
                db.add(report)
                log_audit(
                    db,
                    action="report_generation_failed",
                    target_type="report",
                    target_id=report.public_id,
                    actor_user_id=report.created_by_user_id,
                    tenant_id=report.tenant_id or report.company_id,
                    company_id=report.company_id,
                    detail_json={
                        "photo_ids": report.source_photo_ids,
                        "error": str(exc),
                    },
                )
                db.commit()
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.exception(
                "report_generation_failed",
                report_id=report.public_id if report is not None else report_id,
                company_id=report.company_id if report is not None else None,
                tenant_id=(report.tenant_id or report.company_id) if report is not None else None,
                duration_ms=duration_ms,
                error=str(exc),
            )
            if raise_on_failure:
                raise
