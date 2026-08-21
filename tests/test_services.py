from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from requests import RequestException
from sqlalchemy import select

from app.core.config import Settings
from app.models import AIAnalysisLog, AIAnalysisStatus, AIAnalysisType, AnnotationRole, AnnotationStatus, AnnotationType, AnnotationVisibility, AuditLog, CameraApprovalStatus, Company, Employee, IPCamera, MediaAnnotation, MediaAsset, MediaAssetStatus, MediaType, PasswordResetToken, Photo, PhotoType, PlanCode, ProgressReport, ProgressReportStatus, Project, ProjectMember, Subscription, SubscriptionStatus, SystemSetting, TaskJob, TaskStatus, Tenant, User, UserRole
from app.models import LensDefinition, LensDefinitionVersion, PhotoLensObservationRun
from app.core.security import generate_api_key, normalize_api_key
from app.services.access import can_access_photo, can_access_project
from app.services.ai_pipeline import AIBackendError, AIBackendNode, _backend_in_cooldown, _build_backend_order, _clear_backend_cooldown, _register_backend_failure_cooldown, build_ai_prompt, build_ai_prompt_for_context, configure_ai_backend_failure_cooldown_seconds, configure_ai_concurrency_limit, configure_ai_dynamic_fallback_enabled, generate_markdown_completion, get_ai_dynamic_fallback_enabled, get_ai_max_concurrent_requests, merge_active_ai_analysis_logs, normalize_ai_result, perform_ai_healthcheck, process_photo_with_ai, resolve_ai_backends, set_ai_analysis_log_status
from app.services.auth_tokens import consume_password_reset_token, create_password_reset_token
from app.services.billing import enforce_upload_limit, get_usage_counter, record_upload_usage
from app.services.camera_scheduler import _enabled_rtsp_cameras, camera_health_summary
from app.services.employees import create_employee_for_user, list_accessible_employees
from app.services.job_queue import enqueue_photo_ai_task, process_due_jobs, recover_stale_running_jobs, schedule_job_worker
from app.services.media_pipeline import cleanup_expired_media_assets, get_media_storage_monitor, next_media_asset_id
from app.services.photos import build_relative_image_url, build_thumb_filename
from app.services.photo_lenses import _lens_validation_status, _normalize_lens_payload, _ollama_response_schema, _validate_lens_payload, run_photo_lens_shadow
from app.services.system_health import perform_system_health_audit
from app.services.tenant import DEFAULT_COMPANY_CODE, normalize_company_scoped_identifier


def _lens_item(key: str, *, state: str = "not_observed", evidence: str = "") -> dict[str, str]:
    return {
        "key": key,
        "state": state,
        "value": "visible value" if state == "observed" else "",
        "confidence": "high" if state == "observed" else "unknown",
        "evidence": evidence,
    }


def test_photo_lens_validator_requires_exact_allowed_item_keys():
    payload = {
        "schema_version": "materials_visible:v2",
        "lens_key": "materials_visible",
        "summary": "No supported material evidence was observed.",
        "items": [
            _lens_item("material_categories"),
            _lens_item("invented_material_score", state="observed", evidence="model-added field"),
            _lens_item("material_categories"),
        ],
        "limitations": [],
    }

    errors = _validate_lens_payload(
        payload,
        expected_lens_key="materials_visible",
        expected_schema_version="materials_visible:v2",
        allowed_item_keys=["material_categories", "brand_or_product_name"],
    )

    assert "items[1]:unknown_key:invented_material_score" in errors
    assert "items[2]:duplicate_key:material_categories" in errors
    assert "item_missing:brand_or_product_name" in errors


def test_photo_lens_normalization_does_not_hide_lens_mismatch():
    normalized = _normalize_lens_payload(
        {
            "lens_key": "tools_equipment",
            "summary": "Visible tool.",
            "items": [_lens_item("equipment_present", state="observed", evidence="A drill is visible.")],
            "limitations": [],
        },
        lens_key="materials_visible",
    )

    errors = _validate_lens_payload(
        normalized,
        expected_lens_key="materials_visible",
        expected_schema_version="materials_visible:v2",
        allowed_item_keys=["equipment_present"],
    )

    assert normalized["lens_key"] == "tools_equipment"
    assert "lens_key_mismatch" in errors


def test_photo_lens_validator_accepts_complete_bounded_payload():
    keys = ["material_categories", "brand_or_product_name"]
    payload = {
        "schema_version": "materials_visible:v2",
        "lens_key": "materials_visible",
        "summary": "One labeled package is visible.",
        "items": [
            _lens_item("material_categories", state="observed", evidence="A bag is visible."),
            _lens_item("brand_or_product_name", state="observed", evidence="The printed brand is legible."),
        ],
        "limitations": [],
    }

    assert _validate_lens_payload(
        payload,
        expected_lens_key="materials_visible",
        expected_schema_version="materials_visible:v2",
        allowed_item_keys=keys,
    ) == []


def test_photo_lens_backend_failure_is_audited_but_not_activated(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.added = []

        def scalar(self, _statement):
            return None

        def add(self, value):
            self.added.append(value)

        def flush(self):
            return None

    db = FakeSession()
    photo = Photo(
        id=321,
        company_id="acme",
        tenant_id="acme",
        employee_id="E100",
        project_id="P100",
        photo_type=PhotoType.project,
        file_path="/tmp/photo.jpg",
        storage_path="/tmp/photo.jpg",
        image_url="/media/photo.jpg",
        original_file_name="photo.jpg",
        checksum="abc123",
    )
    lens = LensDefinition(
        id="core:materials_visible",
        lens_key="materials_visible",
        scope="core",
        display_name="Visible Materials",
        is_enabled=True,
        priority=70,
        current_version_id="core:materials_visible:v2",
    )
    version = LensDefinitionVersion(
        id="core:materials_visible:v2",
        lens_id=lens.id,
        version="v2",
        prompt_template="test prompt",
        output_schema_json={"allowed_item_keys": ["material_categories"]},
        validation_rules_json={},
        model_profile_json={},
        status="active",
    )
    monkeypatch.setattr("app.services.photo_lenses._read_photo_payload", lambda _photo: ("base64", "image/jpeg"))
    monkeypatch.setattr(
        "app.services.photo_lenses._call_lens_ollama",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("backend unavailable")),
    )

    result = run_photo_lens_shadow(
        db,
        app_settings=Settings(lens_vision_backend_url="http://lens.invalid"),
        photo=photo,
        lens=lens,
        version=version,
        force=True,
    )

    assert result is not None
    assert result.observation is None
    assert result.run.run_status == "failed"
    assert result.run.validation_status == "shadow_invalid"
    assert result.ai_log.status == AIAnalysisStatus.rejected
    assert result.run.result_json["provenance"]["legacy_caption_used"] is False
    assert result.run.result_json["provenance"]["validation_status"] == "shadow_invalid"
    assert any(isinstance(value, PhotoLensObservationRun) for value in db.added)


def test_high_risk_photo_lenses_require_review_even_when_structurally_valid():
    for lens_key in ("defect_surface", "remediation_evidence", "concealed_work", "safety_observables"):
        assert _lens_validation_status(lens_key=lens_key, run_status="completed", errors=[]) == "shadow_review_required"

    assert _lens_validation_status(lens_key="materials_visible", run_status="completed", errors=[]) == "shadow_valid"
    assert _lens_validation_status(lens_key="materials_visible", run_status="completed", errors=["bad_key"]) == "shadow_invalid"


def test_photo_lens_ollama_schema_is_closed_and_bounded():
    schema = _ollama_response_schema(
        lens_key="materials_visible",
        allowed_item_keys=["material_categories", "brand_or_product_name"],
    )

    assert schema["additionalProperties"] is False
    assert schema["properties"]["lens_key"]["const"] == "materials_visible"
    assert schema["properties"]["schema_version"]["const"] == "materials_visible:v2"
    assert schema["properties"]["items"]["minItems"] == 2
    assert schema["properties"]["items"]["maxItems"] == 2
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_password_reset_tokens_are_single_use(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        user = db.scalar(select(User).where(User.email == "admin@example.com"))
        assert user is not None
        token = create_password_reset_token(db, user=user, settings=settings)
        db.commit()

    with session_maker() as db:
        consumed_user = consume_password_reset_token(db, token)
        assert consumed_user is not None
        db.commit()

    with session_maker() as db:
        assert consume_password_reset_token(db, token) is None
        entry = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == consumed_user.id))
        assert entry is not None
        assert entry.used_at is not None


def test_plan_enforcement_and_usage_tracking(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        company = Company(company_id="quota", company_code="20001", company_name="Quota Co", active=True)
        db.add(company)
        db.add(Subscription(company_id="quota", plan_id=PlanCode.free, status=SubscriptionStatus.active))
        db.commit()

    with session_maker() as db:
        enforce_upload_limit(db, company_id="quota", timezone_name=settings.default_timezone)
        record_upload_usage(db, company_id="quota", timezone_name=settings.default_timezone, file_size=100)
        record_upload_usage(db, company_id="quota", timezone_name=settings.default_timezone, file_size=200)
        db.commit()

    with session_maker() as db:
        try:
            enforce_upload_limit(db, company_id="quota", timezone_name=settings.default_timezone)
            assert False, "Expected daily upload limit to block the third upload"
        except Exception as exc:
            assert "Daily upload limit" in str(exc)


def test_usage_counter_is_idempotent_by_month(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        company = Company(company_id="repeat", company_code="20002", company_name="Repeat Co", active=True)
        db.add(company)
        db.add(Subscription(company_id="repeat", plan_id=PlanCode.free, status=SubscriptionStatus.active))
        db.commit()

    with session_maker() as db:
        first = get_usage_counter(db, company_id="repeat", month_key="2026-03", usage_date="2026-03-01")
        first.photos_this_month = 4
        first.uploaded_count = 2
        db.add(first)
        db.commit()
        first_id = first.id

    with session_maker() as db:
        second = get_usage_counter(db, company_id="repeat", month_key="2026-03", usage_date="2026-03-02")
        assert second.id == first_id
        assert second.usage_date == "2026-03-02"
        assert second.uploaded_count == 0
        assert second.photos_this_month == 4


def test_cross_tenant_photo_access_is_blocked(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        db.add(
            Project(
                company_id="acme",
                project_id="P201",
                project_name="Acme Work",
                client_name="Acme Client",
                location="Dallas",
            )
        )
        db.add(
            Photo(
                company_id="acme",
                tenant_id="acme",
                employee_id="E200",
                project_id="P201",
                photo_type=PhotoType.project,
                file_path="/tmp/acme.jpg",
                storage_path="/tmp/acme.jpg",
                image_url="/media/acme/P201/E200/acme.jpg",
                original_file_name="acme.jpg",
                captured_at_utc=datetime.now(timezone.utc),
            )
        )
        db.commit()

    with session_maker() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        other_photo = db.scalar(select(Photo).where(Photo.company_id == "acme"))
        assert admin is not None
        assert other_photo is not None
        assert can_access_photo(db, admin, other_photo) is False


def test_project_manager_scope_blocks_cross_company_and_unassigned_employees(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        assert can_access_project(db, manager, "P100") is True
        assert can_access_project(db, manager, "P200") is False

        visible_employee_ids = {employee.employee_id for employee in list_accessible_employees(db, manager)}
        assert "E100" in visible_employee_ids
        assert "E200" not in visible_employee_ids

        with pytest.raises(Exception) as forbidden:
            create_employee_for_user(
                db,
                user=manager,
                employee_id="E220",
                name="Wrong Project Worker",
                role_name="worker",
                project_id="P200",
            )
        assert getattr(forbidden.value, "status_code", None) == 403

        with pytest.raises(Exception) as missing_project:
            create_employee_for_user(
                db,
                user=manager,
                employee_id="E221",
                name="Unassigned Worker",
                role_name="worker",
                project_id=None,
            )
        assert getattr(missing_project.value, "status_code", None) == 400


def test_company_admin_employee_creation_stays_in_tenant_scope(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert admin is not None
        assert manager is not None

        unassigned = create_employee_for_user(
            db,
            user=admin,
            employee_id="225",
            name="Tenant Bench Worker",
            role_name="worker",
            project_id=None,
        )
        assigned = create_employee_for_user(
            db,
            user=admin,
            employee_id="226",
            name="Tenant Assigned Worker",
            role_name="worker",
            project_id="P100",
        )
        db.commit()

    with session_maker() as db:
        unassigned = db.scalar(select(Employee).where(Employee.name == "Tenant Bench Worker"))
        assigned = db.scalar(select(Employee).where(Employee.name == "Tenant Assigned Worker"))
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert unassigned is not None
        assert assigned is not None
        assert manager is not None
        assert unassigned.company_id == "default"
        assert unassigned.tenant_id == "default"
        assert unassigned.project_id is None
        assert assigned.company_id == "default"
        assert assigned.project_id == "P100"

        manager_visible_ids = {employee.employee_id for employee in list_accessible_employees(db, manager)}
        assert assigned.employee_id in manager_visible_ids
        assert unassigned.employee_id not in manager_visible_ids


def test_storage_path_builders():
    assert build_relative_image_url(PhotoType.project, "tenant-a", "P100", "E100", "photo.jpg") == "/media/tenant-a/P100/E100/photo.jpg"
    assert build_relative_image_url(PhotoType.invoice, "tenant-a", "invoice", "E100", "receipt.jpg") == "/media/tenant-a/invoice/E100/receipt.jpg"
    assert build_thumb_filename("photo.jpg") == "thumb_photo.jpg"


def test_company_scoped_identifier_normalization_supports_numeric_and_legacy_ids():
    assert normalize_company_scoped_identifier("12", company_code=DEFAULT_COMPANY_CODE, kind="project") == "100000012"
    assert normalize_company_scoped_identifier("100000012", company_code=DEFAULT_COMPANY_CODE, kind="project") == "100000012"
    assert normalize_company_scoped_identifier("P100", company_code=DEFAULT_COMPANY_CODE, kind="project") == "P100"

    with pytest.raises(ValueError):
        normalize_company_scoped_identifier("200000012", company_code=DEFAULT_COMPANY_CODE, kind="project")


def test_employee_api_keys_are_shorter_and_normalized():
    generated = generate_api_key()
    assert 12 <= len(generated) <= 24
    assert normalize_api_key(f"  {generated}  ") == generated

    with pytest.raises(ValueError):
        normalize_api_key("short")

    with pytest.raises(ValueError):
        normalize_api_key("bad key with spaces")


def test_build_ai_prompt_includes_public_completed_annotations_only(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="/tmp/annotated.jpg",
            storage_path="/tmp/annotated.jpg",
            image_url="/media/default/P100/E100/annotated.jpg",
            original_file_name="annotated.jpg",
            captured_at_utc=datetime.now(timezone.utc),
            note="base note",
        )
        db.add(photo)
        db.flush()
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        db.add_all(
            [
                MediaAnnotation(
                    id="annot-public-1",
                    photo_id=photo.id,
                    user_id=manager.id,
                    role_at_time=AnnotationRole.manager,
                    annotation_type=AnnotationType.text,
                    content_text="public crack near north wall",
                    source_language="en",
                    translations_json={
                        "zh": "北墙附近有裂缝",
                        "en": "public crack near north wall",
                        "es": "grieta pública cerca del muro norte",
                    },
                    visibility=AnnotationVisibility.public,
                    status=AnnotationStatus.completed,
                ),
                MediaAnnotation(
                    id="annot-private-1",
                    photo_id=photo.id,
                    user_id=manager.id,
                    role_at_time=AnnotationRole.manager,
                    annotation_type=AnnotationType.text,
                    content_text="manager-only note",
                    visibility=AnnotationVisibility.manager_only,
                    status=AnnotationStatus.completed,
                ),
                MediaAnnotation(
                    id="annot-failed-1",
                    photo_id=photo.id,
                    user_id=manager.id,
                    role_at_time=AnnotationRole.manager,
                    annotation_type=AnnotationType.voice,
                    content_text="failed transcript should be ignored",
                    visibility=AnnotationVisibility.public,
                    status=AnnotationStatus.failed,
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        photo = db.scalar(select(Photo).where(Photo.original_file_name == "annotated.jpg"))
        assert photo is not None
        prompt = build_ai_prompt(db, photo, custom_prompt="focus on defects")
        assert "public crack near north wall" in prompt
        assert "北墙附近有裂缝" in prompt
        assert "grieta pública cerca del muro norte" in prompt
        assert "manager-only note" not in prompt
        assert "failed transcript should be ignored" not in prompt


def test_build_ai_prompt_for_context_uses_work_helpful_project_guidance():
    prompt = build_ai_prompt_for_context(
        photo_type=PhotoType.project,
        project_id="P100",
        note="north wall walkthrough",
        project_prompt="Focus on staging, housekeeping, and visible safety gaps.",
    )

    assert "field evidence triage analyst" in prompt
    assert "Do not infer construction context" in prompt
    assert "Do not copy any example object names." not in prompt
    assert "Do not copy words from project guidance into output" in prompt
    assert "Focus on staging, housekeeping, and visible safety gaps." in prompt


def test_build_ai_prompt_for_context_uses_invoice_guidance():
    prompt = build_ai_prompt_for_context(
        photo_type=PhotoType.invoice,
        project_id="invoice",
        note="fuel stop",
        project_prompt="Focus on vendor, gallons, and total amount.",
    )

    assert "expense-evidence analyst" in prompt
    assert "financially useful" in prompt
    assert "vendor, total amount, date, gallons or units" in prompt
    assert "Do not copy placeholder values." in prompt
    assert "Focus on vendor, gallons, and total amount." in prompt


def test_normalize_ai_result_discards_empty_defect_markers_and_dedupes_labels():
    normalized = normalize_ai_result(
        {
            "ai_summary": "Desk with a laptop and mouse visible.",
            "labels": ["Laptop", "desk", "laptop", "Desk"],
            "defects": ["None", "no obvious defects", "Loose cable"],
        }
    )

    assert normalized["labels"] == ["laptop", "desk"]
    assert normalized["defects"] == ["Loose cable"]


def test_normalize_ai_result_canonicalizes_label_variants_and_caps_length():
    normalized = normalize_ai_result(
        {
            "ai_summary": "Workspace with visible equipment.",
            "labels": [
                "Wall Outlet",
                "wall_outlet",
                "white-door",
                "White Door",
                "Desk Lamp",
                "Monitor",
                "Keyboard",
                "Mouse",
                "Laptop",
                "Phone",
                "Extra Label",
            ],
            "defects": [],
        }
    )

    assert normalized["labels"] == [
        "wall_outlet",
        "white_door",
        "desk_lamp",
        "monitor",
        "keyboard",
        "mouse",
        "laptop",
        "phone",
    ]


def test_merge_active_ai_analysis_logs_prefers_latest_active_snapshot():
    older_log = AIAnalysisLog(
        id="older-log",
        photo_id=1,
        batch_id="batch-1",
        analysis_type=AIAnalysisType.fast_screen,
        prompt_used="old prompt",
        model_used="ollama:old",
        result_data={
            "ai_summary": "Older generic summary.",
            "labels": ["device", "room"],
            "defects": ["none"],
        },
        status=AIAnalysisStatus.active,
        created_by="system",
        created_at=datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc),
    )
    newer_log = AIAnalysisLog(
        id="newer-log",
        photo_id=1,
        batch_id="batch-2",
        analysis_type=AIAnalysisType.fast_screen,
        prompt_used="new prompt",
        model_used="ollama:new",
        result_data={
            "ai_summary": "Laptop workstation on a wooden desk with a mouse and phone visible; no obvious damage or obstruction.",
            "labels": ["workstation", "desk", "laptop", "mouse", "phone"],
            "defects": [],
        },
        status=AIAnalysisStatus.active,
        created_by="system",
        created_at=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc),
    )

    merged = merge_active_ai_analysis_logs([older_log, newer_log])

    assert merged["ai_summary"].startswith("Laptop workstation")
    assert merged["labels"] == ["workstation", "desk", "laptop", "mouse", "phone"]
    assert merged["defects"] == []


def test_weighted_round_robin_prefers_heavier_backend():
    backends = [
        AIBackendNode(
            id="weighted-primary",
            type="ollama",
            url="http://primary.example",
            model="llava:latest",
            weight=2,
            enabled=True,
        ),
        AIBackendNode(
            id="weighted-secondary",
            type="gemini",
            url="https://secondary.example",
            model="gemini-1.5-flash",
            weight=1,
            enabled=True,
        ),
    ]

    assert [node.id for node in _build_backend_order(backends)] == ["weighted-primary", "weighted-secondary"]
    assert [node.id for node in _build_backend_order(backends)] == ["weighted-primary", "weighted-secondary"]
    assert [node.id for node in _build_backend_order(backends)] == ["weighted-secondary", "weighted-primary"]


def test_backend_order_skips_backends_in_temporary_cooldown():
    primary = AIBackendNode(
        id="cooldown-primary",
        type="ollama",
        url="http://primary.example",
        model="llama3.2-vision",
        weight=1,
        enabled=True,
        api_key=None,
        embedding_model=None,
    )
    secondary = AIBackendNode(
        id="cooldown-secondary",
        type="gemini",
        url="https://secondary.example",
        model="gemini-2.5-flash",
        weight=1,
        enabled=True,
        api_key="secret",
        embedding_model=None,
    )

    original_seconds = configure_ai_backend_failure_cooldown_seconds(120)
    try:
        _register_backend_failure_cooldown(
            primary,
            operation="vision",
            error_message="Connection timed out while contacting backend",
        )

        assert _backend_in_cooldown(primary) is True
        assert [node.id for node in _build_backend_order([primary, secondary])] == ["cooldown-secondary"]
    finally:
        _clear_backend_cooldown(primary)
        _clear_backend_cooldown(secondary)
        configure_ai_backend_failure_cooldown_seconds(original_seconds)


def test_generate_markdown_completion_cools_down_transiently_failed_backend(monkeypatch):
    primary = AIBackendNode(
        id="markdown-primary",
        type="ollama",
        url="http://markdown-primary.example",
        model="qwen2.5:7b-instruct",
        weight=1,
        enabled=True,
        api_key=None,
        embedding_model=None,
    )
    secondary = AIBackendNode(
        id="markdown-secondary",
        type="gemini",
        url="https://markdown-secondary.example",
        model="gemini-2.5-flash",
        weight=1,
        enabled=True,
        api_key="secret",
        embedding_model=None,
    )

    call_order: list[str] = []

    def fake_call_text_backend(node: AIBackendNode, *, prompt: str) -> str:
        call_order.append(node.id)
        if node.id == "markdown-primary":
            raise AIBackendError(node, "Remote end closed connection without response")
        return "## Stable answer"

    original_seconds = configure_ai_backend_failure_cooldown_seconds(120)
    monkeypatch.setattr("app.services.ai_pipeline.call_text_backend", fake_call_text_backend)
    try:
        content, backend_ref, attempts = generate_markdown_completion(
            backends=[primary, secondary],
            prompt="Summarize current construction risk.",
        )

        assert content == "## Stable answer"
        assert backend_ref == "gemini:gemini-2.5-flash"
        assert call_order == ["markdown-primary", "markdown-secondary"]
        assert attempts[0]["status"] == "failed"
        assert _backend_in_cooldown(primary) is True
        assert [node.id for node in _build_backend_order([primary, secondary])] == ["markdown-secondary"]
    finally:
        _clear_backend_cooldown(primary)
        _clear_backend_cooldown(secondary)
        configure_ai_backend_failure_cooldown_seconds(original_seconds)


def test_ai_concurrency_setting_defaults_and_clamps(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        assert get_ai_max_concurrent_requests(db, settings) == 3
        assert get_ai_dynamic_fallback_enabled(db, settings) is True
        setting = db.get(SystemSetting, "ai_max_concurrent_requests")
        assert setting is not None
        setting.value = "99"
        db.add(setting)
        fallback_setting = db.get(SystemSetting, "ai_enable_dynamic_fallback")
        assert fallback_setting is not None
        fallback_setting.value = "false"
        db.add(fallback_setting)
        db.commit()

    with session_maker() as db:
        assert get_ai_max_concurrent_requests(db, settings) == 10
        assert get_ai_dynamic_fallback_enabled(db, settings) is False

    assert configure_ai_concurrency_limit(0) == 1
    assert configure_ai_concurrency_limit(7) == 7
    assert configure_ai_dynamic_fallback_enabled("on") is True


def test_media_asset_cleanup_archives_and_deletes_video_files(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    warm_asset_path = settings.media_assets_root / "video" / "default" / "warm-asset" / "warm.mp4"
    warm_asset_path.parent.mkdir(parents=True, exist_ok=True)
    warm_asset_path.write_bytes(b"warm-video")
    expired_asset_path = settings.media_assets_root / "video" / "default" / "expired-asset" / "expired.mp4"
    expired_asset_path.parent.mkdir(parents=True, exist_ok=True)
    expired_asset_path.write_bytes(b"expired-video")

    with session_maker() as db:
        hot_setting = db.get(SystemSetting, "hot_storage_days")
        retention_setting = db.get(SystemSetting, "video_retention_days")
        delete_only_setting = db.get(SystemSetting, "video_delete_files_only")
        assert hot_setting is not None and retention_setting is not None and delete_only_setting is not None
        hot_setting.value = "7"
        retention_setting.value = "30"
        delete_only_setting.value = "true"
        db.add_all([hot_setting, retention_setting, delete_only_setting])

        archived_asset = MediaAsset(
            asset_id="warm-asset",
            tenant_id="default",
            company_id="default",
            media_type=MediaType.video,
            source="manual_upload",
            file_path=str(warm_asset_path),
            original_file_name="warm.mp4",
            mime_type="video/mp4",
            file_size=warm_asset_path.stat().st_size,
            checksum="warm",
            duration_seconds=12.0,
            status=MediaAssetStatus.completed,
            created_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
            completed_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
        )
        deleted_asset = MediaAsset(
            asset_id="expired-asset",
            tenant_id="default",
            company_id="default",
            media_type=MediaType.video,
            source="manual_upload",
            file_path=str(expired_asset_path),
            original_file_name="expired.mp4",
            mime_type="video/mp4",
            file_size=expired_asset_path.stat().st_size,
            checksum="expired",
            duration_seconds=18.0,
            status=MediaAssetStatus.completed,
            created_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
            updated_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
            completed_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
        )
        db.add_all([archived_asset, deleted_asset])
        db.commit()

    summary = cleanup_expired_media_assets(session_maker, settings)
    assert summary["checked"] >= 2
    assert summary["archived"] == 0
    assert summary["deleted"] >= 2

    with session_maker() as db:
        archived_asset = db.get(MediaAsset, "warm-asset")
        deleted_asset = db.get(MediaAsset, "expired-asset")
        assert archived_asset is not None and deleted_asset is not None
        assert archived_asset.status == MediaAssetStatus.deleted
        assert not warm_asset_path.exists()
        assert deleted_asset.status == MediaAssetStatus.deleted
        assert not expired_asset_path.exists()


def test_media_storage_monitor_warns_near_capacity(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        threshold_setting = db.get(SystemSetting, "storage_warning_threshold_percent")
        cost_setting = db.get(SystemSetting, "storage_cost_per_gb_month")
        retention_setting = db.get(SystemSetting, "video_retention_days")
        assert threshold_setting is not None and cost_setting is not None and retention_setting is not None
        threshold_setting.value = "80"
        cost_setting.value = "0.25"
        retention_setting.value = "30"
        db.add_all([threshold_setting, cost_setting, retention_setting])
        expiring_reference_time = datetime.now(timezone.utc) - timedelta(days=25)
        db.add(
            MediaAsset(
                asset_id="storage-heavy-asset",
                tenant_id="default",
                company_id="default",
                media_type=MediaType.video,
                source="manual_upload",
                file_path="C:/tmp/storage-heavy-asset.mp4",
                original_file_name="storage-heavy-asset.mp4",
                mime_type="video/mp4",
                file_size=450 * 1024 * 1024,
                checksum="storage-heavy",
                duration_seconds=60.0,
                status=MediaAssetStatus.completed,
                created_at=expiring_reference_time,
                updated_at=expiring_reference_time,
                completed_at=expiring_reference_time,
            )
        )
        db.commit()

    with session_maker() as db:
        summary = get_media_storage_monitor(db, settings, company_id="default", storage_limit_mb=512)
        assert summary["warning"] is True
        assert summary["usage_rate_percent"] >= 80
        assert summary["estimated_next_month_cost"] > 0
        assert summary["expiring_soon_count"] >= 1


def test_camera_health_summary_includes_recent_failure_records(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        camera = IPCamera(
            company_id="default",
            name="Failing Camera",
            stream_url="rtsp://failing.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="error",
            consecutive_failures=4,
            error_log="connection refused",
            approval_status=CameraApprovalStatus.approved,
        )
        db.add(camera)
        db.flush()
        db.add(
            AuditLog(
                action="ip_camera_test_failed",
                target_type="ip_camera",
                target_id=str(camera.id),
                company_id="default",
                detail_json={"error": "connection refused"},
            )
        )
        db.commit()

    with session_maker() as db:
        summary = camera_health_summary(db, "default", failure_threshold=3)
        assert summary["attention_count"] >= 1
        assert summary["recent_failures"]
        assert summary["recent_failures"][0]["camera_name"] == "Failing Camera"


def test_enabled_rtsp_cameras_only_include_platform_approved_streams(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        approved = IPCamera(
            company_id="default",
            name="Approved Camera",
            stream_url="rtsp://approved.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="online",
            approval_status=CameraApprovalStatus.approved,
        )
        pending = IPCamera(
            company_id="default",
            name="Pending Camera",
            stream_url="rtsp://pending.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="online",
            approval_status=CameraApprovalStatus.pending,
        )
        rejected = IPCamera(
            company_id="default",
            name="Rejected Camera",
            stream_url="rtsp://rejected.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="online",
            approval_status=CameraApprovalStatus.rejected,
        )
        disabled = IPCamera(
            company_id="default",
            name="Disabled Camera",
            stream_url="rtsp://disabled.local/live",
            protocol="rtsp",
            is_enabled=False,
            status="online",
            approval_status=CameraApprovalStatus.approved,
        )
        db.add_all([approved, pending, rejected, disabled])
        db.commit()

        eligible = _enabled_rtsp_cameras(db)
        assert [camera.name for camera in eligible] == ["Approved Camera"]


def test_next_media_asset_id_rejects_path_traversal_like_values():
    first = next_media_asset_id("../../escape")
    second = next_media_asset_id("..\\escape")
    assert first != "../../escape"
    assert second != "..\\escape"
    assert len(first) >= 32
    assert len(second) >= 32
    assert next_media_asset_id("123e4567-e89b-12d3-a456-426614174000") == "123e4567-e89b-12d3-a456-426614174000"


def test_system_health_audit_sends_queue_backlog_alert(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.queue_backlog_warning_threshold = 1
    delivered_messages: list[dict[str, str]] = []

    def fake_send_email(db, *, settings, to_email, subject, html_content, action, company_id=None):
        delivered_messages.append({"to": to_email, "subject": subject, "action": action})
        return True

    monkeypatch.setattr("app.services.system_health.send_email", fake_send_email)

    with session_maker() as db:
        db.add(
            TaskJob(
                public_id="queue-alert-job",
                tenant_id="default",
                company_id="default",
                task_type="photo_ai_pipeline",
                status=TaskStatus.queued,
                priority=100,
                attempt_count=0,
                max_attempts=3,
                payload_json={"photo_id": 1},
                related_type="photo",
                related_id="1",
            )
        )
        db.commit()

    summary = perform_system_health_audit(session_maker, settings)
    assert summary["queue_alerts"] == 1
    assert delivered_messages
    assert delivered_messages[0]["action"] == "system_queue_backlog_alert_sent"


def test_dynamic_fallback_prioritizes_gemini_after_ollama_oom(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    photo_path = _create_photo_file(settings, "dynamic-fallback-photo.jpg")

    with session_maker() as db:
        system_setting = db.get(SystemSetting, "ai_backends")
        assert system_setting is not None
        system_setting.value = json.dumps(
            [
                {
                    "id": "dynamic-ollama-primary",
                    "type": "ollama",
                    "url": "http://dynamic-ollama-primary.invalid",
                    "model": "llava:latest",
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "id": "dynamic-ollama-secondary",
                    "type": "ollama",
                    "url": "http://dynamic-ollama-secondary.invalid",
                    "model": "llava:latest",
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "id": "dynamic-gemini-fallback",
                    "type": "gemini",
                    "url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-1.5-flash",
                    "api_key": "dynamic-fallback-key",
                    "weight": 1,
                    "enabled": True,
                },
            ]
        )
        fallback_setting = db.get(SystemSetting, "ai_enable_dynamic_fallback")
        assert fallback_setting is not None
        fallback_setting.value = "true"
        db.add(fallback_setting)
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
        assert tenant is not None
        tenant.settings_json = None
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/dynamic-fallback-photo.jpg",
            original_file_name="dynamic-fallback-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="pending",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        photo_id = photo.id

    call_order: list[str] = []

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None, **kwargs):
        assert timeout in {60, 180}
        call_order.append(url)
        if "dynamic-ollama-primary.invalid" in url:
            raise RequestException("CUDA out of memory while loading model")
        if "dynamic-ollama-secondary.invalid" in url:
            return MockResponse({"response": '{"ai_summary":"Secondary ollama should be skipped","labels":["secondary"],"defects":[]}'})
        return MockResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"ai_summary":"Gemini dynamic fallback","labels":["fallback","inspection"],"defects":[]}'
                                }
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.ai_pipeline.requests.post", fake_post)

    process_photo_with_ai(photo_id, session_maker, settings)

    vision_calls = [item for item in call_order if "/api/generate" in item or ":generateContent" in item]
    assert vision_calls[0].startswith("http://dynamic-ollama-primary.invalid")
    assert "generativelanguage.googleapis.com" in vision_calls[1]
    # Only the primary visual inference path should skip the secondary Ollama backend.
    assert not any("dynamic-ollama-secondary.invalid" in item for item in vision_calls[:2])

    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.labeling_status == "completed"
        assert "Gemini dynamic fallback" in photo.tag_json["ai_summary"]


def test_ai_healthcheck_reports_enabled_ollama_backends(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        system_setting = db.get(SystemSetting, "ai_backends")
        assert system_setting is not None
        system_setting.value = json.dumps(
            [
                {
                    "id": "healthcheck-ollama",
                    "type": "ollama",
                    "url": "http://healthcheck-ollama.invalid",
                    "model": "llava:latest",
                    "weight": 1,
                    "enabled": True,
                }
            ]
        )
        db.add(system_setting)
        db.commit()

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, timeout=None, **kwargs):
        assert timeout == settings.ai_healthcheck_timeout_seconds
        assert url.endswith("/api/tags")
        return MockResponse({"models": [{"name": "llava:latest"}]})

    monkeypatch.setattr("app.services.ai_pipeline.requests.get", fake_get)

    summary = perform_ai_healthcheck(session_maker, settings)
    assert summary["checked_backends"] == 1
    assert summary["healthy_backends"] == 1
    assert summary["failed_backends"] == 0


def _create_photo_file(settings, filename: str) -> Path:
    file_path = settings.photos_root / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"\xff\xd8\xff\xe0service-test-image")
    return file_path


def test_ai_backend_resolution_prefers_tenant_settings(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
        assert tenant is not None
        tenant.settings_json = {
            "ai_backends": [
                {
                    "id": "tenant-gemini",
                    "type": "gemini",
                    "url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-1.5-flash",
                    "api_key": "tenant-key",
                    "weight": 2,
                    "enabled": True,
                }
            ]
        }
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="C:/tmp/tenant.jpg",
            storage_path="C:/tmp/tenant.jpg",
            image_url="/media/default/P100/E100/tenant.jpg",
            original_file_name="tenant.jpg",
            captured_at_utc=datetime.now(timezone.utc),
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)

        backends, source = resolve_ai_backends(db, settings, photo)
        assert source == "tenant"
        assert [backend.id for backend in backends] == ["tenant-gemini"]


def test_process_photo_with_ai_falls_back_between_backends(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    photo_path = _create_photo_file(settings, "fallback-photo.jpg")

    with session_maker() as db:
        system_setting = db.get(SystemSetting, "ai_backends")
        assert system_setting is not None
        system_setting.value = json.dumps(
            [
                {
                    "id": "ollama-primary",
                    "type": "ollama",
                    "url": "http://ollama.invalid",
                    "model": "llava:latest",
                    "weight": 2,
                    "enabled": True,
                },
                {
                    "id": "gemini-fallback",
                    "type": "gemini",
                    "url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-1.5-flash",
                    "api_key": "fallback-key",
                    "weight": 1,
                    "enabled": True,
                },
            ]
        )
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
        assert tenant is not None
        tenant.settings_json = None
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/fallback-photo.jpg",
            original_file_name="fallback-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="pending",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        photo_id = photo.id

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None, **kwargs):
        assert timeout in {60, 180}
        if "ollama.invalid" in url:
            raise RequestException("ollama offline")
        return MockResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"ai_summary":"Fallback success","labels":["project","inspection"],"defects":[]}'
                                }
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.ai_pipeline.requests.post", fake_post)

    process_photo_with_ai(photo_id, session_maker, settings)

    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.labeling_status == "completed"
        assert "Fallback success" in photo.tag_json["ai_summary"]
        assert isinstance(photo.tag_json["labels"], list)
        assert photo.tag_json["defects"] == []
        assert "ai_summary_translations" in photo.tag_json
        audit_entry = db.scalar(
            select(AuditLog).where(AuditLog.target_type == "photo", AuditLog.target_id == str(photo_id), AuditLog.action == "photo_labeling_completed")
        )
        assert audit_entry is not None
        assert audit_entry.tenant_id == "default"
        assert audit_entry.company_id == "default"
        assert audit_entry.project_id == "P100"
        assert audit_entry.detail_json["backend_id"] == "gemini-fallback"
        assert len(audit_entry.detail_json["attempts"]) == 2


def test_process_photo_with_ai_marks_failed_after_backend_exhaustion(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    photo_path = _create_photo_file(settings, "failed-photo.jpg")

    with session_maker() as db:
        system_setting = db.get(SystemSetting, "ai_backends")
        assert system_setting is not None
        system_setting.value = json.dumps(
            [
                {
                    "id": "ollama-primary",
                    "type": "ollama",
                    "url": "http://ollama.invalid",
                    "model": "llava:latest",
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "id": "gemini-fallback",
                    "type": "gemini",
                    "url": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-1.5-flash",
                    "api_key": "fallback-key",
                    "weight": 1,
                    "enabled": True,
                },
            ]
        )
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
        assert tenant is not None
        tenant.settings_json = None
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/failed-photo.jpg",
            original_file_name="failed-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="pending",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        photo_id = photo.id

    def fake_post(url, json=None, timeout=None, **kwargs):
        raise RequestException("backend unavailable")

    monkeypatch.setattr("app.services.ai_pipeline.requests.post", fake_post)

    process_photo_with_ai(photo_id, session_maker, settings)

    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.labeling_status == "failed"
        assert photo.tag_json["error"]["code"] == "ai_processing_failed"
        assert len(photo.tag_json["error"]["attempts"]) == 2
        audit_entry = db.scalar(
            select(AuditLog).where(AuditLog.target_type == "photo", AuditLog.target_id == str(photo_id), AuditLog.action == "photo_labeling_failed")
        )
        assert audit_entry is not None
        assert audit_entry.tenant_id == "default"
        assert audit_entry.company_id == "default"
        assert audit_entry.project_id == "P100"


def test_ai_analysis_logs_append_only_and_snapshot_rebuild(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    photo_path = _create_photo_file(settings, "analysis-history-photo.jpg")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/analysis-history-photo.jpg",
            original_file_name="analysis-history-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="pending",
            note="Initial note context",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        photo_id = photo.id

    results = iter(
        [
            {"ai_summary": "First summary", "labels": ["project"], "defects": []},
            {"ai_summary": "Second summary", "labels": ["equipment"], "defects": ["Loose ladder"]},
        ]
    )

    def fake_call_ai_backend(*args, **kwargs):
        return next(results)

    monkeypatch.setattr("app.services.ai_pipeline.call_ai_backend", fake_call_ai_backend)

    process_photo_with_ai(photo_id, session_maker, settings)
    process_photo_with_ai(
        photo_id,
        session_maker,
        settings,
        custom_prompt="Perform a deeper review for manager handoff.",
        trigger_source="portal_single",
    )

    with session_maker() as db:
        logs = list(db.scalars(select(AIAnalysisLog).where(AIAnalysisLog.photo_id == photo_id).order_by(AIAnalysisLog.created_at.asc())))
        assert len(logs) == 2
        assert logs[0].analysis_type == AIAnalysisType.fast_screen
        assert logs[1].analysis_type == AIAnalysisType.deep_analysis
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert "Second summary" in photo.tag_json["ai_summary"]
        assert isinstance(photo.tag_json["labels"], list)
        assert photo.tag_json["defects"] == ["Loose ladder"]
        latest_log = logs[-1]
        set_ai_analysis_log_status(
            db,
            app_settings=settings,
            analysis_log=latest_log,
            status=AIAnalysisStatus.rejected,
            actor_user_id=1,
            source="test_reject",
        )
        db.commit()

    with session_maker() as db:
        logs = list(db.scalars(select(AIAnalysisLog).where(AIAnalysisLog.photo_id == photo_id).order_by(AIAnalysisLog.created_at.asc())))
        assert logs[-1].status == AIAnalysisStatus.rejected
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert "First summary" in photo.tag_json["ai_summary"]
        assert isinstance(photo.tag_json["labels"], list)
        assert photo.tag_json["defects"] == []


def test_task_queue_retries_and_dead_letters_failed_photo_ai_jobs(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.queue_retry_delay_seconds = 0
    settings.queue_max_attempts = 2
    photo_path = _create_photo_file(settings, "queued-fail-photo.jpg")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/queued-fail-photo.jpg",
            original_file_name="queued-fail-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="pending",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        enqueue_photo_ai_task(
            db,
            app_settings=settings,
            photo=photo,
            actor_user_id=None,
            custom_prompt=None,
            trigger_source="test_queue",
            priority="normal",
        )
        db.commit()
        photo_id = photo.id

    def fake_post(url, json=None, timeout=None, **kwargs):
        raise RequestException("backend unavailable")

    monkeypatch.setattr("app.services.ai_pipeline.requests.post", fake_post)

    assert process_due_jobs(session_maker, settings, max_jobs=1, worker_name="test-worker") == 1
    assert process_due_jobs(session_maker, settings, max_jobs=1, worker_name="test-worker") == 1

    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.labeling_status == "failed"
        job = db.scalar(select(TaskJob).where(TaskJob.related_type == "photo", TaskJob.related_id == str(photo_id)))
        assert job is not None
        assert job.status == TaskStatus.dead_letter
        assert job.attempt_count == 2
        dead_letter_audit = db.scalar(select(AuditLog).where(AuditLog.action == "task_job_dead_lettered"))
        assert dead_letter_audit is not None


def test_schedule_job_worker_can_be_disabled(app_context):
    settings = app_context["settings"]
    scheduled_calls: list[tuple] = []

    def fake_schedule_task(*args):
        scheduled_calls.append(args)

    settings.queue_inline_request_worker_enabled = False
    schedule_job_worker(fake_schedule_task, app_context["session_maker"], settings)
    assert scheduled_calls == []

    settings.queue_inline_request_worker_enabled = True
    schedule_job_worker(fake_schedule_task, app_context["session_maker"], settings)
    assert len(scheduled_calls) == 1


def test_recover_stale_running_photo_jobs_marks_retryable_work_failed_and_requeues(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.queue_running_timeout_seconds = 60
    settings.queue_retry_delay_seconds = 0
    photo_path = _create_photo_file(settings, "stale-running-photo.jpg")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/stale-running-photo.jpg",
            original_file_name="stale-running-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="processing",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        job = TaskJob(
            public_id="stale-photo-job-1",
            tenant_id="default",
            company_id="default",
            task_type="photo_ai_pipeline",
            status=TaskStatus.running,
            priority=100,
            attempt_count=1,
            max_attempts=3,
            available_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc) - timedelta(minutes=90),
            locked_by="worker-old",
            payload_json={"photo_id": photo.id},
            related_type="photo",
            related_id=str(photo.id),
        )
        db.add(job)
        db.commit()

    recovered = recover_stale_running_jobs(session_maker, settings, worker_name="test-recovery")
    assert recovered == 1

    with session_maker() as db:
        job = db.scalar(select(TaskJob).where(TaskJob.public_id == "stale-photo-job-1"))
        assert job is not None
        assert job.status == TaskStatus.failed
        assert job.locked_by is None
        assert "Recovered stale running job" in (job.last_error or "")
        photo = db.get(Photo, int(job.related_id))
        assert photo is not None
        assert photo.labeling_status == "pending"
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "task_job_stale_recovered"))
        assert audit is not None


def test_recover_stale_running_progress_reports_dead_letters_last_attempt_and_marks_failed(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.queue_running_timeout_seconds = 60

    with session_maker() as db:
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        report = ProgressReport(
            id="stale-progress-report-1",
            tenant_id="default",
            company_id="default",
            created_by_user_id=manager.id,
            project_id="P100",
            status=ProgressReportStatus.processing,
            source_photo_ids=[1, 2],
        )
        db.add(report)
        job = TaskJob(
            public_id="stale-progress-job-1",
            tenant_id="default",
            company_id="default",
            created_by_user_id=manager.id,
            task_type="progress_compare_generation",
            status=TaskStatus.running,
            priority=120,
            attempt_count=3,
            max_attempts=3,
            available_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            locked_by="worker-old",
            payload_json={"report_id": report.id},
            related_type="progress_report",
            related_id=report.id,
        )
        db.add(job)
        db.commit()

    recovered = recover_stale_running_jobs(session_maker, settings, worker_name="test-recovery")
    assert recovered == 1

    with session_maker() as db:
        job = db.scalar(select(TaskJob).where(TaskJob.public_id == "stale-progress-job-1"))
        assert job is not None
        assert job.status == TaskStatus.dead_letter
        report = db.get(ProgressReport, "stale-progress-report-1")
        assert report is not None
        assert report.status == ProgressReportStatus.failed
        assert "Recovered stale running job" in (report.error_message or "")
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "task_job_stale_dead_lettered"))
        assert audit is not None
