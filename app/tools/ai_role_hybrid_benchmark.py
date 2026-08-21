from __future__ import annotations

import argparse
import json
import requests
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
    AIBackendError,
    AIBackendNode,
    DEFAULT_VOICE_TRANSLATION_OLLAMA_MODEL,
    OLLAMA_TYPE,
    _build_backend_order,
    _clear_backend_cooldown,
    _read_photo_payload,
    build_ai_prompt,
    build_evidence_engine_result,
    build_manager_summary_from_role_reviews,
    call_ai_backend,
    resolve_ai_backends,
    run_ai_role_reviews,
)
from app.services.ai_role_prompts import PRIORITY_P0, PRIORITY_P1, PRIORITY_P2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one vision fact extraction plus text role reviews without DB writes.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--company-id", default=None)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--summary-output", default=None)
    parser.add_argument("--two-phase", action="store_true", help="Run all vision extraction first, unload vision model, then run text roles.")
    return parser.parse_args()


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"/tmp/kk_role_hybrid_benchmark_{timestamp}.jsonl")


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
        "note": photo.note or "",
    }


def _write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str))
        handle.write("\n")


def _text_role_backends(backends: list[AIBackendNode]) -> list[AIBackendNode]:
    text_backends: list[AIBackendNode] = []
    for backend in backends:
        if backend.type != OLLAMA_TYPE:
            text_backends.append(backend)
            continue
        text_backends.append(
            AIBackendNode(
                id=f"{backend.id}-text",
                type=backend.type,
                url=backend.url,
                model=DEFAULT_VOICE_TRANSLATION_OLLAMA_MODEL,
                api_key=backend.api_key,
                embedding_model=backend.embedding_model,
                weight=backend.weight,
                enabled=backend.enabled,
            )
        )
    return text_backends


def _stop_ollama_model(backends: list[AIBackendNode], model_name: str) -> None:
    seen_urls: set[str] = set()
    for backend in backends:
        if backend.type != OLLAMA_TYPE or not backend.url or backend.url in seen_urls:
            continue
        seen_urls.add(backend.url)
        endpoint = backend.url.rstrip("/") + "/api/generate"
        try:
            requests.post(endpoint, json={"model": model_name, "keep_alive": 0}, timeout=20)
        except Exception as exc:
            print("ollama_stop_failed", backend.url, model_name, exc)


def _summarize(path: Path) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    role_status_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    durations: list[float] = []
    vision_durations: list[float] = []
    role_durations: list[float] = []
    completed = 0
    if not path.exists():
        return {"completed": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            status_counts["invalid_jsonl"] += 1
            continue
        status = str(row.get("status") or "unknown")
        status_counts[status] += 1
        if status != "completed":
            continue
        completed += 1
        for key, bucket in (
            ("duration_seconds", durations),
            ("vision_duration_seconds", vision_durations),
            ("role_duration_seconds", role_durations),
        ):
            value = row.get(key)
            if isinstance(value, (int, float)):
                bucket.append(float(value))
        for item in (row.get("role_reviews") or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("role_id") or "")
            role_status = str(item.get("status") or "")
            if role_id and role_status:
                role_status_counts[f"{role_id}:{role_status}"] += 1
            if role_status == "risk":
                risk_counts[role_id] += 1
            if role_status == "warning":
                warning_counts[role_id] += 1

    def avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 2) if values else 0.0

    return {
        "completed": completed,
        "status_counts": dict(status_counts),
        "avg_duration_seconds": avg(durations),
        "avg_vision_duration_seconds": avg(vision_durations),
        "avg_role_duration_seconds": avg(role_durations),
        "top_risk_roles": dict(risk_counts.most_common(20)),
        "top_warning_roles": dict(warning_counts.most_common(20)),
        "role_status_counts": dict(role_status_counts.most_common()),
    }


def main() -> None:
    args = parse_args()
    settings = load_settings()
    output_path = Path(args.output) if args.output else _default_output_path()
    summary_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    engine = create_engine(settings.database_url)
    Session = sessionmaker(bind=engine)
    priorities = {PRIORITY_P0, PRIORITY_P1, PRIORITY_P2}

    with Session() as db:
        stmt = (
            select(Photo)
            .where(Photo.deleted.is_(False), Photo.photo_type == PhotoType.project)
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .limit(max(1, args.limit))
        )
        if args.company_id:
            stmt = stmt.where(Photo.company_id == args.company_id)
        if args.project_id:
            stmt = stmt.where(Photo.project_id == args.project_id)
        photos = list(db.scalars(stmt).all())
        print("ai_role_hybrid_benchmark", "photos", len(photos), "output", output_path)

        if args.two_phase:
            vision_rows: list[dict[str, Any]] = []
            all_backends: list[AIBackendNode] = []
            print("phase", "vision", "photos", len(photos))
            for index, photo in enumerate(photos, start=1):
                started = time.monotonic()
                context = _photo_context(photo)
                try:
                    backends, config_source = resolve_ai_backends(db, settings, photo)
                    if not all_backends:
                        all_backends = backends
                    image_base64, mime_type = _read_photo_payload(photo)
                    prompt = build_ai_prompt(db, photo)
                    structured_result: dict[str, Any] | None = None
                    model_used = ""
                    attempts: list[dict[str, Any]] = []
                    for backend in _build_backend_order(backends):
                        try:
                            structured_result = call_ai_backend(
                                backend,
                                prompt=prompt,
                                image_base64=image_base64,
                                mime_type=mime_type,
                            )
                            _clear_backend_cooldown(backend)
                            model_used = f"{backend.type}:{backend.model}"
                            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "success"})
                            break
                        except AIBackendError as exc:
                            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "failed", "error": exc.message})
                    if structured_result is None:
                        raise RuntimeError("All configured vision backends failed")
                    payload = build_evidence_engine_result(structured_result, photo_context=context)
                    vision_duration = round(time.monotonic() - started, 2)
                    vision_rows.append(
                        {
                            "sample_index": index,
                            "photo_id": photo.id,
                            "vision_duration_seconds": vision_duration,
                            "config_source": config_source,
                            "model_used": model_used,
                            "vision_attempts": attempts,
                            "photo_context": context,
                            "vision_payload": payload,
                            "backends": backends,
                        }
                    )
                    _write_jsonl(
                        output_path.with_suffix(".vision.jsonl"),
                        {
                            "status": "vision_completed",
                            "sample_index": index,
                            "photo_id": photo.id,
                            "vision_duration_seconds": vision_duration,
                            "model_used": model_used,
                            "photo_context": context,
                            "vision_payload": payload,
                        },
                    )
                    print("vision_completed", index, "photo", photo.id, "seconds", vision_duration)
                except Exception as exc:
                    _write_jsonl(
                        output_path,
                        {
                            "status": "failed",
                            "sample_index": index,
                            "photo_id": photo.id,
                            "duration_seconds": round(time.monotonic() - started, 2),
                            "photo_context": context,
                            "error": str(exc),
                        },
                    )
                    print("vision_failed", index, "photo", photo.id, "error", exc)

            for backend in all_backends:
                if backend.type == OLLAMA_TYPE:
                    _stop_ollama_model(all_backends, backend.model)
            print("phase", "roles", "photos", len(vision_rows))
            for row in vision_rows:
                started = time.monotonic()
                try:
                    text_backends = _text_role_backends(row["backends"])
                    role_started = time.monotonic()
                    role_reviews = run_ai_role_reviews(
                        backends=text_backends,
                        payload=row["vision_payload"],
                        photo_context=row["photo_context"],
                        priorities=priorities,
                    )
                    role_duration = round(time.monotonic() - role_started, 2)
                    manager_summary = build_manager_summary_from_role_reviews(
                        role_reviews,
                        fallback_summary=row["vision_payload"].get("ai_summary"),
                    )
                    completed = {
                        "status": "completed",
                        "sample_index": row["sample_index"],
                        "photo_id": row["photo_id"],
                        "duration_seconds": round(row["vision_duration_seconds"] + role_duration, 2),
                        "vision_duration_seconds": row["vision_duration_seconds"],
                        "role_duration_seconds": role_duration,
                        "config_source": row["config_source"],
                        "model_used": row["model_used"],
                        "text_models": [f"{backend.type}:{backend.model}" for backend in text_backends],
                        "vision_attempts": row["vision_attempts"],
                        "photo_context": row["photo_context"],
                        "vision_payload": row["vision_payload"],
                        "manager_summary": manager_summary,
                        "role_reviews": role_reviews,
                    }
                    _write_jsonl(output_path, completed)
                    print("completed", row["sample_index"], "photo", row["photo_id"], "seconds", completed["duration_seconds"], "roles", role_duration)
                except Exception as exc:
                    _write_jsonl(
                        output_path,
                        {
                            "status": "failed",
                            "sample_index": row["sample_index"],
                            "photo_id": row["photo_id"],
                            "duration_seconds": round(time.monotonic() - started, 2),
                            "photo_context": row["photo_context"],
                            "error": str(exc),
                        },
                    )
                    print("role_failed", row["sample_index"], "photo", row["photo_id"], "error", exc)

            summary = _summarize(output_path)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print("summary", summary_path)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return

        for index, photo in enumerate(photos, start=1):
            started = time.monotonic()
            context = _photo_context(photo)
            try:
                backends, config_source = resolve_ai_backends(db, settings, photo)
                image_base64, mime_type = _read_photo_payload(photo)
                prompt = build_ai_prompt(db, photo)
                vision_started = time.monotonic()
                structured_result: dict[str, Any] | None = None
                model_used = ""
                attempts: list[dict[str, Any]] = []
                for backend in _build_backend_order(backends):
                    try:
                        structured_result = call_ai_backend(
                            backend,
                            prompt=prompt,
                            image_base64=image_base64,
                            mime_type=mime_type,
                        )
                        _clear_backend_cooldown(backend)
                        model_used = f"{backend.type}:{backend.model}"
                        attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "success"})
                        break
                    except AIBackendError as exc:
                        attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "failed", "error": exc.message})
                if structured_result is None:
                    raise RuntimeError("All configured vision backends failed")
                vision_duration = round(time.monotonic() - vision_started, 2)
                payload = build_evidence_engine_result(structured_result, photo_context=context)
                role_started = time.monotonic()
                text_backends = _text_role_backends(backends)
                role_reviews = run_ai_role_reviews(
                    backends=text_backends,
                    payload=payload,
                    photo_context=context,
                    priorities=priorities,
                )
                role_duration = round(time.monotonic() - role_started, 2)
                manager_summary = build_manager_summary_from_role_reviews(
                    role_reviews,
                    fallback_summary=payload.get("ai_summary"),
                )
                row = {
                    "status": "completed",
                    "sample_index": index,
                    "photo_id": photo.id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "vision_duration_seconds": vision_duration,
                    "role_duration_seconds": role_duration,
                    "config_source": config_source,
                    "model_used": model_used,
                    "text_models": [f"{backend.type}:{backend.model}" for backend in text_backends],
                    "vision_attempts": attempts,
                    "photo_context": context,
                    "vision_payload": payload,
                    "manager_summary": manager_summary,
                    "role_reviews": role_reviews,
                }
                _write_jsonl(output_path, row)
                print("completed", index, "photo", photo.id, "seconds", row["duration_seconds"], "vision", vision_duration, "roles", role_duration)
            except Exception as exc:
                _write_jsonl(
                    output_path,
                    {
                        "status": "failed",
                        "sample_index": index,
                        "photo_id": photo.id,
                        "duration_seconds": round(time.monotonic() - started, 2),
                        "photo_context": context,
                        "error": str(exc),
                    },
                )
                print("failed", index, "photo", photo.id, "error", exc)

    summary = _summarize(output_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("summary", summary_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
