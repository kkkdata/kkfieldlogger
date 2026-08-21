from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import to_utc_iso, utc_now
from app.models import LensDefinition, Photo, PhotoLensObservation, Project


LENS_SECTION_TITLES = {
    "scene_context": "Scene Context",
    "spatial_anchor": "Location and Area Clues",
    "visible_text": "Visible Text and Labels",
    "work_progress": "Work Progress Evidence",
    "defect_surface": "Defect or Surface Conditions",
    "tools_equipment": "Tools and Equipment",
    "materials_visible": "Visible Materials",
    "remediation_evidence": "Remediation Evidence",
    "concealed_work": "Concealed Work Clues",
    "safety_observables": "Safety Observables",
}

EVIDENCE_ALLOWED_CONFIDENCE = {"high", "medium"}
EVIDENCE_BLOCKED_TERMS = (
    "bar_",
    "foo_",
    "lorem",
    "placeholder",
    "a photo of",
    "project-",
    "not applicable",
    "n/a",
    "unknown",
)
EVIDENCE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
VISIBLE_TEXT_RE = re.compile(r"('[^']{2,}'|\"[^\"]{2,}\"|[A-Z][A-Z0-9-]{2,}|\d{2,})")
DISPLAY_EVIDENCE_LENSES = {"defect_surface", "materials_visible", "visible_text"}
GENERIC_EVIDENCE_VALUES = {
    "yes",
    "no",
    "building",
    "construction",
    "construction site",
    "industrial",
    "people",
    "good",
    "wall",
    "floor",
    "in progress",
}
LENS_BLOCKED_CONTEXT_TERMS = {
    "defect_surface": (
        "cardboard",
        "box",
        "paper",
        "document",
        "drawing",
        "plan",
        "book",
        "manual",
        "table",
    ),
    "materials_visible": (
        "table-like",
        "table- like",
        "paper",
        "document",
        "drawing",
        "plan",
    ),
    "remediation_evidence": (
        "plan",
        "drawing",
        "document",
        "paper",
        "book",
        "manual",
        "table",
        "laptop",
        "page",
        "model",
    ),
    "safety_observables": (
        "cardboard",
        "box",
        "paper",
        "document",
        "writing",
        "table",
        "book",
        "plan",
        "drawing",
    ),
    "tools_equipment": (
        "manual",
        "book",
        "page",
        "paper",
        "document",
        "drawing",
        "building design",
        "3d model",
        "table",
    ),
    "work_progress": (
        "3d model",
        "drawing",
        "document",
        "paper",
        "plan",
    ),
    "visible_text": (
        "document with a table",
        "2-page document",
        "3d model",
        "building in the background",
    ),
}


def build_project_manager_lens_report(
    db: Session,
    *,
    company_id: str,
    project_id: str,
    window_days: int,
    max_photos: int = 200,
    max_evidence_per_lens: int = 5,
) -> dict[str, Any]:
    window_days = max(1, min(int(window_days), 365))
    max_photos = max(1, min(int(max_photos), 500))
    max_evidence_per_lens = max(1, min(int(max_evidence_per_lens), 12))
    now_value = utc_now()
    window_start = now_value - timedelta(days=window_days)
    project = db.scalar(select(Project).where(Project.company_id == company_id, Project.project_id == project_id))
    if project is None:
        raise ValueError(f"Project {project_id} was not found for company {company_id}")

    photos = list(
        db.scalars(
            select(Photo)
            .where(
                Photo.company_id == company_id,
                Photo.project_id == project_id,
                Photo.deleted.is_(False),
                Photo.soft_deleted_at.is_(None),
                Photo.captured_at_utc >= window_start,
            )
            .order_by(Photo.captured_at_utc.desc(), Photo.id.desc())
            .limit(max_photos)
        )
    )
    photo_by_id = {photo.id: photo for photo in photos}
    photo_ids = list(photo_by_id)
    observations: list[PhotoLensObservation] = []
    lens_by_id: dict[str, LensDefinition] = {}
    if photo_ids:
        observations = list(
            db.scalars(
                select(PhotoLensObservation)
                .where(
                    PhotoLensObservation.photo_id.in_(photo_ids),
                    PhotoLensObservation.status == "active",
                )
                .order_by(PhotoLensObservation.created_at.desc())
            )
        )
        lens_ids = sorted({row.lens_id for row in observations})
        if lens_ids:
            lens_by_id = {lens.id: lens for lens in db.scalars(select(LensDefinition).where(LensDefinition.id.in_(lens_ids)))}

    lens_sections = _lens_sections(
        observations,
        photo_by_id=photo_by_id,
        lens_by_id=lens_by_id,
        max_evidence_per_lens=max_evidence_per_lens,
    )
    timeline = _timeline(photos, observations, lens_by_id=lens_by_id)
    daily_summary = _daily_capture_summary(photos, observations, lens_by_id=lens_by_id)
    validation_counts = Counter(row.validation_status for row in observations)
    observed_lens_count = sum(1 for row in observations if row.observed_count > 0 and row.validation_status == "shadow_valid")
    fallback_count = int(validation_counts.get("shadow_fallback_valid", 0))
    filtered_evidence_count = sum(int(section.get("filtered_evidence_count", 0)) for section in lens_sections)
    accepted_evidence_count = sum(len(section.get("evidence_lines", [])) for section in lens_sections)
    return {
        "schema_version": "project_manager_lens_report_v1",
        "status": "shadow_only",
        "company_id": company_id,
        "project_id": project_id,
        "project": {
            "project_name": project.project_name,
            "client_name": project.client_name,
            "location": project.location,
            "status": project.status.value if hasattr(project.status, "value") else str(project.status),
        },
        "window": {
            "days": window_days,
            "start_utc": to_utc_iso(window_start),
            "end_utc": to_utc_iso(now_value),
            "photos_scanned": len(photos),
            "max_photos": max_photos,
        },
        "summary_card": {
            "photos": len(photos),
            "active_recorders": len({photo.employee_id for photo in photos if photo.employee_id}),
            "captured_day_count": len({photo.captured_at_utc.date().isoformat() for photo in photos if photo.captured_at_utc}),
            "observation_count": len(observations),
            "observed_lens_count": observed_lens_count,
            "fallback_lens_count": fallback_count,
            "fallback_ratio": round(fallback_count / len(observations), 4) if observations else 0,
            "accepted_evidence_lines": accepted_evidence_count,
            "filtered_evidence_candidates": filtered_evidence_count,
            "first_captured_at": to_utc_iso(min((photo.captured_at_utc for photo in photos), default=None)),
            "last_captured_at": to_utc_iso(max((photo.captured_at_utc for photo in photos), default=None)),
            "first_upload_at": to_utc_iso(min((photo.created_at for photo in photos), default=None)),
            "last_upload_at": to_utc_iso(max((photo.created_at for photo in photos), default=None)),
        },
        "daily_capture_summary": daily_summary,
        "timeline": timeline,
        "lens_sections": lens_sections,
        "open_confirmation": [
            "Completion percentage is not inferred from photos.",
            "Fallback or uncertain lens outputs stay shadow-only and are separated from observed evidence.",
            "Formal project status still requires manager review of representative photos and notes.",
        ],
        "fact_refs": [
            {"table": "photos", "field_path": "photos.project_window.count", "observed_value": len(photos)},
            {
                "table": "photo_lens_observations",
                "field_path": "photo_lens_observations.project_window.active_count",
                "observed_value": len(observations),
            },
        ],
    }


def render_project_manager_lens_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary_card") if isinstance(report.get("summary_card"), dict) else {}
    lines = [
        f"# Project Manager Lens Report: {report.get('project_id')}",
        "",
        "## Summary",
        f"- Photos scanned: {summary.get('photos', 0)}",
        f"- Active recorders: {summary.get('active_recorders', 0)}",
        f"- Captured days: {summary.get('captured_day_count', 0)}",
        f"- Lens observations: {summary.get('observation_count', 0)}",
        f"- Observed evidence lens results: {summary.get('observed_lens_count', 0)}",
        f"- Fallback lens results: {summary.get('fallback_lens_count', 0)} ({summary.get('fallback_ratio', 0)})",
        f"- Accepted evidence lines: {summary.get('accepted_evidence_lines', 0)}",
        f"- Filtered evidence candidates: {summary.get('filtered_evidence_candidates', 0)}",
    ]
    daily_summary = report.get("daily_capture_summary") if isinstance(report.get("daily_capture_summary"), list) else []
    if daily_summary:
        lines.extend(["", "## Daily Capture Summary"])
        for day in daily_summary[:14]:
            lenses = ", ".join(str(item) for item in day.get("dominant_lenses", []))
            lines.append(
                f"- {day.get('captured_date')}: {day.get('photo_count')} photos, employees "
                f"{', '.join(day.get('employee_ids', [])) or 'unknown'}, observed lenses {lenses or 'none'}"
            )
    timeline = report.get("timeline") if isinstance(report.get("timeline"), list) else []
    if timeline:
        lines.extend(["", "## Timeline"])
        for bucket in timeline[:12]:
            photo_ids = ", ".join(str(item) for item in bucket.get("representative_photo_ids", []))
            lenses = ", ".join(str(item) for item in bucket.get("dominant_lenses", []))
            lines.append(
                f"- {bucket.get('time_range')}: {bucket.get('photo_count')} photos, employees "
                f"{', '.join(bucket.get('employee_ids', [])) or 'unknown'}, lenses {lenses or 'none'}, photos {photo_ids or 'none'}"
            )
    sections = report.get("lens_sections") if isinstance(report.get("lens_sections"), list) else []
    if sections:
        lines.extend(["", "## Lens Sections"])
        for section in sections:
            lines.append(f"### {section.get('title')}")
            lines.append(
                f"- observed photos: {section.get('observed_photo_count', 0)}, "
                f"uncertain/fallback photos: {section.get('uncertain_or_fallback_photo_count', 0)}"
            )
            lines.append(
                f"- accepted evidence: {len(section.get('evidence_lines', []))}, "
                f"filtered candidates: {section.get('filtered_evidence_count', 0)}"
            )
            reps = ", ".join(str(item) for item in section.get("representative_photo_ids", []))
            lines.append(f"- representative photos: {reps or 'none'}")
            for evidence in section.get("evidence_lines", [])[:5]:
                lines.append(f"- {evidence}")
    confirmations = report.get("open_confirmation") if isinstance(report.get("open_confirmation"), list) else []
    if confirmations:
        lines.extend(["", "## Confirmation Notes"])
        lines.extend(f"- {item}" for item in confirmations)
    return "\n".join(lines)


def _lens_sections(
    observations: list[PhotoLensObservation],
    *,
    photo_by_id: dict[int, Photo],
    lens_by_id: dict[str, LensDefinition],
    max_evidence_per_lens: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[PhotoLensObservation]] = defaultdict(list)
    for row in observations:
        lens = lens_by_id.get(row.lens_id)
        lens_key = lens.lens_key if lens is not None else row.lens_id
        grouped[lens_key].append(row)

    sections: list[dict[str, Any]] = []
    for lens_key, rows in sorted(grouped.items()):
        observed_rows = [row for row in rows if row.observed_count > 0 and row.validation_status == "shadow_valid"]
        uncertain_rows = [
            row
            for row in rows
            if row.validation_status != "shadow_valid" or row.uncertain_count > 0 or row.state_summary == "uncertain"
        ]
        evidence_lines: list[str] = []
        evidence_seen: set[str] = set()
        representative_photo_ids: list[int] = []
        item_counter: Counter[str] = Counter()
        filtered_counter: Counter[str] = Counter()
        for row in observed_rows:
            payload = row.observation_json if isinstance(row.observation_json, dict) else {}
            for item in payload.get("items") or []:
                ok, reason = _is_project_manager_evidence(item, lens_key=lens_key)
                if not ok:
                    filtered_counter[reason] += 1
                    continue
                if row.photo_id not in representative_photo_ids:
                    representative_photo_ids.append(row.photo_id)
                key = str(item.get("key") or "").strip()
                if key:
                    item_counter[key] += 1
                if len(evidence_lines) < max_evidence_per_lens:
                    value = _short_inline(item.get("value"), 90)
                    evidence = _short_inline(item.get("evidence"), 140)
                    line = f"photo {row.photo_id}: {key or 'observed'}"
                    if value:
                        line += f" = {value}"
                    if evidence:
                        line += f" ({evidence})"
                    if line not in evidence_seen:
                        evidence_lines.append(line)
                        evidence_seen.add(line)
        sections.append(
            {
                "lens_key": lens_key,
                "title": LENS_SECTION_TITLES.get(lens_key, lens_key.replace("_", " ").title()),
                "observed_photo_count": len({row.photo_id for row in observed_rows}),
                "uncertain_or_fallback_photo_count": len({row.photo_id for row in uncertain_rows}),
                "representative_photo_ids": representative_photo_ids[:max_evidence_per_lens],
                "dominant_item_keys": [key for key, _ in item_counter.most_common(6)],
                "evidence_lines": evidence_lines,
                "filtered_evidence_count": sum(filtered_counter.values()),
                "filtered_evidence_reasons": dict(filtered_counter.most_common()),
                "fact_refs": [
                    {
                        "table": "photo_lens_observations",
                        "field_path": f"lens_sections.{lens_key}.observed_photo_count",
                        "observed_value": len({row.photo_id for row in observed_rows}),
                    }
                ],
            }
        )
    return sections


def _timeline(
    photos: list[Photo],
    observations: list[PhotoLensObservation],
    *,
    lens_by_id: dict[str, LensDefinition],
) -> list[dict[str, Any]]:
    by_photo_lenses: dict[int, Counter[str]] = defaultdict(Counter)
    for row in observations:
        if row.observed_count <= 0 or row.validation_status != "shadow_valid":
            continue
        lens = lens_by_id.get(row.lens_id)
        lens_key = lens.lens_key if lens is not None else row.lens_id
        by_photo_lenses[row.photo_id][lens_key] += row.observed_count

    buckets: dict[str, dict[str, Any]] = {}
    for photo in photos:
        hour = photo.captured_at_utc.replace(minute=0, second=0, microsecond=0)
        key = to_utc_iso(hour) or "unknown"
        bucket = buckets.setdefault(
            key,
            {
                "time_range": key,
                "photo_count": 0,
                "employee_ids": set(),
                "representative_photo_ids": [],
                "lens_counter": Counter(),
            },
        )
        bucket["photo_count"] += 1
        if photo.employee_id:
            bucket["employee_ids"].add(photo.employee_id)
        if len(bucket["representative_photo_ids"]) < 5:
            bucket["representative_photo_ids"].append(photo.id)
        bucket["lens_counter"].update(by_photo_lenses.get(photo.id, Counter()))

    result: list[dict[str, Any]] = []
    for key in sorted(buckets, reverse=True):
        bucket = buckets[key]
        result.append(
            {
                "time_range": bucket["time_range"],
                "photo_count": bucket["photo_count"],
                "employee_ids": sorted(bucket["employee_ids"]),
                "dominant_lenses": [lens for lens, _ in bucket["lens_counter"].most_common(5)],
                "representative_photo_ids": bucket["representative_photo_ids"],
            }
        )
    return result


def _daily_capture_summary(
    photos: list[Photo],
    observations: list[PhotoLensObservation],
    *,
    lens_by_id: dict[str, LensDefinition],
) -> list[dict[str, Any]]:
    by_photo_lenses: dict[int, Counter[str]] = defaultdict(Counter)
    for row in observations:
        if row.observed_count <= 0 or row.validation_status != "shadow_valid":
            continue
        lens = lens_by_id.get(row.lens_id)
        lens_key = lens.lens_key if lens is not None else row.lens_id
        by_photo_lenses[row.photo_id][lens_key] += row.observed_count

    buckets: dict[str, dict[str, Any]] = {}
    for photo in photos:
        key = photo.captured_at_utc.date().isoformat()
        bucket = buckets.setdefault(
            key,
            {
                "captured_date": key,
                "photo_count": 0,
                "employee_ids": set(),
                "representative_photo_ids": [],
                "lens_counter": Counter(),
            },
        )
        bucket["photo_count"] += 1
        if photo.employee_id:
            bucket["employee_ids"].add(photo.employee_id)
        if len(bucket["representative_photo_ids"]) < 8:
            bucket["representative_photo_ids"].append(photo.id)
        bucket["lens_counter"].update(by_photo_lenses.get(photo.id, Counter()))

    result: list[dict[str, Any]] = []
    for key in sorted(buckets, reverse=True):
        bucket = buckets[key]
        result.append(
            {
                "captured_date": bucket["captured_date"],
                "photo_count": bucket["photo_count"],
                "employee_ids": sorted(bucket["employee_ids"]),
                "dominant_lenses": [lens for lens, _ in bucket["lens_counter"].most_common(5)],
                "representative_photo_ids": bucket["representative_photo_ids"],
            }
        )
    return result


def _is_project_manager_evidence(item: Any, *, lens_key: str) -> tuple[bool, str]:
    if not isinstance(item, dict):
        return False, "not_object"
    if item.get("state") != "observed":
        return False, "not_observed"
    if lens_key not in DISPLAY_EVIDENCE_LENSES:
        return False, "section_summary_only"
    confidence = str(item.get("confidence") or "").strip().lower()
    if confidence not in EVIDENCE_ALLOWED_CONFIDENCE:
        return False, "low_or_missing_confidence"
    key = " ".join(str(item.get("key") or "").split())
    value = " ".join(str(item.get("value") or "").split())
    evidence = " ".join(str(item.get("evidence") or "").split())
    combined = f"{key} {value} {evidence}".lower()
    if any(ord(ch) > 127 for ch in f"{value} {evidence}"):
        return False, "non_ascii_or_corrupt_text"
    if any(term in combined for term in EVIDENCE_BLOCKED_TERMS):
        return False, "placeholder_or_unknown_text"
    if value.lower() in GENERIC_EVIDENCE_VALUES:
        return False, "generic_value"
    if lens_key in LENS_BLOCKED_CONTEXT_TERMS and any(term in combined for term in LENS_BLOCKED_CONTEXT_TERMS[lens_key]):
        return False, "lens_context_mismatch"
    if lens_key == "visible_text" and not VISIBLE_TEXT_RE.search(f"{value} {evidence}"):
        return False, "no_specific_visible_text"
    if lens_key == "spatial_anchor" and ("table" in combined or "paper" in combined or "document" in combined):
        return False, "not_spatial_anchor"
    if not value and not evidence:
        return False, "empty_evidence"
    if value.isdigit() and len(evidence) < 24:
        return False, "numeric_without_context"
    if len(value) < 3 and len(evidence) < 24:
        return False, "too_short"
    if not EVIDENCE_WORD_RE.search(f"{value} {evidence}"):
        return False, "no_meaningful_words"
    return True, "accepted"


def _short_inline(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
