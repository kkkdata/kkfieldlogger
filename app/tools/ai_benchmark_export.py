from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import load_settings

if TYPE_CHECKING:
    from app.models.entities import AIAnalysisLog


GENERIC_SUMMARY_MARKERS = (
    "appears to show",
    "image shows",
    "photo shows",
    "construction site",
    "work area",
    "various",
    "some objects",
    "several items",
)
RISK_MARKERS = (
    "risk",
    "hazard",
    "unsafe",
    "danger",
    "defect",
    "issue",
    "concern",
    "missing",
    "blocked",
    "obstruction",
    "damage",
    "leak",
    "standing water",
)
OVERCONFIDENT_MARKERS = (
    "clearly",
    "definitely",
    "confirms",
    "shows that",
    "is present",
    "requires",
    "must",
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
FIELD_LISTS = (
    "visible_objects",
    "materials",
    "equipment",
    "people_ppe",
    "safety_observations",
    "quality_observations",
    "inventory_observations",
    "water_or_housekeeping_observations",
    "recommended_actions",
    "evidence_limitations",
)


@dataclass(frozen=True)
class BenchmarkRow:
    photo_id: int
    analysis_log_id: str
    company_id: str
    project_id: str | None
    employee_id: str | None
    photo_type: str
    model_used: str
    created_at: str
    confidence_level: str
    issue_flags: list[str]
    ai_summary: str
    defects: list[str]
    labels: list[str]
    recommended_actions: list[str]
    evidence_limitations: list[str]


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [_text(item) for item in values if _text(item)]


def _payload(log: "AIAnalysisLog") -> dict[str, Any]:
    return log.result_data if isinstance(log.result_data, dict) else {}


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(marker in normalized for marker in markers)


def classify_ai_output(payload: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    summary = _text(payload.get("ai_summary"))
    summary_folded = summary.casefold()
    defects = _items(payload.get("defects"))
    recommended_actions = _items(payload.get("recommended_actions"))
    evidence_limitations = _items(payload.get("evidence_limitations"))
    confidence_level = _text(payload.get("confidence_level")).casefold()
    evidence_engine = payload.get("evidence_engine") if isinstance(payload.get("evidence_engine"), dict) else {}
    engine_summary = evidence_engine.get("summary") if isinstance(evidence_engine.get("summary"), dict) else {}
    engine_flags = _items(engine_summary.get("pollution_flags"))
    flags.extend(f"engine:{flag}" for flag in engine_flags)

    if not summary:
        flags.append("missing_summary")
    elif len(summary.split()) < 10 or _has_any(summary, GENERIC_SUMMARY_MARKERS):
        flags.append("generic_summary")

    if _has_any(summary, RISK_MARKERS) and not defects:
        flags.append("risk_language_without_defect")

    if confidence_level == "low" and _has_any(summary, OVERCONFIDENT_MARKERS) and not _has_any(summary, UNCERTAINTY_MARKERS):
        flags.append("low_confidence_overassertive")

    if "construction site" in summary_folded and not any(
        _items(payload.get(field_name)) for field_name in ("materials", "equipment", "people_ppe")
    ):
        flags.append("construction_overclaim")

    if defects and not recommended_actions:
        flags.append("defect_without_action")

    if confidence_level == "low" and not evidence_limitations:
        flags.append("low_confidence_without_limitations")

    populated_structured_fields = sum(1 for field_name in FIELD_LISTS if _items(payload.get(field_name)))
    if populated_structured_fields <= 2:
        flags.append("thin_structured_output")

    return flags


def collect_rows(db: Session, *, company_id: str | None, project_id: str | None, limit: int) -> list[BenchmarkRow]:
    from app.models.entities import AIAnalysisLog, AIAnalysisStatus, Photo

    stmt = (
        select(AIAnalysisLog, Photo)
        .join(Photo, AIAnalysisLog.photo_id == Photo.id)
        .where(AIAnalysisLog.status == AIAnalysisStatus.active)
        .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        .limit(limit)
    )
    if company_id:
        stmt = stmt.where(Photo.company_id == company_id)
    if project_id:
        stmt = stmt.where(Photo.project_id == project_id)

    rows: list[BenchmarkRow] = []
    for log, photo in db.execute(stmt).all():
        payload = _payload(log)
        rows.append(
            BenchmarkRow(
                photo_id=photo.id,
                analysis_log_id=log.id,
                company_id=photo.company_id,
                project_id=photo.project_id,
                employee_id=photo.employee_id,
                photo_type=photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type),
                model_used=log.model_used,
                created_at=log.created_at.isoformat() if log.created_at else "",
                confidence_level=_text(payload.get("confidence_level")),
                issue_flags=classify_ai_output(payload),
                ai_summary=_text(payload.get("ai_summary")),
                defects=_items(payload.get("defects")),
                labels=_items(payload.get("labels")),
                recommended_actions=_items(payload.get("recommended_actions")),
                evidence_limitations=_items(payload.get("evidence_limitations")),
            )
        )
    return rows


def write_csv(rows: list[BenchmarkRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "photo_id",
                "analysis_log_id",
                "company_id",
                "project_id",
                "employee_id",
                "photo_type",
                "model_used",
                "created_at",
                "confidence_level",
                "issue_flags",
                "ai_summary",
                "defects",
                "labels",
                "recommended_actions",
                "evidence_limitations",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "photo_id": row.photo_id,
                    "analysis_log_id": row.analysis_log_id,
                    "company_id": row.company_id,
                    "project_id": row.project_id or "",
                    "employee_id": row.employee_id or "",
                    "photo_type": row.photo_type,
                    "model_used": row.model_used,
                    "created_at": row.created_at,
                    "confidence_level": row.confidence_level,
                    "issue_flags": "|".join(row.issue_flags),
                    "ai_summary": row.ai_summary,
                    "defects": json.dumps(row.defects, ensure_ascii=False),
                    "labels": json.dumps(row.labels, ensure_ascii=False),
                    "recommended_actions": json.dumps(row.recommended_actions, ensure_ascii=False),
                    "evidence_limitations": json.dumps(row.evidence_limitations, ensure_ascii=False),
                }
            )


def write_summary(rows: list[BenchmarkRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flag_counts = Counter(flag for row in rows for flag in row.issue_flags)
    model_counts = Counter(row.model_used for row in rows)
    summary = {
        "sample_count": len(rows),
        "flag_counts": dict(flag_counts.most_common()),
        "model_counts": dict(model_counts.most_common()),
        "clean_count": sum(1 for row in rows if not row.issue_flags),
    }
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a read-only AI benchmark sample from existing analysis logs.")
    parser.add_argument("--database-url", default=None, help="Override database URL. Defaults to configured KK_DATABASE_URL.")
    parser.add_argument("--company-id", default=None, help="Optional company_id filter.")
    parser.add_argument("--project-id", default=None, help="Optional project_id filter.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum active AI log rows to sample.")
    parser.add_argument("--output", default="ai_benchmark_sample.csv", help="CSV output path.")
    parser.add_argument("--summary-output", default="ai_benchmark_summary.json", help="JSON summary output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    database_url = args.database_url or settings.database_url
    engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        rows = collect_rows(db, company_id=args.company_id, project_id=args.project_id, limit=max(1, args.limit))
    write_csv(rows, Path(args.output))
    write_summary(rows, Path(args.summary_output))
    print(f"Exported {len(rows)} benchmark rows to {args.output}")
    print(f"Wrote issue summary to {args.summary_output}")


if __name__ == "__main__":
    main()
