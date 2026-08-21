from __future__ import annotations

import base64
from collections import deque
import json
import math
import mimetypes
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, perf_counter, sleep
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import requests
from requests import RequestException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.observability import get_logger
from app.core.time import to_utc_iso
from app.models import AIAnalysisLog, AIAnalysisStatus, AIAnalysisType, MediaAsset, Photo, PhotoType, Project, Tenant
from app.services.audit import log_audit
from app.services.ai_evidence_engine import build_evidence_engine_result
from app.services.ai_role_prompts import (
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    build_role_prompt,
    build_role_vision_prompt,
    extract_role_json,
    normalize_role_result,
    selected_role_prompts,
)
from app.services.cache import TTLMemoryCache
from app.services.settings import get_system_settings

logger = get_logger("kkfieldlogger.ai_pipeline")

AI_REQUEST_TIMEOUT_SECONDS = 180
DEFAULT_AI_BACKENDS_JSON = "[]"
OLLAMA_TYPE = "ollama"
GEMINI_TYPE = "gemini"
EMBEDDING_DIMENSIONS = 768
DEFAULT_OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"
DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_AI_MAX_CONCURRENT_REQUESTS = 3
MIN_AI_MAX_CONCURRENT_REQUESTS = 1
MAX_AI_MAX_CONCURRENT_REQUESTS = 10
DEFAULT_AI_ENABLE_DYNAMIC_FALLBACK = True
DEFAULT_AI_BACKEND_FAILURE_COOLDOWN_SECONDS = 120
DEFAULT_AI_RATE_LIMIT_PER_MINUTE = 300
MIN_AI_RATE_LIMIT_PER_MINUTE = 1
MAX_AI_RATE_LIMIT_PER_MINUTE = 2000
AI_RATE_LIMIT_WINDOW_SECONDS = 60.0
AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS = 10
MAX_NORMALIZED_LABEL_COUNT = 8
DEFAULT_VOICE_TRANSCRIPTION_MODEL = "small"
DEFAULT_VOICE_TRANSLATION_OLLAMA_MODEL = "qwen2.5:7b-instruct"
VOICE_TRANSLATION_LANGUAGES = ("zh", "en", "es")
SAMPLE_TEST_IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9WnQ+e4AAAAASUVORK5CYII="
)
SAMPLE_TEST_IMAGE_MIME_TYPE = "image/png"
MIN_AUDITABLE_IMAGE_BYTES = 512

_BACKEND_ROTATION_LOCK = threading.Lock()
_BACKEND_ROTATION_INDEX: dict[tuple[tuple[str, int, str, str, str], ...], int] = {}
_AI_DYNAMIC_FALLBACK_LOCK = threading.Lock()
_AI_DYNAMIC_FALLBACK_ENABLED = DEFAULT_AI_ENABLE_DYNAMIC_FALLBACK
_AI_BACKEND_FAILURE_COOLDOWN_LOCK = threading.Lock()
_AI_BACKEND_FAILURE_COOLDOWN_SECONDS = DEFAULT_AI_BACKEND_FAILURE_COOLDOWN_SECONDS
_AI_BACKEND_COOLDOWN_UNTIL: dict[tuple[str, str, str, str | None], float] = {}
SEMANTIC_SEARCH_CACHE = TTLMemoryCache(default_ttl_seconds=30, max_entries=256)
_FASTER_WHISPER_MODEL_CACHE_LOCK = threading.Lock()
_FASTER_WHISPER_MODEL_CACHE: dict[str, Any] = {}
_OLLAMA_RESOURCE_ERROR_MARKERS = (
    "oom",
    "out of memory",
    "insufficient memory",
    "insufficient gpu memory",
    "cuda",
    "vram",
    "gpu memory",
    "timed out",
    "timeout",
    "deadline exceeded",
)
_OLLAMA_MULTI_IMAGE_SINGLE_IMAGE_MARKERS = (
    "only supports one image",
    "more than one image requested",
)
OLLAMA_MULTI_IMAGE_FALLBACK_MODEL = "llava:latest"
OLLAMA_VISION_GENERATION_OPTIONS = {"temperature": 0.0, "top_p": 0.75, "repeat_penalty": 1.45, "num_predict": 1300}
OLLAMA_VISION_REPAIR_GENERATION_OPTIONS = {"temperature": 0.0, "top_p": 0.7, "repeat_penalty": 1.15, "num_predict": 700}
_BACKEND_TRANSIENT_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",
    "remote end closed connection",
    "remote disconnected",
    "max retries exceeded",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "502",
    "503",
    "504",
)


def _coerce_ai_rate_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AI_RATE_LIMIT_PER_MINUTE
    return max(MIN_AI_RATE_LIMIT_PER_MINUTE, min(MAX_AI_RATE_LIMIT_PER_MINUTE, parsed))


def _coerce_ai_concurrency_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AI_MAX_CONCURRENT_REQUESTS
    return max(MIN_AI_MAX_CONCURRENT_REQUESTS, min(MAX_AI_MAX_CONCURRENT_REQUESTS, parsed))


def _coerce_ai_backend_failure_cooldown_seconds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AI_BACKEND_FAILURE_COOLDOWN_SECONDS
    return max(0, parsed)


class AIConcurrencyLimiter:
    def __init__(self, initial_limit: int):
        self._lock = threading.Lock()
        self._limit = _coerce_ai_concurrency_limit(initial_limit)
        self._pending_shrink = 0
        self._active = 0
        self._semaphore = threading.Semaphore(self._limit)

    def current_limit(self) -> int:
        with self._lock:
            return self._limit

    def configure(self, new_limit: int) -> int:
        normalized_limit = _coerce_ai_concurrency_limit(new_limit)
        with self._lock:
            previous_limit = self._limit
            if normalized_limit == previous_limit:
                return self._limit

            if normalized_limit > previous_limit:
                delta = normalized_limit - previous_limit
                absorbed_delta = min(self._pending_shrink, delta)
                self._pending_shrink -= absorbed_delta
                for _ in range(delta - absorbed_delta):
                    self._semaphore.release()
            else:
                delta = previous_limit - normalized_limit
                absorbed_now = 0
                while absorbed_now < delta and self._semaphore.acquire(blocking=False):
                    absorbed_now += 1
                self._pending_shrink += delta - absorbed_now

            self._limit = normalized_limit
            active_requests = self._active
            pending_shrink = self._pending_shrink

        logger.info(
            "ai_concurrency_limit_updated",
            previous_limit=previous_limit,
            configured_limit=normalized_limit,
            active_requests=active_requests,
            pending_shrink=pending_shrink,
        )
        return normalized_limit

    @contextmanager
    def acquire(self, *, operation: str, backend: AIBackendNode | None = None) -> Iterator[None]:
        wait_started = perf_counter()
        self._semaphore.acquire()
        wait_ms = round((perf_counter() - wait_started) * 1000, 2)
        with self._lock:
            self._active += 1
            active_requests = self._active
            configured_limit = self._limit
            pending_shrink = self._pending_shrink
        logger.info(
            "ai_concurrency_acquired",
            operation=operation,
            backend_id=backend.id if backend is not None else None,
            backend_type=backend.type if backend is not None else None,
            active_requests=active_requests,
            configured_limit=configured_limit,
            pending_shrink=pending_shrink,
            wait_ms=wait_ms,
        )
        try:
            yield
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
                if self._pending_shrink > 0:
                    self._pending_shrink -= 1
                    should_release = False
                else:
                    should_release = True
                active_requests = self._active
                configured_limit = self._limit
                pending_shrink = self._pending_shrink
            if should_release:
                self._semaphore.release()
            logger.info(
                "ai_concurrency_released",
                operation=operation,
                backend_id=backend.id if backend is not None else None,
                backend_type=backend.type if backend is not None else None,
                active_requests=active_requests,
                configured_limit=configured_limit,
                pending_shrink=pending_shrink,
            )

    def record_dynamic_fallback(
        self,
        *,
        operation: str,
        backend: AIBackendNode | None,
        error_message: str,
        fallback_backend: AIBackendNode | None,
    ) -> bool:
        if backend is None or backend.type != OLLAMA_TYPE:
            return False
        if not dynamic_fallback_enabled():
            return False
        normalized_error = str(error_message or "").strip()
        if not _looks_like_ollama_resource_error(normalized_error):
            return False
        logger.warning(
            "ai_dynamic_fallback_triggered",
            operation=operation,
            backend_id=backend.id,
            backend_type=backend.type,
            backend_model=backend.model,
            fallback_backend_id=fallback_backend.id if fallback_backend is not None else None,
            fallback_backend_type=fallback_backend.type if fallback_backend is not None else None,
            fallback_backend_model=fallback_backend.model if fallback_backend is not None else None,
            error=normalized_error,
        )
        return True


class AIRateLimiter:
    def __init__(self, initial_limit_per_minute: int):
        self._lock = threading.Lock()
        self._limit_per_minute = _coerce_ai_rate_limit(initial_limit_per_minute)
        self._timestamps: deque[float] = deque()

    def configure(self, new_limit_per_minute: int) -> int:
        normalized_limit = _coerce_ai_rate_limit(new_limit_per_minute)
        with self._lock:
            previous_limit = self._limit_per_minute
            self._limit_per_minute = normalized_limit
        if previous_limit != normalized_limit:
            logger.info(
                "ai_rate_limit_updated",
                previous_limit=previous_limit,
                configured_limit=normalized_limit,
            )
        return normalized_limit

    def current_limit(self) -> int:
        with self._lock:
            return self._limit_per_minute

    @contextmanager
    def acquire(self, *, operation: str, backend: AIBackendNode | None = None) -> Iterator[None]:
        wait_ms = 0.0
        while True:
            with self._lock:
                now = monotonic()
                while self._timestamps and now - self._timestamps[0] >= AI_RATE_LIMIT_WINDOW_SECONDS:
                    self._timestamps.popleft()
                current_limit = self._limit_per_minute
                if len(self._timestamps) < current_limit:
                    self._timestamps.append(now)
                    in_window = len(self._timestamps)
                    break
                wait_seconds = max(0.05, AI_RATE_LIMIT_WINDOW_SECONDS - (now - self._timestamps[0]))
            logger.info(
                "ai_rate_limit_wait",
                operation=operation,
                backend_id=backend.id if backend is not None else None,
                backend_type=backend.type if backend is not None else None,
                configured_limit=current_limit,
                wait_ms=round(wait_seconds * 1000, 2),
            )
            sleep(wait_seconds)
            wait_ms += wait_seconds * 1000

        logger.info(
            "ai_rate_limit_acquired",
            operation=operation,
            backend_id=backend.id if backend is not None else None,
            backend_type=backend.type if backend is not None else None,
            configured_limit=current_limit,
            requests_in_window=in_window,
            total_wait_ms=round(wait_ms, 2),
        )
        try:
            yield
        finally:
            logger.info(
                "ai_rate_limit_released",
                operation=operation,
                backend_id=backend.id if backend is not None else None,
                backend_type=backend.type if backend is not None else None,
                configured_limit=current_limit,
            )


AI_CONCURRENCY_LIMITER = AIConcurrencyLimiter(DEFAULT_AI_MAX_CONCURRENT_REQUESTS)
AI_RATE_LIMITER = AIRateLimiter(DEFAULT_AI_RATE_LIMIT_PER_MINUTE)


def get_ai_max_concurrent_requests(db: Session, app_settings: Settings) -> int:
    system_settings = get_system_settings(db, app_settings)
    return _coerce_ai_concurrency_limit(system_settings.get("ai_max_concurrent_requests"))


def configure_ai_concurrency_limit(limit: Any) -> int:
    return AI_CONCURRENCY_LIMITER.configure(limit)


def sync_ai_concurrency_limit(db: Session, app_settings: Settings) -> int:
    return configure_ai_concurrency_limit(get_ai_max_concurrent_requests(db, app_settings))


def configure_ai_rate_limit(limit_per_minute: Any) -> int:
    return AI_RATE_LIMITER.configure(limit_per_minute)


def sync_ai_rate_limit(app_settings: Settings) -> int:
    return configure_ai_rate_limit(app_settings.ai_rate_limit_per_minute)


def configure_ai_dynamic_fallback_enabled(value: Any) -> bool:
    global _AI_DYNAMIC_FALLBACK_ENABLED
    normalized_enabled = _coerce_bool(value)
    with _AI_DYNAMIC_FALLBACK_LOCK:
        previous_enabled = _AI_DYNAMIC_FALLBACK_ENABLED
        _AI_DYNAMIC_FALLBACK_ENABLED = normalized_enabled
    if previous_enabled != normalized_enabled:
        logger.info(
            "ai_dynamic_fallback_setting_updated",
            previous_enabled=previous_enabled,
            enabled=normalized_enabled,
        )
    return normalized_enabled


def configure_ai_backend_failure_cooldown_seconds(value: Any) -> int:
    global _AI_BACKEND_FAILURE_COOLDOWN_SECONDS
    normalized_seconds = _coerce_ai_backend_failure_cooldown_seconds(value)
    with _AI_BACKEND_FAILURE_COOLDOWN_LOCK:
        previous_seconds = _AI_BACKEND_FAILURE_COOLDOWN_SECONDS
        _AI_BACKEND_FAILURE_COOLDOWN_SECONDS = normalized_seconds
        if normalized_seconds == 0:
            _AI_BACKEND_COOLDOWN_UNTIL.clear()
    if previous_seconds != normalized_seconds:
        logger.info(
            "ai_backend_failure_cooldown_updated",
            previous_seconds=previous_seconds,
            cooldown_seconds=normalized_seconds,
        )
    return normalized_seconds


def backend_failure_cooldown_seconds() -> int:
    with _AI_BACKEND_FAILURE_COOLDOWN_LOCK:
        return _AI_BACKEND_FAILURE_COOLDOWN_SECONDS


def dynamic_fallback_enabled() -> bool:
    with _AI_DYNAMIC_FALLBACK_LOCK:
        return _AI_DYNAMIC_FALLBACK_ENABLED


def get_ai_dynamic_fallback_enabled(db: Session, app_settings: Settings) -> bool:
    system_settings = get_system_settings(db, app_settings)
    return _coerce_bool(system_settings.get("ai_enable_dynamic_fallback", DEFAULT_AI_ENABLE_DYNAMIC_FALLBACK))


def get_ai_backend_failure_cooldown_seconds(db: Session, app_settings: Settings) -> int:
    system_settings = get_system_settings(db, app_settings)
    return _coerce_ai_backend_failure_cooldown_seconds(
        system_settings.get("ai_backend_failure_cooldown_seconds", DEFAULT_AI_BACKEND_FAILURE_COOLDOWN_SECONDS)
    )


def sync_ai_dynamic_fallback_setting(db: Session, app_settings: Settings) -> bool:
    return configure_ai_dynamic_fallback_enabled(get_ai_dynamic_fallback_enabled(db, app_settings))


def sync_ai_backend_failure_cooldown(db: Session, app_settings: Settings) -> int:
    return configure_ai_backend_failure_cooldown_seconds(get_ai_backend_failure_cooldown_seconds(db, app_settings))


def sync_ai_runtime_settings(db: Session, app_settings: Settings) -> tuple[int, bool]:
    concurrency_limit = sync_ai_concurrency_limit(db, app_settings)
    auto_dynamic_fallback = sync_ai_dynamic_fallback_setting(db, app_settings)
    sync_ai_backend_failure_cooldown(db, app_settings)
    sync_ai_rate_limit(app_settings)
    return concurrency_limit, auto_dynamic_fallback


@dataclass(frozen=True)
class AIBackendNode:
    id: str
    type: str
    url: str
    model: str
    api_key: str | None = None
    embedding_model: str | None = None
    weight: int = 1
    enabled: bool = True


class AIBackendError(RuntimeError):
    def __init__(self, node: AIBackendNode, message: str):
        super().__init__(message)
        self.node = node
        self.message = message


def normalize_operator_prompt(custom_prompt: str | None) -> str | None:
    cleaned = (custom_prompt or "").strip()
    return cleaned or None


def _normalized_annotation_contexts(annotation_contexts: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in annotation_contexts or []:
        cleaned = " ".join(str(raw_item or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned[:400])
        if len(normalized) >= 12:
            break
    return normalized


_PROJECT_PROMPT_SCHEMA_KEYS = (
    "ai_summary",
    "scene_description",
    "material_storage_violation",
    "housekeeping_issue",
    "defects_found",
    "receipt_facts",
    "financial_anomaly_flags",
)


def _strip_project_prompt_examples(fragment: str) -> str:
    # Project prompts are audit priorities, not training examples. Removing examples
    # prevents small vision models from copying sample objects or defects into output.
    without_parenthetical_examples = re.sub(
        r"[\uFF08(]\s*(?:\u5982|\u4F8B\u5982|\u6BD4\u5982|e\.g\.|for example|such as)[:\uFF1A]?.*?[\uFF09)]",
        "",
        fragment,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?:\u5982|\u4F8B\u5982|\u6BD4\u5982)[:\uFF1A][^\u3002\uFF1B;\n]+",
        "",
        without_parenthetical_examples,
    ).strip()


def _looks_like_project_prompt_format_instruction(fragment: str) -> bool:
    lowered = fragment.casefold()
    compact = re.sub(r"\s+", "", lowered)
    if any(key in lowered for key in _PROJECT_PROMPT_SCHEMA_KEYS):
        return True
    if "json" in lowered or "schema" in lowered:
        return True
    if "true/false" in compact or re.search(r"\b(?:true|false|null)\b", lowered):
        return True
    if any(
        marker in compact
        for marker in (
            "\u8F93\u51FA\u683C\u5F0F",
            "\u5982\u4E0B\u683C\u5F0F",
            "\u4E25\u683C\u8F93\u51FA",
            "\u53EA\u8F93\u51FA",
            "\u8FD4\u56DE\u683C\u5F0F",
            "\u4E0D\u8981\u5305\u542B\u4EFB\u4F55\u89E3\u91CA\u6587\u672C",
        )
    ):
        return True
    if fragment.strip() in ("{", "}", "[", "]"):
        return True
    if re.search(r"[\"']?[a-zA-Z_][\w]*[\"']?\s*:", fragment) and ("\"" in fragment or "," in fragment):
        return True
    if any(token in fragment for token in ("{", "}", "[", "]")) and ":" in fragment:
        return True
    return False


def _project_prompt_text(raw_value: str | None) -> str | None:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None

    fragments: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fragments.extend(part.strip() for part in re.split(r"(?<=[\u3002\uFF1B;])\s*", line) if part.strip())

    cleaned_fragments: list[str] = []
    for fragment in fragments:
        if "```" in fragment or _looks_like_project_prompt_format_instruction(fragment):
            continue
        cleaned = _strip_project_prompt_examples(fragment)
        if cleaned:
            cleaned_fragments.append(cleaned)
        if len(cleaned_fragments) >= 20:
            break

    cleaned_text = " ".join(" ".join(cleaned_fragments).split())
    if not cleaned_text:
        return None
    return cleaned_text[:1800]


AI_ANALYSIS_TEXT_FIELDS = (
    "scene_type",
    "confidence_level",
)

AI_ANALYSIS_LIST_FIELDS = (
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
)

AI_ANALYSIS_RECEIPT_FACT_KEYS = (
    "document_type",
    "vendor",
    "date",
    "total_amount",
    "currency",
    "tax_amount",
    "fuel_gallons",
    "unit_price",
    "location_hint",
    "payment_hint",
    "employee_or_project_hint",
)

GENERIC_AI_LABELS = {
    "project",
    "photo",
    "image",
    "scene",
    "object",
    "objects",
    "item",
    "items",
    "area",
    "material",
    "materials",
    "equipment",
    "construction",
    "building",
}

OBSERVATION_SIGNAL_WORDS = {
    "risk",
    "hazard",
    "unsafe",
    "safe",
    "blocked",
    "obstruction",
    "missing",
    "damage",
    "damaged",
    "rust",
    "rusting",
    "wet",
    "water",
    "spill",
    "leak",
    "housekeeping",
    "clutter",
    "stored",
    "staged",
    "installed",
    "touching",
    "ground",
    "base",
    "plate",
    "visible",
    "unclear",
    "limited",
    "cannot",
    "verify",
    "confirm",
    "inspect",
    "check",
    "review",
}

ACTION_SIGNAL_WORDS = {
    "ask",
    "check",
    "clean",
    "confirm",
    "correct",
    "document",
    "elevate",
    "ensure",
    "inspect",
    "mark",
    "monitor",
    "move",
    "protect",
    "repair",
    "replace",
    "review",
    "secure",
    "separate",
    "store",
    "use",
    "verify",
}

LIMITATION_SIGNAL_WORDS = {
    "angle",
    "blur",
    "blurry",
    "cannot",
    "difficult",
    "limited",
    "low light",
    "not visible",
    "occluded",
    "partial",
    "partially",
    "resolution",
    "unclear",
}


def _ollama_array_schema(*, max_items: int = 6) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "maxItems": max_items}


def _ollama_string_schema(*, max_length: int = 500) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


def _ollama_field_photo_json_schema() -> dict[str, Any]:
    array_fields = (
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
        "recommended_actions",
        "evidence_limitations",
    )
    properties: dict[str, Any] = {field_name: _ollama_array_schema() for field_name in array_fields}
    properties.update(
        {
            "ai_summary": _ollama_string_schema(max_length=420),
            "scene_type": _ollama_string_schema(max_length=120),
            "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": [
            "ai_summary",
            "labels",
            "defects",
            "scene_type",
            "visible_objects",
            "materials",
            "equipment",
            "people_ppe",
            "safety_observations",
            "quality_observations",
            "inventory_observations",
            "water_or_housekeeping_observations",
            "recommended_actions",
            "confidence_level",
            "evidence_limitations",
        ],
    }


def _ollama_receipt_json_schema() -> dict[str, Any]:
    receipt_facts = {
        "type": "object",
        "additionalProperties": False,
        "properties": {field_name: _ollama_string_schema(max_length=180) for field_name in AI_ANALYSIS_RECEIPT_FACT_KEYS},
        "required": list(AI_ANALYSIS_RECEIPT_FACT_KEYS),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ai_summary": _ollama_string_schema(max_length=420),
            "labels": _ollama_array_schema(),
            "defects": _ollama_array_schema(),
            "receipt_facts": receipt_facts,
            "financial_anomaly_flags": _ollama_array_schema(),
            "missing_evidence": _ollama_array_schema(),
            "recommended_actions": _ollama_array_schema(),
            "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence_limitations": _ollama_array_schema(),
        },
        "required": [
            "ai_summary",
            "labels",
            "defects",
            "receipt_facts",
            "financial_anomaly_flags",
            "missing_evidence",
            "recommended_actions",
            "confidence_level",
            "evidence_limitations",
        ],
    }


def _ollama_json_schema_for_prompt(prompt: str) -> dict[str, Any]:
    if "receipt_facts" in prompt or "financial_anomaly_flags" in prompt:
        return _ollama_receipt_json_schema()
    return _ollama_field_photo_json_schema()


def _ollama_translation_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_language": {"type": "string", "enum": ["zh", "en", "es", "other"]},
            "translations": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "zh": _ollama_string_schema(max_length=900),
                    "en": _ollama_string_schema(max_length=900),
                    "es": _ollama_string_schema(max_length=900),
                },
                "required": ["zh", "en", "es"],
            },
        },
        "required": ["source_language", "translations"],
    }


def _ollama_text_format_for_prompt(prompt: str) -> str | dict[str, Any] | None:
    if '"translations":{"zh"' in prompt or '"translations": {"zh"' in prompt:
        return _ollama_translation_json_schema()
    if (
        "Return strict JSON only" in prompt
        and "role_id" in prompt
        and "should_retry" in prompt
        and "Output schema example" in prompt
    ):
        return "json"
    return None


def _field_photo_prompt_examples() -> list[str]:
    return [
        "Format reminder: return the full JSON schema for the actual attached image.",
        "Do not copy any example object names. Every object, material, risk, and action must come from the attached image.",
    ]


def _invoice_photo_prompt_examples() -> list[str]:
    return [
        "Format reminder: return the full JSON schema for the actual attached receipt or invoice.",
        "Do not copy placeholder values. Use empty strings for unreadable fields and explain the audit limitation.",
    ]


def _project_ai_prompt_for_photo(db: Session, photo: Photo) -> str | None:
    if not photo.project_id:
        return None
    project = db.scalar(
        select(Project).where(
            Project.project_id == photo.project_id,
            Project.company_id == photo.company_id,
        )
    )
    if project is None:
        return None
    if photo.photo_type == PhotoType.invoice:
        return _project_prompt_text(project.billing_receipt_ai_prompt)
    return _project_prompt_text(project.image_video_ai_prompt)


def _project_ai_prompt_for_media_asset(
    db: Session,
    *,
    company_id: str,
    project_id: str | None,
    photo_type: PhotoType,
) -> str | None:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return None
    project = db.scalar(
        select(Project).where(
            Project.project_id == normalized_project_id,
            Project.company_id == company_id,
        )
    )
    if project is None:
        return None
    if photo_type == PhotoType.invoice:
        return _project_prompt_text(project.billing_receipt_ai_prompt)
    return _project_prompt_text(project.image_video_ai_prompt)


def build_ai_prompt_for_context(
    *,
    photo_type: PhotoType,
    project_id: str | None,
    note: str | None,
    project_prompt: str | None = None,
    custom_prompt: str | None = None,
    annotation_contexts: list[str] | None = None,
) -> str:
    note_context = note.strip() if note else ""
    operator_prompt = normalize_operator_prompt(custom_prompt)
    normalized_annotations = _normalized_annotation_contexts(annotation_contexts)
    if photo_type == PhotoType.invoice:
        schema_lines = [
            "{",
            '  "ai_summary": "English 2-3 sentence finance-useful summary",',
            '  "labels": ["short_lowercase_tags"],',
            '  "defects": ["specific visible audit gaps"],',
            '  "receipt_facts": {"document_type": "", "vendor": "", "date": "", "total_amount": "", "currency": "", "tax_amount": "", "fuel_gallons": "", "unit_price": "", "location_hint": "", "payment_hint": "", "employee_or_project_hint": ""},',
            '  "financial_anomaly_flags": ["receipt/audit concerns, or empty array"],',
            '  "missing_evidence": ["important supporting evidence not visible, or empty array"],',
            '  "recommended_actions": ["finance next actions sorted by priority"],',
            '  "confidence_level": "high|medium|low",',
            '  "evidence_limitations": ["why extraction may be limited"]',
            "}",
        ]
    else:
        schema_lines = [
            "{",
            '  "ai_summary": "", "labels": [], "defects": [], "scene_type": "",',
            '  "visible_objects": [], "materials": [], "equipment": [], "people_ppe": [],',
            '  "safety_observations": [], "quality_observations": [], "inventory_observations": [],',
            '  "water_or_housekeeping_observations": [], "recommended_actions": [],',
            '  "confidence_level": "high|medium|low", "evidence_limitations": []',
            "}",
        ]
    prompt_sections = [
        (
            "You are a field evidence triage analyst. The image may be useful work evidence, or it may be ordinary/non-auditable media."
            if photo_type != PhotoType.invoice
            else "You are an expense-evidence analyst reviewing a receipt or invoice photo for project audit traceability."
        ),
        "Analyze the attached image and return only valid JSON.",
        "Do not return markdown, code fences, explanations, or any text outside the JSON object.",
        "Required JSON schema:",
        *schema_lines,
        "Instructions:",
    ]
    if photo_type == PhotoType.invoice:
        prompt_sections.extend(
            [
                "- Start with what is clearly visible and financially useful.",
                "- Summarize the document type and the most important readable details that are actually visible.",
                "- If readable, prefer vendor, total amount, date, gallons or units, line items, and payment clues.",
                "- Extract receipt_facts only from visible text; use an empty string for any field that is not readable.",
                "- Use financial_anomaly_flags for visible audit concerns, such as missing totals, unreadable vendor, suspicious cropping, duplicate-looking receipts, missing pump/vehicle evidence for fuel, or mismatch between visible goods and the claimed category.",
                "- Use missing_evidence for evidence that a finance reviewer would reasonably request but that is not visible in this image.",
                "- If text is blurry or cropped, say that it is unreadable instead of inventing values.",
                "- Use labels for document category and notable readable fields, such as receipt, invoice, fuel, vendor_visible, total_visible, date_visible, blurry_document, or cropped_document.",
                "- defects should contain only visually supported audit-usefulness issues, such as vendor not readable, total amount not readable, blurry document, cropped receipt, or no supporting equipment or pump visible when relevant.",
                "- If there is no clear issue visible, return [] for defects.",
                "- Keep the summary factual, finance-useful, and grounded in what can be read.",
                *_invoice_photo_prompt_examples(),
            ]
        )
    else:
        prompt_sections.extend(
            [
                "- Describe visible facts only.",
                "- Do not infer construction context, hazards, defects, progress, PPE issues, materials, or equipment unless clearly visible.",
                "- Do not use possibly/probably/may indicate to turn ordinary photos into project, worksite, or construction evidence.",
                "- If the image is ordinary or unclear, say so in ai_summary and leave risk/action fields empty.",
                "- Empty fields mean not visible or not applicable.",
            ]
        )
    project_prompt_text = _project_prompt_text(project_prompt)
    prompt_sections.extend(
        [
            "- Use the note only as context; do not invent details that are not visually supported.",
            "- If confidence_level is low, explain why in evidence_limitations.",
            "- Return every schema field even when the value is an empty string or empty array.",
            "- Use at most 6 concise, non-duplicate strings in each array field.",
            "- Do not create extra JSON keys. Do not repeat the same noun to fill arrays.",
            "- The first top-level key must be ai_summary. Do not rename ai_summary to scene_description, caption, summary, or description.",
            f"- photo_type: {photo_type.value}",
            f"- project_id: {project_id or ''}",
            f"- note: {note_context}",
        ]
    )
    if project_prompt_text:
        prompt_sections.extend(
            [
                "Project-specific audit priorities (sanitized; not evidence):",
                project_prompt_text,
                "- Treat these priorities as review focus areas, not conclusions.",
                "- Do not copy words from project guidance into output unless the attached image visibly supports them.",
            ]
        )
    if normalized_annotations:
        prompt_sections.extend(
            [
                "Public annotation context:",
                *[f"- {item}" for item in normalized_annotations],
                "- Treat annotation context as hints only; visible evidence in the image must remain the source of truth.",
            ]
        )
    if operator_prompt:
        prompt_sections.extend(
            [
                "Operator guidance:",
                operator_prompt,
                "- Apply operator guidance only when it remains consistent with visible evidence in the image.",
            ]
        )
    return "\n".join(prompt_sections)


def build_ai_prompt(db: Session, photo: Photo, custom_prompt: str | None = None) -> str:
    from app.services.annotations import list_public_completed_annotation_texts

    return build_ai_prompt_for_context(
        photo_type=photo.photo_type,
        project_id=photo.project_id,
        note=photo.note,
        project_prompt=_project_ai_prompt_for_photo(db, photo),
        custom_prompt=custom_prompt,
        annotation_contexts=list_public_completed_annotation_texts(db, photo_id=photo.id),
    )


def build_media_asset_ai_prompt(
    db: Session,
    asset: MediaAsset,
    *,
    photo_type: PhotoType = PhotoType.project,
    custom_prompt: str | None = None,
) -> str:
    from app.services.annotations import list_public_completed_annotation_texts

    metadata_json = dict(asset.metadata_json) if isinstance(asset.metadata_json, dict) else {}
    annotation_contexts = list_public_completed_annotation_texts(db, media_asset_id=asset.asset_id)
    legacy_photo_id = metadata_json.get("legacy_photo_id")
    try:
        resolved_legacy_photo_id = int(legacy_photo_id)
    except (TypeError, ValueError):
        resolved_legacy_photo_id = None
    if resolved_legacy_photo_id is not None:
        annotation_contexts.extend(list_public_completed_annotation_texts(db, photo_id=resolved_legacy_photo_id))
    project_id = str(metadata_json.get("project_id") or "").strip() or None
    return build_ai_prompt_for_context(
        photo_type=photo_type,
        project_id=project_id,
        note=str(metadata_json.get("note") or "").strip() or None,
        project_prompt=_project_ai_prompt_for_media_asset(
            db,
            company_id=asset.company_id,
            project_id=project_id,
            photo_type=photo_type,
        ),
        custom_prompt=custom_prompt,
        annotation_contexts=annotation_contexts,
    )


def _analysis_created_by(actor_user_id: int | None) -> str:
    return str(actor_user_id) if actor_user_id is not None else "system"


def _resolve_analysis_type(*, custom_prompt: str | None, trigger_source: str) -> AIAnalysisType:
    normalized_source = (trigger_source or "").strip().lower()
    normalized_prompt = (custom_prompt or "").strip().lower()
    combined = f"{normalized_source} {normalized_prompt}"
    if "sticker" in combined:
        return AIAnalysisType.sticker_trigger
    if "inventory" in combined or "tool" in combined:
        return AIAnalysisType.inventory_scan
    if custom_prompt:
        return AIAnalysisType.deep_analysis
    return AIAnalysisType.fast_screen


def _normalize_optional_ai_text(value: Any, *, limit: int = 800) -> str:
    if value is None:
        return ""
    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return ""
    return cleaned[:limit]


def _normalize_ai_summary_text(value: Any) -> str:
    cleaned = _normalize_optional_ai_text(value, limit=900)
    if not cleaned:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidate = " ".join(sentence for sentence in sentences[:2] if sentence).strip() if sentences else cleaned
    if not candidate:
        candidate = cleaned
    if len(candidate) <= 420:
        return candidate

    truncated = candidate[:420].rstrip()
    sentence_end = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if sentence_end >= 140:
        return truncated[: sentence_end + 1]
    word_safe = truncated.rsplit(" ", 1)[0].rstrip(",;:")
    return f"{word_safe}." if word_safe else truncated


def _normalize_ai_translations(value: Any) -> dict[str, str]:
    translations: dict[str, str] = {}
    if not isinstance(value, dict):
        return translations
    for language in VOICE_TRANSLATION_LANGUAGES:
        translated_text = _normalize_optional_ai_text(value.get(language), limit=1200)
        if translated_text:
            translations[language] = translated_text
    return translations


def _normalize_ai_string_map(value: Any, *, allowed_keys: tuple[str, ...], limit: int = 240) -> dict[str, str]:
    normalized: dict[str, str] = {}
    source = value if isinstance(value, dict) else {}
    for key in allowed_keys:
        normalized[key] = _normalize_optional_ai_text(source.get(key), limit=limit)
    return normalized


def _first_present_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _fallback_ai_summary_from_payload(payload: dict[str, Any]) -> str:
    visible_terms: list[str] = []
    for field_name in ("visible_objects", "materials", "equipment", "people_ppe", "labels"):
        values = payload.get(field_name)
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text and text.casefold() not in {"none", "unknown", "n/a"}:
                visible_terms.append(text)
            if len(visible_terms) >= 4:
                break
        if len(visible_terms) >= 4:
            break
    if visible_terms:
        return f"Visible evidence includes {', '.join(visible_terms[:4])}, but the model did not return a reliable summary."
    return "Not enough reliable visual evidence to generate a field audit summary."


def _normalize_structured_ai_payload(raw_payload: Any, *, require_summary: bool) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    ai_summary = _normalize_ai_summary_text(
        _first_present_payload_value(
            payload,
            (
                "ai_summary",
                "summary",
                "scene_description",
                "description",
                "caption",
                "manager_summary",
                "executive_summary",
            ),
        ),
    )
    if require_summary and not ai_summary:
        ai_summary = _fallback_ai_summary_from_payload(payload)
        payload.setdefault("confidence_level", "low")
        if not payload.get("evidence_limitations"):
            payload["evidence_limitations"] = ["Model response did not include a reliable summary."]

    raw_labels = payload.get("labels")
    if raw_labels in (None, "", [], {}) and payload.get("visible_objects"):
        raw_labels = payload.get("visible_objects")
    raw_defects = _first_present_payload_value(
        payload,
        ("defects", "defects_found", "issues", "risks", "audit_gaps"),
    )
    normalized: dict[str, Any] = {
        "ai_summary": ai_summary or None,
        "ai_summary_translations": _normalize_ai_translations(payload.get("ai_summary_translations")),
        "labels": _normalize_string_list(raw_labels, field_name="labels"),
        "defects": _normalize_string_list(raw_defects, field_name="defects"),
        "receipt_facts": _normalize_ai_string_map(
            payload.get("receipt_facts"),
            allowed_keys=AI_ANALYSIS_RECEIPT_FACT_KEYS,
        ),
    }
    for field_name in AI_ANALYSIS_TEXT_FIELDS:
        normalized[field_name] = _normalize_optional_ai_text(payload.get(field_name), limit=160)
    for field_name in AI_ANALYSIS_LIST_FIELDS:
        normalized[field_name] = _normalize_string_list(payload.get(field_name), field_name=field_name)
    cleaned = _clean_low_information_ai_fields(normalized)
    for field_name in ("evidence_engine", "ai_role_reviews", "manager_evidence_summary"):
        if isinstance(payload.get(field_name), dict):
            cleaned[field_name] = payload[field_name]
    if payload.get("manager_evidence_version"):
        cleaned["manager_evidence_version"] = _normalize_optional_ai_text(
            payload.get("manager_evidence_version"),
            limit=100,
        )
    if payload.get("ai_role_reviews_error"):
        cleaned["ai_role_reviews_error"] = _normalize_optional_ai_text(payload.get("ai_role_reviews_error"), limit=500)
    return cleaned


def _normalize_analysis_snapshot(raw_payload: Any) -> dict[str, Any]:
    return _normalize_structured_ai_payload(raw_payload, require_summary=False)


def localized_ai_summary(raw_payload: Any, language: str | None) -> str | None:
    normalized = _normalize_analysis_snapshot(raw_payload)
    translated_text = normalized["ai_summary_translations"].get(_normalize_language_code(language))
    if translated_text:
        return translated_text
    return normalized["ai_summary"]


def _append_unique_casefold(target: list[str], seen: set[str], values: list[str]) -> None:
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        target.append(value)


def _sortable_created_at(value: Any) -> float:
    if not isinstance(value, datetime):
        return float("-inf")
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.timestamp()


def merge_active_ai_analysis_logs(logs: list[AIAnalysisLog]) -> dict[str, Any]:
    if not logs:
        return {"ai_summary": None, "ai_summary_translations": {}, "labels": [], "defects": []}

    ordered_logs = sorted(
        logs,
        key=lambda log: (_sortable_created_at(log.created_at), log.id),
        reverse=True,
    )
    return _normalize_analysis_snapshot(ordered_logs[0].result_data)


def ai_confidence_level(active_log_count: int) -> str | None:
    if active_log_count <= 0:
        return None
    if active_log_count >= 3:
        return "high"
    if active_log_count >= 2:
        return "medium"
    return "low"


def active_ai_log_counts_by_photo(db: Session, photo_ids: list[int]) -> dict[int, int]:
    if not photo_ids:
        return {}
    rows = db.execute(
        select(AIAnalysisLog.photo_id, func.count())
        .where(
            AIAnalysisLog.photo_id.in_(photo_ids),
            AIAnalysisLog.status == AIAnalysisStatus.active,
        )
        .group_by(AIAnalysisLog.photo_id)
    ).all()
    return {int(photo_id): int(count) for photo_id, count in rows}


def _analysis_text_blob(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
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
        "ai_summary_translations",
        "evidence_engine",
        "ai_role_reviews",
        "manager_evidence_summary",
    ):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, dict):
            parts.extend(str(item).strip() for item in value.values() if str(item).strip())
    return " ".join(parts)


def photo_matches_ai_keyword(
    db: Session,
    photo: Photo,
    keyword: str,
    *,
    include_rejected: bool,
) -> bool:
    normalized_keyword = keyword.strip().casefold()
    if not normalized_keyword:
        return True

    active_snapshot = _normalize_analysis_snapshot(photo.tag_json)
    if normalized_keyword in _analysis_text_blob(active_snapshot).casefold():
        return True

    if not include_rejected:
        return False

    for analysis_log in list_photo_ai_logs(db, photo.id):
        snapshot = _normalize_analysis_snapshot(analysis_log.result_data)
        if normalized_keyword in _analysis_text_blob(snapshot).casefold():
            return True
    return False


def promote_ai_analysis_log_to_primary(
    db: Session,
    *,
    app_settings: Settings,
    analysis_log: AIAnalysisLog,
    actor_user_id: int | None,
    source: str,
) -> tuple[AIAnalysisLog, dict[str, Any]]:
    photo = db.get(Photo, analysis_log.photo_id)
    if photo is None:
        raise RuntimeError("Associated photo not found")

    promoted_log = _create_ai_analysis_log(
        db,
        photo=photo,
        batch_id=analysis_log.batch_id,
        analysis_type=analysis_log.analysis_type,
        prompt_used=analysis_log.prompt_used,
        model_used=analysis_log.model_used,
        result_data=_normalize_analysis_snapshot(analysis_log.result_data),
        created_by=_analysis_created_by(actor_user_id),
    )
    snapshot, embedding_stored, embedding_ref, active_log_count = rebuild_photo_ai_snapshot(
        db,
        app_settings=app_settings,
        photo=photo,
        actor_user_id=actor_user_id,
        source=source,
        priority="normal",
    )
    log_audit(
        db,
        action="photo_ai_log_promoted",
        target_type="ai_analysis_log",
        target_id=promoted_log.id,
        actor_user_id=actor_user_id,
        tenant_id=photo.tenant_id or photo.company_id,
        project_id=photo.project_id,
        company_id=photo.company_id,
        detail_json={
            "photo_id": photo.id,
            "source_log_id": analysis_log.id,
            "source": source,
            "batch_id": analysis_log.batch_id,
            "active_analysis_log_count": active_log_count,
            "embedding_stored": embedding_stored,
            "embedding_ref": embedding_ref,
        },
    )
    return promoted_log, snapshot


def list_photo_ai_logs(db: Session, photo_id: int) -> list[AIAnalysisLog]:
    return list(
        db.scalars(
            select(AIAnalysisLog)
            .where(AIAnalysisLog.photo_id == photo_id)
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        )
    )


def list_photo_ai_logs_by_photo_ids(db: Session, photo_ids: list[int]) -> dict[int, list[AIAnalysisLog]]:
    if not photo_ids:
        return {}
    grouped: dict[int, list[AIAnalysisLog]] = {}
    for log in db.scalars(
        select(AIAnalysisLog)
        .where(AIAnalysisLog.photo_id.in_(photo_ids))
        .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
    ):
        grouped.setdefault(log.photo_id, []).append(log)
    return grouped


def list_media_asset_ai_logs_by_asset_ids(db: Session, media_asset_ids: list[str]) -> dict[str, list[AIAnalysisLog]]:
    if not media_asset_ids:
        return {}
    grouped: dict[str, list[AIAnalysisLog]] = {}
    for log in db.scalars(
        select(AIAnalysisLog)
        .where(AIAnalysisLog.media_asset_id.in_(media_asset_ids))
        .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
    ):
        grouped.setdefault(log.media_asset_id, []).append(log)
    return grouped


def list_media_asset_ai_logs(db: Session, media_asset_id: str) -> list[AIAnalysisLog]:
    return list(
        db.scalars(
            select(AIAnalysisLog)
            .where(AIAnalysisLog.media_asset_id == media_asset_id)
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        )
    )


def serialize_ai_analysis_log(log: AIAnalysisLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "photo_id": log.photo_id,
        "media_asset_id": log.media_asset_id,
        "batch_id": log.batch_id,
        "analysis_type": log.analysis_type.value,
        "prompt_used": log.prompt_used,
        "model_used": log.model_used,
        "result_data": log.result_data,
        "status": log.status.value,
        "created_at": to_utc_iso(log.created_at),
        "created_by": log.created_by,
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _looks_like_ollama_resource_error(message: str) -> bool:
    normalized_message = str(message or "").casefold()
    if not normalized_message:
        return False
    return any(marker in normalized_message for marker in _OLLAMA_RESOURCE_ERROR_MARKERS)


def _looks_like_single_image_only_error(message: str) -> bool:
    normalized_message = str(message or "").casefold()
    if not normalized_message:
        return False
    return all(marker in normalized_message for marker in _OLLAMA_MULTI_IMAGE_SINGLE_IMAGE_MARKERS)


def _reprioritize_backends_for_dynamic_fallback(
    remaining_backends: list[AIBackendNode],
    *,
    failed_backend: AIBackendNode,
    operation: str,
    error_message: str,
) -> list[AIBackendNode]:
    gemini_backends = [backend for backend in remaining_backends if backend.type == GEMINI_TYPE]
    if not gemini_backends:
        return remaining_backends
    triggered = AI_CONCURRENCY_LIMITER.record_dynamic_fallback(
        operation=operation,
        backend=failed_backend,
        error_message=error_message,
        fallback_backend=gemini_backends[0],
    )
    if not triggered:
        return remaining_backends
    return gemini_backends + [backend for backend in remaining_backends if backend.type != GEMINI_TYPE]


def _coerce_weight(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _parse_backend_nodes(raw_nodes: Any, *, source: str) -> list[AIBackendNode]:
    if not isinstance(raw_nodes, list):
        return []

    parsed_nodes: list[AIBackendNode] = []
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            logger.warning(
                "ai_backend_skipped",
                source=source,
                reason="invalid_entry",
                index=index,
            )
            continue

        node_type = str(raw_node.get("type") or "").strip().lower()
        node_id = str(raw_node.get("id") or "").strip()
        url = str(raw_node.get("url") or "").strip()
        model = str(raw_node.get("model") or "").strip()
        if not node_id or not node_type or not url or not model:
            logger.warning(
                "ai_backend_skipped",
                source=source,
                reason="missing_fields",
                index=index,
            )
            continue
        if node_type not in {OLLAMA_TYPE, GEMINI_TYPE}:
            logger.warning(
                "ai_backend_skipped",
                source=source,
                reason="unsupported_type",
                index=index,
                backend_type=node_type,
            )
            continue

        parsed_nodes.append(
            AIBackendNode(
                id=node_id,
                type=node_type,
                url=url,
                model=model,
                api_key=str(raw_node.get("api_key")).strip() if raw_node.get("api_key") else None,
                embedding_model=(
                    str(raw_node.get("embedding_model")).strip() if raw_node.get("embedding_model") else None
                ),
                weight=_coerce_weight(raw_node.get("weight")),
                enabled=_coerce_bool(raw_node.get("enabled", True)),
            )
        )
    return parsed_nodes


def _load_system_backends(db: Session, app_settings: Settings) -> list[AIBackendNode]:
    system_settings = get_system_settings(db, app_settings)
    raw_value = system_settings.get("ai_backends", DEFAULT_AI_BACKENDS_JSON)
    try:
        parsed = json.loads(raw_value or DEFAULT_AI_BACKENDS_JSON)
    except json.JSONDecodeError:
        logger.warning("ai_backend_config_invalid", source="system_settings")
        return []
    return _parse_backend_nodes(parsed, source="system_settings")


def serialize_backend_node(node: AIBackendNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "url": node.url,
        "model": node.model,
        "api_key": node.api_key,
        "embedding_model": node.embedding_model,
        "weight": node.weight,
        "enabled": node.enabled,
    }


def get_system_ai_backends(db: Session, app_settings: Settings) -> list[AIBackendNode]:
    return _load_system_backends(db, app_settings)


def get_tenant_ai_backends(db: Session, tenant_slug: str) -> list[AIBackendNode]:
    if not tenant_slug:
        return []
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None or not isinstance(tenant.settings_json, dict):
        return []
    raw_value = tenant.settings_json.get("ai_backends")
    return _parse_backend_nodes(raw_value, source=f"tenant:{tenant_slug}")


def save_tenant_ai_backends(db: Session, tenant_slug: str, nodes: list[AIBackendNode]) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        raise ValueError("Tenant not found")

    settings_json = tenant.settings_json if isinstance(tenant.settings_json, dict) else {}
    updated_settings = dict(settings_json)
    updated_settings["ai_backends"] = [serialize_backend_node(node) for node in nodes]
    tenant.settings_json = updated_settings
    db.add(tenant)
    return tenant


def resolve_ai_backends_for_tenant(
    db: Session,
    app_settings: Settings,
    tenant_slug: str | None,
) -> tuple[list[AIBackendNode], str]:
    tenant_nodes = get_tenant_ai_backends(db, tenant_slug or "")
    usable_tenant_nodes = [node for node in tenant_nodes if node.enabled and node.weight > 0]
    if usable_tenant_nodes:
        return usable_tenant_nodes, "tenant"

    system_nodes = get_system_ai_backends(db, app_settings)
    usable_system_nodes = [node for node in system_nodes if node.enabled and node.weight > 0]
    if usable_system_nodes:
        return usable_system_nodes, "system"

    return [], "none"


def resolve_ai_backends(db: Session, app_settings: Settings, photo: Photo) -> tuple[list[AIBackendNode], str]:
    return resolve_ai_backends_for_tenant(db, app_settings, photo.tenant_id or photo.company_id)


def collect_enabled_ollama_backends(db: Session, app_settings: Settings) -> list[AIBackendNode]:
    nodes: list[AIBackendNode] = [node for node in get_system_ai_backends(db, app_settings) if node.enabled and node.type == OLLAMA_TYPE]
    tenant_slugs = list(db.scalars(select(Tenant.slug)))
    for tenant_slug in tenant_slugs:
        nodes.extend(
            node
            for node in get_tenant_ai_backends(db, tenant_slug)
            if node.enabled and node.type == OLLAMA_TYPE
        )

    unique_nodes: list[AIBackendNode] = []
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        key = (node.id, node.url.rstrip("/"), node.model)
        if key in seen:
            continue
        seen.add(key)
        unique_nodes.append(node)
    return unique_nodes


def perform_ai_healthcheck(session_maker: sessionmaker[Session], app_settings: Settings) -> dict[str, Any]:
    started = perf_counter()
    with session_maker() as db:
        ollama_backends = collect_enabled_ollama_backends(db, app_settings)

    if not ollama_backends:
        logger.info(
            "ai_healthcheck_skipped",
            reason="no_enabled_ollama_backends",
            timeout_seconds=app_settings.ai_healthcheck_timeout_seconds or AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS,
        )
        return {"checked_backends": 0, "healthy_backends": 0, "failed_backends": 0}

    healthy_backends = 0
    failed_backends = 0
    for backend in ollama_backends:
        backend_started = perf_counter()
        try:
            response = requests.get(
                f"{backend.url.rstrip('/')}/api/tags",
                timeout=app_settings.ai_healthcheck_timeout_seconds or AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS,
            )
            _raise_for_status_with_message(response)
            payload = response.json()
            model_count = len(payload.get("models", [])) if isinstance(payload, dict) else None
            healthy_backends += 1
            logger.info(
                "ai_healthcheck_backend_ok",
                backend_id=backend.id,
                backend_type=backend.type,
                backend_url=backend.url,
                backend_model=backend.model,
                duration_ms=round((perf_counter() - backend_started) * 1000, 2),
                model_count=model_count,
                timeout_seconds=app_settings.ai_healthcheck_timeout_seconds or AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            failed_backends += 1
            logger.warning(
                "ai_healthcheck_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                backend_url=backend.url,
                backend_model=backend.model,
                duration_ms=round((perf_counter() - backend_started) * 1000, 2),
                error=str(exc),
                exception_type=type(exc).__name__,
                timeout_seconds=app_settings.ai_healthcheck_timeout_seconds or AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS,
            )

    summary = {
        "checked_backends": len(ollama_backends),
        "healthy_backends": healthy_backends,
        "failed_backends": failed_backends,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "timeout_seconds": app_settings.ai_healthcheck_timeout_seconds or AI_HEALTHCHECK_DEFAULT_TIMEOUT_SECONDS,
        "degraded": failed_backends > 0,
    }
    logger.info("ai_healthcheck_completed", **summary)
    return summary


def _build_backend_order(nodes: list[AIBackendNode]) -> list[AIBackendNode]:
    if not nodes:
        return []

    current_now = monotonic()
    dispatchable_nodes = [node for node in nodes if not _backend_in_cooldown(node, now=current_now)]
    effective_nodes = dispatchable_nodes if dispatchable_nodes else nodes

    weighted_indexes: list[int] = []
    for index, node in enumerate(effective_nodes):
        weighted_indexes.extend([index] * node.weight)

    pool_key = tuple((node.id, node.weight, node.type, node.url, node.model) for node in effective_nodes)
    with _BACKEND_ROTATION_LOCK:
        current_index = _BACKEND_ROTATION_INDEX.get(pool_key, 0) % len(weighted_indexes)
        _BACKEND_ROTATION_INDEX[pool_key] = (current_index + 1) % len(weighted_indexes)

    rotated_indexes = weighted_indexes[current_index:] + weighted_indexes[:current_index]
    ordered_nodes: list[AIBackendNode] = []
    seen_indexes: set[int] = set()
    for index in rotated_indexes:
        if index in seen_indexes:
            continue
        seen_indexes.add(index)
        ordered_nodes.append(effective_nodes[index])
    return ordered_nodes


def _backend_cooldown_key(node: AIBackendNode) -> tuple[str, str, str, str | None]:
    return (node.type, node.url.rstrip("/"), node.model, node.api_key)


def _backend_in_cooldown(node: AIBackendNode, *, now: float | None = None) -> bool:
    current_now = monotonic() if now is None else now
    key = _backend_cooldown_key(node)
    with _AI_BACKEND_FAILURE_COOLDOWN_LOCK:
        cooldown_until = _AI_BACKEND_COOLDOWN_UNTIL.get(key)
        if cooldown_until is None:
            return False
        if cooldown_until <= current_now:
            _AI_BACKEND_COOLDOWN_UNTIL.pop(key, None)
            return False
        return True


def _clear_backend_cooldown(node: AIBackendNode) -> None:
    key = _backend_cooldown_key(node)
    with _AI_BACKEND_FAILURE_COOLDOWN_LOCK:
        if key in _AI_BACKEND_COOLDOWN_UNTIL:
            _AI_BACKEND_COOLDOWN_UNTIL.pop(key, None)
            logger.info(
                "ai_backend_cooldown_cleared",
                backend_id=node.id,
                backend_type=node.type,
                backend_url=node.url,
                backend_model=node.model,
            )


def _looks_like_transient_backend_error(message: str) -> bool:
    normalized_message = str(message or "").casefold()
    if not normalized_message:
        return False
    if _looks_like_ollama_resource_error(normalized_message):
        return True
    return any(marker in normalized_message for marker in _BACKEND_TRANSIENT_ERROR_MARKERS)


def _register_backend_failure_cooldown(node: AIBackendNode, *, operation: str, error_message: str) -> None:
    cooldown_seconds = backend_failure_cooldown_seconds()
    if cooldown_seconds <= 0:
        return
    if not _looks_like_transient_backend_error(error_message):
        return
    key = _backend_cooldown_key(node)
    with _AI_BACKEND_FAILURE_COOLDOWN_LOCK:
        _AI_BACKEND_COOLDOWN_UNTIL[key] = monotonic() + float(cooldown_seconds)
    logger.warning(
        "ai_backend_cooldown_started",
        backend_id=node.id,
        backend_type=node.type,
        backend_url=node.url,
        backend_model=node.model,
        operation=operation,
        cooldown_seconds=cooldown_seconds,
        error=error_message,
    )


def _resolve_photo_file(photo: Photo) -> Path:
    for candidate in (photo.storage_path, photo.file_path):
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.is_file():
            return candidate_path
    raise FileNotFoundError("Photo file not found on local storage")


def _photo_payload_mime_type(photo: Photo, photo_path: Path) -> str:
    return photo.mime_type or mimetypes.guess_type(photo_path.name)[0] or "image/jpeg"


def _visual_payload_skip_reason(photo_path: Path, mime_type: str, *, min_image_bytes: int = MIN_AUDITABLE_IMAGE_BYTES) -> str | None:
    normalized_mime = (mime_type or "").strip().lower()
    if not normalized_mime.startswith("image/"):
        return f"unsupported_media_type:{normalized_mime or 'unknown'}"
    try:
        file_size = photo_path.stat().st_size
    except OSError:
        return "file_stat_unavailable"
    if file_size < max(0, min_image_bytes):
        return f"image_too_small:{file_size}_bytes"
    return None


def _media_gate_ai_result(*, mime_type: str, skip_reason: str) -> dict[str, Any]:
    if skip_reason.startswith("unsupported_media_type:"):
        summary = f"{mime_type or 'Media file'} is not supported by the current still-image vision model. No visual field evidence was audited from this asset."
        labels = ["unsupported_media", "vision_skipped"]
    else:
        summary = "Image file is too small or invalid for reliable visual evidence review. No auditable field conclusion was generated from this asset."
        labels = ["invalid_image", "vision_skipped"]
    return {
        "ai_summary": summary,
        "labels": labels,
        "defects": [],
        "scene_type": "unsupported_media" if skip_reason.startswith("unsupported_media_type:") else "invalid_image",
        "visible_objects": [],
        "materials": [],
        "equipment": [],
        "people_ppe": [],
        "safety_observations": [],
        "quality_observations": [],
        "inventory_observations": [],
        "water_or_housekeeping_observations": [],
        "recommended_actions": [],
        "confidence_level": "low",
        "evidence_limitations": [skip_reason],
    }


def _read_photo_payload(photo: Photo, *, photo_path: Path | None = None, mime_type: str | None = None) -> tuple[str, str]:
    photo_path = photo_path or _resolve_photo_file(photo)
    photo_bytes = photo_path.read_bytes()
    mime_type = mime_type or _photo_payload_mime_type(photo, photo_path)
    return base64.b64encode(photo_bytes).decode("ascii"), mime_type


def _normalize_embedding_values(raw_values: Any) -> list[float]:
    if not isinstance(raw_values, list) or not raw_values:
        raise ValueError("Embedding response missing values")

    vector = [float(value) for value in raw_values]
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"Embedding dimension mismatch: expected {EMBEDDING_DIMENSIONS}, got {len(vector)}")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        raise ValueError("Embedding vector norm must be positive")
    return [value / norm for value in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    return sum(left_item * right_item for left_item, right_item in zip(left, right))


def _embedding_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    return []


def _embedding_text_value(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {item}"
            for key, raw_item in value.items()
            if (item := str(raw_item).strip())
        )
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _build_photo_embedding_text(photo: Photo, ai_result: dict[str, Any] | None = None) -> str:
    structured_result = ai_result if ai_result is not None else (photo.tag_json if isinstance(photo.tag_json, dict) else {})
    ai_summary = str(structured_result.get("ai_summary") or "").strip()
    labels = _embedding_text_list(structured_result.get("labels"))
    defects = _embedding_text_list(structured_result.get("defects"))

    lines = [
        "document_type: construction_field_photo",
        f"photo_type: {photo.photo_type.value}",
        f"company_id: {photo.company_id}",
        f"tenant_id: {photo.tenant_id or photo.company_id or ''}",
        f"project_id: {photo.project_id or ''}",
        f"employee_id: {photo.employee_id}",
        f"original_file_name: {photo.original_file_name}",
        f"mime_type: {photo.mime_type or ''}",
        f"location: {photo.location or ''}",
        f"gps: {photo.gps or ''}",
        f"note: {(photo.note or '').strip()}",
        f"ai_summary: {ai_summary}",
        f"labels: {', '.join(labels)}",
        f"defects: {', '.join(defects)}",
        f"scene_type: {_embedding_text_value(structured_result.get('scene_type'))}",
        f"visible_objects: {_embedding_text_value(structured_result.get('visible_objects'))}",
        f"materials: {_embedding_text_value(structured_result.get('materials'))}",
        f"equipment: {_embedding_text_value(structured_result.get('equipment'))}",
        f"people_ppe: {_embedding_text_value(structured_result.get('people_ppe'))}",
        f"safety_observations: {_embedding_text_value(structured_result.get('safety_observations'))}",
        f"quality_observations: {_embedding_text_value(structured_result.get('quality_observations'))}",
        f"inventory_observations: {_embedding_text_value(structured_result.get('inventory_observations'))}",
        f"water_or_housekeeping_observations: {_embedding_text_value(structured_result.get('water_or_housekeeping_observations'))}",
        f"receipt_facts: {_embedding_text_value(structured_result.get('receipt_facts'))}",
        f"financial_anomaly_flags: {_embedding_text_value(structured_result.get('financial_anomaly_flags'))}",
        f"missing_evidence: {_embedding_text_value(structured_result.get('missing_evidence'))}",
        f"recommended_actions: {_embedding_text_value(structured_result.get('recommended_actions'))}",
        f"evidence_limitations: {_embedding_text_value(structured_result.get('evidence_limitations'))}",
        f"device_model: {photo.device_model or ''}",
        f"os_version: {photo.os_version or ''}",
        f"app_version: {photo.app_version or ''}",
    ]
    return "\n".join(line for line in lines if ":" in line)


def _strip_json_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _iter_json_object_candidates(raw_text: str):
    in_string = False
    escaped = False
    depth = 0
    start_index: int | None = None
    for index, char in enumerate(raw_text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
            continue
        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_index is not None:
                yield raw_text[start_index : index + 1]
                start_index = None


def _loads_model_json_object(raw_text: str) -> dict[str, Any]:
    text = _strip_json_fence(raw_text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as original_exc:
        for candidate in _iter_json_object_candidates(text):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise original_exc
    if not isinstance(payload, dict):
        raise ValueError("AI response JSON must be an object")
    return payload


def _json_decode_error_is_likely_truncation(exc: json.JSONDecodeError) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "unterminated string",
            "expecting ',' delimiter",
            "expecting property name enclosed in double quotes",
            "expecting value",
        )
    )


def _repair_truncated_json_text(raw_text: str) -> str | None:
    text = _strip_json_fence(raw_text).strip()
    if not text or "{" not in text:
        return None
    text = text[text.find("{") :]

    in_string = False
    escaped = False
    stack: list[str] = []
    repaired_chars: list[str] = []
    for char in text:
        repaired_chars.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            stack.append("}")
            continue
        if char == "[":
            stack.append("]")
            continue
        if char in ("}", "]") and stack and stack[-1] == char:
            stack.pop()

    if in_string:
        repaired_chars.append('"')
    while stack:
        closer = stack.pop()
        if repaired_chars and repaired_chars[-1] == ",":
            repaired_chars.pop()
        repaired_chars.append(closer)
    candidate = "".join(repaired_chars)
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate


def _extract_gemini_text(response_payload: Any, *, response_kind: str) -> str:
    if not isinstance(response_payload, dict):
        raise ValueError(f"Gemini {response_kind} response body must be a JSON object")

    error_payload = response_payload.get("error")
    if isinstance(error_payload, dict):
        error_message = str(error_payload.get("message") or f"Gemini returned a {response_kind} error payload")
        raise ValueError(error_message)

    candidates = response_payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"Gemini {response_kind} response missing candidates")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            candidate_text = part.get("text")
            if isinstance(candidate_text, str) and candidate_text.strip():
                return candidate_text

    raise ValueError(f"Gemini {response_kind} response missing text content")


def _parse_model_json(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        payload = raw_value
    elif isinstance(raw_value, str):
        payload = _loads_model_json_object(raw_value)
    else:
        raise ValueError("AI response body is not valid JSON")

    if not isinstance(payload, dict):
        raise ValueError("AI response JSON must be an object")
    return payload


def _normalize_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError(f"{field_name} must be an array of strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text:
            continue
        if field_name == "labels":
            normalized_text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
            if not normalized_text:
                continue
            if normalized_text in GENERIC_AI_LABELS:
                continue
        else:
            normalized_text = text
            if normalized_text.casefold() in {
                "none",
                "none.",
                "n/a",
                "na",
                "no defect",
                "no defects",
                "no visible defect",
                "no visible defects",
                "no obvious defect",
                "no obvious defects",
                "no clear defect",
                "no clear defects",
                "no issue",
                "no issues",
                "project",
                "building",
                "construction",
                "image",
                "photo",
                "scene",
                "object",
                "objects",
                "item",
                "items",
            }:
                continue
        dedupe_key = normalized_text.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(normalized_text)
        if field_name == "labels" and len(normalized) >= MAX_NORMALIZED_LABEL_COUNT:
            break
    return normalized


def _casefold_terms(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {" ".join(str(value or "").strip().casefold().split()) for value in values if str(value or "").strip()}


def _has_signal_word(text: str, signals: set[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(signal in normalized for signal in signals)


NEGATED_RISK_PATTERNS = (
    r"\bno\s+(?:visible\s+|clear\s+|obvious\s+)?(?:safety\s+)?(?:risk|risks|hazard|hazards|issue|issues|defect|defects|damage|concerns)\b",
    r"\bwithout\s+(?:visible\s+|clear\s+|obvious\s+)?(?:safety\s+)?(?:risk|risks|hazard|hazards|issue|issues|defect|defects|damage|concerns)\b",
    r"\bnot\s+(?:showing|indicating|displaying)\s+(?:visible\s+|clear\s+|obvious\s+)?(?:risk|risks|hazard|hazards|issue|issues|defect|defects)\b",
)
RISK_OR_ISSUE_WORDS = {
    "risk",
    "risks",
    "hazard",
    "hazards",
    "unsafe",
    "danger",
    "defect",
    "defects",
    "issue",
    "issues",
    "concern",
    "concerns",
    "damage",
    "damaged",
    "fall",
    "electrical",
    "blocked",
    "obstruction",
    "missing",
    "unprotected",
}
RISK_LABELS = {
    "safety_risk",
    "safety_risks",
    "quality_issue",
    "quality_issues",
    "defect",
    "defects",
    "hazard",
    "hazards",
    "fall_hazard",
    "electrical_hazard",
    "ppe_missing",
    "unsafe",
    "damage",
    "damaged",
}
MALFORMED_TEXT_MARKERS = ("[ ]", "[ ] [ ]", "} {", "[ [", "] ]")
NEGATIVE_LABEL_PREFIXES = ("no_visible_", "no_clear_", "no_obvious_")
CONSTRUCTION_SUPPORT_FIELDS = ("materials", "equipment", "people_ppe", "safety_observations", "quality_observations", "inventory_observations")
CONSTRUCTION_SUPPORT_WORDS = {
    "construction",
    "worker",
    "workers",
    "hard hat",
    "helmet",
    "vest",
    "ppe",
    "lumber",
    "concrete",
    "rebar",
    "drywall",
    "pipe",
    "conduit",
    "scaffold",
    "ladder",
    "excavator",
    "forklift",
    "equipment",
    "tool",
    "tools",
    "installed",
    "installation",
    "jobsite",
    "worksite",
}


def _contains_negated_risk_statement(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(re.search(pattern, normalized) for pattern in NEGATED_RISK_PATTERNS)


def _contains_risk_language(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in RISK_OR_ISSUE_WORDS)


def _clean_summary_prefix(summary: Any) -> str:
    text = str(summary or "").strip()
    text = re.sub(
        r"^(?:the\s+)?(?:image|photo|picture)\s+(?:shows|appears to show|depicts|contains)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if text:
        text = text[0].upper() + text[1:]
    return text


def _remove_unsupported_work_context(summary: Any) -> str:
    text = str(summary or "").strip()
    if not text:
        return ""
    text = re.sub(
        r",?\s*(?:possibly|probably|potentially)\s+(?:on|at|in)?\s*(?:a\s+)?(?:construction\s+site|jobsite|worksite|project\s+site|project)\b\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:may|might|could)\s+(?:indicate|suggest|represent)\s+(?:a\s+)?(?:construction\s+site|jobsite|worksite|project|work\s+activity)\b\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s+\bor\s+(?:a\s+)?(?:construction\s+site|jobsite|worksite|project\s+site|project)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split()).strip(" ,")


def _looks_malformed_ai_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if any(marker in text for marker in MALFORMED_TEXT_MARKERS):
        return True
    bracket_count = text.count("[") + text.count("]")
    if bracket_count >= 3:
        return True
    if len(text) > 80 and len(set(text.split())) <= 3:
        return True
    return False


def _looks_repetitive_ai_text(value: Any) -> bool:
    words = re.findall(r"[a-zA-Z0-9]+", str(value or "").casefold())
    if len(words) < 16:
        return False
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    if max(counts.values(), default=0) >= max(8, int(len(words) * 0.35)):
        return True
    bigrams = [" ".join(pair) for pair in zip(words, words[1:])]
    bigram_counts: dict[str, int] = {}
    for bigram in bigrams:
        bigram_counts[bigram] = bigram_counts.get(bigram, 0) + 1
    return max(bigram_counts.values(), default=0) >= max(5, int(len(bigrams) * 0.28))


def _has_positive_risk_evidence(payload: dict[str, Any]) -> bool:
    for field_name in (
        "defects",
        "safety_observations",
        "quality_observations",
        "water_or_housekeeping_observations",
        "financial_anomaly_flags",
    ):
        values = payload.get(field_name)
        if not isinstance(values, list):
            continue
        for item in values:
            text = str(item or "").strip()
            if text and _contains_risk_language(text) and not _contains_negated_risk_statement(text):
                return True
    return False


def _has_construction_support(payload: dict[str, Any]) -> bool:
    for field_name in CONSTRUCTION_SUPPORT_FIELDS:
        values = payload.get(field_name)
        if not isinstance(values, list):
            continue
        text = " ".join(str(item or "") for item in values).casefold()
        if any(word in text for word in CONSTRUCTION_SUPPORT_WORDS):
            return True
    return False


def _remove_risk_labels_without_evidence(payload: dict[str, Any]) -> None:
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return
    has_risk_evidence = _has_positive_risk_evidence(payload)
    cleaned: list[str] = []
    for label in labels:
        normalized_label = re.sub(r"[^a-z0-9]+", "_", str(label or "").casefold()).strip("_")
        if not normalized_label:
            continue
        if normalized_label.startswith(NEGATIVE_LABEL_PREFIXES):
            continue
        if normalized_label in RISK_LABELS and not has_risk_evidence:
            continue
        cleaned.append(label)
    payload["labels"] = cleaned


def _sanitize_ai_result_consistency(payload: dict[str, Any]) -> dict[str, Any]:
    payload["ai_summary"] = _clean_summary_prefix(payload.get("ai_summary"))
    if _looks_malformed_ai_text(payload.get("ai_summary")) or _looks_repetitive_ai_text(payload.get("ai_summary")):
        payload["ai_summary"] = "Model response was repetitive and not reliable enough for a field audit conclusion."
        payload["confidence_level"] = "low"
        payload["evidence_limitations"] = ["Model response contained repetitive text and should be reviewed or regenerated."]
    for field_name in ("scene_type", "confidence_level"):
        if _looks_malformed_ai_text(payload.get(field_name)):
            payload[field_name] = "" if field_name == "scene_type" else "low"
    for field_name in ("labels", "visible_objects", "materials", "equipment"):
        values = payload.get(field_name)
        if isinstance(values, list):
            payload[field_name] = [item for item in values if not _looks_malformed_ai_text(item)]

    summary = str(payload.get("ai_summary") or "")
    negated_risk_summary = _contains_negated_risk_statement(summary)
    has_positive_risk_evidence = _has_positive_risk_evidence(payload)

    if negated_risk_summary and not has_positive_risk_evidence:
        for field_name in (
            "defects",
            "safety_observations",
            "quality_observations",
            "water_or_housekeeping_observations",
            "financial_anomaly_flags",
        ):
            payload[field_name] = []
        payload["recommended_actions"] = []

    _remove_risk_labels_without_evidence(payload)

    if not _has_construction_support(payload):
        labels = payload.get("labels")
        if isinstance(labels, list):
            payload["labels"] = [
                label
                for label in labels
                if re.sub(r"[^a-z0-9]+", "_", str(label or "").casefold()).strip("_") != "construction_site"
            ]
        if str(payload.get("scene_type") or "").strip().casefold() in {"construction_site", "construction site"}:
            payload["scene_type"] = "unclear_context"
        payload["ai_summary"] = _remove_unsupported_work_context(payload.get("ai_summary"))

    if payload.get("confidence_level") == "low" and not payload.get("evidence_limitations"):
        payload["evidence_limitations"] = ["Low confidence: image clarity, angle, crop, distance, or visible context is insufficient for a reliable audit conclusion."]

    return payload


def _clean_low_information_ai_fields(payload: dict[str, Any]) -> dict[str, Any]:
    object_terms = set()
    for source_field in ("visible_objects", "materials", "equipment", "labels"):
        object_terms.update(_casefold_terms(payload.get(source_field)))

    def clean_field(field_name: str, *, signals: set[str], require_signal: bool = True) -> None:
        raw_values = payload.get(field_name)
        if not isinstance(raw_values, list):
            return
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_values:
            item = " ".join(str(raw_item or "").strip().split())
            if not item:
                continue
            folded = item.casefold()
            if folded in object_terms:
                continue
            if folded in {"high", "medium", "low", "unknown", "n/a", "na"}:
                continue
            if require_signal and not _has_signal_word(item, signals):
                continue
            if folded in seen:
                continue
            seen.add(folded)
            cleaned.append(item)
        payload[field_name] = cleaned

    for observation_field in (
        "safety_observations",
        "quality_observations",
        "water_or_housekeeping_observations",
    ):
        clean_field(observation_field, signals=OBSERVATION_SIGNAL_WORDS)
    clean_field("recommended_actions", signals=ACTION_SIGNAL_WORDS)
    clean_field("evidence_limitations", signals=LIMITATION_SIGNAL_WORDS)
    return _sanitize_ai_result_consistency(payload)


def normalize_ai_result(raw_payload: Any) -> dict[str, Any]:
    payload = _parse_model_json(raw_payload)
    return _normalize_structured_ai_payload(payload, require_summary=True)


def _create_ai_analysis_log(
    db: Session,
    *,
    photo: Photo | None = None,
    media_asset_id: str | None = None,
    batch_id: str | None,
    analysis_type: AIAnalysisType,
    prompt_used: str,
    model_used: str,
    result_data: dict[str, Any],
    created_by: str,
) -> AIAnalysisLog:
    if photo is None and not media_asset_id:
        raise ValueError("AI analysis log target is required")
    analysis_log = AIAnalysisLog(
        id=str(uuid4()),
        photo_id=photo.id if photo is not None else None,
        media_asset_id=media_asset_id,
        batch_id=batch_id,
        analysis_type=analysis_type,
        prompt_used=prompt_used,
        model_used=model_used,
        result_data=result_data,
        status=AIAnalysisStatus.active,
        created_by=created_by,
    )
    db.add(analysis_log)
    db.flush()
    return analysis_log


def _active_ai_analysis_logs_for_photo(db: Session, photo_id: int) -> list[AIAnalysisLog]:
    return list(
        db.scalars(
            select(AIAnalysisLog)
            .where(
                AIAnalysisLog.photo_id == photo_id,
                AIAnalysisLog.status == AIAnalysisStatus.active,
            )
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        )
    )


def _active_ai_analysis_logs_for_media_asset(db: Session, media_asset_id: str) -> list[AIAnalysisLog]:
    return list(
        db.scalars(
            select(AIAnalysisLog)
            .where(
                AIAnalysisLog.media_asset_id == media_asset_id,
                AIAnalysisLog.status == AIAnalysisStatus.active,
            )
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        )
    )


def rebuild_photo_ai_snapshot(
    db: Session,
    *,
    app_settings: Settings,
    photo: Photo,
    actor_user_id: int | None = None,
    source: str = "upload",
    priority: str = "normal",
    backends: list[AIBackendNode] | None = None,
    config_source: str | None = None,
) -> tuple[dict[str, Any], bool, str | None, int]:
    active_logs = _active_ai_analysis_logs_for_photo(db, photo.id)
    merged_snapshot = merge_active_ai_analysis_logs(active_logs)
    photo.tag_json = merged_snapshot
    photo.labeling_status = "completed"
    sync_ai_runtime_settings(db, app_settings)

    if not active_logs:
        photo.embedding = None
        photo.embedding_ref = None
        db.add(photo)
        return merged_snapshot, False, None, 0

    embedding_ref: str | None = None
    embedding_stored = False
    try:
        embedding_stored, embedding_ref = refresh_photo_embedding(
            db,
            app_settings=app_settings,
            photo=photo,
            ai_result=merged_snapshot,
            actor_user_id=actor_user_id,
            source=source,
            priority=priority,
            backends=backends,
            config_source=config_source,
        )
    except Exception as embedding_exc:
        photo.embedding = None
        photo.embedding_ref = None
        db.add(photo)
        logger.warning(
            "photo_embedding_generation_failed",
            photo_id=photo.id,
            company_id=photo.company_id,
            project_id=photo.project_id,
            config_source=config_source,
            source=source,
            priority=priority,
            error=str(embedding_exc),
        )
        log_audit(
            db,
            action="photo_embedding_failed",
            target_type="photo",
            target_id=str(photo.id),
            actor_user_id=actor_user_id,
            tenant_id=photo.tenant_id or photo.company_id,
            project_id=photo.project_id,
            company_id=photo.company_id,
            detail_json={
                "config_source": config_source,
                "source": source,
                "priority": priority,
                "labeling_status": photo.labeling_status,
                "active_analysis_log_count": len(active_logs),
                "error": str(embedding_exc),
            },
        )
    db.add(photo)
    return merged_snapshot, embedding_stored, embedding_ref, len(active_logs)


def set_ai_analysis_log_status(
    db: Session,
    *,
    app_settings: Settings,
    analysis_log: AIAnalysisLog,
    status: AIAnalysisStatus,
    actor_user_id: int | None,
    source: str,
) -> tuple[AIAnalysisLog, dict[str, Any]]:
    analysis_log.status = status
    db.add(analysis_log)
    db.flush()
    photo = db.get(Photo, analysis_log.photo_id)
    if photo is None:
        raise RuntimeError("Associated photo not found")

    snapshot, embedding_stored, embedding_ref, active_log_count = rebuild_photo_ai_snapshot(
        db,
        app_settings=app_settings,
        photo=photo,
        actor_user_id=actor_user_id,
        source=source,
        priority="normal",
    )
    log_audit(
        db,
        action="photo_ai_log_status_updated",
        target_type="ai_analysis_log",
        target_id=analysis_log.id,
        actor_user_id=actor_user_id,
        tenant_id=photo.tenant_id or photo.company_id,
        project_id=photo.project_id,
        company_id=photo.company_id,
        detail_json={
            "photo_id": photo.id,
            "status": status.value,
            "source": source,
            "batch_id": analysis_log.batch_id,
            "active_analysis_log_count": active_log_count,
            "embedding_stored": embedding_stored,
            "embedding_ref": embedding_ref,
        },
    )
    return analysis_log, snapshot


def rollback_ai_analysis_batch(
    db: Session,
    *,
    app_settings: Settings,
    batch_id: str,
    actor_user_id: int | None,
    source: str,
    allowed_photo_ids: list[int] | None = None,
) -> tuple[int, list[int]]:
    batch_logs = list(
        db.scalars(
            select(AIAnalysisLog)
            .where(AIAnalysisLog.batch_id == batch_id)
            .order_by(AIAnalysisLog.created_at.desc(), AIAnalysisLog.id.desc())
        )
    )
    if allowed_photo_ids is not None:
        allowed_ids = set(allowed_photo_ids)
        batch_logs = [log for log in batch_logs if log.photo_id in allowed_ids]

    affected_photo_ids: set[int] = set()
    updated_count = 0
    for analysis_log in batch_logs:
        if analysis_log.status == AIAnalysisStatus.rejected:
            continue
        analysis_log.status = AIAnalysisStatus.rejected
        db.add(analysis_log)
        affected_photo_ids.add(analysis_log.photo_id)
        updated_count += 1

    db.flush()

    for photo_id in sorted(affected_photo_ids):
        photo = db.get(Photo, photo_id)
        if photo is None:
            continue
        rebuild_photo_ai_snapshot(
            db,
            app_settings=app_settings,
            photo=photo,
            actor_user_id=actor_user_id,
            source=source,
            priority="normal",
        )
        log_audit(
            db,
            action="photo_ai_batch_rolled_back",
            target_type="photo",
            target_id=str(photo.id),
            actor_user_id=actor_user_id,
            tenant_id=photo.tenant_id or photo.company_id,
            project_id=photo.project_id,
            company_id=photo.company_id,
            detail_json={
                "batch_id": batch_id,
                "source": source,
                "rolled_back_log_count": updated_count,
            },
        )

    return updated_count, sorted(affected_photo_ids)


def _build_ollama_endpoint(node: AIBackendNode) -> str:
    base_url = node.url.rstrip("/")
    if base_url.endswith("/api/generate"):
        return base_url
    return f"{base_url}/api/generate"


def _build_ollama_embedding_endpoint(node: AIBackendNode) -> str:
    base_url = node.url.rstrip("/")
    if base_url.endswith("/api/embeddings"):
        return base_url
    return f"{base_url}/api/embeddings"


def _build_gemini_endpoint(node: AIBackendNode) -> str:
    base_url = node.url.strip().rstrip("/")
    if base_url.endswith(":generateContent"):
        endpoint = base_url
    elif "/models/" in base_url:
        endpoint = f"{base_url}:generateContent"
    else:
        endpoint = f"{base_url}/models/{node.model}:generateContent"

    api_key = (node.api_key or "").strip()
    if not api_key:
        return endpoint

    parsed = urlsplit(endpoint)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query_params.get("key"):
        return endpoint

    query_params["key"] = api_key
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_params, quote_via=quote),
            parsed.fragment,
        )
    )


def _build_gemini_embedding_endpoint(node: AIBackendNode, embedding_model: str) -> str:
    base_url = node.url.strip().rstrip("/")
    if base_url.endswith(":embedContent"):
        endpoint = base_url
    elif "/models/" in base_url:
        endpoint = f"{base_url}:embedContent"
    else:
        endpoint = f"{base_url}/models/{embedding_model}:embedContent"

    api_key = (node.api_key or "").strip()
    if not api_key:
        return endpoint

    parsed = urlsplit(endpoint)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query_params.get("key"):
        return endpoint

    query_params["key"] = api_key
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_params, quote_via=quote),
            parsed.fragment,
        )
    )


def _embedding_model_for_node(node: AIBackendNode) -> str:
    if node.embedding_model:
        return node.embedding_model
    if node.type == OLLAMA_TYPE:
        return DEFAULT_OLLAMA_EMBEDDING_MODEL
    if node.type == GEMINI_TYPE:
        return DEFAULT_GEMINI_EMBEDDING_MODEL
    raise ValueError(f"Unsupported backend type: {node.type}")


def _response_error_message(response: Any, fallback_message: str) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        if isinstance(payload.get("error"), dict):
            error_message = str(payload["error"].get("message") or "").strip()
            if error_message:
                return error_message
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            return payload["message"].strip()

    text_body = str(getattr(response, "text", "") or "").strip()
    if text_body:
        return text_body[:500]
    return fallback_message


def _raise_for_status_with_message(response: Any) -> None:
    try:
        response.raise_for_status()
    except RequestException as exc:
        raise ValueError(_response_error_message(response, str(exc))) from exc


def call_ollama_backend(node: AIBackendNode, *, prompt: str, image_base64: str) -> dict[str, Any]:
    try:
        response = requests.post(
            _build_ollama_endpoint(node),
            json={
                "model": node.model,
                "prompt": prompt,
                "images": [image_base64],
                "stream": False,
                "format": _ollama_json_schema_for_prompt(prompt),
                "options": dict(OLLAMA_VISION_GENERATION_OPTIONS),
            },
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        response_payload = response.json()
        raw_response = response_payload.get("response")
        try:
            return normalize_ai_result(raw_response)
        except json.JSONDecodeError as parse_exc:
            if not _json_decode_error_is_likely_truncation(parse_exc) or not isinstance(raw_response, str):
                raise
            repaired_text = _repair_truncated_json_text(raw_response)
            if repaired_text is not None:
                try:
                    repaired_result = normalize_ai_result(repaired_text)
                except (ValueError, json.JSONDecodeError) as local_repair_exc:
                    logger.warning(
                        "ollama_vision_json_local_repair_rejected",
                        backend_id=node.id,
                        backend_type=node.type,
                        backend_model=node.model,
                        parse_error=str(parse_exc),
                        repair_error=str(local_repair_exc),
                    )
                else:
                    logger.warning(
                        "ollama_vision_json_repaired_locally",
                        backend_id=node.id,
                        backend_type=node.type,
                        backend_model=node.model,
                        error=str(parse_exc),
                    )
                    return repaired_result
            repair_prompt = (
                "Repair the following malformed JSON into one valid JSON object that follows the same keys. "
                "Do not add facts. Do not explain. Return JSON only.\n\n"
                f"{raw_response[:6000]}"
            )
            repair_response = requests.post(
                _build_ollama_endpoint(node),
                json={
                    "model": node.model,
                    "prompt": repair_prompt,
                    "stream": False,
                    "format": _ollama_json_schema_for_prompt(prompt),
                    "options": dict(OLLAMA_VISION_REPAIR_GENERATION_OPTIONS),
                },
                timeout=AI_REQUEST_TIMEOUT_SECONDS,
            )
            _raise_for_status_with_message(repair_response)
            repair_payload = repair_response.json()
            logger.warning(
                "ollama_vision_json_repaired_by_model",
                backend_id=node.id,
                backend_type=node.type,
                backend_model=node.model,
                error=str(parse_exc),
            )
            return normalize_ai_result(repair_payload.get("response"))
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_gemini_backend(node: AIBackendNode, *, prompt: str, image_base64: str, mime_type: str) -> dict[str, Any]:
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_base64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            _build_gemini_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        text_part = _extract_gemini_text(response.json(), response_kind="vision")
        return normalize_ai_result(text_part)
    except (RequestException, ValueError, json.JSONDecodeError, IndexError, KeyError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_ai_backend(node: AIBackendNode, *, prompt: str, image_base64: str, mime_type: str) -> dict[str, Any]:
    with AI_RATE_LIMITER.acquire(operation="vision", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation="vision", backend=node):
            if node.type == OLLAMA_TYPE:
                return call_ollama_backend(node, prompt=prompt, image_base64=image_base64)
            if node.type == GEMINI_TYPE:
                return call_gemini_backend(node, prompt=prompt, image_base64=image_base64, mime_type=mime_type)
            raise AIBackendError(node, f"Unsupported backend type: {node.type}")


def call_ollama_embedding_backend(node: AIBackendNode, *, text: str) -> tuple[list[float], str]:
    embedding_model = _embedding_model_for_node(node)
    try:
        response = requests.post(
            _build_ollama_embedding_endpoint(node),
            json={
                "model": embedding_model,
                "prompt": text,
            },
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError("Ollama embedding response body must be a JSON object")
        raw_embedding = response_payload.get("embedding")
        if raw_embedding is None and isinstance(response_payload.get("embeddings"), list):
            embeddings = response_payload["embeddings"]
            raw_embedding = embeddings[0] if embeddings else None
        normalized_embedding = _normalize_embedding_values(raw_embedding)
        return normalized_embedding, f"{node.type}:{embedding_model}"
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_gemini_embedding_backend(
    node: AIBackendNode,
    *,
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> tuple[list[float], str]:
    embedding_model = _embedding_model_for_node(node)
    request_body = {
        "content": {
            "parts": [
                {"text": text},
            ]
        },
        "taskType": task_type,
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }
    try:
        response = requests.post(
            _build_gemini_embedding_endpoint(node, embedding_model),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError("Gemini embedding response body must be a JSON object")
        error_payload = response_payload.get("error")
        if isinstance(error_payload, dict):
            raise ValueError(str(error_payload.get("message") or "Gemini returned an error payload"))
        embedding_payload = response_payload.get("embedding")
        if not isinstance(embedding_payload, dict):
            embeddings = response_payload.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                first_embedding = embeddings[0]
                if isinstance(first_embedding, dict):
                    embedding_payload = first_embedding
        if not isinstance(embedding_payload, dict):
            raise ValueError("Gemini embedding response missing embedding payload")

        raw_values = embedding_payload.get("values")
        if raw_values is None and isinstance(embedding_payload.get("embedding"), dict):
            raw_values = embedding_payload["embedding"].get("values")
        if raw_values is None:
            raise ValueError("Gemini embedding response missing embedding values")

        normalized_embedding = _normalize_embedding_values(raw_values)
        return normalized_embedding, f"{node.type}:{embedding_model}"
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_embedding_backend(
    node: AIBackendNode,
    *,
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> tuple[list[float], str]:
    with AI_RATE_LIMITER.acquire(operation=f"embedding:{task_type.lower()}", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation=f"embedding:{task_type.lower()}", backend=node):
            if node.type == OLLAMA_TYPE:
                return call_ollama_embedding_backend(node, text=text)
            if node.type == GEMINI_TYPE:
                return call_gemini_embedding_backend(node, text=text, task_type=task_type)
            raise AIBackendError(node, f"Unsupported backend type: {node.type}")


def _strip_markdown_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def call_ollama_text_backend(node: AIBackendNode, *, prompt: str) -> str:
    request_body: dict[str, Any] = {
        "model": node.model,
        "prompt": prompt,
        "stream": False,
    }
    response_format = _ollama_text_format_for_prompt(prompt)
    if response_format is not None:
        request_body["format"] = response_format
        request_body["options"] = {"temperature": 0.0, "top_p": 0.8, "repeat_penalty": 1.15, "num_predict": 900}
    try:
        response = requests.post(
            _build_ollama_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError("Ollama text response body must be a JSON object")
        raw_text = response_payload.get("response")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("Ollama text response missing content")
        return _strip_markdown_fence(raw_text)
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_gemini_text_backend(node: AIBackendNode, *, prompt: str) -> str:
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "text/plain",
        },
    }
    try:
        response = requests.post(
            _build_gemini_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        return _strip_markdown_fence(_extract_gemini_text(response.json(), response_kind="text"))
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_text_backend(node: AIBackendNode, *, prompt: str) -> str:
    with AI_RATE_LIMITER.acquire(operation="text", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation="text", backend=node):
            if node.type == OLLAMA_TYPE:
                return call_ollama_text_backend(node, prompt=prompt)
            if node.type == GEMINI_TYPE:
                return call_gemini_text_backend(node, prompt=prompt)
            raise AIBackendError(node, f"Unsupported backend type: {node.type}")


def call_ollama_role_vision_backend(node: AIBackendNode, *, prompt: str, image_base64: str) -> dict[str, Any]:
    try:
        response = requests.post(
            _build_ollama_endpoint(node),
            json={
                "model": node.model,
                "prompt": prompt,
                "images": [image_base64],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "top_p": 0.8, "repeat_penalty": 1.15, "num_predict": 700},
            },
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError("Ollama role vision response body must be a JSON object")
        raw_text = response_payload.get("response")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("Ollama role vision response missing content")
        return extract_role_json(raw_text)
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_gemini_role_vision_backend(node: AIBackendNode, *, prompt: str, image_base64: str, mime_type: str) -> dict[str, Any]:
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_base64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            _build_gemini_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        return extract_role_json(_extract_gemini_text(response.json(), response_kind="role_vision"))
    except (RequestException, ValueError, json.JSONDecodeError, IndexError, KeyError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_role_vision_backend(node: AIBackendNode, *, prompt: str, image_base64: str, mime_type: str) -> dict[str, Any]:
    with AI_RATE_LIMITER.acquire(operation="role-vision", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation="role-vision", backend=node):
            if node.type == OLLAMA_TYPE:
                return call_ollama_role_vision_backend(node, prompt=prompt, image_base64=image_base64)
            if node.type == GEMINI_TYPE:
                return call_gemini_role_vision_backend(node, prompt=prompt, image_base64=image_base64, mime_type=mime_type)
            raise AIBackendError(node, f"Unsupported backend type: {node.type}")


def call_ollama_multi_image_text_backend(
    node: AIBackendNode,
    *,
    prompt: str,
    images: list[tuple[str, str]],
) -> tuple[str, str]:
    image_payload = [image_base64 for _, image_base64 in images]
    models_to_try = [node.model]
    if len(images) > 1 and node.model != OLLAMA_MULTI_IMAGE_FALLBACK_MODEL:
        models_to_try.append(OLLAMA_MULTI_IMAGE_FALLBACK_MODEL)

    last_error: Exception | None = None
    for index, model_name in enumerate(models_to_try):
        try:
            response = requests.post(
                _build_ollama_endpoint(node),
                json={
                    "model": model_name,
                    "prompt": prompt,
                    "images": image_payload,
                    "stream": False,
                },
                timeout=AI_REQUEST_TIMEOUT_SECONDS,
            )
            _raise_for_status_with_message(response)
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise ValueError("Ollama multi-image response body must be a JSON object")
            raw_text = response_payload.get("response")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ValueError("Ollama multi-image response missing content")
            return _strip_markdown_fence(raw_text), model_name
        except (RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if index + 1 < len(models_to_try) and _looks_like_single_image_only_error(str(exc)):
                logger.warning(
                    "multi_image_generation_model_retry",
                    backend_id=node.id,
                    backend_type=node.type,
                    failed_model=model_name,
                    fallback_model=models_to_try[index + 1],
                    error=str(exc),
                )
                continue
            raise AIBackendError(node, str(exc)) from exc

    raise AIBackendError(node, str(last_error or "Ollama multi-image request failed"))


def call_gemini_multi_image_text_backend(
    node: AIBackendNode,
    *,
    prompt: str,
    images: list[tuple[str, str]],
) -> tuple[str, str]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for mime_type, image_base64 in images:
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime_type,
                    "data": image_base64,
                }
            }
        )
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {
            "responseMimeType": "text/plain",
        },
    }
    try:
        response = requests.post(
            _build_gemini_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        return _strip_markdown_fence(_extract_gemini_text(response.json(), response_kind="multi_image")), node.model
    except (RequestException, ValueError, json.JSONDecodeError, IndexError, KeyError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def call_multi_image_text_backend(
    node: AIBackendNode,
    *,
    prompt: str,
    images: list[tuple[str, str]],
) -> tuple[str, str]:
    with AI_RATE_LIMITER.acquire(operation="multi-image-text", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation="multi-image-text", backend=node):
            if node.type == OLLAMA_TYPE:
                return call_ollama_multi_image_text_backend(node, prompt=prompt, images=images)
            if node.type == GEMINI_TYPE:
                return call_gemini_multi_image_text_backend(node, prompt=prompt, images=images)
            raise AIBackendError(node, f"Unsupported backend type: {node.type}")


def generate_multi_image_completion(
    *,
    backends: list[AIBackendNode],
    prompt: str,
    images: list[tuple[str, str]],
) -> tuple[str, str, list[dict[str, Any]]]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("Multi-image prompt cannot be empty")
    if not images:
        raise ValueError("At least one image is required for multi-image analysis")

    ordered_backends = _build_backend_order([backend for backend in backends if backend.enabled and backend.weight > 0])
    if not ordered_backends:
        raise RuntimeError("No enabled AI vision backends are configured")

    attempts: list[dict[str, Any]] = []
    remaining_backends = list(ordered_backends)
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            response_text, model_used = call_multi_image_text_backend(backend, prompt=normalized_prompt, images=images)
            _clear_backend_cooldown(backend)
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "success",
                    "model": model_used,
                }
            )
            return response_text, f"{backend.type}:{model_used}", attempts
        except AIBackendError as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": exc.message,
                }
            )
            _register_backend_failure_cooldown(
                backend,
                operation="multi-image-text",
                error_message=exc.message,
            )
            logger.warning(
                "multi_image_generation_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                error=exc.message,
            )
            remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                remaining_backends,
                failed_backend=backend,
                operation="multi-image-text",
                error_message=exc.message,
            )

    raise RuntimeError("All enabled AI vision backends failed to generate a multi-image report")


def call_gemini_audio_transcription_backend(node: AIBackendNode, *, audio_base64: str, mime_type: str) -> str:
    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Transcribe this construction or field voice note. "
                            "Return only the plain transcript text. "
                            "Do not add summaries, labels, or commentary."
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": audio_base64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "text/plain",
        },
    }
    try:
        response = requests.post(
            _build_gemini_endpoint(node),
            json=request_body,
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
        _raise_for_status_with_message(response)
        transcript = _strip_markdown_fence(_extract_gemini_text(response.json(), response_kind="audio"))
        normalized_transcript = transcript.strip()
        if not normalized_transcript:
            raise ValueError("Gemini audio transcription response was empty")
        return normalized_transcript
    except (RequestException, ValueError, json.JSONDecodeError) as exc:
        raise AIBackendError(node, str(exc)) from exc


def _voice_transcription_model_name(db: Session, app_settings: Settings) -> str:
    system_settings = get_system_settings(db, app_settings)
    configured_model = str(system_settings.get("voice_transcription_model") or "").strip()
    return configured_model or DEFAULT_VOICE_TRANSCRIPTION_MODEL


def _voice_translation_model_name(db: Session, app_settings: Settings) -> str:
    system_settings = get_system_settings(db, app_settings)
    configured_model = str(system_settings.get("voice_translation_ollama_model") or "").strip()
    return configured_model or DEFAULT_VOICE_TRANSLATION_OLLAMA_MODEL


def _normalize_language_code(raw_value: str | None) -> str:
    normalized_value = str(raw_value or "").strip().lower()
    if normalized_value.startswith("zh"):
        return "zh"
    if normalized_value.startswith("en"):
        return "en"
    if normalized_value.startswith("es"):
        return "es"
    return normalized_value or "unknown"


def _get_faster_whisper_model(model_name: str):
    with _FASTER_WHISPER_MODEL_CACHE_LOCK:
        cached_model = _FASTER_WHISPER_MODEL_CACHE.get(model_name)
        if cached_model is not None:
            return cached_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on runtime install
            raise RuntimeError(
                "faster-whisper is not installed. Install backend dependencies before enabling voice transcription."
            ) from exc
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _FASTER_WHISPER_MODEL_CACHE[model_name] = model
        return model


def transcribe_audio_with_local_model(
    db: Session,
    app_settings: Settings,
    *,
    audio_path: Path,
) -> tuple[dict[str, str], str, list[dict[str, Any]]]:
    model_name = _voice_transcription_model_name(db, app_settings)
    attempts: list[dict[str, Any]] = []
    model = _get_faster_whisper_model(model_name)
    try:
        segments, info = model.transcribe(
            str(audio_path),
            task="transcribe",
            vad_filter=True,
            beam_size=5,
            best_of=5,
        )
        transcript_parts = [str(segment.text or "").strip() for segment in segments]
        transcript = " ".join(part for part in transcript_parts if part).strip()
        if not transcript:
            raise RuntimeError("Local transcription returned an empty transcript")
        detected_language = _normalize_language_code(getattr(info, "language", None))
        attempts.append(
            {
                "backend_id": f"faster-whisper:{model_name}",
                "backend_type": "faster_whisper",
                "status": "success",
                "language": detected_language,
            }
        )
        return {
            "source_text": transcript,
            "source_language": detected_language,
        }, f"faster_whisper:{model_name}", attempts
    except Exception as exc:
        attempts.append(
            {
                "backend_id": f"faster-whisper:{model_name}",
                "backend_type": "faster_whisper",
                "status": "failed",
                "error": str(exc),
            }
        )
        raise RuntimeError(str(exc)) from exc


def _ollama_translation_nodes(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
) -> list[AIBackendNode]:
    backends, _ = resolve_ai_backends_for_tenant(db, app_settings, tenant_slug)
    preferred_model = _voice_translation_model_name(db, app_settings)
    translation_nodes: list[AIBackendNode] = []
    seen: set[tuple[str, str, str | None]] = set()
    for backend in backends:
        if not backend.enabled or backend.weight <= 0 or backend.type != OLLAMA_TYPE:
            continue
        preferred_key = (backend.url, preferred_model, backend.api_key)
        if preferred_key not in seen:
            translation_nodes.append(
                AIBackendNode(
                    id=f"{backend.id}-voice-translation",
                    type=backend.type,
                    url=backend.url,
                    model=preferred_model,
                    api_key=backend.api_key,
                    embedding_model=backend.embedding_model,
                    weight=backend.weight,
                    enabled=True,
                )
            )
            seen.add(preferred_key)
    return translation_nodes


def translate_transcript_with_ollama_backends(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    source_text: str,
    source_language: str,
) -> tuple[dict[str, str], str, list[dict[str, Any]]]:
    normalized_source_text = str(source_text or "").strip()
    if not normalized_source_text:
        raise RuntimeError("Transcript text is empty")
    translation_nodes = _ollama_translation_nodes(db, app_settings, tenant_slug=tenant_slug)
    if not translation_nodes:
        raise RuntimeError("No enabled Ollama backends are configured for voice translation")

    prompt = "\n".join(
        [
            "You are a multilingual construction voice-note processor.",
            "Rewrite the transcript faithfully in Simplified Chinese, English, and Spanish.",
            "Do not add extra commentary, labels, or explanations.",
            "Return only valid JSON using this schema:",
            '{"source_language":"zh|en|es|other","translations":{"zh":"...","en":"...","es":"..."}}',
            f"Detected source language hint: {_normalize_language_code(source_language)}",
            "Transcript:",
            normalized_source_text,
        ]
    )
    attempts: list[dict[str, Any]] = []
    for backend in translation_nodes:
        try:
            raw_response = call_text_backend(backend, prompt=prompt)
            _clear_backend_cooldown(backend)
            payload = _parse_model_json(raw_response)
            if not isinstance(payload, dict):
                raise ValueError("Translation response must be a JSON object")
            raw_translations = payload.get("translations")
            if not isinstance(raw_translations, dict):
                raise ValueError("Translation response missing translations object")
            normalized_translations: dict[str, str] = {}
            for language in VOICE_TRANSLATION_LANGUAGES:
                translated_text = str(raw_translations.get(language) or "").strip()
                if not translated_text and language == _normalize_language_code(source_language):
                    translated_text = normalized_source_text
                if not translated_text:
                    raise ValueError(f"Translation response missing {language} text")
                normalized_translations[language] = translated_text
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "success",
                }
            )
            return normalized_translations, f"{backend.type}:{backend.model}", attempts
        except (AIBackendError, ValueError, json.JSONDecodeError) as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            _register_backend_failure_cooldown(
                backend,
                operation="voice-translation",
                error_message=str(exc),
            )
            logger.warning(
                "voice_translation_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                backend_model=backend.model,
                error=str(exc),
            )
    raise RuntimeError("All configured Ollama voice translation backends failed")


def translate_text_with_ollama_backends(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    source_text: str,
    source_language: str = "unknown",
    content_label: str = "construction AI summary",
) -> tuple[dict[str, str], str, list[dict[str, Any]]]:
    normalized_source_text = str(source_text or "").strip()
    if not normalized_source_text:
        raise RuntimeError("Source text is empty")
    translation_nodes = _ollama_translation_nodes(db, app_settings, tenant_slug=tenant_slug)
    if not translation_nodes:
        raise RuntimeError("No enabled Ollama backends are configured for text translation")

    prompt = "\n".join(
        [
            "You are a multilingual construction operations translator.",
            f"Translate the following {content_label} faithfully into Simplified Chinese, English, and Spanish.",
            "Keep the meaning exact and concise.",
            "Do not add commentary, headings, or explanations.",
            "Return only valid JSON using this schema:",
            '{"source_language":"zh|en|es|other","translations":{"zh":"...","en":"...","es":"..."}}',
            f"Detected source language hint: {_normalize_language_code(source_language)}",
            "Text:",
            normalized_source_text,
        ]
    )
    attempts: list[dict[str, Any]] = []
    for backend in translation_nodes:
        try:
            raw_response = call_text_backend(backend, prompt=prompt)
            _clear_backend_cooldown(backend)
            payload = _parse_model_json(raw_response)
            if not isinstance(payload, dict):
                raise ValueError("Translation response must be a JSON object")
            raw_translations = payload.get("translations")
            if not isinstance(raw_translations, dict):
                raise ValueError("Translation response missing translations object")
            normalized_translations: dict[str, str] = {}
            for language in VOICE_TRANSLATION_LANGUAGES:
                translated_text = str(raw_translations.get(language) or "").strip()
                if not translated_text and language == _normalize_language_code(source_language):
                    translated_text = normalized_source_text
                if not translated_text:
                    raise ValueError(f"Translation response missing {language} text")
                normalized_translations[language] = translated_text
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "success",
                }
            )
            return normalized_translations, f"{backend.type}:{backend.model}", attempts
        except (AIBackendError, ValueError, json.JSONDecodeError) as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            _register_backend_failure_cooldown(
                backend,
                operation="text-translation",
                error_message=str(exc),
            )
            logger.warning(
                "text_translation_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                backend_model=backend.model,
                error=str(exc),
                content_label=content_label,
            )
    raise RuntimeError("All configured Ollama text translation backends failed")


def ensure_ai_summary_translations(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = _normalize_analysis_snapshot(payload)
    if not normalized["ai_summary"]:
        return normalized
    missing_languages = [
        language for language in VOICE_TRANSLATION_LANGUAGES
        if language not in normalized["ai_summary_translations"]
    ]
    if not missing_languages:
        return normalized
    try:
        translations, backend_ref, attempts = translate_text_with_ollama_backends(
            db,
            app_settings,
            tenant_slug=tenant_slug,
            source_text=normalized["ai_summary"],
            content_label="construction AI summary",
        )
        normalized["ai_summary_translations"] = translations
        normalized["ai_summary_translation_backend"] = backend_ref
        normalized["ai_summary_translation_attempts"] = attempts
        return normalized
    except Exception as exc:
        logger.warning(
            "ai_summary_translation_failed",
            tenant_slug=tenant_slug,
            error=str(exc),
        )
        return normalized


def call_audio_transcription_backend(node: AIBackendNode, *, audio_base64: str, mime_type: str) -> str:
    with AI_RATE_LIMITER.acquire(operation="audio_transcription", backend=node):
        with AI_CONCURRENCY_LIMITER.acquire(operation="audio_transcription", backend=node):
            if node.type == GEMINI_TYPE:
                return call_gemini_audio_transcription_backend(node, audio_base64=audio_base64, mime_type=mime_type)
            raise AIBackendError(node, "Audio transcription is only supported on Gemini backends")


def transcribe_audio_with_ai_backends(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    audio_path: Path,
    mime_type: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    backends, _ = resolve_ai_backends_for_tenant(db, app_settings, tenant_slug)
    ordered_backends = _build_backend_order(
        [
            backend
            for backend in backends
            if backend.enabled and backend.weight > 0 and backend.type == GEMINI_TYPE
        ]
    )
    transcription_payload: dict[str, str] | None = None
    transcription_backend_ref: str | None = None
    attempts: list[dict[str, Any]] = []
    if ordered_backends:
        audio_base64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        for backend in ordered_backends:
            try:
                transcript = call_audio_transcription_backend(
                    backend,
                    audio_base64=audio_base64,
                    mime_type=mime_type,
                )
                _clear_backend_cooldown(backend)
                attempts.append(
                    {
                        "backend_id": backend.id,
                        "backend_type": backend.type,
                        "status": "success",
                    }
                )
                transcription_payload = {
                    "source_text": transcript,
                    "source_language": "unknown",
                }
                transcription_backend_ref = f"{backend.type}:{backend.model}"
                break
            except AIBackendError as exc:
                attempts.append(
                    {
                        "backend_id": backend.id,
                        "backend_type": backend.type,
                        "status": "failed",
                        "error": exc.message,
                    }
                )
                _register_backend_failure_cooldown(
                    backend,
                    operation="audio-transcription",
                    error_message=exc.message,
                )
                logger.warning(
                    "audio_transcription_backend_failed",
                    backend_id=backend.id,
                    backend_type=backend.type,
                    error=exc.message,
                )
    if transcription_payload is None or transcription_backend_ref is None:
        local_payload, local_backend_ref, local_attempts = transcribe_audio_with_local_model(
            db,
            app_settings,
            audio_path=audio_path,
        )
        attempts.extend(local_attempts)
        transcription_payload = local_payload
        transcription_backend_ref = local_backend_ref

    translations, translation_backend_ref, translation_attempts = translate_transcript_with_ollama_backends(
        db,
        app_settings,
        tenant_slug=tenant_slug,
        source_text=transcription_payload["source_text"],
        source_language=transcription_payload["source_language"],
    )
    attempts.extend(translation_attempts)
    return (
        {
            "source_text": transcription_payload["source_text"],
            "source_language": _normalize_language_code(transcription_payload.get("source_language")),
            "translations": translations,
        },
        f"{transcription_backend_ref} + {translation_backend_ref}",
        attempts,
    )


def generate_markdown_completion(
    *,
    backends: list[AIBackendNode],
    prompt: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("Completion prompt cannot be empty")

    ordered_backends = _build_backend_order([backend for backend in backends if backend.enabled and backend.weight > 0])
    if not ordered_backends:
        raise RuntimeError("No enabled AI text backends are configured")

    attempts: list[dict[str, Any]] = []
    remaining_backends = list(ordered_backends)
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            markdown_text = call_text_backend(backend, prompt=normalized_prompt)
            _clear_backend_cooldown(backend)
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "success",
                }
            )
            return markdown_text, f"{backend.type}:{backend.model}", attempts
        except AIBackendError as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": exc.message,
                }
            )
            _register_backend_failure_cooldown(
                backend,
                operation="text",
                error_message=exc.message,
            )
            logger.warning(
                "text_generation_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                error=exc.message,
            )
            remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                remaining_backends,
                failed_backend=backend,
                operation="text",
                error_message=exc.message,
            )

    raise RuntimeError("All configured AI text backends failed")


def generate_role_completion(
    *,
    backends: list[AIBackendNode],
    prompt: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("Role prompt cannot be empty")

    ordered_backends = _build_backend_order([backend for backend in backends if backend.enabled and backend.weight > 0])
    if not ordered_backends:
        raise RuntimeError("No enabled AI role backends are configured")

    attempts: list[dict[str, Any]] = []
    remaining_backends = list(ordered_backends)
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            raw_text = call_text_backend(backend, prompt=normalized_prompt)
            payload = extract_role_json(raw_text)
            _clear_backend_cooldown(backend)
            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "success"})
            return payload, f"{backend.type}:{backend.model}", attempts
        except (AIBackendError, ValueError, json.JSONDecodeError) as exc:
            error_message = exc.message if isinstance(exc, AIBackendError) else str(exc)
            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "failed", "error": error_message})
            if isinstance(exc, AIBackendError):
                _register_backend_failure_cooldown(backend, operation="role-text", error_message=error_message)
                remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                    remaining_backends,
                    failed_backend=backend,
                    operation="role-text",
                    error_message=error_message,
                )
            logger.warning("ai_role_backend_failed", backend_id=backend.id, backend_type=backend.type, error=error_message)

    raise RuntimeError("All configured AI role backends failed")


def generate_role_vision_completion(
    *,
    backends: list[AIBackendNode],
    prompt: str,
    image_base64: str,
    mime_type: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        raise ValueError("Role vision prompt cannot be empty")

    ordered_backends = _build_backend_order([backend for backend in backends if backend.enabled and backend.weight > 0])
    if not ordered_backends:
        raise RuntimeError("No enabled AI role vision backends are configured")

    attempts: list[dict[str, Any]] = []
    remaining_backends = list(ordered_backends)
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            payload = call_role_vision_backend(
                backend,
                prompt=normalized_prompt,
                image_base64=image_base64,
                mime_type=mime_type,
            )
            _clear_backend_cooldown(backend)
            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "success"})
            return payload, f"{backend.type}:{backend.model}", attempts
        except (AIBackendError, ValueError, json.JSONDecodeError) as exc:
            error_message = exc.message if isinstance(exc, AIBackendError) else str(exc)
            attempts.append({"backend_id": backend.id, "backend_type": backend.type, "status": "failed", "error": error_message})
            if isinstance(exc, AIBackendError):
                _register_backend_failure_cooldown(backend, operation="role-vision", error_message=error_message)
                remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                    remaining_backends,
                    failed_backend=backend,
                    operation="role-vision",
                    error_message=error_message,
                )
            logger.warning("ai_role_vision_backend_failed", backend_id=backend.id, backend_type=backend.type, error=error_message)

    raise RuntimeError("All configured AI role vision backends failed")


ROLE_EVIDENCE_FIELD_NAMES = {
    "ai_summary",
    "labels",
    "defects",
    "scene_type",
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
    "gps",
    "location",
}

ROLE_LOW_INFORMATION_OBJECTS = {
    "car",
    "cars",
    "vehicle",
    "vehicles",
    "truck",
    "tree",
    "trees",
    "house",
    "home",
    "rock",
    "rocks",
    "gravel",
    "stone",
    "shed",
    "sheds",
    "table",
    "tables",
    "chair",
    "chairs",
    "tarp",
    "yard",
    "outdoor_scene",
}

ROLE_UNCERTAINTY_MARKERS = {
    "low confidence",
    "insufficient",
    "unclear",
    "blur",
    "blurry",
    "crop",
    "cropped",
    "distance",
    "angle",
    "context",
    "not reliable",
    "not enough",
    "model response",
    "repetitive",
}


def _role_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _role_result_has_concrete_evidence(result: dict[str, Any]) -> bool:
    for item in result.get("evidence") or []:
        text = _role_text(item)
        folded = text.casefold()
        normalized = re.sub(r"[^a-z0-9_]+", "_", folded).strip("_")
        if not text:
            continue
        if normalized in ROLE_EVIDENCE_FIELD_NAMES:
            continue
        if "empty list" in folded or "empty array" in folded or folded in {"none", "n/a", "na", "empty"}:
            continue
        if folded.startswith("gps") or folded.startswith("location"):
            continue
        if any(marker in folded for marker in ROLE_UNCERTAINTY_MARKERS):
            continue
        return True
    return False


def _payload_has_any(payload: dict[str, Any], fields: tuple[str, ...]) -> bool:
    for field_name in fields:
        value = payload.get(field_name)
        if isinstance(value, list) and any(_role_text(item) for item in value):
            return True
        if isinstance(value, str) and _role_text(value):
            return True
        if isinstance(value, dict) and any(_role_text(item) for item in value.values()):
            return True
    return False


def _payload_text_has_any(payload: dict[str, Any], terms: set[str]) -> bool:
    chunks: list[str] = []
    for field_name in (
        "ai_summary",
        "scene_type",
        "labels",
        "visible_objects",
        "materials",
        "equipment",
        "people_ppe",
        "safety_observations",
        "quality_observations",
        "water_or_housekeeping_observations",
        "defects",
    ):
        value = payload.get(field_name)
        if isinstance(value, list):
            chunks.extend(_role_text(item) for item in value)
        elif isinstance(value, str):
            chunks.append(value)
    text = " ".join(chunks).casefold()
    return any(term in text for term in terms)


def _downgrade_polluted_role_result(result: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = dict(result)
    flags = list(updated.get("inspector_flags") or [])
    if reason not in flags:
        flags.append(reason)
    updated["status"] = "not_enough_evidence"
    updated["confidence"] = "low"
    updated["should_retry"] = False
    updated["inspector_flags"] = flags
    missing = list(updated.get("missing_evidence") or [])
    if "A concrete visible fact is required before reporting risk or warning." not in missing:
        missing.append("A concrete visible fact is required before reporting risk or warning.")
    updated["missing_evidence"] = missing[:6]
    return updated


def enforce_role_evidence_gate(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "").casefold()
    if status not in {"risk", "warning"}:
        return result

    role_id = str(result.get("role_id") or "")
    finding_text = _role_text(result.get("finding")).casefold()
    has_concrete_evidence = _role_result_has_concrete_evidence(result)
    payload_confidence = str(payload.get("confidence_level") or "").casefold()
    has_visible_risk_fields = _payload_has_any(
        payload,
        ("defects", "safety_observations", "quality_observations", "water_or_housekeeping_observations"),
    )

    if not has_concrete_evidence:
        return _downgrade_polluted_role_result(result, "role_evidence_not_concrete")

    if payload_confidence == "low" and not has_visible_risk_fields:
        return _downgrade_polluted_role_result(result, "low_confidence_cannot_support_role_risk")

    if role_id == "client_visibility_risk" and not has_visible_risk_fields:
        return _downgrade_polluted_role_result(result, "client_visibility_requires_visible_issue")

    if role_id == "rework_risk" and not _payload_has_any(payload, ("defects", "quality_observations", "recommended_actions")):
        return _downgrade_polluted_role_result(result, "rework_requires_quality_or_defect_evidence")

    if role_id == "installation_completeness" and not _payload_has_any(payload, ("defects", "quality_observations", "equipment")):
        return _downgrade_polluted_role_result(result, "installation_review_requires_visible_system_or_defect")

    if role_id == "stalled_area_signal" and not _payload_text_has_any(payload, {"stalled", "inactive", "no progress", "not started", "delay"}):
        return _downgrade_polluted_role_result(result, "stalled_signal_requires_progress_evidence")

    if role_id == "ppe_compliance" and not _payload_has_any(payload, ("people_ppe",)):
        return _downgrade_polluted_role_result(result, "ppe_requires_visible_people")

    if "empty" in finding_text and any(term in finding_text for term in ("ppe", "safety", "quality", "defect")):
        return _downgrade_polluted_role_result(result, "empty_fields_cannot_support_violation")

    if any(term in finding_text for term in ROLE_LOW_INFORMATION_OBJECTS) and not has_visible_risk_fields:
        return _downgrade_polluted_role_result(result, "ordinary_object_cannot_support_role_risk")

    return result


def enforce_direct_role_evidence_gate(result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "").casefold()
    role_id = str(result.get("role_id") or "")
    combined_text = " ".join(
        [_role_text(result.get("finding"))]
        + [_role_text(item) for item in result.get("evidence") or []]
    ).casefold()
    if status == "pass":
        if str(result.get("confidence") or "").casefold() == "low":
            return _downgrade_polluted_role_result(result, "low_confidence_cannot_support_role_pass")
        if role_id in {"ppe_compliance", "worker_activity", "scene_type", "asset_material_equipment_facts"} and any(
            marker in combined_text
            for marker in (
                "no visible",
                "not visible",
                "not enough",
                "insufficient",
                "cannot determine",
                "unable to determine",
            )
        ):
            return _downgrade_polluted_role_result(result, "absence_of_evidence_is_not_role_pass")
        if role_id == "worker_activity" and any(term in combined_text for term in ("appears to be working", "working on a project")) and not any(
            term in combined_text for term in ("tool", "equipment", "install", "carry", "active task", "using")
        ):
            return _downgrade_polluted_role_result(result, "worker_activity_requires_visible_task")
        return result
    if status not in {"risk", "warning"}:
        return result
    if not _role_result_has_concrete_evidence(result):
        return _downgrade_polluted_role_result(result, "role_evidence_not_concrete")
    if str(result.get("confidence") or "").casefold() == "low":
        return _downgrade_polluted_role_result(result, "low_confidence_cannot_support_role_risk")
    if any(term in combined_text for term in ROLE_LOW_INFORMATION_OBJECTS) and not _contains_risk_language(combined_text):
        return _downgrade_polluted_role_result(result, "ordinary_object_cannot_support_role_risk")
    return result


def run_ai_role_reviews(
    *,
    backends: list[AIBackendNode],
    payload: dict[str, Any],
    photo_context: dict[str, Any],
    priorities: set[str] | None = None,
    role_ids: set[str] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    attempts_by_role: dict[str, Any] = {}
    model_by_role: dict[str, str] = {}
    photo_type = str(photo_context.get("photo_type") or "").casefold()
    for role_prompt in selected_role_prompts(priorities=priorities, role_ids=role_ids):
        if role_prompt.category == "finance" and photo_type != "invoice":
            result = normalize_role_result(
                {
                    "status": "not_applicable",
                    "finding": "Finance receipt review does not apply to this non-invoice field photo.",
                    "evidence": [],
                    "missing_evidence": [],
                    "recommended_action": "",
                    "confidence": "high",
                    "should_retry": False,
                },
                role_prompt,
            )
            attempts_by_role[role_prompt.role_id] = [{"status": "skipped", "reason": "non_invoice_photo"}]
            results.append(result)
            continue
        prompt = build_role_prompt(role_prompt, payload, photo_context=photo_context)
        try:
            raw_result, model_used, attempts = generate_role_completion(backends=backends, prompt=prompt)
            result = normalize_role_result(raw_result, role_prompt)
            result = enforce_role_evidence_gate(result, payload)
            model_by_role[role_prompt.role_id] = model_used
            attempts_by_role[role_prompt.role_id] = attempts
        except Exception as exc:
            result = normalize_role_result(
                {
                    "status": "not_enough_evidence",
                    "finding": f"Role review failed: {exc}",
                    "missing_evidence": ["The role model did not return a usable JSON result."],
                    "confidence": "low",
                    "should_retry": True,
                },
                role_prompt,
            )
            attempts_by_role[role_prompt.role_id] = [{"status": "failed", "error": str(exc)}]
        results.append(result)

    summary = {
        "role_count": len(results),
        "risk_roles": [item["role_id"] for item in results if item["status"] == "risk"],
        "warning_roles": [item["role_id"] for item in results if item["status"] == "warning"],
        "retry_roles": [item["role_id"] for item in results if item.get("should_retry")],
        "not_applicable_roles": [item["role_id"] for item in results if item["status"] == "not_applicable"],
    }
    return {
        "version": "2026-05-04-role-v1",
        "results": results,
        "summary": summary,
        "model_by_role": model_by_role,
        "attempts_by_role": attempts_by_role,
    }


def run_ai_role_vision_reviews(
    *,
    backends: list[AIBackendNode],
    image_base64: str,
    mime_type: str,
    photo_context: dict[str, Any],
    priorities: set[str] | None = None,
    role_ids: set[str] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    attempts_by_role: dict[str, Any] = {}
    model_by_role: dict[str, str] = {}
    photo_type = str(photo_context.get("photo_type") or "").casefold()
    for role_prompt in selected_role_prompts(priorities=priorities, role_ids=role_ids):
        if role_prompt.category == "finance" and photo_type != "invoice":
            result = normalize_role_result(
                {
                    "status": "not_applicable",
                    "finding": "Finance receipt review does not apply to this non-invoice field photo.",
                    "evidence": [],
                    "missing_evidence": [],
                    "recommended_action": "",
                    "confidence": "high",
                    "should_retry": False,
                },
                role_prompt,
            )
            attempts_by_role[role_prompt.role_id] = [{"status": "skipped", "reason": "non_invoice_photo"}]
            results.append(result)
            continue
        prompt = build_role_vision_prompt(role_prompt, photo_context=photo_context)
        try:
            raw_result, model_used, attempts = generate_role_vision_completion(
                backends=backends,
                prompt=prompt,
                image_base64=image_base64,
                mime_type=mime_type,
            )
            result = normalize_role_result(raw_result, role_prompt)
            result = enforce_direct_role_evidence_gate(result)
            model_by_role[role_prompt.role_id] = model_used
            attempts_by_role[role_prompt.role_id] = attempts
        except Exception as exc:
            result = normalize_role_result(
                {
                    "status": "not_enough_evidence",
                    "finding": f"Role vision review failed: {exc}",
                    "missing_evidence": ["The role model did not return a usable JSON result from the image."],
                    "confidence": "low",
                    "should_retry": False,
                },
                role_prompt,
            )
            attempts_by_role[role_prompt.role_id] = [{"status": "failed", "error": str(exc)}]
        results.append(result)

    summary = {
        "role_count": len(results),
        "risk_roles": [item["role_id"] for item in results if item["status"] == "risk"],
        "warning_roles": [item["role_id"] for item in results if item["status"] == "warning"],
        "retry_roles": [item["role_id"] for item in results if item.get("should_retry")],
        "not_applicable_roles": [item["role_id"] for item in results if item["status"] == "not_applicable"],
    }
    return {
        "version": "2026-05-04-role-vision-v1",
        "mode": "direct_image_per_role",
        "results": results,
        "summary": summary,
        "model_by_role": model_by_role,
        "attempts_by_role": attempts_by_role,
    }


_COUNT_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_PPE_TERMS = {
    "hard hat",
    "helmet",
    "safety vest",
    "vest",
    "eye protection",
    "safety glasses",
    "gloves",
    "harness",
    "ppe",
}
_PERSON_TERMS = {"person", "people", "worker", "workers", "employee", "employees", "crew", "man", "men", "woman", "women"}
_WORK_CONTEXT_TERMS = {
    "construction",
    "work site",
    "worksite",
    "jobsite",
    "site",
    "worker",
    "workers",
    "tool",
    "tools",
    "level",
    "shovel",
    "equipment",
    "material",
    "materials",
    "concrete",
    "cement",
    "install",
    "attach",
}


def _role_items(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = [f"{key}: {item}" for key, item in value.items() if str(item or "").strip()]
    else:
        return []
    normalized: list[str] = []
    for item in values:
        text = " ".join(str(item or "").strip().split())
        if text:
            normalized.append(text)
    return normalized


def _role_payload_text(payload: dict[str, Any], fields: tuple[str, ...] | None = None) -> str:
    field_names = fields or (
        "ai_summary",
        "scene_type",
        "labels",
        "visible_objects",
        "materials",
        "equipment",
        "people_ppe",
        "safety_observations",
        "quality_observations",
        "inventory_observations",
        "water_or_housekeeping_observations",
        "defects",
        "evidence_limitations",
    )
    chunks: list[str] = []
    for field_name in field_names:
        chunks.extend(_role_items(payload.get(field_name)))
    return " ".join(chunks)


def _extract_visible_people_count(payload: dict[str, Any]) -> tuple[int | None, bool, str]:
    text = _role_payload_text(payload, ("ai_summary", "people_ppe", "visible_objects", "labels")).casefold()
    if re.search(r"\ba\s+man\s+and\s+a\s+woman\b", text):
        return 2, False, ""
    if re.search(r"\ba\s+(?:visible\s+)?(?:person|worker|employee|man|woman)\b", text):
        return 1, False, ""
    if re.search(r"\bone\s+(?:visible\s+)?(?:person|worker|employee|man|woman)\b", text):
        return 1, False, ""
    for match in re.finditer(r"\b(\d{1,3})\s+(?:visible\s+)?(?:people|persons|workers|employees|crew members|men|women)\b", text):
        return max(0, min(int(match.group(1)), 200)), False, ""
    for match in re.finditer(r"\b(\d{1,3})\s+(?:[a-z0-9_-]+\s+){0,3}(?:people|persons|workers|employees|crew members|men|women)\b", text):
        return max(0, min(int(match.group(1)), 200)), True, ""
    for word, value in _COUNT_WORDS.items():
        if re.search(rf"\b{word}\s+(?:visible\s+)?(?:people|persons|workers|employees|crew members|men|women)\b", text):
            return value, False, ""
        if re.search(rf"\b{word}\s+(?:[a-z0-9_-]+\s+){{0,3}}(?:people|persons|workers|employees|crew members|men|women)\b", text):
            return value, True, ""
    if any(term in text for term in _PERSON_TERMS):
        return None, True, "People are visible, but the extracted facts do not provide a reliable count."
    return 0, False, ""


def _role_result(
    role_prompt: Any,
    *,
    status: str,
    finding: str,
    evidence: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    recommended_action: str = "",
    confidence: str = "medium",
    should_retry: bool = False,
    numeric_value: int | None = None,
    count_range: str = "",
    is_estimate: bool = False,
) -> dict[str, Any]:
    return normalize_role_result(
        {
            "status": status,
            "finding": finding,
            "evidence": evidence or [],
            "missing_evidence": missing_evidence or [],
            "recommended_action": recommended_action,
            "confidence": confidence,
            "should_retry": should_retry,
            "numeric_value": numeric_value,
            "count_range": count_range,
            "is_estimate": is_estimate,
        },
        role_prompt,
    )


def _rule_result_for_role(role_prompt: Any, payload: dict[str, Any], photo_context: dict[str, Any]) -> dict[str, Any]:
    role_id = str(role_prompt.role_id)
    photo_type = str(photo_context.get("photo_type") or "").casefold()
    if role_prompt.category == "finance" and photo_type != "invoice":
        return _role_result(
            role_prompt,
            status="not_applicable",
            finding="Finance review does not apply to this non-invoice field photo.",
            confidence="high",
        )

    summary = " ".join(str(payload.get("ai_summary") or "").split())
    confidence_level = str(payload.get("confidence_level") or "").casefold()
    scene_type = " ".join(str(payload.get("scene_type") or "").split())
    visible_objects = _role_items(payload.get("visible_objects"))
    materials = _role_items(payload.get("materials"))
    equipment = _role_items(payload.get("equipment"))
    people_ppe = _role_items(payload.get("people_ppe"))
    safety = _role_items(payload.get("safety_observations"))
    quality = _role_items(payload.get("quality_observations"))
    housekeeping = _role_items(payload.get("water_or_housekeeping_observations"))
    defects = _role_items(payload.get("defects"))
    limitations = _role_items(payload.get("evidence_limitations"))
    actions = _role_items(payload.get("recommended_actions"))
    facts_text = _role_payload_text(payload).casefold()

    if role_id == "visible_people_count":
        count, estimate, missing = _extract_visible_people_count(payload)
        if count is None:
            return _role_result(
                role_prompt,
                status="not_enough_evidence",
                finding=missing,
                missing_evidence=[missing],
                confidence="low",
                is_estimate=estimate,
            )
        return _role_result(
            role_prompt,
            status="pass",
            finding=f"Visible people count is {count}.",
            evidence=[f"Extracted field facts support {count} visible people."],
            confidence="medium" if estimate else "high",
            numeric_value=count,
            is_estimate=estimate,
        )

    if role_id == "ppe_compliance":
        people_count, people_estimate, _ = _extract_visible_people_count(payload)
        ppe_text = " ".join(people_ppe).casefold()
        has_people = (people_count is not None and people_count > 0) or any(term in facts_text for term in _PERSON_TERMS)
        has_work_context = any(term in facts_text for term in _WORK_CONTEXT_TERMS)
        has_ppe = any(term in ppe_text for term in _PPE_TERMS)
        if not has_people:
            return _role_result(
                role_prompt,
                status="not_enough_evidence",
                finding="No visible people evidence is available for PPE review.",
                missing_evidence=["People must be visible before PPE can be reviewed."],
                confidence="low",
            )
        if not has_work_context:
            return _role_result(
                role_prompt,
                status="not_enough_evidence",
                finding="People are visible, but worksite PPE context was not extracted.",
                missing_evidence=["Visible work activity or jobsite context is needed before PPE review."],
                confidence="low",
            )
        if has_ppe:
            return _role_result(
                role_prompt,
                status="pass",
                finding="Visible PPE evidence was extracted for people in the photo.",
                evidence=people_ppe[:3],
                confidence="medium" if people_estimate else "high",
            )
        return _role_result(
            role_prompt,
            status="warning",
            finding="People are visible, but PPE details were not extracted.",
            evidence=[summary] if summary else [],
            missing_evidence=["A clear view of hard hats, vests, eye protection, gloves, or harnesses is needed."],
            recommended_action="Ask for a clearer worker/PPE photo if PPE compliance matters.",
            confidence="medium",
        )

    if role_id == "time_gps_reasonableness":
        has_gps = bool(photo_context.get("gps") or (photo_context.get("gps_lat") and photo_context.get("gps_lon")))
        has_time = bool(photo_context.get("captured_at_utc"))
        if has_gps and has_time:
            return _role_result(
                role_prompt,
                status="pass",
                finding="Capture time and GPS metadata are present for review.",
                evidence=["GPS metadata is present.", "Capture timestamp is present."],
                confidence="high",
            )
        missing = []
        if not has_gps:
            missing.append("GPS metadata is missing.")
        if not has_time:
            missing.append("Capture timestamp is missing.")
        return _role_result(
            role_prompt,
            status="warning",
            finding="Time or GPS metadata is incomplete for evidence-chain review.",
            missing_evidence=missing,
            recommended_action="Verify the upload device is sending GPS and capture time.",
            confidence="medium",
        )

    if role_id == "photo_validity":
        has_field_facts = bool(summary or scene_type or visible_objects or materials or equipment or people_ppe)
        if has_field_facts and confidence_level != "low":
            return _role_result(role_prompt, status="pass", finding="Photo has usable extracted field evidence.", evidence=([summary] if summary else visible_objects[:2]), confidence="high")
        return _role_result(
            role_prompt,
            status="not_enough_evidence",
            finding="Extracted facts are too limited for reliable field audit value.",
            missing_evidence=limitations[:3] or ["A clearer photo with identifiable work evidence is needed."],
            confidence="low",
        )

    if role_id in {"scene_type", "site_area"}:
        if scene_type:
            return _role_result(role_prompt, status="pass", finding=f"Scene classified as {scene_type}.", evidence=[scene_type], confidence="medium" if confidence_level == "low" else "high")
        return _role_result(role_prompt, status="not_enough_evidence", finding="Scene type was not extracted clearly.", missing_evidence=["Wider context is needed to classify the scene."], confidence="low")

    if role_id in {"asset_material_equipment_facts", "equipment_status", "material_delivery", "material_storage", "tool_management"}:
        evidence = (materials + equipment + visible_objects)[:3]
        if evidence:
            return _role_result(role_prompt, status="pass", finding="Visible asset, material, or equipment facts were extracted.", evidence=evidence, confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No concrete asset, material, or equipment facts were extracted.", missing_evidence=["Clear objects, materials, tools, or equipment are needed."], confidence="low")

    if role_id in {"safety_overall", "fall_hazard", "struck_by_hazard", "caught_between_hazard", "electrical_hazard", "emergency_access", "water_slip_hazard", "housekeeping", "access_logistics"}:
        evidence = (safety + housekeeping + defects)[:3]
        if evidence:
            status = "risk" if any(_contains_risk_language(item.casefold()) for item in evidence) else "warning"
            return _role_result(role_prompt, status=status, finding="Safety or site-control evidence requires manager review.", evidence=evidence, recommended_action=actions[0] if actions else "Review the visible condition before approval.", confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No specific safety or site-control issue was extracted.", missing_evidence=["A visible hazard or access condition is needed for this role."], confidence="low")

    if role_id in {"visible_quality_defect", "rework_risk", "finished_work_protection", "installation_completeness", "quality_positive", "defect_severity", "defect_location_usability", "inspection_readiness", "issue_closure_evidence", "client_visibility_risk"}:
        evidence = (quality + defects)[:3]
        if evidence:
            status = "risk" if any(_contains_risk_language(item.casefold()) for item in evidence) else "warning"
            return _role_result(role_prompt, status=status, finding="Visible quality or defect evidence requires review.", evidence=evidence, recommended_action=actions[0] if actions else "", confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No visible quality defect evidence was extracted.", missing_evidence=["A clear defect, completed work, or repair condition is needed."], confidence="low")

    if role_id in {"progress_state", "daily_work_inference", "work_phase", "worker_activity"}:
        evidence = ([summary] if summary else []) + visible_objects[:2] + materials[:1] + equipment[:1]
        if evidence:
            return _role_result(role_prompt, status="pass", finding="Extracted facts support basic work progress review.", evidence=evidence[:3], confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No reliable progress or worker activity evidence was extracted.", missing_evidence=["Visible work, workers, materials, or equipment are needed."], confidence="low")

    if role_id == "missing_photo_evidence":
        if confidence_level == "low" or limitations:
            return _role_result(
                role_prompt,
                status="warning",
                finding="Supplemental photo evidence would improve review confidence.",
                evidence=limitations[:3],
                missing_evidence=limitations[:3] or ["Wider context or a clearer close-up is needed."],
                recommended_action="Request a wider context photo and a close-up of the relevant work.",
                confidence="medium",
            )
        return _role_result(role_prompt, status="not_enough_evidence", finding="No specific supplemental photo gap was extracted.", confidence="low", missing_evidence=["No low-confidence limitation was found."])

    if role_id == "evidence_limitations":
        if limitations:
            return _role_result(role_prompt, status="pass", finding="Evidence limitations were extracted for this photo.", evidence=limitations[:3], confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No concrete evidence limitation was extracted.", missing_evidence=["No blur, crop, distance, or context limitation was reported."], confidence="low")

    if role_id == "duplicate_low_value":
        low_value = any(term in facts_text for term in ("unclear", "surface texture", "no auditable", "ordinary", "duplicate"))
        if low_value:
            return _role_result(role_prompt, status="warning", finding="Photo may have limited audit value based on extracted facts.", evidence=[summary] if summary else limitations[:2], confidence="medium")
        return _role_result(role_prompt, status="not_enough_evidence", finding="No duplicate or low-value signal was extracted.", confidence="low")

    if role_id == "chief_ai_review":
        flags: list[str] = []
        if "risk" in str(summary).casefold() and not defects and not safety and not quality:
            flags.append("Summary uses risk language without supporting extracted issue fields.")
        if confidence_level == "low" and not limitations:
            flags.append("Low confidence result lacks concrete limitations.")
        if flags:
            return _role_result(role_prompt, status="warning", finding="AI output has evidence consistency issues.", evidence=flags[:3], recommended_action="Review AI facts before using the summary.", confidence="medium")
        return _role_result(role_prompt, status="pass", finding="AI output passed basic evidence consistency checks.", evidence=["No rule-based pollution flag was found."], confidence="medium")

    return _role_result(
        role_prompt,
        status="not_enough_evidence",
        finding=f"No rule-based evidence was available for {role_prompt.title}.",
        missing_evidence=["This role needs more specific extracted facts or a later text review."],
        confidence="low",
    )


def run_rule_first_role_reviews(
    *,
    payload: dict[str, Any],
    photo_context: dict[str, Any],
    priorities: set[str] | None = None,
    role_ids: set[str] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    attempts_by_role: dict[str, Any] = {}
    model_by_role: dict[str, str] = {}
    for role_prompt in selected_role_prompts(priorities=priorities, role_ids=role_ids):
        result = _rule_result_for_role(role_prompt, payload, photo_context)
        attempts_by_role[role_prompt.role_id] = [{"status": "rule_evaluated"}]
        model_by_role[role_prompt.role_id] = "rule:first-pass"
        results.append(result)
    summary = {
        "role_count": len(results),
        "risk_roles": [item["role_id"] for item in results if item["status"] == "risk"],
        "warning_roles": [item["role_id"] for item in results if item["status"] == "warning"],
        "retry_roles": [item["role_id"] for item in results if item.get("should_retry")],
        "not_applicable_roles": [item["role_id"] for item in results if item["status"] == "not_applicable"],
    }
    return {
        "version": "2026-05-04-rule-first-v1",
        "mode": "rule_first_from_vision_facts",
        "results": results,
        "summary": summary,
        "model_by_role": model_by_role,
        "attempts_by_role": attempts_by_role,
    }


MANAGER_EVIDENCE_SUMMARY_VERSION = "2026-05-06-manager-evidence-people-count-v1"


_LOW_VALUE_SUMMARY_MARKERS = (
    "role-based ai review",
    "visible people count:",
    "no role-confirmed",
    "the image shows",
    "the photo shows",
    "this appears to show",
)


def _manager_clean_text(value: Any, *, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text[:limit].rstrip()


def _manager_items(value: Any, *, limit: int = 4) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif value:
        items = [value]
    else:
        items = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _manager_clean_text(item, limit=140)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _role_review_results(role_reviews: dict[str, Any]) -> list[dict[str, Any]]:
    results = role_reviews.get("results") if isinstance(role_reviews, dict) else None
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _role_result_by_id(role_reviews: dict[str, Any], role_id: str) -> dict[str, Any] | None:
    for item in _role_review_results(role_reviews):
        if item.get("role_id") == role_id:
            return item
    return None


def _merge_role_review_override(role_reviews: dict[str, Any], override_reviews: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(override_reviews, dict):
        return role_reviews
    override_results = {
        str(item.get("role_id") or ""): item
        for item in _role_review_results(override_reviews)
        if str(item.get("role_id") or "")
    }
    if not override_results:
        return role_reviews

    merged_reviews = dict(role_reviews or {})
    merged_results: list[dict[str, Any]] = []
    replaced: set[str] = set()
    for item in _role_review_results(role_reviews):
        role_id = str(item.get("role_id") or "")
        replacement = override_results.get(role_id)
        if replacement is not None:
            merged_results.append(replacement)
            replaced.add(role_id)
        else:
            merged_results.append(item)
    for role_id, item in override_results.items():
        if role_id not in replaced:
            merged_results.append(item)

    merged_reviews["results"] = merged_results
    merged_reviews["summary"] = {
        "role_count": len(merged_results),
        "risk_roles": [item["role_id"] for item in merged_results if item.get("status") == "risk"],
        "warning_roles": [item["role_id"] for item in merged_results if item.get("status") == "warning"],
        "retry_roles": [item["role_id"] for item in merged_results if item.get("should_retry")],
        "not_applicable_roles": [item["role_id"] for item in merged_results if item.get("status") == "not_applicable"],
    }
    model_by_role = dict(role_reviews.get("model_by_role") or {}) if isinstance(role_reviews, dict) else {}
    model_by_role.update(override_reviews.get("model_by_role") or {})
    attempts_by_role = dict(role_reviews.get("attempts_by_role") or {}) if isinstance(role_reviews, dict) else {}
    attempts_by_role.update(override_reviews.get("attempts_by_role") or {})
    merged_reviews["model_by_role"] = model_by_role
    merged_reviews["attempts_by_role"] = attempts_by_role
    merged_reviews["direct_vision_overrides"] = sorted(override_results)
    return merged_reviews


def _manager_evidence_from_role(item: dict[str, Any] | None, *, limit: int = 2) -> list[str]:
    if not isinstance(item, dict):
        return []
    return (
        _manager_items(item.get("evidence"), limit=limit)
        or _manager_items(item.get("finding"), limit=limit)
        or _manager_items(item.get("missing_evidence"), limit=limit)
    )


def _is_useful_evidence_gap(text: str) -> bool:
    normalized = _manager_clean_text(text, limit=220).casefold()
    if not normalized:
        return False
    low_value_starts = (
        "no ",
        "no specific ",
        "no concrete ",
        "no reliable ",
        "no visible ",
        "no blur",
        "no low-confidence",
    )
    low_value_phrases = (
        "was not found",
        "were not found",
        "was extracted",
        "was reported",
        "is needed",
        "is needed for this role",
    )
    if normalized.startswith(low_value_starts):
        return False
    if any(phrase in normalized for phrase in low_value_phrases):
        return False
    return True


def _manager_people_count(role_reviews: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    count_role = _role_result_by_id(role_reviews, "visible_people_count")
    if isinstance(count_role, dict) and isinstance(count_role.get("numeric_value"), int):
        count = int(count_role["numeric_value"])
        confidence = _manager_clean_text(count_role.get("confidence"), limit=20) or "medium"
        count_range = _manager_clean_text(count_role.get("count_range"), limit=40)
        value = count_range if count_range else str(count)
        if count_role.get("is_estimate") and not count_range:
            value = f"about {count}"
        return {
            "value": value,
            "numeric_value": count,
            "is_estimate": bool(count_role.get("is_estimate")),
            "confidence": confidence,
            "reason": _manager_clean_text(count_role.get("finding"), limit=180),
            "source": "visible_people_count_role",
        }
    people_items = _manager_items(payload.get("people_ppe"), limit=3)
    if people_items:
        return {
            "value": "people visible, count not reliable",
            "numeric_value": None,
            "is_estimate": True,
            "confidence": "low",
            "reason": "; ".join(people_items),
            "source": "people_ppe_facts",
        }
    return {
        "value": "unknown",
        "numeric_value": None,
        "is_estimate": False,
        "confidence": "low",
        "reason": "No reliable visible people count was extracted.",
        "source": "not_enough_evidence",
    }


def _manager_field_activity(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("base_ai_summary"),
        payload.get("ai_summary"),
        payload.get("scene_type"),
    ]
    for candidate in candidates:
        text = _manager_clean_text(candidate, limit=220)
        if text and not any(marker in text.casefold() for marker in _LOW_VALUE_SUMMARY_MARKERS):
            return text
    facts = (
        _manager_items(payload.get("visible_objects"), limit=2)
        + _manager_items(payload.get("materials"), limit=2)
        + _manager_items(payload.get("equipment"), limit=2)
    )
    if facts:
        return "Visible field evidence includes " + ", ".join(facts[:4]) + "."
    return "No reliable field activity could be confirmed from this photo."


def _manager_work_evidence(payload: dict[str, Any], role_reviews: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for role_id in ("progress_state", "daily_work_inference", "worker_activity", "asset_material_equipment_facts"):
        evidence.extend(_manager_evidence_from_role(_role_result_by_id(role_reviews, role_id), limit=2))
    if not evidence:
        evidence.extend(_manager_items(payload.get("materials"), limit=2))
        evidence.extend(_manager_items(payload.get("equipment"), limit=2))
        evidence.extend(_manager_items(payload.get("visible_objects"), limit=2))
    return _manager_items(evidence, limit=5)


def _manager_issue_evidence(payload: dict[str, Any], role_reviews: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for item in _role_review_results(role_reviews):
        if item.get("status") in {"risk", "warning"} and item.get("role_id") not in {"ppe_compliance"}:
            issues.extend(_manager_evidence_from_role(item, limit=2))
    issues.extend(_manager_items(payload.get("defects"), limit=3))
    issues.extend(_manager_items(payload.get("safety_observations"), limit=3))
    issues.extend(_manager_items(payload.get("quality_observations"), limit=3))
    issues.extend(_manager_items(payload.get("water_or_housekeeping_observations"), limit=3))
    return _manager_items(issues, limit=5)


def _manager_evidence_gaps(payload: dict[str, Any], role_reviews: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    for role_id in ("missing_photo_evidence", "photo_validity", "inspection_readiness", "evidence_limitations"):
        role = _role_result_by_id(role_reviews, role_id)
        if isinstance(role, dict):
            gaps.extend(_manager_items(role.get("missing_evidence"), limit=2))
            if role.get("status") == "not_enough_evidence":
                gaps.extend(_manager_items(role.get("finding"), limit=1))
    gaps.extend(_manager_items(payload.get("evidence_limitations"), limit=3))
    gaps = [gap for gap in gaps if _is_useful_evidence_gap(gap)]
    if not gaps:
        people_count = _manager_people_count(role_reviews, payload)
        if people_count.get("numeric_value") is not None:
            gaps.append("This photo cannot prove full attendance or PPE compliance without clearer worker views.")
        gaps.append("This photo does not prove completed quantity, connection quality, or final acceptance by itself.")
    return _manager_items(gaps, limit=5)


def _manager_followup_actions(payload: dict[str, Any], role_reviews: dict[str, Any]) -> list[str]:
    actions = _manager_items(payload.get("recommended_actions"), limit=3)
    issue_evidence = _manager_issue_evidence(payload, role_reviews)
    facts_text = " ".join(
        _manager_items(payload.get("visible_objects"), limit=8)
        + _manager_items(payload.get("materials"), limit=8)
        + _manager_items(payload.get("equipment"), limit=8)
        + [_manager_field_activity(payload)]
    ).casefold()
    if issue_evidence and not actions:
        actions.append("Review the flagged visible condition before approving the photo.")
    if any(term in facts_text for term in ("frame", "metal", "steel", "connection", "foundation", "post", "beam")):
        actions.append("Request close-up photos of fastening points, alignment, and installed connection details.")
    if any(term in facts_text for term in ("worker", "person", "people", "crew")):
        actions.append("Request a clearer worker view if attendance or PPE compliance must be verified.")
    actions.append("Request one wide context photo plus one close-up if this photo will support a daily report.")
    return _manager_items(actions, limit=4)


def build_manager_evidence_summary(
    payload: dict[str, Any],
    role_reviews: dict[str, Any],
    *,
    photo_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    people_count = _manager_people_count(role_reviews, payload)
    work_evidence = _manager_work_evidence(payload, role_reviews)
    issue_evidence = _manager_issue_evidence(payload, role_reviews)
    evidence_gaps = _manager_evidence_gaps(payload, role_reviews)
    confidence = _manager_clean_text(payload.get("confidence_level"), limit=20) or people_count.get("confidence") or "medium"
    manager_value = "medium"
    if issue_evidence:
        manager_value = "high"
    elif confidence == "low" or not work_evidence:
        manager_value = "low"
    context = photo_context or {}
    return {
        "version": MANAGER_EVIDENCE_SUMMARY_VERSION,
        "manager_value": manager_value,
        "field_activity": _manager_field_activity(payload),
        "visible_people": people_count,
        "work_progress_evidence": work_evidence,
        "issue_evidence": issue_evidence,
        "what_this_photo_proves": work_evidence[:3] or [_manager_field_activity(payload)],
        "what_it_does_not_prove": evidence_gaps,
        "followup_photo_requests": _manager_followup_actions(payload, role_reviews),
        "manager_next_actions": _manager_followup_actions(payload, role_reviews),
        "confidence": confidence,
        "photo_context": {
            "photo_id": context.get("photo_id"),
            "photo_type": context.get("photo_type"),
            "project_id": context.get("project_id"),
            "employee_id": context.get("employee_id"),
            "captured_at_utc": context.get("captured_at_utc"),
        },
    }


def build_project_manager_ai_summary(manager_evidence: dict[str, Any], *, fallback_summary: str | None = None) -> str:
    if not isinstance(manager_evidence, dict):
        return _manager_clean_text(fallback_summary, limit=420)
    activity = _manager_clean_text(manager_evidence.get("field_activity"), limit=180)
    people = manager_evidence.get("visible_people") if isinstance(manager_evidence.get("visible_people"), dict) else {}
    people_value = _manager_clean_text(people.get("value") if isinstance(people, dict) else "", limit=60)
    people_confidence = _manager_clean_text(people.get("confidence") if isinstance(people, dict) else "", limit=30)
    proves = _manager_items(manager_evidence.get("what_this_photo_proves"), limit=2)
    gaps = _manager_items(manager_evidence.get("what_it_does_not_prove"), limit=2)
    actions = _manager_items(manager_evidence.get("manager_next_actions"), limit=2)
    parts: list[str] = []
    if activity:
        parts.append(activity.rstrip(".") + ".")
    if people_value and people_value != "unknown":
        suffix = f" ({people_confidence} confidence)" if people_confidence else ""
        parts.append(f"Visible crew estimate: {people_value}{suffix}.")
    if proves:
        parts.append("Evidence value: " + "; ".join(proves) + ".")
    if gaps:
        parts.append("Not proven: " + "; ".join(gaps) + ".")
    if actions:
        parts.append("Next: " + "; ".join(actions) + ".")
    summary = " ".join(parts).strip()
    return (summary or _manager_clean_text(fallback_summary, limit=420))[:520].rstrip()


def run_people_count_vision_review(
    *,
    backends: list[AIBackendNode],
    image_base64: str,
    mime_type: str,
    photo_context: dict[str, Any],
) -> dict[str, Any]:
    return run_ai_role_vision_reviews(
        backends=backends,
        image_base64=image_base64,
        mime_type=mime_type,
        photo_context=photo_context,
        role_ids={"visible_people_count"},
    )


def attach_manager_evidence_review(
    normalized_result: dict[str, Any],
    *,
    photo_context: dict[str, Any],
    direct_role_reviews: dict[str, Any] | None = None,
) -> None:
    normalized_result["base_ai_summary"] = normalized_result.get("ai_summary")
    rule_reviews = run_rule_first_role_reviews(
        payload=normalized_result,
        photo_context=photo_context,
        priorities={PRIORITY_P0, PRIORITY_P1, PRIORITY_P2},
    )
    normalized_result["ai_direct_role_reviews"] = direct_role_reviews or {}
    normalized_result["ai_role_reviews"] = _merge_role_review_override(rule_reviews, direct_role_reviews)
    manager_evidence = build_manager_evidence_summary(
        normalized_result,
        normalized_result["ai_role_reviews"],
        photo_context=photo_context,
    )
    normalized_result["manager_evidence_summary"] = manager_evidence
    normalized_result["manager_evidence_version"] = MANAGER_EVIDENCE_SUMMARY_VERSION
    normalized_result["ai_summary"] = build_project_manager_ai_summary(
        manager_evidence,
        fallback_summary=normalized_result.get("base_ai_summary"),
    )
    normalized_result["ai_summary_translations"] = {}


def build_manager_summary_from_role_reviews(role_reviews: dict[str, Any], *, fallback_summary: str | None = None) -> str:
    results = role_reviews.get("results") if isinstance(role_reviews, dict) else None
    if not isinstance(results, list):
        return str(fallback_summary or "").strip()
    risks = [item for item in results if isinstance(item, dict) and item.get("status") == "risk"]
    warnings = [item for item in results if isinstance(item, dict) and item.get("status") == "warning"]
    people_count = next(
        (
            item
            for item in results
            if isinstance(item, dict)
            and item.get("role_id") == "visible_people_count"
            and isinstance(item.get("numeric_value"), int)
        ),
        None,
    )
    workforce_prefix = ""
    if people_count is not None:
        estimate_note = " estimated" if people_count.get("is_estimate") else ""
        range_text = str(people_count.get("count_range") or "").strip()
        range_note = f" ({range_text})" if range_text else ""
        workforce_prefix = f"Visible people count: {people_count.get('numeric_value')}{estimate_note}{range_note}. "
    useful_limits = [
        item
        for item in results
        if isinstance(item, dict)
        and item.get("status") == "not_enough_evidence"
        and item.get("role_id") in {"photo_validity", "scene_type", "missing_photo_evidence", "inspection_readiness"}
    ]
    if risks or warnings:
        issue_items = risks[:3] + warnings[:3]
        issue_text = "; ".join(
            f"{item.get('role_id')}: {str(item.get('finding') or '').strip()}"
            for item in issue_items
            if str(item.get("finding") or "").strip()
        )
        return f"{workforce_prefix}Role-based AI review found {len(risks)} risk(s) and {len(warnings)} warning(s). {issue_text}"[:420]
    if useful_limits:
        finding = str(useful_limits[0].get("finding") or "").strip()
        if finding:
            return f"{workforce_prefix}No role-confirmed risk or warning. Evidence value is limited: {finding}"[:420]
    return f"{workforce_prefix}{str(fallback_summary or 'No role-confirmed risk or warning was found in this photo.').strip()}"[:420]


def generate_text_embedding(
    *,
    backends: list[AIBackendNode],
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> tuple[list[float], str, list[dict[str, Any]]]:
    normalized_text = text.strip()
    if not normalized_text:
        raise ValueError("Embedding text cannot be empty")

    ordered_backends = _build_backend_order([backend for backend in backends if backend.enabled and backend.weight > 0])
    if not ordered_backends:
        raise RuntimeError("No enabled AI embedding backends are configured")

    attempts: list[dict[str, Any]] = []
    remaining_backends = list(ordered_backends)
    while remaining_backends:
        backend = remaining_backends.pop(0)
        try:
            embedding, embedding_ref = call_embedding_backend(backend, text=normalized_text, task_type=task_type)
            _clear_backend_cooldown(backend)
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "success",
                    "embedding_ref": embedding_ref,
                }
            )
            return embedding, embedding_ref, attempts
        except AIBackendError as exc:
            attempts.append(
                {
                    "backend_id": backend.id,
                    "backend_type": backend.type,
                    "status": "failed",
                    "error": exc.message,
                }
            )
            _register_backend_failure_cooldown(
                backend,
                operation=f"embedding:{task_type.lower()}",
                error_message=exc.message,
            )
            logger.warning(
                "photo_embedding_backend_failed",
                backend_id=backend.id,
                backend_type=backend.type,
                task_type=task_type,
                error=exc.message,
            )
            remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                remaining_backends,
                failed_backend=backend,
                operation=f"embedding:{task_type.lower()}",
                error_message=exc.message,
            )
    raise RuntimeError("All configured AI embedding backends failed")


def refresh_photo_embedding(
    db: Session,
    *,
    app_settings: Settings,
    photo: Photo,
    ai_result: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    source: str = "upload",
    priority: str = "normal",
    backends: list[AIBackendNode] | None = None,
    config_source: str | None = None,
) -> tuple[bool, str | None]:
    resolved_backends = backends
    resolved_source = config_source
    if resolved_backends is None or resolved_source is None:
        resolved_backends, resolved_source = resolve_ai_backends(db, app_settings, photo)
    if not resolved_backends:
        raise RuntimeError("No enabled AI backends are configured")

    embedding_text = _build_photo_embedding_text(photo, ai_result=ai_result)
    if len(embedding_text) > app_settings.ai_embedding_max_text_chars:
        logger.warning(
            "photo_embedding_text_truncated",
            photo_id=photo.id,
            company_id=photo.company_id,
            project_id=photo.project_id,
            original_chars=len(embedding_text),
            max_chars=app_settings.ai_embedding_max_text_chars,
        )
        embedding_text = embedding_text[: app_settings.ai_embedding_max_text_chars]
    embedding_values, embedding_ref, embedding_attempts = generate_text_embedding(
        backends=resolved_backends,
        text=embedding_text,
        task_type="RETRIEVAL_DOCUMENT",
    )
    photo.embedding = embedding_values
    photo.embedding_ref = embedding_ref
    db.add(photo)
    log_audit(
        db,
        action="photo_embedding_completed",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user_id,
        tenant_id=photo.tenant_id or photo.company_id,
        project_id=photo.project_id,
        company_id=photo.company_id,
        detail_json={
            "config_source": resolved_source,
            "embedding_ref": embedding_ref,
            "source": source,
            "priority": priority,
            "attempts": embedding_attempts,
        },
    )
    return True, embedding_ref


def semantic_search_photos(
    db: Session,
    app_settings: Settings,
    *,
    tenant_slug: str | None,
    query: str,
    candidate_photo_ids: list[int],
    limit: int = 20,
) -> list[tuple[Photo, float]]:
    normalized_query = query.strip()
    if not normalized_query or not candidate_photo_ids:
        return []

    sync_ai_runtime_settings(db, app_settings)
    backends, _ = resolve_ai_backends_for_tenant(db, app_settings, tenant_slug)
    if not backends:
        raise RuntimeError("No enabled AI backends are configured")

    candidate_ids = list(dict.fromkeys(candidate_photo_ids))
    cache_key = {
        "tenant_slug": tenant_slug or "",
        "query": normalized_query.casefold(),
        "candidate_ids": candidate_ids,
        "limit": limit,
    }
    cached = SEMANTIC_SEARCH_CACHE.get(cache_key)
    if cached is not None:
        photos_by_id = {
            photo.id: photo
            for photo in db.scalars(select(Photo).where(Photo.id.in_([item["photo_id"] for item in cached])))
        }
        ranked_cached: list[tuple[Photo, float]] = []
        for item in cached:
            photo = photos_by_id.get(int(item["photo_id"]))
            if photo is not None:
                ranked_cached.append((photo, float(item["similarity"])))
        if ranked_cached:
            logger.info(
                "semantic_search_cache_hit",
                tenant_slug=tenant_slug or "",
                candidate_count=len(candidate_ids),
                result_count=len(ranked_cached),
            )
            return ranked_cached

    query_embedding, _, _ = generate_text_embedding(
        backends=backends,
        text=normalized_query,
        task_type="RETRIEVAL_QUERY",
    )

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        similarity = (1 - Photo.embedding.cosine_distance(query_embedding)).label("similarity")
        rows = db.execute(
            select(Photo, similarity)
            .where(Photo.id.in_(candidate_ids), Photo.embedding.is_not(None))
            .order_by(similarity.desc(), Photo.captured_at_utc.desc())
            .limit(limit)
        ).all()
        results = [(row[0], float(row[1])) for row in rows]
        SEMANTIC_SEARCH_CACHE.set(
            cache_key,
            [{"photo_id": photo.id, "similarity": similarity_value} for photo, similarity_value in results],
            ttl_seconds=app_settings.semantic_search_cache_ttl_seconds,
        )
        return results

    photos = list(
        db.scalars(
            select(Photo).where(Photo.id.in_(candidate_ids), Photo.embedding.is_not(None))
        )
    )
    scored_results: list[tuple[Photo, float]] = []
    for photo in photos:
        if not isinstance(photo.embedding, list):
            continue
        similarity = _cosine_similarity(query_embedding, [float(value) for value in photo.embedding])
        scored_results.append((photo, similarity))
    scored_results.sort(key=lambda item: (item[1], item[0].captured_at_utc), reverse=True)
    limited_results = scored_results[:limit]
    SEMANTIC_SEARCH_CACHE.set(
        cache_key,
        [{"photo_id": photo.id, "similarity": similarity_value} for photo, similarity_value in limited_results],
        ttl_seconds=app_settings.semantic_search_cache_ttl_seconds,
    )
    return limited_results


def test_ai_backend_connection(node: AIBackendNode, custom_prompt: str | None = None) -> dict[str, Any]:
    prompt = "\n".join(
        [
            "You are a connection test for a construction photo AI backend.",
            "The attached image is a small sample image used only to verify that the backend can accept image bytes and return JSON.",
            "Return only valid JSON with ai_summary, labels, defects, visible_objects, recommended_actions, confidence_level, and evidence_limitations.",
            f"- operator_prompt: {normalize_operator_prompt(custom_prompt) or ''}",
        ]
    )
    return call_ai_backend(
        node,
        prompt=prompt,
        image_base64=SAMPLE_TEST_IMAGE_BASE64,
        mime_type=SAMPLE_TEST_IMAGE_MIME_TYPE,
    )


def _build_failure_tag_json(message: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ai_summary": None,
        "labels": [],
        "defects": [],
        "error": {
            "code": "ai_processing_failed",
            "message": message,
            "attempts": attempts,
        },
    }


def _mark_photo_failed(
    db: Session,
    photo: Photo,
    *,
    message: str,
    attempts: list[dict[str, Any]],
    config_source: str | None = None,
    actor_user_id: int | None = None,
    source: str | None = None,
    priority: str | None = None,
    custom_prompt_supplied: bool | None = None,
) -> None:
    photo.labeling_status = "failed"
    existing_tag_json = photo.tag_json if isinstance(photo.tag_json, dict) else None
    if existing_tag_json and existing_tag_json.get("ai_summary"):
        # A failed reprocess must not destroy the last good analysis; keep the
        # existing snapshot and record the failure alongside it.
        preserved = dict(existing_tag_json)
        preserved["last_failed_error"] = {
            "code": "ai_processing_failed",
            "message": message,
            "attempts": attempts,
        }
        photo.tag_json = preserved
    else:
        photo.tag_json = _build_failure_tag_json(message, attempts)
    db.add(photo)

    detail_json: dict[str, Any] = {
        "labeling_status": photo.labeling_status,
        "error": message,
    }
    if config_source is not None:
        detail_json["config_source"] = config_source
    if source is not None:
        detail_json["source"] = source
    if priority is not None:
        detail_json["priority"] = priority
    if custom_prompt_supplied is not None:
        detail_json["custom_prompt_supplied"] = custom_prompt_supplied
    if attempts:
        detail_json["attempts"] = attempts

    log_audit(
        db,
        action="photo_labeling_failed",
        target_type="photo",
        target_id=str(photo.id),
        actor_user_id=actor_user_id,
        tenant_id=photo.tenant_id or photo.company_id,
        project_id=photo.project_id,
        company_id=photo.company_id,
        detail_json=detail_json,
    )
    db.commit()


def queue_photo_reprocess(
    db: Session,
    *,
    photos: list[Photo],
    schedule_task: Callable[..., None],
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    actor_user_id: int,
    source: str,
    custom_prompt: str | None = None,
    priority: str = "high",
    ip_address: str | None = None,
) -> list[int]:
    from app.services.job_queue import enqueue_photo_ai_task, schedule_job_worker

    normalized_prompt = normalize_operator_prompt(custom_prompt)
    batch_id = str(uuid4())
    queued_ids: list[int] = []

    for photo in photos:
        photo.labeling_status = "pending"
        db.add(photo)
        log_audit(
            db,
            action="photo_reprocess_requested",
            target_type="photo",
            target_id=str(photo.id),
            actor_user_id=actor_user_id,
            tenant_id=photo.tenant_id or photo.company_id,
            project_id=photo.project_id,
            company_id=photo.company_id,
            ip_address=ip_address,
            detail_json={
                "source": source,
                "priority": priority,
                "batch_id": batch_id,
                "custom_prompt_supplied": normalized_prompt is not None,
            },
        )
        enqueue_photo_ai_task(
            db,
            app_settings=app_settings,
            photo=photo,
            actor_user_id=actor_user_id,
            custom_prompt=normalized_prompt,
            trigger_source=source,
            priority=priority,
            batch_id=batch_id,
        )
        queued_ids.append(photo.id)

    if queued_ids:
        schedule_job_worker(schedule_task, session_maker, app_settings)
    return queued_ids


def process_photo_with_ai(
    photo_id: int,
    session_maker: sessionmaker[Session],
    app_settings: Settings,
    custom_prompt: str | None = None,
    triggered_by_user_id: int | None = None,
    trigger_source: str = "upload",
    priority: str = "normal",
    raise_on_failure: bool = False,
    batch_id: str | None = None,
) -> None:
    started = perf_counter()
    already_marked_failed = False
    with session_maker() as db:
        try:
            sync_ai_runtime_settings(db, app_settings)
            photo = db.get(Photo, photo_id)
            if photo is None:
                logger.warning(
                    "photo_ai_processing_skipped",
                    photo_id=photo_id,
                    reason="missing_photo",
                )
                return
            if photo.deleted:
                logger.info(
                    "photo_ai_processing_skipped",
                    photo_id=photo_id,
                    company_id=photo.company_id,
                    project_id=photo.project_id,
                    reason="deleted",
                )
                return

            photo_path = _resolve_photo_file(photo)
            mime_type = _photo_payload_mime_type(photo, photo_path)
            normalized_prompt = normalize_operator_prompt(custom_prompt)
            analysis_type = _resolve_analysis_type(custom_prompt=normalized_prompt, trigger_source=trigger_source)
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
            skip_reason = _visual_payload_skip_reason(
                photo_path,
                mime_type,
                min_image_bytes=app_settings.ai_min_auditable_image_bytes,
            )
            if skip_reason:
                normalized_result = _media_gate_ai_result(mime_type=mime_type, skip_reason=skip_reason)
                normalized_result = build_evidence_engine_result(
                    normalized_result,
                    photo_context=photo_context,
                )
                try:
                    attach_manager_evidence_review(
                        normalized_result,
                        photo_context=photo_context,
                    )
                except Exception as role_exc:
                    normalized_result["ai_role_reviews_error"] = str(role_exc)
                    logger.warning(
                        "photo_ai_role_reviews_failed",
                        photo_id=photo.id,
                        company_id=photo.company_id,
                        project_id=photo.project_id,
                        error=str(role_exc),
                    )
                analysis_log = _create_ai_analysis_log(
                    db,
                    photo=photo,
                    batch_id=batch_id,
                    analysis_type=analysis_type,
                    prompt_used=f"media_gate:{skip_reason}",
                    model_used="media_gate:local",
                    result_data=normalized_result,
                    created_by=_analysis_created_by(triggered_by_user_id),
                )
                snapshot, embedding_stored, embedding_ref, active_log_count = rebuild_photo_ai_snapshot(
                    db,
                    app_settings=app_settings,
                    photo=photo,
                    actor_user_id=triggered_by_user_id,
                    source=trigger_source,
                    priority=priority,
                    backends=[],
                    config_source="media_gate",
                )
                log_audit(
                    db,
                    action="photo_labeling_completed",
                    target_type="photo",
                    target_id=str(photo.id),
                    actor_user_id=triggered_by_user_id,
                    tenant_id=photo.tenant_id or photo.company_id,
                    project_id=photo.project_id,
                    company_id=photo.company_id,
                    detail_json={
                        "config_source": "media_gate",
                        "backend_id": "media_gate",
                        "backend_type": "local",
                        "source": trigger_source,
                        "priority": priority,
                        "batch_id": batch_id,
                        "analysis_log_id": analysis_log.id,
                        "analysis_type": analysis_type.value,
                        "custom_prompt_supplied": normalized_prompt is not None,
                        "labeling_status": photo.labeling_status,
                        "active_analysis_log_count": active_log_count,
                        "snapshot_ai_summary": snapshot.get("ai_summary"),
                        "embedding_stored": embedding_stored,
                        "embedding_ref": embedding_ref,
                        "attempts": [{"backend_id": "media_gate", "backend_type": "local", "status": "skipped", "reason": skip_reason}],
                    },
                )
                try:
                    from app.services.evidence_copilot import sync_photo_evidence_bundle

                    sync_photo_evidence_bundle(db, photo)
                except Exception as sync_exc:
                    logger.warning(
                        "photo_evidence_sync_failed",
                        photo_id=photo.id,
                        company_id=photo.company_id,
                        project_id=photo.project_id,
                        error=str(sync_exc),
                    )
                db.commit()
                duration_ms = round((perf_counter() - started) * 1000, 2)
                logger.info(
                    "photo_ai_processing_media_gate_completed",
                    photo_id=photo.id,
                    company_id=photo.company_id,
                    project_id=photo.project_id,
                    source=trigger_source,
                    priority=priority,
                    mime_type=mime_type,
                    reason=skip_reason,
                    duration_ms=duration_ms,
                )
                return

            backends, config_source = resolve_ai_backends(db, app_settings, photo)
            if not backends:
                raise RuntimeError("No enabled AI backends are configured")

            image_base64, mime_type = _read_photo_payload(photo, photo_path=photo_path, mime_type=mime_type)
            prompt = build_ai_prompt(db, photo, custom_prompt=normalized_prompt)
            attempts: list[dict[str, Any]] = []
            selected_backend: AIBackendNode | None = None
            structured_result: dict[str, Any] | None = None

            remaining_backends = list(_build_backend_order(backends))
            while remaining_backends:
                backend = remaining_backends.pop(0)
                try:
                    structured_result = call_ai_backend(
                        backend,
                        prompt=prompt,
                        image_base64=image_base64,
                        mime_type=mime_type,
                    )
                    _clear_backend_cooldown(backend)
                    selected_backend = backend
                    attempts.append(
                        {
                            "backend_id": backend.id,
                            "backend_type": backend.type,
                            "status": "success",
                        }
                    )
                    break
                except AIBackendError as exc:
                    attempts.append(
                        {
                            "backend_id": backend.id,
                            "backend_type": backend.type,
                            "status": "failed",
                            "error": exc.message,
                        }
                    )
                    _register_backend_failure_cooldown(
                        backend,
                        operation="vision",
                        error_message=exc.message,
                    )
                    logger.warning(
                        "photo_ai_backend_failed",
                        photo_id=photo.id,
                        company_id=photo.company_id,
                        project_id=photo.project_id,
                        backend_id=backend.id,
                        backend_type=backend.type,
                        config_source=config_source,
                        error=exc.message,
                    )
                    remaining_backends = _reprioritize_backends_for_dynamic_fallback(
                        remaining_backends,
                        failed_backend=backend,
                        operation="vision",
                        error_message=exc.message,
                    )

            if structured_result is None or selected_backend is None:
                failure_message = "All configured AI backends failed"
                _mark_photo_failed(
                    db,
                    photo,
                    message=failure_message,
                    attempts=attempts,
                    config_source=config_source,
                    actor_user_id=triggered_by_user_id,
                    source=trigger_source,
                    priority=priority,
                    custom_prompt_supplied=normalized_prompt is not None,
                )
                already_marked_failed = True
                logger.error(
                    "photo_ai_processing_failed",
                    photo_id=photo.id,
                    company_id=photo.company_id,
                    project_id=photo.project_id,
                    config_source=config_source,
                    reason="backend_exhausted",
                    fallback_count=len(attempts),
                )
                if raise_on_failure:
                    raise RuntimeError(failure_message)
                return

            # Normalize only; skip summary translation here because
            # attach_manager_evidence_review below unconditionally replaces
            # ai_summary and resets ai_summary_translations, so translating the
            # pre-review summary was one discarded LLM call per photo.
            normalized_result = _normalize_analysis_snapshot(structured_result)
            direct_role_reviews: dict[str, Any] | None = None
            if photo.photo_type == PhotoType.project:
                try:
                    direct_role_reviews = run_people_count_vision_review(
                        backends=backends,
                        image_base64=image_base64,
                        mime_type=mime_type,
                        photo_context=photo_context,
                    )
                except Exception as role_exc:
                    direct_role_reviews = {
                        "version": "2026-05-06-people-count-v1",
                        "mode": "direct_image_visible_people_count",
                        "results": [],
                        "summary": {"role_count": 0, "retry_roles": ["visible_people_count"]},
                        "attempts_by_role": {"visible_people_count": [{"status": "failed", "error": str(role_exc)}]},
                    }
                    logger.warning(
                        "photo_ai_people_count_review_failed",
                        photo_id=photo.id,
                        company_id=photo.company_id,
                        project_id=photo.project_id,
                        error=str(role_exc),
                    )
            normalized_result = build_evidence_engine_result(
                normalized_result,
                photo_context=photo_context,
            )
            try:
                attach_manager_evidence_review(
                    normalized_result,
                    photo_context=photo_context,
                    direct_role_reviews=direct_role_reviews,
                )
            except Exception as role_exc:
                normalized_result["ai_role_reviews_error"] = str(role_exc)
                logger.warning(
                    "photo_ai_role_reviews_failed",
                    photo_id=photo.id,
                    company_id=photo.company_id,
                    project_id=photo.project_id,
                    error=str(role_exc),
                )
            analysis_log = _create_ai_analysis_log(
                db,
                photo=photo,
                batch_id=batch_id,
                analysis_type=analysis_type,
                prompt_used=prompt,
                model_used=f"{selected_backend.type}:{selected_backend.model}",
                result_data=normalized_result,
                created_by=_analysis_created_by(triggered_by_user_id),
            )
            snapshot, embedding_stored, embedding_ref, active_log_count = rebuild_photo_ai_snapshot(
                db,
                app_settings=app_settings,
                photo=photo,
                actor_user_id=triggered_by_user_id,
                source=trigger_source,
                priority=priority,
                backends=backends,
                config_source=config_source,
            )
            log_audit(
                db,
                action="photo_labeling_completed",
                target_type="photo",
                target_id=str(photo.id),
                actor_user_id=triggered_by_user_id,
                tenant_id=photo.tenant_id or photo.company_id,
                project_id=photo.project_id,
                company_id=photo.company_id,
                detail_json={
                    "config_source": config_source,
                    "backend_id": selected_backend.id,
                    "backend_type": selected_backend.type,
                    "source": trigger_source,
                    "priority": priority,
                    "batch_id": batch_id,
                    "analysis_log_id": analysis_log.id,
                    "analysis_type": analysis_type.value,
                    "custom_prompt_supplied": normalized_prompt is not None,
                    "labeling_status": photo.labeling_status,
                    "active_analysis_log_count": active_log_count,
                    "snapshot_ai_summary": snapshot.get("ai_summary"),
                    "embedding_stored": embedding_stored,
                    "embedding_ref": embedding_ref,
                    "attempts": attempts,
                },
            )
            try:
                from app.services.evidence_copilot import sync_photo_evidence_bundle

                sync_photo_evidence_bundle(db, photo)
            except Exception as sync_exc:
                logger.warning(
                    "photo_evidence_sync_failed",
                    photo_id=photo.id,
                    company_id=photo.company_id,
                    project_id=photo.project_id,
                    error=str(sync_exc),
                )
            db.commit()
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.info(
                "photo_ai_processing_completed",
                photo_id=photo.id,
                backend_id=selected_backend.id,
                backend_type=selected_backend.type,
                company_id=photo.company_id,
                project_id=photo.project_id,
                config_source=config_source,
                source=trigger_source,
                priority=priority,
                duration_ms=duration_ms,
                fallback_count=len([attempt for attempt in attempts if attempt["status"] == "failed"]),
                embedding_stored=embedding_stored,
                embedding_ref=embedding_ref,
            )
        except Exception as exc:
            db.rollback()
            photo = db.get(Photo, photo_id)
            if photo is not None and not photo.deleted and not already_marked_failed:
                _mark_photo_failed(
                    db,
                    photo,
                    message=str(exc),
                    attempts=[],
                    actor_user_id=triggered_by_user_id,
                    source=trigger_source,
                    priority=priority,
                    custom_prompt_supplied=normalize_operator_prompt(custom_prompt) is not None,
                )
            duration_ms = round((perf_counter() - started) * 1000, 2)
            logger.exception(
                "photo_ai_processing_crashed",
                photo_id=photo_id,
                company_id=photo.company_id if photo is not None else None,
                project_id=photo.project_id if photo is not None else None,
                source=trigger_source,
                priority=priority,
                duration_ms=duration_ms,
                error=str(exc),
            )
            if raise_on_failure:
                raise
