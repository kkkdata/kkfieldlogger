from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import load_settings
from app.core.time import to_utc_iso
from app.models import Photo, PhotoType
from app.services.ai_pipeline import (
    _read_photo_payload,
    build_manager_summary_from_role_reviews,
    resolve_ai_backends_for_tenant,
    run_ai_role_vision_reviews,
)
from app.services.ai_role_prompts import selected_role_prompts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run direct-image AI role benchmark on existing photos without changing production AI summaries."
    )
    parser.add_argument("--company-id", default=None, help="Optional company_id filter.")
    parser.add_argument("--project-id", default=None, help="Optional project_id filter.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum photos to process.")
    parser.add_argument(
        "--order",
        choices=("latest", "oldest"),
        default="latest",
        help="Photo sample order. Defaults to latest uploads.",
    )
    parser.add_argument(
        "--photo-id",
        action="append",
        type=int,
        default=[],
        help="Specific photo id to include. Can be repeated.",
    )
    parser.add_argument(
        "--role-id",
        action="append",
        default=[],
        help="Specific role id to run. Can be repeated. Defaults to all roles.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="JSONL output path. Defaults to /tmp/kk_role_vision_benchmark_<timestamp>.jsonl.",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Summary JSON path. Defaults to output path with .summary.json suffix.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print selected photos and roles.")
    return parser.parse_args()


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _photo_context(photo: Photo) -> dict[str, Any]:
    return {
        "photo_id": photo.id,
        "photo_type": _enum_value(photo.photo_type),
        "company_id": photo.company_id,
        "project_id": photo.project_id,
        "employee_id": photo.employee_id,
        "gps": photo.gps,
        "gps_lat": photo.gps_lat,
        "gps_lon": photo.gps_lon,
        "location": photo.location,
        "captured_at_utc": to_utc_iso(photo.captured_at_utc),
        "created_at_utc": to_utc_iso(photo.created_at),
        "note": photo.note or "",
        "mime_type": photo.mime_type or "",
        "original_file_name": photo.original_file_name or "",
    }


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"/tmp/kk_role_vision_benchmark_{timestamp}.jsonl")


def _write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str))
        handle.write("\n")


def _load_processed_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    processed: set[int] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("status") == "completed" and isinstance(payload.get("photo_id"), int):
                processed.add(payload["photo_id"])
    return processed


def _summarize_output(path: Path) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    role_status_counts: Counter[str] = Counter()
    role_risk_counts: Counter[str] = Counter()
    role_warning_counts: Counter[str] = Counter()
    role_retry_counts: Counter[str] = Counter()
    people_counts: Counter[str] = Counter()
    durations: list[float] = []
    completed = 0

    if not path.exists():
        return {"output": str(path), "completed": 0}

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                status_counts["invalid_jsonl"] += 1
                continue
            status = str(payload.get("status") or "unknown")
            status_counts[status] += 1
            if status != "completed":
                continue
            completed += 1
            duration = payload.get("duration_seconds")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
            role_reviews = payload.get("role_reviews") if isinstance(payload.get("role_reviews"), dict) else {}
            for item in role_reviews.get("results") or []:
                if not isinstance(item, dict):
                    continue
                role_id = str(item.get("role_id") or "")
                role_status = str(item.get("status") or "")
                if role_id and role_status:
                    role_status_counts[f"{role_id}:{role_status}"] += 1
                if role_status == "risk":
                    role_risk_counts[role_id] += 1
                if role_status == "warning":
                    role_warning_counts[role_id] += 1
                if item.get("should_retry"):
                    role_retry_counts[role_id] += 1
                if role_id == "visible_people_count":
                    people_counts[str(item.get("numeric_value"))] += 1

    avg_duration = round(sum(durations) / len(durations), 2) if durations else 0
    return {
        "output": str(path),
        "completed": completed,
        "status_counts": dict(status_counts),
        "avg_duration_seconds": avg_duration,
        "min_duration_seconds": round(min(durations), 2) if durations else 0,
        "max_duration_seconds": round(max(durations), 2) if durations else 0,
        "top_risk_roles": dict(role_risk_counts.most_common(20)),
        "top_warning_roles": dict(role_warning_counts.most_common(20)),
        "top_retry_roles": dict(role_retry_counts.most_common(20)),
        "visible_people_count_distribution": dict(people_counts.most_common()),
        "role_status_counts": dict(role_status_counts.most_common()),
    }


def main() -> None:
    args = parse_args()
    settings = load_settings()
    output_path = Path(args.output) if args.output else _default_output_path()
    summary_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    role_ids = {item.strip() for item in args.role_id if item.strip()} or None
    selected_roles = selected_role_prompts(role_ids=role_ids)

    engine = create_engine(settings.database_url)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        stmt = select(Photo).where(Photo.deleted.is_(False), Photo.photo_type == PhotoType.project)
        if args.company_id:
            stmt = stmt.where(Photo.company_id == args.company_id)
        if args.project_id:
            stmt = stmt.where(Photo.project_id == args.project_id)
        if args.photo_id:
            stmt = stmt.where(Photo.id.in_(args.photo_id))
        if args.order == "oldest":
            stmt = stmt.order_by(Photo.created_at.asc(), Photo.id.asc())
        else:
            stmt = stmt.order_by(Photo.created_at.desc(), Photo.id.desc())
        stmt = stmt.limit(max(1, args.limit))
        photos = list(db.scalars(stmt).all())

        print(
            "ai_role_vision_benchmark",
            "photos",
            len(photos),
            "roles",
            len(selected_roles),
            "output",
            output_path,
        )
        if args.dry_run:
            print("photo_ids", [photo.id for photo in photos])
            print("role_ids", [role.role_id for role in selected_roles])
            return

        processed_ids = _load_processed_ids(output_path)
        for index, photo in enumerate(photos, start=1):
            if photo.id in processed_ids:
                print("skip_completed", photo.id)
                continue
            started = time.monotonic()
            context = _photo_context(photo)
            try:
                backends, config_source = resolve_ai_backends_for_tenant(db, settings, photo.company_id)
                image_base64, mime_type = _read_photo_payload(photo)
                role_reviews = run_ai_role_vision_reviews(
                    backends=backends,
                    image_base64=image_base64,
                    mime_type=mime_type,
                    photo_context=context,
                    role_ids=role_ids,
                )
                manager_summary = build_manager_summary_from_role_reviews(role_reviews)
                duration = round(time.monotonic() - started, 2)
                row = {
                    "status": "completed",
                    "sample_index": index,
                    "photo_id": photo.id,
                    "duration_seconds": duration,
                    "config_source": config_source,
                    "photo_context": context,
                    "manager_summary": manager_summary,
                    "role_reviews": role_reviews,
                }
                _write_jsonl(output_path, row)
                print(
                    "completed",
                    index,
                    "photo",
                    photo.id,
                    "seconds",
                    duration,
                    "roles",
                    role_reviews.get("summary", {}).get("role_count"),
                )
            except Exception as exc:
                duration = round(time.monotonic() - started, 2)
                _write_jsonl(
                    output_path,
                    {
                        "status": "failed",
                        "sample_index": index,
                        "photo_id": photo.id,
                        "duration_seconds": duration,
                        "photo_context": context,
                        "error": str(exc),
                    },
                )
                print("failed", index, "photo", photo.id, "seconds", duration, "error", exc)

    summary = _summarize_output(output_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("summary", summary_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
