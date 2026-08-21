from __future__ import annotations

import argparse
from copy import deepcopy

from sqlalchemy import create_engine, select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import sessionmaker

from app.core.config import load_settings
from app.core.time import to_utc_iso
from app.models import AIAnalysisLog, AIAnalysisStatus, Photo
from app.services.ai_pipeline import resolve_ai_backends_for_tenant, run_ai_role_reviews
from app.services.ai_role_prompts import PRIORITY_P0, PRIORITY_P1, role_catalog_payload
from app.services.evidence_copilot import sync_photo_evidence_bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AI role prompts against existing photo AI facts.")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--photo-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--priorities", default="P0,P1")
    parser.add_argument("--catalog", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.catalog:
        print(role_catalog_payload())
        return

    settings = load_settings()
    engine = create_engine(settings.database_url)
    Session = sessionmaker(bind=engine)
    priorities = {item.strip() for item in args.priorities.split(",") if item.strip()} or {PRIORITY_P0, PRIORITY_P1}

    with Session() as db:
        backends, config_source = resolve_ai_backends_for_tenant(db, settings, args.company_id)
        stmt = (
            select(AIAnalysisLog, Photo)
            .join(Photo, AIAnalysisLog.photo_id == Photo.id)
            .where(
                AIAnalysisLog.status == AIAnalysisStatus.active,
                Photo.company_id == args.company_id,
                Photo.deleted.is_(False),
            )
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
            .limit(max(1, args.limit))
        )
        if args.project_id:
            stmt = stmt.where(Photo.project_id == args.project_id)
        if args.photo_id:
            stmt = stmt.where(Photo.id.in_(args.photo_id))
        rows = list(db.execute(stmt).all())
        print("role_smoke_count", len(rows), "priorities", sorted(priorities), "config_source", config_source)
        for log, photo in rows:
            payload = deepcopy(log.result_data) if isinstance(log.result_data, dict) else {}
            photo_context = {
                "photo_id": photo.id,
                "photo_type": photo.photo_type.value if hasattr(photo.photo_type, "value") else str(photo.photo_type),
                "company_id": photo.company_id,
                "project_id": photo.project_id,
                "employee_id": photo.employee_id,
                "gps": photo.gps,
                "gps_lat": photo.gps_lat,
                "gps_lon": photo.gps_lon,
                "location": photo.location,
                "captured_at_utc": to_utc_iso(photo.captured_at_utc),
            }
            role_reviews = run_ai_role_reviews(
                backends=backends,
                payload=payload,
                photo_context=photo_context,
                priorities=priorities,
            )
            payload["ai_role_reviews"] = role_reviews
            log.result_data = payload
            photo.tag_json = payload
            flag_modified(log, "result_data")
            flag_modified(photo, "tag_json")
            db.add(log)
            db.add(photo)
            sync_photo_evidence_bundle(db, photo)
            summary = role_reviews["summary"]
            print(
                "photo",
                photo.id,
                "roles",
                summary["role_count"],
                "risk",
                summary["risk_roles"],
                "warning",
                summary["warning_roles"],
                "retry",
                summary["retry_roles"][:8],
            )
        db.commit()


if __name__ == "__main__":
    main()
