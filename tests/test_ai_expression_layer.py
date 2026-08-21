from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from app.core.time import utc_now
from app.services.expression_roadmap import (
    EXPRESSION_ARTIFACT_IMPLEMENTATION,
    EXPRESSION_FUTURE_ROADMAP_TARGETS,
    ROADMAP_SCHEMA_VERSION,
    build_expression_target_roadmap_gap_audit,
)
from app.cli import (
    EXPRESSION_REGISTRY_EXPECTATIONS,
    GENERATED_REPORT_MARKDOWN_LEGACY_USER_PROMPT_TEMPLATE,
    LEGACY_AI_PROMPT_SURFACE_EXPECTATIONS,
    PHOTO_FIELD_ANALYSIS_USER_PROMPT_TEMPLATE,
    PHOTO_INVOICE_ANALYSIS_USER_PROMPT_TEMPLATE,
    PROGRESS_REPORT_MULTI_IMAGE_USER_PROMPT_TEMPLATE,
    VIDEO_FRAME_INSIGHT_USER_PROMPT_TEMPLATE,
    apply_finance_fact_attribution_payload,
    audit_finance_fact_attribution_payload,
    audit_finance_fact_health_payload,
    audit_executive_company_health_shadow,
    audit_expression_audience_coverage,
    build_ai_prompt_surface_coverage_payload,
    build_expression_artifact_promotion_preflight_payload,
    build_generated_report_markdown_legacy_prompt_parity_payload,
    build_photo_field_analysis_prompt_parity_payload,
    build_photo_invoice_analysis_prompt_parity_payload,
    build_progress_report_multi_image_prompt_parity_payload,
    build_video_frame_insight_prompt_parity_payload,
    build_client_progress_summary_promotion_preflight_payload,
    build_executive_company_health_promotion_preflight_payload,
    build_expression_contract_matrix_payload,
    build_finance_summary_promotion_preflight_payload,
    build_operations_health_summary_promotion_preflight_payload,
    build_project_manager_status_card_contract_payload,
    build_project_manager_status_card_promotion_preflight_payload,
    build_expression_registry_bootstrap_payload,
    build_expression_prompt_catalog_readiness_payload,
    build_expression_promotion_surface_readiness_payload,
    build_expression_promotion_readiness_payload,
    build_expression_visibility_fact_boundary_payload,
    build_mobile_promoted_read_contracts_payload,
    expression_audit_project_manager_status_card_contract,
    expression_audit_mobile_promoted_read_contracts,
    expression_audit_project_manager_status_card_promotion,
    expression_audit_artifact_promotion,
    expression_audit_client_progress_summary_promotion,
    expression_audit_executive_company_health_promotion,
    expression_audit_finance_summary_promotion,
    expression_audit_operations_health_summary_promotion,
    expression_audit_promotion_readiness,
    expression_audit_layer_readiness,
    expression_audit_eval_readiness,
    expression_audit_prompt_catalog,
    expression_audit_ai_prompt_surface_coverage,
    expression_audit_generated_report_markdown_legacy_prompt_parity,
    expression_audit_photo_field_analysis_prompt_parity,
    expression_audit_photo_invoice_analysis_prompt_parity,
    expression_audit_progress_report_multi_image_prompt_parity,
    expression_audit_video_frame_insight_prompt_parity,
    expression_audit_contract_matrix,
    expression_audit_visibility_fact_boundary,
    expression_golden_replay,
    expression_revalidate_artifacts,
    expression_audit_promotion_surfaces,
    expression_promote_project_manager_status_card,
    expression_bootstrap_registry,
    audit_expression_registry,
    audit_expression_target_roadmap,
    expression_audit_production_gate,
    rollback_finance_fact_attribution_payload,
)
from app.models import (
    ApprovalStatus,
    AIAnalysisLog,
    AIAnalysisStatus,
    AIAnalysisType,
    ExpressionArtifact,
    ExpressionAudience,
    ExpressionEvalItem,
    ExpressionEvalRun,
    ExpressionForbiddenPhrase,
    ExpressionOutputContract,
    ExpressionPromptBinding,
    ExpressionPromptTemplate,
    ExpressionPromptVersion,
    FactSnapshot,
    Photo,
    PhotoType,
    PhotoVisibility,
    ProgressReport,
    ProgressReportStatus,
    Project,
    ReceiptFact,
    TaskJob,
    TaskStatus,
)
from app.services.ai_pipeline import AIBackendNode, OLLAMA_TYPE
from app.services.expression import (
    DEFAULT_EXPRESSION_TEXT_MODEL,
    FIXED_EMPLOYEE_DISCLAIMER,
    CLIENT_PROGRESS_DISCLAIMER,
    PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION,
    PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION,
    _expression_backend_candidates,
    _format_prompt,
    _progress_report_translatable_strings,
    _surface_immutability_errors,
    _surface_summary_polished_payload,
    _validate_expression_payload,
    build_client_progress_summary_facts,
    build_employee_contribution_fallback,
    build_executive_company_health_facts,
    build_finance_summary_facts,
    build_operations_health_summary_facts,
    build_project_manager_decision_brief_fallback,
    generate_client_progress_summary_shadow,
    generate_employee_contribution_shadow,
    generate_executive_company_health_shadow,
    generate_finance_summary_shadow,
    generate_operations_health_summary_shadow,
    generate_project_manager_decision_brief_shadow,
    generate_progress_report_translation_shadow,
    generate_progress_report_string_translation_shadow,
    generate_report_markdown_shadow,
    generate_report_markdown_section_shadow,
    _markdown_section_spec_for_key,
    promote_expression_artifact,
    summarize_project_manager_decision_brief_errors,
)

def seed_photo_field_analysis_prompt(db) -> None:
    if db.get(ExpressionPromptTemplate, "photo_field_analysis") is None:
        db.add(
            ExpressionPromptTemplate(
                id="photo_field_analysis",
                slug="photo_field_analysis",
                title="Photo field analysis prompt",
                artifact_type="photo_field_analysis",
                stage="vision",
                default_audience_id=None,
                description="Shadow registry copy of the legacy project-photo field evidence prompt.",
            )
        )
    if db.get(ExpressionPromptVersion, "photo_field_analysis:v1") is None:
        db.add(
            ExpressionPromptVersion(
                id="photo_field_analysis:v1",
                template_id="photo_field_analysis",
                version="v1",
                system_prompt="Legacy project-photo field analysis prompt registry shadow.",
                user_prompt_template=PHOTO_FIELD_ANALYSIS_USER_PROMPT_TEMPLATE,
                status="active",
                notes=(
                    "Shadow-only parity seed. Runtime photo AI still uses "
                    "app.services.ai_pipeline.build_ai_prompt_for_context."
                ),
            )
        )
    if db.get(ExpressionPromptBinding, "global:photo_field_analysis:v1") is None:
        db.add(
            ExpressionPromptBinding(
                id="global:photo_field_analysis:v1",
                scope_type="global",
                scope_id=None,
                template_id="photo_field_analysis",
                prompt_version_id="photo_field_analysis:v1",
                priority=100,
            )
        )


def seed_photo_invoice_analysis_prompt(db) -> None:
    if db.get(ExpressionPromptTemplate, "photo_invoice_analysis") is None:
        db.add(
            ExpressionPromptTemplate(
                id="photo_invoice_analysis",
                slug="photo_invoice_analysis",
                title="Photo invoice analysis prompt",
                artifact_type="photo_invoice_analysis",
                stage="vision_finance",
                default_audience_id=None,
                description="Shadow registry copy of the legacy receipt and invoice photo prompt.",
            )
        )
    if db.get(ExpressionPromptVersion, "photo_invoice_analysis:v1") is None:
        db.add(
            ExpressionPromptVersion(
                id="photo_invoice_analysis:v1",
                template_id="photo_invoice_analysis",
                version="v1",
                system_prompt="Legacy receipt and invoice photo analysis prompt registry shadow.",
                user_prompt_template=PHOTO_INVOICE_ANALYSIS_USER_PROMPT_TEMPLATE,
                status="active",
                notes=(
                    "Shadow-only parity seed. Runtime invoice AI still uses "
                    "app.services.ai_pipeline.build_ai_prompt_for_context."
                ),
            )
        )
    if db.get(ExpressionPromptBinding, "global:photo_invoice_analysis:v1") is None:
        db.add(
            ExpressionPromptBinding(
                id="global:photo_invoice_analysis:v1",
                scope_type="global",
                scope_id=None,
                template_id="photo_invoice_analysis",
                prompt_version_id="photo_invoice_analysis:v1",
                priority=100,
            )
        )


def seed_video_frame_insight_prompt(db) -> None:
    if db.get(ExpressionPromptTemplate, "video_frame_insight") is None:
        db.add(
            ExpressionPromptTemplate(
                id="video_frame_insight",
                slug="video_frame_insight",
                title="Video frame insight prompt",
                artifact_type="video_frame_insight",
                stage="vision_video",
                default_audience_id=None,
                description="Shadow registry copy of the legacy video-frame project evidence prompt.",
            )
        )
    if db.get(ExpressionPromptVersion, "video_frame_insight:v1") is None:
        db.add(
            ExpressionPromptVersion(
                id="video_frame_insight:v1",
                template_id="video_frame_insight",
                version="v1",
                system_prompt="Legacy video-frame insight prompt registry shadow.",
                user_prompt_template=VIDEO_FRAME_INSIGHT_USER_PROMPT_TEMPLATE,
                status="active",
                notes=(
                    "Shadow-only parity seed. Runtime video AI still uses "
                    "app.services.ai_pipeline.build_media_asset_ai_prompt."
                ),
            )
        )
    if db.get(ExpressionPromptBinding, "global:video_frame_insight:v1") is None:
        db.add(
            ExpressionPromptBinding(
                id="global:video_frame_insight:v1",
                scope_type="global",
                scope_id=None,
                template_id="video_frame_insight",
                prompt_version_id="video_frame_insight:v1",
                priority=100,
            )
        )


def seed_progress_report_multi_image_prompt(db) -> None:
    if db.get(ExpressionPromptTemplate, "progress_report_multi_image") is None:
        db.add(
            ExpressionPromptTemplate(
                id="progress_report_multi_image",
                slug="progress_report_multi_image",
                title="Progress report multi-image prompt",
                artifact_type="progress_report_generation",
                stage="vision_report",
                default_audience_id="project_manager",
                description="Shadow registry copy of the legacy multi-image progress report prompt.",
            )
        )
    if db.get(ExpressionPromptVersion, "progress_report_multi_image:v1") is None:
        db.add(
            ExpressionPromptVersion(
                id="progress_report_multi_image:v1",
                template_id="progress_report_multi_image",
                version="v1",
                system_prompt="Legacy multi-image progress report prompt registry shadow.",
                user_prompt_template=PROGRESS_REPORT_MULTI_IMAGE_USER_PROMPT_TEMPLATE,
                status="active",
                notes=(
                    "Shadow-only parity seed. Runtime progress reports still use "
                    "app.services.reports.build_multi_image_progress_prompt."
                ),
            )
        )
    if db.get(ExpressionPromptBinding, "global:progress_report_multi_image:v1") is None:
        db.add(
            ExpressionPromptBinding(
                id="global:progress_report_multi_image:v1",
                scope_type="global",
                scope_id=None,
                template_id="progress_report_multi_image",
                prompt_version_id="progress_report_multi_image:v1",
                priority=100,
            )
        )


def seed_generated_report_markdown_legacy_prompt(db) -> None:
    if db.get(ExpressionPromptTemplate, "generated_report_markdown_legacy") is None:
        db.add(
            ExpressionPromptTemplate(
                id="generated_report_markdown_legacy",
                slug="generated_report_markdown_legacy",
                title="Generated report markdown legacy prompt",
                artifact_type="generated_report_markdown_legacy",
                stage="render_legacy_pdf",
                default_audience_id="project_manager",
                description="Shadow registry copy of the legacy generated_reports Markdown PDF prompt.",
            )
        )
    if db.get(ExpressionPromptVersion, "generated_report_markdown_legacy:v1") is None:
        db.add(
            ExpressionPromptVersion(
                id="generated_report_markdown_legacy:v1",
                template_id="generated_report_markdown_legacy",
                version="v1",
                system_prompt="Legacy generated report Markdown prompt registry shadow.",
                user_prompt_template=GENERATED_REPORT_MARKDOWN_LEGACY_USER_PROMPT_TEMPLATE,
                status="active",
                notes=(
                    "Shadow-only parity seed. Runtime generated report PDFs still use "
                    "app.services.reports.build_report_markdown_prompt."
                ),
            )
        )
    if db.get(ExpressionPromptBinding, "global:generated_report_markdown_legacy:v1") is None:
        db.add(
            ExpressionPromptBinding(
                id="global:generated_report_markdown_legacy:v1",
                scope_type="global",
                scope_id=None,
                template_id="generated_report_markdown_legacy",
                prompt_version_id="generated_report_markdown_legacy:v1",
                priority=100,
            )
        )


def test_expression_layer_models_accept_shadow_artifact(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            visibility_policy_json={"show_to": ["self"]},
            forbidden_phrase_set_id="employee_contribution_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="employee_contribution_narrative",
            slug="employee_contribution_narrative",
            title="Employee contribution narrative",
            artifact_type="employee_contribution_narrative",
            stage="expression",
            default_audience_id="employee",
        )
        version = ExpressionPromptVersion(
            id="employee_contribution_narrative:v1",
            template_id="employee_contribution_narrative",
            version="v1",
            system_prompt="只根据 facts 写中文。",
            user_prompt_template="{facts_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:employee_contribution_narrative:v1",
            scope_type="global",
            scope_id=None,
            template_id="employee_contribution_narrative",
            prompt_version_id="employee_contribution_narrative:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="employee_contribution_narrative:employee:v1",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["summary_line"],
                "properties": {"summary_line": {"type": "string"}},
            },
            required_fact_paths_json=["scope.employee_id", "scope.project_id"],
            forbidden_claims_json=["no ranking"],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="employee_contribution_zh_v1:performance",
            set_id="employee_contribution_zh_v1",
            audience_id="employee",
            language="zh",
            phrase="绩效",
            match_type="literal",
            severity="error",
            is_active=True,
        )
        db.add_all([audience, template, version, binding, contract, phrase])
        db.flush()

        snapshot = FactSnapshot(
            id="snapshot-000000000000000000000001",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            scope_type="employee_project_window",
            scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
            scope_key_hash="hash-e100-p100-30d",
            assembler_version="v1",
            source_manifest_json={"tables": ["photos", "evidence_observations"]},
            facts_json={
                "scope": {"employee_id": "E100", "project_id": "P100"},
                "window": {"current": {"days": 30}},
                "counts": {"photos_current": 6, "completed_ai_current": 6},
            },
            fact_count=4,
            coverage_json={"has_recent_photos": True},
            created_at=utc_now(),
        )
        artifact = ExpressionArtifact(
            id="artifact-000000000000000000000001",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=version.id,
            contract_id=contract.id,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "最近 30 天有连续的现场记录。"},
            raw_model_output='{"summary_line":"最近 30 天有连续的现场记录。"}',
            rendered_markdown="最近 30 天有连续的现场记录。",
            validation_status="shadow_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "ollama-qwen-shadow"},
            promoted=False,
        )
        db.add_all([snapshot, artifact])
        db.commit()

    with session_maker() as db:
        saved_artifact = db.scalar(select(ExpressionArtifact).where(ExpressionArtifact.id == "artifact-000000000000000000000001"))
        assert saved_artifact is not None
        assert saved_artifact.promoted is False
        assert saved_artifact.structured_json["summary_line"].startswith("最近 30 天")
        saved_contract = db.scalar(select(ExpressionOutputContract).where(ExpressionOutputContract.audience_id == "employee"))
        assert saved_contract is not None
        assert saved_contract.json_schema["required"] == ["summary_line"]


def test_expression_contract_validation_enforces_schema_facts_and_claims(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            forbidden_phrase_set_id="empty-test-set",
            is_active=True,
        )
        contract = ExpressionOutputContract(
            id="employee_contribution_narrative:employee:strict-test",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            version="strict-test",
            json_schema={
                "type": "object",
                "required": ["summary_line", "tone", "recent_highlights"],
                "additionalProperties": False,
                "properties": {
                    "summary_line": {"type": "string"},
                    "tone": {"type": "string", "enum": ["neutral"]},
                    "comparison_text": {"type": "string"},
                    "contribution_explanation": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                    "recent_highlights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["photo_id", "text"],
                            "additionalProperties": False,
                            "properties": {
                                "photo_id": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                    "disclaimer": {"type": "string", "const": FIXED_EMPLOYEE_DISCLAIMER},
                },
            },
            required_fact_paths_json=["scope.employee_id", "counts.photos_current"],
            forbidden_claims_json=[{"phrase": "绩效评分"}],
            is_active=True,
        )
        db.add_all([audience, contract])
        db.commit()

        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload={
                "summary_line": "最近记录窗口内共有 1 张现场照片。",
                "tone": "praise",
                "comparison_text": "与上一记录窗口相比，本窗口的现场照片数量基本持平。",
                "contribution_explanation": ["这些照片用于现场回看。"],
                "strengths": [],
                "suggestions": [],
                "recent_highlights": [{"photo_id": "1", "text": "这是一条绩效评分。", "extra": True}],
                "disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
            },
            language="zh",
            facts={"scope": {"project_id": "P100"}},
        )

    assert "enum:tone" in errors
    assert "type:recent_highlights[0].photo_id" in errors
    assert "unexpected_property:recent_highlights[0].extra" in errors
    assert "missing_fact_path:scope.employee_id" in errors
    assert "missing_fact_path:counts.photos_current" in errors
    assert "forbidden_claim:绩效评分" in errors


def test_expression_prompt_template_allows_literal_json_examples():
    version = ExpressionPromptVersion(
        id="progress_report_string_translation:v2",
        template_id="progress_report_string_translation",
        version="v2",
        system_prompt="Translate one field.",
        user_prompt_template='Example JSON: {{"zh":"粗装施工已有进展。","en":"Rough-in progressed.","es":"La instalación preliminar avanzó."}}\n{facts_json}\n{contract_schema_json}',
        status="active",
    )
    contract = ExpressionOutputContract(
        id="progress_report_string_translation:project_manager:v2-test",
        artifact_type="progress_report_string_translation",
        audience_id="project_manager",
        version="v2-test",
        json_schema={"type": "object"},
        is_active=True,
    )
    prompt = _format_prompt(
        version,
        facts={"field_label": "progress_signal", "source_text": "Rough-in progressed."},
        contract=contract,
    )
    assert '{"zh":"粗装施工已有进展。","en":"Rough-in progressed.","es":"La instalación preliminar avanzó."}' in prompt
    assert '"source_text": "Rough-in progressed."' in prompt


def test_progress_report_translation_skips_low_information_signal_tokens():
    fields = _progress_report_translatable_strings(
        {
            "executive_summary": "The work area changed.",
            "manager_brief": "Review the next step.",
            "timeline_observations": [
                {
                    "observation": "Machine installation is visible.",
                    "progress_signal": "1",
                    "risk_signal": "none",
                },
                {
                    "observation": "Follow-up photo shows the same machine.",
                    "progress_signal": "Rough-in progressed.",
                    "risk_signal": "n/a",
                },
            ],
        }
    )
    field_paths = [field_path for field_path, _ in fields]
    assert "timeline_observations.0.progress_signal" not in field_paths
    assert "timeline_observations.0.risk_signal" not in field_paths
    assert "timeline_observations.1.risk_signal" not in field_paths
    assert "timeline_observations.1.progress_signal" in field_paths


def test_project_manager_decision_brief_error_summary_groups_shadow_reasons():
    summary = summarize_project_manager_decision_brief_errors(
        [
            "decision_brief:paragraphs[0]:imperative_language",
            "forbidden_claim:\\bpriority\\b",
            "decision_brief:paragraphs[1]:ai_polish_unknown_numbers:65",
            "decision_brief:paragraphs[2]:ai_polish_low_grounding",
            "backend:ollama-main:timeout",
            "project_manager_polish:body.paragraphs:empty",
            "schema:required",
            "unexpected_validator_error",
        ]
    )

    assert summary == {
        "forbidden_language": 2,
        "unknown_numbers": 1,
        "low_grounding": 1,
        "backend_or_json": 2,
        "schema_or_contract": 1,
        "other": 1,
    }


def test_employee_contribution_shadow_generation_uses_database_facts(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            visibility_policy_json={"show_to": ["self"]},
            forbidden_phrase_set_id="employee_contribution_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="employee_contribution_narrative",
            slug="employee_contribution_narrative",
            title="Employee contribution narrative",
            artifact_type="employee_contribution_narrative",
            stage="expression",
            default_audience_id="employee",
        )
        version = ExpressionPromptVersion(
            id="employee_contribution_narrative:v1",
            template_id="employee_contribution_narrative",
            version="v1",
            system_prompt="只根据 facts 写中文。",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:employee_contribution_narrative:v1",
            scope_type="global",
            scope_id=None,
            template_id="employee_contribution_narrative",
            prompt_version_id="employee_contribution_narrative:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="employee_contribution_narrative:employee:v1",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            version="v1",
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "summary_line",
                    "contribution_explanation",
                    "strengths",
                    "suggestions",
                    "comparison_text",
                    "recent_highlights",
                    "disclaimer",
                ],
                "properties": {
                    "summary_line": {"type": "string"},
                    "contribution_explanation": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                    "comparison_text": {"type": "string"},
                    "recent_highlights": {"type": "array", "items": {"type": "object"}},
                    "disclaimer": {"type": "string", "const": FIXED_EMPLOYEE_DISCLAIMER},
                },
            },
            required_fact_paths_json=["scope.employee_id", "scope.project_id"],
            forbidden_claims_json=["no ranking"],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="employee_contribution_zh_v1:performance",
            set_id="employee_contribution_zh_v1",
            audience_id="employee",
            language="zh",
            phrase="绩效",
            match_type="literal",
            severity="error",
            is_active=True,
        )
        photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="/tmp/test.jpg",
            image_url="/media/photos/test.jpg",
            original_file_name="test.jpg",
            captured_at_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            labeling_status="completed",
            tag_json={
                "ai_summary": "Electrical conduit installation is visible near the wall.",
                "labels": ["electrical", "conduit"],
                "defects": [],
            },
        )
        db.add_all([audience, template, version, binding, contract, phrase, photo])
        db.flush()
        photo_id = photo.id
        db.commit()

    with session_maker() as db:
        result = generate_employee_contribution_shadow(
            db,
            app_settings=settings,
            company_id="default",
            employee_id="E100",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        snapshot = db.get(FactSnapshot, snapshot_id)
        artifact = db.get(ExpressionArtifact, artifact_id)
        assert snapshot is not None
        assert snapshot.facts_json["counts"]["photos_current"] == 1
        assert snapshot.facts_json["recent_highlights"][0]["photo_id"] == photo_id
        assert artifact is not None
        assert artifact.promoted is False
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.structured_json["disclaimer"] == FIXED_EMPLOYEE_DISCLAIMER
        assert artifact.validation_errors_json == []


def test_progress_report_translation_shadow_uses_db_report_payload(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    report_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["zh", "en", "es"],
        "properties": {
            language: {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "executive_summary",
                    "overall_status",
                    "confidence_level",
                    "manager_brief",
                    "timeline_observations",
                ],
                "properties": {
                    "executive_summary": {"type": "string"},
                    "overall_status": {"type": "string", "enum": ["on_track", "at_risk", "blocked", "unknown"]},
                    "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
                    "manager_brief": {"type": "string"},
                    "timeline_observations": {"type": "array"},
                },
            }
            for language in ("zh", "en", "es")
        },
    }
    structured_report = {
        "executive_summary": "Crew completed conduit rough-in and documented open punch items.",
        "overall_progress_percent": 42,
        "overall_status": "at_risk",
        "confidence_level": "medium",
        "manager_brief": "Review open safety and material constraints before the next shift.",
        "angle_bias_notes": [],
        "key_changes": ["Conduit rough-in visible in recent photos."],
        "work_completed": ["Electrical rough-in documented."],
        "work_remaining": ["Panel labeling still needs confirmation."],
        "safety_risks": ["Open trench requires verification."],
        "quality_risks": [],
        "material_inventory_signals": [],
        "water_housekeeping_signals": [],
        "uncertain_items": [],
        "evidence_limitations": ["Only selected photos were reviewed."],
        "immediate_decisions": ["Confirm trench cover plan."],
        "recommended_actions": ["Capture follow-up photos after cover placement."],
        "timeline_observations": [
            {
                "photo_id": None,
                "captured_at": "2026-06-15T12:00:00Z",
                "observation": "Conduit work is visible.",
                "progress_signal": "Rough-in progressed.",
                "risk_signal": "",
            }
        ],
    }

    with session_maker() as db:
        audience = ExpressionAudience(
            id="project_manager",
            code="project_manager",
            title="Project manager",
            default_language="zh",
            forbidden_phrase_set_id="manager_overview_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="progress_report_translation",
            slug="progress_report_translation",
            title="Progress report translation",
            artifact_type="progress_report_translation",
            stage="translate",
            default_audience_id="project_manager",
        )
        version = ExpressionPromptVersion(
            id="progress_report_translation:v1",
            template_id="progress_report_translation",
            version="v1",
            system_prompt="Translate strings only.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:progress_report_translation:v1",
            scope_type="global",
            scope_id=None,
            template_id="progress_report_translation",
            prompt_version_id="progress_report_translation:v1",
            priority=100,
        )
        string_template = ExpressionPromptTemplate(
            id="progress_report_string_translation",
            slug="progress_report_string_translation",
            title="Progress report string translation",
            artifact_type="progress_report_string_translation",
            stage="translate",
            default_audience_id="project_manager",
        )
        string_version = ExpressionPromptVersion(
            id="progress_report_string_translation:v1",
            template_id="progress_report_string_translation",
            version="v1",
            system_prompt="Translate one string only.",
            user_prompt_template="{field_label}\n{source_text}\n{facts_json}\n{contract_schema_json}",
            status="active",
        )
        string_binding = ExpressionPromptBinding(
            id="global:progress_report_string_translation:v1",
            scope_type="global",
            scope_id=None,
            template_id="progress_report_string_translation",
            prompt_version_id="progress_report_string_translation:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="progress_report_translation:project_manager:v1",
            artifact_type="progress_report_translation",
            audience_id="project_manager",
            version="v1",
            json_schema=report_schema,
            required_fact_paths_json=[
                "scope.report_id",
                "scope.project_id",
                "structured_report.executive_summary",
                "structured_report.manager_brief",
                "structured_report.timeline_observations",
            ],
            forbidden_claims_json=[],
            is_active=True,
        )
        string_contract = ExpressionOutputContract(
            id="progress_report_string_translation:project_manager:v1",
            artifact_type="progress_report_string_translation",
            audience_id="project_manager",
            version="v1",
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["zh", "en", "es"],
                "properties": {
                    "zh": {"type": "string"},
                    "en": {"type": "string"},
                    "es": {"type": "string"},
                },
            },
            required_fact_paths_json=["source_text", "field_label"],
            forbidden_claims_json=[],
            is_active=True,
        )
        report = ProgressReport(
            id="progress-report-translation-001",
            tenant_id="default",
            company_id="default",
            project_id="P100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content=json.dumps(structured_report),
            completed_at=utc_now(),
        )
        db.add_all(
            [
                audience,
                template,
                version,
                binding,
                string_template,
                string_version,
                string_binding,
                contract,
                string_contract,
                report,
            ]
        )
        db.commit()

    with session_maker() as db:
        result = generate_progress_report_translation_shadow(
            db,
            app_settings=settings,
            report_id="progress-report-translation-001",
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        assert artifact is not None
        assert artifact.artifact_type == "progress_report_translation"
        assert artifact.audience_id == "project_manager"
        assert artifact.scope_type == "progress_report"
        assert artifact.validation_status == "shadow_invalid"
        assert "language:zh:missing_cjk" in artifact.validation_errors_json
        assert artifact.structured_json["zh"]["overall_status"] == "at_risk"
        assert artifact.structured_json["en"]["manager_brief"].startswith("Review open safety")
        assert artifact.structured_json["es"]["timeline_observations"][0]["photo_id"] is None
        snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id)
        assert snapshot is not None
        assert snapshot.source_manifest_json["tables"] == ["progress_reports", "photos"]
        assert snapshot.facts_json["scope"]["report_id"] == "progress-report-translation-001"
        audience = db.get(ExpressionAudience, "project_manager")
        contract = db.get(ExpressionOutputContract, "progress_report_translation:project_manager:v1")
        assert audience is not None
        assert contract is not None
        bad_payload = dict(artifact.structured_json)
        bad_payload["zh"] = dict(bad_payload["zh"])
        bad_payload["zh"]["overall_status"] = "blocked"
        bad_payload["zh"]["overall_progress_percent"] = 99
        bad_payload["zh"]["timeline_observations"] = [dict(item) for item in bad_payload["zh"]["timeline_observations"]]
        bad_payload["zh"]["timeline_observations"][0]["photo_id"] = 999
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="multi",
            facts=snapshot.facts_json,
        )
        assert "preserve:zh.overall_status" in errors
        assert "preserve:zh.overall_progress_percent" in errors
        assert "preserve:zh.timeline_observations[0].photo_id" in errors
        bad_payload = dict(artifact.structured_json)
        bad_payload["es"] = dict(bad_payload["es"])
        bad_payload["es"]["executive_summary"] = "El roca blocks the work area."
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="multi",
            facts=snapshot.facts_json,
        )
        assert any(error.startswith("language:es:low_quality") for error in errors)
        string_contract = db.get(ExpressionOutputContract, "progress_report_string_translation:project_manager:v1")
        assert string_contract is not None
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=string_contract,
            payload={
                "zh": "The rock blocks the work area.",
                "en": "The rock blocks the work area.",
                "es": "El roca blocks the work area.",
            },
            language="multi",
            facts={"field_label": "executive_summary", "source_text": "The rock blocks the work area."},
        )
        assert "translation_chunk:zh:missing_cjk" in errors
        assert "translation_chunk:zh:untranslated" in errors
        assert any(error.startswith("translation_chunk:es:low_quality") for error in errors)

    with session_maker() as db:
        chunk_result = generate_progress_report_string_translation_shadow(
            db,
            app_settings=settings,
            report_id="progress-report-translation-001",
            field_path="manager_brief",
            use_ai=False,
        )
        db.commit()
        chunk_artifact_id = chunk_result.artifact.id

    with session_maker() as db:
        chunk_artifact = db.get(ExpressionArtifact, chunk_artifact_id)
        assert chunk_artifact is not None
        assert chunk_artifact.artifact_type == "progress_report_string_translation"
        assert chunk_artifact.validation_status == "shadow_fallback_valid"
        assert chunk_artifact.validation_errors_json == []
        assert chunk_artifact.scope_type == "progress_report_string"
        assert chunk_artifact.scope_key_json["field_label"] == "manager_brief"
        assert chunk_artifact.structured_json["zh"] == "该字段的机器翻译待复核。"
        assert chunk_artifact.structured_json["en"].startswith("Review open safety")
        assert chunk_artifact.structured_json["es"] == "La traducción de este campo está pendiente de revisión."
        chunk_snapshot = db.get(FactSnapshot, chunk_artifact.fact_snapshot_id)
        assert chunk_snapshot is not None
        assert len(PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION) <= 32
        assert chunk_snapshot.assembler_version == PROGRESS_REPORT_STRING_TRANSLATION_ASSEMBLER_VERSION
        assert chunk_snapshot.facts_json["field_label"] == "manager_brief"
        assert chunk_snapshot.facts_json["source_text"].startswith("Review open safety")
        assert chunk_artifact.backend_profile_json["strategy"] == "progress_report_string_translation_chunk"

    roadmap = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    assert roadmap["totals"]["product_risk"] == 0
    assert roadmap["registry_gaps"] == []


def test_generated_report_markdown_shadow_uses_db_report_payload(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    section_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["key", "heading", "markdown", "source_refs"],
        "properties": {
            "key": {"type": "string"},
            "heading": {"type": "string"},
            "markdown": {"type": "string"},
            "source_refs": {"type": "array", "items": {"type": "string"}},
            "generation_mode": {"type": "string", "enum": ["ai", "deterministic_fallback"]},
        },
    }
    markdown_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "summary", "metrics", "sections", "source_artifacts", "disclaimer"],
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "metrics": {
                "type": "object",
                "additionalProperties": False,
                "required": ["overall_progress_percent", "overall_status", "confidence_level"],
                "properties": {
                    "overall_progress_percent": {"type": ["integer", "null"]},
                    "overall_status": {"type": "string", "enum": ["on_track", "at_risk", "blocked", "unknown"]},
                    "confidence_level": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
            "sections": {"type": "array", "items": section_schema},
            "source_artifacts": {"type": "object"},
            "disclaimer": {"type": "string"},
        },
    }
    structured_report = {
        "executive_summary": "Crew completed conduit rough-in and documented open punch items.",
        "overall_progress_percent": 42,
        "overall_status": "at_risk",
        "confidence_level": "medium",
        "manager_brief": "Review open safety and material constraints before the next shift.",
        "angle_bias_notes": [],
        "key_changes": ["Conduit rough-in visible in recent photos."],
        "work_completed": ["Electrical rough-in documented."],
        "work_remaining": ["Panel labeling still needs confirmation."],
        "safety_risks": ["Open trench requires verification."],
        "quality_risks": [],
        "material_inventory_signals": [],
        "water_housekeeping_signals": [],
        "uncertain_items": [],
        "evidence_limitations": ["Only selected photos were reviewed."],
        "immediate_decisions": ["Confirm trench cover plan."],
        "recommended_actions": ["Capture follow-up photos after cover placement."],
        "timeline_observations": [
            {
                "photo_id": None,
                "captured_at": "2026-06-15T12:00:00Z",
                "observation": "Conduit work is visible.",
                "progress_signal": "Rough-in progressed.",
                "risk_signal": "",
            }
        ],
    }

    with session_maker() as db:
        audience = ExpressionAudience(
            id="project_manager",
            code="project_manager",
            title="Project manager",
            default_language="en",
            forbidden_phrase_set_id="manager_overview_en_v1",
            is_active=True,
        )
        markdown_template = ExpressionPromptTemplate(
            id="generated_report_markdown",
            slug="generated_report_markdown",
            title="Generated report markdown",
            artifact_type="generated_report_markdown",
            stage="render",
            default_audience_id="project_manager",
        )
        markdown_version = ExpressionPromptVersion(
            id="generated_report_markdown:v1",
            template_id="generated_report_markdown",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        markdown_binding = ExpressionPromptBinding(
            id="global:generated_report_markdown:v1",
            scope_type="global",
            scope_id=None,
            template_id="generated_report_markdown",
            prompt_version_id="generated_report_markdown:v1",
            priority=100,
        )
        markdown_contract = ExpressionOutputContract(
            id="generated_report_markdown:project_manager:v1",
            artifact_type="generated_report_markdown",
            audience_id="project_manager",
            version="v1",
            json_schema=markdown_schema,
            required_fact_paths_json=[
                "scope.report_id",
                "scope.project_id",
                "structured_report.executive_summary",
                "structured_report.manager_brief",
            ],
            forbidden_claims_json=[],
            is_active=True,
        )
        section_template = ExpressionPromptTemplate(
            id="generated_report_markdown_section",
            slug="generated_report_markdown_section",
            title="Generated report markdown section",
            artifact_type="generated_report_markdown_section",
            stage="render",
            default_audience_id="project_manager",
        )
        section_version = ExpressionPromptVersion(
            id="generated_report_markdown_section:v1",
            template_id="generated_report_markdown_section",
            version="v1",
            system_prompt="Write one section.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        section_binding = ExpressionPromptBinding(
            id="global:generated_report_markdown_section:v1",
            scope_type="global",
            scope_id=None,
            template_id="generated_report_markdown_section",
            prompt_version_id="generated_report_markdown_section:v1",
            priority=100,
        )
        section_contract = ExpressionOutputContract(
            id="generated_report_markdown_section:project_manager:v1",
            artifact_type="generated_report_markdown_section",
            audience_id="project_manager",
            version="v1",
            json_schema=section_schema,
            required_fact_paths_json=["section_key", "heading", "allowed_source_refs"],
            forbidden_claims_json=[],
            is_active=True,
        )
        report = ProgressReport(
            id="progress-report-markdown-001",
            tenant_id="default",
            company_id="default",
            project_id="P100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content=json.dumps(structured_report),
            completed_at=utc_now(),
        )
        db.add_all(
            [
                audience,
                markdown_template,
                markdown_version,
                markdown_binding,
                markdown_contract,
                section_template,
                section_version,
                section_binding,
                section_contract,
                report,
            ]
        )
        db.commit()

    with session_maker() as db:
        result = generate_report_markdown_shadow(
            db,
            app_settings=settings,
            report_id="progress-report-markdown-001",
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        assert artifact is not None
        assert artifact.artifact_type == "generated_report_markdown"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        assert len(artifact.structured_json["sections"]) == 5
        assert {section["key"] for section in artifact.structured_json["sections"]} == {
            "summary",
            "completed_work",
            "risks",
            "next_steps",
            "timeline",
        }
        assert artifact.structured_json["metrics"]["overall_status"] == "at_risk"
        assert "## Progress Summary" in artifact.rendered_markdown
        snapshot = db.get(FactSnapshot, artifact.fact_snapshot_id)
        assert snapshot is not None
        assert snapshot.source_manifest_json["tables"] == ["progress_reports", "photos", "expression_artifacts"]
        assert snapshot.facts_json["scope"]["report_id"] == "progress-report-markdown-001"
        audience = db.get(ExpressionAudience, "project_manager")
        contract = db.get(ExpressionOutputContract, "generated_report_markdown:project_manager:v1")
        assert audience is not None
        assert contract is not None
        bad_payload = dict(artifact.structured_json)
        bad_payload["sections"] = [dict(section) for section in bad_payload["sections"]]
        bad_payload["sections"][0]["markdown"] = "This item should be treated as a top priority."
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert "markdown:sections[0]:unsupported_recommendation:should" in errors
        assert "markdown:sections[0]:unsupported_recommendation:priority" in errors

    with session_maker() as db:
        section_result = generate_report_markdown_section_shadow(
            db,
            app_settings=settings,
            report_id="progress-report-markdown-001",
            section_key="summary",
            use_ai=False,
        )
        db.commit()
        section_artifact_id = section_result.artifact.id

    with session_maker() as db:
        section_artifact = db.get(ExpressionArtifact, section_artifact_id)
        assert section_artifact is not None
        assert section_artifact.artifact_type == "generated_report_markdown_section"
        assert section_artifact.validation_status == "shadow_fallback_valid"
        assert section_artifact.promoted is False
        assert section_artifact.scope_key_json["section_key"] == "summary"
        assert section_artifact.structured_json["key"] == "summary"
        assert section_artifact.structured_json["generation_mode"] == "deterministic_fallback"
        refs = section_artifact.structured_json["source_refs"]
        assert refs
        assert all(ref.startswith("structured_report.") for ref in refs)
        assert "## Progress Summary" in section_artifact.rendered_markdown
        gap_ids = {
            gap["gap_id"]
            for gap in build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)[
                "registry_gaps"
            ]
        }
        assert "shadow_path_not_implemented:generated_report_markdown_section" not in gap_ids


def test_markdown_section_spec_for_key_rejects_unknown_section_key():
    with pytest.raises(ValueError, match="Unknown markdown section key"):
        _markdown_section_spec_for_key({}, "not_a_section")


def test_project_manager_decision_brief_uses_db_project_facts(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    contract_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "artifact_type",
            "artifact_version",
            "audience",
            "visibility",
            "project_id",
            "as_of",
            "fact_snapshot_ids",
            "decision_surface",
            "body",
            "provenance",
            "validation",
        ],
        "properties": {
            "artifact_type": {"type": "string", "const": "project_manager_decision_brief"},
            "artifact_version": {"type": "string"},
            "audience": {"type": "string", "const": "project_manager"},
            "visibility": {"type": "string", "const": "shadow"},
            "project_id": {"type": "string"},
            "as_of": {"type": "string"},
            "fact_snapshot_ids": {"type": "array", "items": {"type": "string"}},
            "decision_surface": {
                "type": "object",
                "required": ["computed_at", "items"],
                "properties": {
                    "computed_at": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "item_id",
                                "category",
                                "severity",
                                "deterministic_summary",
                                "fact_refs",
                                "metrics",
                            ],
                            "properties": {
                                "item_id": {"type": "string"},
                                "category": {"type": "string"},
                                "severity": {"type": "string"},
                                "deterministic_summary": {"type": "string"},
                                "fact_refs": {"type": "array", "items": {"type": "object"}},
                                "metrics": {"type": "object"},
                            },
                        },
                    },
                },
            },
            "body": {
                "type": "object",
                "required": ["format", "paragraphs", "word_count", "generation_path"],
                "properties": {
                    "format": {"type": "string", "const": "markdown"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "word_count": {"type": "integer"},
                    "generation_path": {"type": "string"},
                },
            },
            "provenance": {"type": "object"},
            "validation": {"type": "object"},
        },
    }
    structured_report = {
        "executive_summary": "Conduit rough-in moved forward with open safety verification.",
        "overall_progress_percent": 55,
        "overall_status": "at_risk",
        "confidence_level": "medium",
        "manager_brief": "Open safety verification remains visible in the evidence set.",
        "safety_risks": ["Open trench requires verification."],
        "quality_risks": [],
        "work_remaining": ["Panel labeling confirmation."],
        "evidence_limitations": ["Only selected photos were reviewed."],
        "timeline_observations": [],
    }

    with session_maker() as db:
        audience = ExpressionAudience(
            id="project_manager",
            code="project_manager",
            title="Project manager",
            default_language="en",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="project_manager_decision_brief",
            slug="project_manager_decision_brief",
            title="Project manager decision brief",
            artifact_type="project_manager_decision_brief",
            stage="expression",
            default_audience_id="project_manager",
        )
        version = ExpressionPromptVersion(
            id="project_manager_decision_brief:v1",
            template_id="project_manager_decision_brief",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:project_manager_decision_brief:v1",
            scope_type="global",
            scope_id=None,
            template_id="project_manager_decision_brief",
            prompt_version_id="project_manager_decision_brief:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="project_manager_decision_brief:project_manager:v1",
            artifact_type="project_manager_decision_brief",
            audience_id="project_manager",
            version="v1",
            json_schema=contract_schema,
            required_fact_paths_json=[
                "scope.company_id",
                "scope.project_id",
                "photo_metrics.photos_current",
                "progress_report_metrics.completed_with_content",
            ],
            forbidden_claims_json=[
                {"pattern": "\\bshould\\b"},
                {"pattern": "\\bpriority\\b"},
                {"pattern": "\\bmust\\b"},
            ],
            is_active=True,
        )
        project = Project(
            company_id="default",
            project_id="PM100",
            project_name="Manager Brief Test",
            client_name="Client",
            location="Site",
        )
        current_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="PM100",
            photo_type=PhotoType.project,
            file_path="/tmp/current.jpg",
            image_url="/media/photos/current.jpg",
            original_file_name="current.jpg",
            captured_at_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            labeling_status="completed",
        )
        previous_photo_1 = Photo(
            company_id="default",
            employee_id="E101",
            project_id="PM100",
            photo_type=PhotoType.project,
            file_path="/tmp/previous1.jpg",
            image_url="/media/photos/previous1.jpg",
            original_file_name="previous1.jpg",
            captured_at_utc=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        )
        previous_photo_2 = Photo(
            company_id="default",
            employee_id="E102",
            project_id="PM100",
            photo_type=PhotoType.project,
            file_path="/tmp/previous2.jpg",
            image_url="/media/photos/previous2.jpg",
            original_file_name="previous2.jpg",
            captured_at_utc=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        )
        report = ProgressReport(
            id="project-manager-brief-report-001",
            tenant_id="default",
            company_id="default",
            project_id="PM100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content=json.dumps(structured_report),
            completed_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        )
        db.add_all(
            [
                audience,
                template,
                version,
                binding,
                contract,
                project,
                current_photo,
                previous_photo_1,
                previous_photo_2,
                report,
            ]
        )
        db.commit()

    with session_maker() as db:
        result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="PM100",
            window_days=30,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        snapshot = db.get(FactSnapshot, snapshot_id)
        assert artifact is not None
        assert snapshot is not None
        assert len(PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION) <= 32
        assert snapshot.assembler_version == PROJECT_MANAGER_DECISION_BRIEF_ASSEMBLER_VERSION
        assert artifact.artifact_type == "project_manager_decision_brief"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        assert artifact.structured_json["visibility"] == "shadow"
        assert artifact.structured_json["decision_surface"]["items"]
        assert artifact.structured_json["body"]["generation_path"] == "deterministic_fallback"
        assert snapshot.facts_json["photo_metrics"]["photos_current"] == 1
        assert snapshot.facts_json["progress_report_metrics"]["completed_with_content"] == 1
        bad_payload = dict(artifact.structured_json)
        bad_payload["body"] = dict(bad_payload["body"])
        bad_payload["body"]["paragraphs"] = ["This should be treated as a top priority."]
        audience = db.get(ExpressionAudience, "project_manager")
        contract = db.get(ExpressionOutputContract, "project_manager_decision_brief:project_manager:v1")
        assert audience is not None
        assert contract is not None
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert "decision_brief:paragraphs[0]:imperative_language" in errors
        assert "forbidden_claim:\\bshould\\b" in errors
        assert "forbidden_claim:\\bpriority\\b" in errors

    def fake_pm_polish(_backend, *, prompt: str) -> str:
        assert "Return strict JSON only with this exact shape" in prompt
        return json.dumps(
            {
                "body": {
                    "paragraphs": [
                        "Database evidence shows 1 current project photo and 1 completed structured progress report."
                    ]
                }
            }
        )

    monkeypatch.setattr("app.services.expression._call_ollama_expression_backend", fake_pm_polish)
    with session_maker() as db:
        result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="PM100",
            window_days=30,
            use_ai=True,
        )
        db.commit()
        ai_artifact_id = result.artifact.id
        ai_snapshot_id = result.snapshot.id

    with session_maker() as db:
        ai_artifact = db.get(ExpressionArtifact, ai_artifact_id)
        ai_snapshot = db.get(FactSnapshot, ai_snapshot_id)
        assert ai_artifact is not None
        assert ai_snapshot is not None
        expected_surface = build_project_manager_decision_brief_fallback(ai_snapshot)["decision_surface"]
        assert ai_artifact.validation_status == "shadow_valid"
        assert ai_artifact.promoted is False
        assert ai_artifact.structured_json["decision_surface"]["items"] == expected_surface["items"]
        assert ai_artifact.structured_json["body"]["generation_path"] == "ai_polish"
        assert ai_artifact.structured_json["validation"]["decision_surface_locked"] is True
        assert ai_artifact.structured_json["validation"]["ai_changed_decision_surface"] is False
        assert ai_artifact.structured_json["body"]["paragraphs"] == [
            "Database evidence shows 1 current project photo and 1 completed structured progress report."
        ]

    pm_polish_calls = {"count": 0}

    def fake_pm_retry_polish(_backend, *, prompt: str) -> str:
        pm_polish_calls["count"] += 1
        if pm_polish_calls["count"] == 1:
            return json.dumps({"body": {"paragraphs": ["Schedule stability is excellent across all work areas."]}})
        assert "Retry as a strict copy editor" in prompt
        return json.dumps(
            {
                "body": {
                    "paragraphs": [
                        "The current project window has 1 database photos across 1 active record days and 1 employee contributors.",
                        "The project has 1 completed progress reports with structured content; the latest completed report is PMR1.",
                        "No promoted employee contribution expression artifact is currently linked to this project.",
                    ]
                }
            }
        )

    monkeypatch.setattr("app.services.expression._call_ollama_expression_backend", fake_pm_retry_polish)
    with session_maker() as db:
        result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="PM100",
            window_days=30,
            use_ai=True,
        )
        db.commit()
        retry_artifact_id = result.artifact.id

    with session_maker() as db:
        retry_artifact = db.get(ExpressionArtifact, retry_artifact_id)
        assert retry_artifact is not None
        assert retry_artifact.validation_status == "shadow_valid"
        assert retry_artifact.backend_profile_json["retry_used"] is True
        assert retry_artifact.backend_profile_json["retry_reason_counts"] == {"low_grounding": 1}
        assert retry_artifact.structured_json["body"]["generation_path"] == "ai_polish"
        assert pm_polish_calls["count"] == 2

    def fake_bad_pm_polish(_backend, *, prompt: str) -> str:
        return json.dumps({"body": {"paragraphs": ["This should be the top priority."]}})

    monkeypatch.setattr("app.services.expression._call_ollama_expression_backend", fake_bad_pm_polish)
    with session_maker() as db:
        result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="PM100",
            window_days=30,
            use_ai=True,
        )
        db.commit()
        fallback_artifact_id = result.artifact.id
        fallback_snapshot_id = result.snapshot.id

    with session_maker() as db:
        fallback_artifact = db.get(ExpressionArtifact, fallback_artifact_id)
        fallback_snapshot = db.get(FactSnapshot, fallback_snapshot_id)
        assert fallback_artifact is not None
        assert fallback_snapshot is not None
        expected_surface = build_project_manager_decision_brief_fallback(fallback_snapshot)["decision_surface"]
        assert fallback_artifact.validation_status == "shadow_fallback_valid"
        assert fallback_artifact.structured_json["decision_surface"]["items"] == expected_surface["items"]
        assert fallback_artifact.structured_json["body"]["generation_path"] == "deterministic_fallback"
        assert "This should be the top priority." not in json.dumps(
            fallback_artifact.structured_json,
            ensure_ascii=False,
        )
        assert any("imperative_language" in error for error in fallback_artifact.validation_errors_json)

    def fake_drifting_pm_polish(_backend, *, prompt: str) -> str:
        return json.dumps({"body": {"paragraphs": ["Schedule stability is excellent and crews are ahead across all work areas."]}})

    monkeypatch.setattr("app.services.expression._call_ollama_expression_backend", fake_drifting_pm_polish)
    with session_maker() as db:
        result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="PM100",
            window_days=30,
            use_ai=True,
        )
        db.commit()
        drifting_artifact_id = result.artifact.id

    with session_maker() as db:
        drifting_artifact = db.get(ExpressionArtifact, drifting_artifact_id)
        assert drifting_artifact is not None
        assert drifting_artifact.validation_status == "shadow_fallback_valid"
        assert drifting_artifact.structured_json["body"]["generation_path"] == "deterministic_fallback"
        assert "crews are ahead" not in json.dumps(drifting_artifact.structured_json, ensure_ascii=False)
        assert "decision_brief:paragraphs[0]:ai_polish_low_grounding" in drifting_artifact.validation_errors_json


def test_client_progress_summary_uses_redacted_client_visible_facts(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    item_schema = {
        "type": "object",
        "required": ["item_id", "category", "deterministic_summary", "fact_refs", "metrics"],
        "properties": {
            "item_id": {"type": "string"},
            "category": {"type": "string"},
            "deterministic_summary": {"type": "string"},
            "fact_refs": {"type": "array", "items": {"type": "object"}},
            "metrics": {"type": "object"},
        },
    }
    contract_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "artifact_type",
            "artifact_version",
            "audience",
            "visibility",
            "project_id",
            "as_of",
            "fact_snapshot_ids",
            "summary_surface",
            "body",
            "disclaimer",
            "provenance",
            "validation",
        ],
        "properties": {
            "artifact_type": {"type": "string", "const": "client_progress_summary"},
            "artifact_version": {"type": "string"},
            "audience": {"type": "string", "const": "client"},
            "visibility": {"type": "string", "const": "shadow"},
            "project_id": {"type": "string"},
            "as_of": {"type": "string"},
            "fact_snapshot_ids": {"type": "array", "items": {"type": "string"}},
            "summary_surface": {
                "type": "object",
                "required": ["computed_at", "items"],
                "properties": {"computed_at": {"type": "string"}, "items": {"type": "array", "items": item_schema}},
            },
            "body": {
                "type": "object",
                "required": ["format", "paragraphs", "word_count", "generation_path"],
                "properties": {
                    "format": {"type": "string", "const": "markdown"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "word_count": {"type": "integer"},
                    "generation_path": {"type": "string"},
                },
            },
            "disclaimer": {"type": "string", "const": CLIENT_PROGRESS_DISCLAIMER},
            "provenance": {"type": "object"},
            "validation": {"type": "object"},
        },
    }
    structured_report = {
        "executive_summary": "Client-facing progress summary.",
        "overall_progress_percent": 64,
        "overall_status": "on_track",
        "confidence_level": "medium",
        "manager_brief": "Internal manager note should not appear.",
        "work_completed": [
            "Main lobby framing documented.",
            "Internal crew E200 cost note should not appear.",
            "Photo path /tmp/hidden-client-photo.jpg should not appear.",
        ],
        "key_changes": [
            "South wall rough-in advanced.",
            "token=hidden-client-token should not appear.",
        ],
        "work_remaining": ["Internal follow-up item should not appear."],
        "safety_risks": ["Internal safety item should not appear."],
        "quality_risks": ["Internal quality item should not appear."],
        "recommended_actions": ["Internal action should not appear."],
        "evidence_limitations": [
            "Client-visible evidence is limited to approved records.",
            "raw_model_output should not appear.",
        ],
        "timeline_observations": [],
    }

    with session_maker() as db:
        audience = ExpressionAudience(
            id="client",
            code="client",
            title="Client",
            default_language="en",
            forbidden_phrase_set_id="client_progress_en_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="client_progress_summary",
            slug="client_progress_summary",
            title="Client progress summary",
            artifact_type="client_progress_summary",
            stage="expression",
            default_audience_id="client",
        )
        version = ExpressionPromptVersion(
            id="client_progress_summary:v1",
            template_id="client_progress_summary",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:client_progress_summary:v1",
            scope_type="global",
            scope_id=None,
            template_id="client_progress_summary",
            prompt_version_id="client_progress_summary:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="client_progress_summary:client:v1",
            artifact_type="client_progress_summary",
            audience_id="client",
            version="v1",
            json_schema=contract_schema,
            required_fact_paths_json=[
                "scope.company_id",
                "scope.project_id",
                "client_visible_photo_metrics.photos_current",
                "latest_progress_report.has_completed_report",
            ],
            forbidden_claims_json=[
                {"pattern": "\\binternal\\b"},
                {"pattern": "\\bcost\\b"},
                {"pattern": "\\bemployee_id\\b"},
            ],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="client_progress_en_v1:internal",
            set_id="client_progress_en_v1",
            audience_id="client",
            language="en",
            phrase="(?i)\\binternal\\b",
            match_type="regex",
            severity="error",
            is_active=True,
        )
        project = Project(
            company_id="default",
            project_id="CLIENT100",
            project_name="Client Summary Test",
            client_name="Client",
            location="Site",
        )
        visible_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="CLIENT100",
            photo_type=PhotoType.project,
            file_path="/tmp/client-visible.jpg",
            image_url="/media/photos/client-visible.jpg",
            original_file_name="client-visible.jpg",
            captured_at_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            visibility=PhotoVisibility.client_visible,
        )
        internal_photo = Photo(
            company_id="default",
            employee_id="E200",
            project_id="CLIENT100",
            photo_type=PhotoType.project,
            file_path="/tmp/internal.jpg",
            image_url="/media/photos/internal.jpg",
            original_file_name="internal.jpg",
            captured_at_utc=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            visibility=PhotoVisibility.internal,
        )
        report = ProgressReport(
            id="client-progress-report-001",
            tenant_id="default",
            company_id="default",
            project_id="CLIENT100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content=json.dumps(structured_report),
            completed_at=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc),
        )
        db.add_all([audience, template, version, binding, contract, phrase, project, visible_photo, internal_photo, report])
        db.commit()

    with session_maker() as db:
        facts = build_client_progress_summary_facts(
            db,
            company_id="default",
            project_id="CLIENT100",
            window_days=30,
        )
        assert facts["client_visible_photo_metrics"]["photos_current"] == 1
        assert "employee_contributor_summary" not in facts
        assert "expression_metrics" not in facts
        assert "manager_brief" not in facts["latest_progress_report"]["client_report"]
        assert "safety_risks" not in facts["latest_progress_report"]["client_report"]
        assert "recommended_actions" not in facts["latest_progress_report"]["client_report"]
        facts_without_guardrails = {key: value for key, value in facts.items() if key != "guardrails"}
        facts_serialized = json.dumps(facts_without_guardrails, ensure_ascii=False)
        assert "E200" not in facts_serialized
        assert "/tmp/hidden-client-photo.jpg" not in facts_serialized
        assert "hidden-client-token" not in facts_serialized
        assert "raw_model_output" not in facts_serialized

        result = generate_client_progress_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="CLIENT100",
            window_days=30,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        snapshot = db.get(FactSnapshot, snapshot_id)
        assert artifact is not None
        assert snapshot is not None
        assert artifact.artifact_type == "client_progress_summary"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        serialized = json.dumps(artifact.structured_json, ensure_ascii=False)
        assert "Internal manager note" not in serialized
        assert "Internal safety item" not in serialized
        assert "E200" not in serialized
        assert "/tmp/hidden-client-photo.jpg" not in serialized
        assert "hidden-client-token" not in serialized
        assert "raw_model_output" not in serialized
        assert artifact.structured_json["summary_surface"]["items"]
        audience = db.get(ExpressionAudience, "client")
        contract = db.get(ExpressionOutputContract, "client_progress_summary:client:v1")
        assert audience is not None
        assert contract is not None
        uuid_payload = dict(artifact.structured_json)
        uuid_payload["fact_snapshot_ids"] = ["8329185a-e783-4dea-9b75-2697ce6a1ff1"]
        uuid_errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=uuid_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert not any(error.startswith("client_visibility:payload:") for error in uuid_errors)
        bad_payload = dict(artifact.structured_json)
        bad_payload["body"] = dict(bad_payload["body"])
        bad_payload["body"]["paragraphs"] = ["Internal cost priority should be escalated for E200."]
        bad_facts = dict(snapshot.facts_json)
        bad_facts["leak"] = {"file_path": "/tmp/nope.jpg", "note": "api_key=abc"}
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=bad_facts,
        )
        assert "client_summary:paragraphs[0]:imperative_language" in errors
        assert "client_visibility:facts:denied_key" in errors
        assert "client_visibility:facts:denied_text" in errors
        assert "client_visibility:payload:denied_text" in errors
        assert any(error.startswith("client_visibility:payload:") for error in errors)
        assert "forbidden_phrase:(?i)\\binternal\\b" in errors


def test_operations_health_summary_uses_locked_db_metrics(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    contract_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "artifact_type",
            "artifact_version",
            "audience",
            "visibility",
            "company_id",
            "scope_type",
            "as_of",
            "overall_status",
            "fact_snapshot_ids",
            "health_surface",
            "body",
            "provenance",
            "validation",
        ],
        "properties": {
            "artifact_type": {"type": "string", "const": "operations_health_summary"},
            "artifact_version": {"type": "string"},
            "audience": {"type": "string", "const": "operations"},
            "visibility": {"type": "string", "const": "shadow"},
            "company_id": {"type": "string"},
            "scope_type": {"type": "string", "const": "company_operations"},
            "as_of": {"type": "string"},
            "overall_status": {"type": "string", "enum": ["green", "amber", "red"]},
            "fact_snapshot_ids": {"type": "array", "items": {"type": "string"}},
            "health_surface": {
                "type": "object",
                "required": ["computed_at", "dimensions"],
                "properties": {
                    "computed_at": {"type": "string"},
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "item_id",
                                "dimension_id",
                                "status",
                                "deterministic_summary",
                                "fact_refs",
                                "metrics",
                                "threshold_breaches",
                            ],
                            "properties": {
                                "item_id": {"type": "string"},
                                "dimension_id": {"type": "string"},
                                "status": {"type": "string"},
                                "deterministic_summary": {"type": "string"},
                                "fact_refs": {"type": "array", "items": {"type": "object"}},
                                "metrics": {"type": "object"},
                                "threshold_breaches": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
            "body": {
                "type": "object",
                "required": ["format", "paragraphs", "word_count", "generation_path"],
                "properties": {
                    "format": {"type": "string", "const": "markdown"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "word_count": {"type": "integer"},
                    "generation_path": {"type": "string"},
                },
            },
            "provenance": {"type": "object"},
            "validation": {"type": "object"},
        },
    }

    with session_maker() as db:
        audience = ExpressionAudience(
            id="operations",
            code="operations",
            title="Operations",
            default_language="en",
            forbidden_phrase_set_id="operations_health_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="operations_health_summary",
            slug="operations_health_summary",
            title="Operations health summary",
            artifact_type="operations_health_summary",
            stage="expression",
            default_audience_id="operations",
        )
        version = ExpressionPromptVersion(
            id="operations_health_summary:v1",
            template_id="operations_health_summary",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:operations_health_summary:v1",
            scope_type="global",
            scope_id=None,
            template_id="operations_health_summary",
            prompt_version_id="operations_health_summary:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="operations_health_summary:operations:v1",
            artifact_type="operations_health_summary",
            audience_id="operations",
            version="v1",
            json_schema=contract_schema,
            required_fact_paths_json=[
                "scope.company_id",
                "task_jobs.queued_backlog",
                "photo_processing.project_photos_current",
                "ai_analysis.logs_current",
                "expression_artifacts.artifacts_current",
            ],
            forbidden_claims_json=[
                {"pattern": "https?://"},
                {"pattern": "\\bapi_key\\b"},
                {"pattern": "\\bfile_path\\b"},
            ],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="operations_health_v1:api_key",
            set_id="operations_health_zh_v1",
            audience_id="operations",
            language="en",
            phrase="(?i)\\bapi_key\\b",
            match_type="regex",
            severity="error",
            is_active=True,
        )
        queued = TaskJob(
            public_id="ops-queued-1",
            tenant_id="default",
            company_id="default",
            task_type="photo_ai_pipeline",
            status=TaskStatus.queued,
            available_at=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        failed = TaskJob(
            public_id="ops-failed-1",
            tenant_id="default",
            company_id="default",
            task_type="photo_ai_pipeline",
            status=TaskStatus.failed,
            completed_at=datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc),
        )
        photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="OPS100",
            photo_type=PhotoType.project,
            file_path="/tmp/secret-source.jpg",
            image_url="/media/photos/secret-source.jpg",
            original_file_name="secret-source.jpg",
            captured_at_utc=datetime(2026, 6, 16, 11, 30, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 11, 30, tzinfo=timezone.utc),
            labeling_status=None,
        )
        ai_log = AIAnalysisLog(
            id="ops-ai-log-1",
            photo_id=None,
            analysis_type=AIAnalysisType.fast_screen,
            prompt_used="prompt text must not appear",
            model_used="qwen2.5:7b-instruct",
            status=AIAnalysisStatus.rejected,
            created_at=datetime(2026, 6, 16, 11, 45, tzinfo=timezone.utc),
            created_by="system",
        )
        expression_snapshot = FactSnapshot(
            id="ops-expression-snapshot-1",
            tenant_id="default",
            company_id="default",
            employee_id=None,
            project_id="OPS100",
            scope_type="progress_report",
            scope_key_json={"company_id": "default", "project_id": "OPS100", "report_id": "report-1"},
            scope_key_hash="ops-expression-snapshot-1",
            assembler_version="test",
            facts_json={"scope": {"company_id": "default"}},
            fact_count=1,
        )
        old_invalid = ExpressionArtifact(
            id="ops-old-invalid-artifact",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=expression_snapshot.id,
            scope_type="progress_report",
            scope_key_json={"company_id": "default", "project_id": "OPS100", "report_id": "report-1"},
            artifact_type="progress_report_translation",
            audience_id="project_manager",
            language="en",
            structured_json={"body": "old invalid"},
            validation_status="shadow_invalid",
            validation_errors_json=["old error"],
            model_used="qwen2.5:7b-instruct",
            promoted=False,
            created_at=datetime(2026, 6, 16, 10, 30, tzinfo=timezone.utc),
        )
        newer_valid = ExpressionArtifact(
            id="ops-newer-valid-artifact",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=expression_snapshot.id,
            scope_type="progress_report",
            scope_key_json={"company_id": "default", "project_id": "OPS100", "report_id": "report-1"},
            artifact_type="progress_report_translation",
            audience_id="project_manager",
            language="en",
            structured_json={"body": "new valid"},
            validation_status="shadow_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            promoted=False,
            created_at=datetime(2026, 6, 16, 11, 50, tzinfo=timezone.utc),
        )
        db.add_all(
            [
                audience,
                template,
                version,
                binding,
                contract,
                phrase,
                queued,
                failed,
                photo,
                ai_log,
                expression_snapshot,
                old_invalid,
                newer_valid,
            ]
        )
        db.commit()

    with session_maker() as db:
        facts = build_operations_health_summary_facts(
            db,
            company_id="default",
            window_hours=24,
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        serialized_facts = json.dumps(facts, ensure_ascii=False)
        assert facts["task_jobs"]["queued_backlog"] == 1
        assert facts["photo_processing"]["project_photos_current"] == 1
        assert facts["photo_processing"]["labeling_pending_current"] == 1
        assert facts["ai_analysis"]["logs_current"] == 1
        assert facts["expression_artifacts"]["artifact_rows_current"] == 2
        assert facts["expression_artifacts"]["artifacts_current"] == 1
        assert facts["expression_artifacts"]["shadow_invalid_current"] == 0
        assert "/tmp/secret-source.jpg" not in serialized_facts
        assert "prompt text must not appear" not in serialized_facts

        result = generate_operations_health_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            window_hours=24,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        snapshot = db.get(FactSnapshot, snapshot_id)
        assert artifact is not None
        assert snapshot is not None
        assert artifact.artifact_type == "operations_health_summary"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        assert artifact.structured_json["overall_status"] in {"amber", "red"}
        assert len(artifact.structured_json["health_surface"]["dimensions"]) == 4
        serialized = json.dumps(artifact.structured_json, ensure_ascii=False)
        assert "secret-source.jpg" not in serialized
        assert "prompt text must not appear" not in serialized

        bad_payload = json.loads(json.dumps(artifact.structured_json, ensure_ascii=False))
        bad_payload["health_surface"]["dimensions"][0]["deterministic_summary"] = (
            "Check https://example.invalid with api_key and file_path."
        )
        bad_payload["body"]["paragraphs"][0] = "Check https://example.invalid with api_key and file_path."
        audience = db.get(ExpressionAudience, "operations")
        contract = db.get(ExpressionOutputContract, "operations_health_summary:operations:v1")
        assert audience is not None
        assert contract is not None
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert "operations_visibility:payload:path_or_secret" in errors
        assert "forbidden_phrase:(?i)\\bapi_key\\b" in errors


def test_finance_fact_health_reports_empty_invoice_source(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        payload = audit_finance_fact_health_payload(db, company_id=None, window_days=30, limit=10)

    assert payload["status"] == "no_invoice_photos"
    assert payload["source_tables"] == ["photos", "ai_analysis_logs", "receipt_facts", "projects"]
    assert payload["totals"]["invoice_photo_count"] == 0
    assert payload["totals"]["receipt_fact_count"] == 0
    assert payload["fact_refs"][0]["path"] == "photos.invoice.count"


def test_finance_fact_health_distinguishes_ai_facts_from_materialized_receipts(app_context):
    session_maker = app_context["session_maker"]
    now = utc_now()

    with session_maker() as db:
        ai_only_photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-ai-only.jpg",
            image_url="/media/private-ai-only.jpg",
            original_file_name="private-ai-only.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        materialized_photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-materialized.jpg",
            image_url="/media/private-materialized.jpg",
            original_file_name="private-materialized.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        db.add_all([ai_only_photo, materialized_photo])
        db.flush()
        db.add(
            AIAnalysisLog(
                id="finance-health-ai-log",
                photo_id=ai_only_photo.id,
                analysis_type=AIAnalysisType.deep_analysis,
                prompt_used="private prompt must not leak",
                model_used="test-model",
                result_data={"receipt_facts": {"vendor": "Fuel Stop", "total_amount": "42.15"}},
                status=AIAnalysisStatus.active,
                created_at=now,
                created_by="test",
            )
        )
        db.add(
            ReceiptFact(
                id="finance-health-receipt",
                tenant_id="default",
                company_id="default",
                project_id="P100",
                photo_id=materialized_photo.id,
                employee_id="E100",
                total_amount=12.5,
                receipt_timestamp=now,
                summary_text="private receipt text must not leak",
                facts_json={"file_path": "/tmp/private-materialized.jpg"},
            )
        )
        db.commit()

        payload = audit_finance_fact_health_payload(db, company_id="default", window_days=30, limit=10)

    assert payload["status"] == "partial_receipt_fact_coverage"
    assert payload["totals"]["invoice_photo_count"] == 2
    assert payload["totals"]["invoice_photos_with_project_id"] == 2
    assert payload["totals"]["active_ai_logs_with_receipt_facts_photo_count"] == 1
    assert payload["totals"]["receipt_fact_count"] == 1
    assert payload["totals"]["invoice_photos_missing_receipt_fact"] == 1
    assert payload["totals"]["ai_receipt_facts_not_materialized"] == 1
    assert payload["projects"] == [
        {
            "company_id": "default",
            "project_id": "P100",
            "receipt_fact_count": 1,
            "fact_refs": [
                {
                    "source_table": "receipt_facts",
                    "path": "receipt_facts.count_by_company_project",
                    "observed_value": 1,
                }
            ],
        }
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private" not in serialized
    assert "Fuel Stop" not in serialized
    assert "raw_model_output" not in serialized


def test_finance_fact_attribution_dry_run_uses_employee_project_without_leaking_employee_id(app_context):
    session_maker = app_context["session_maker"]
    now = utc_now()

    with session_maker() as db:
        photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="invoice",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-attribution.jpg",
            image_url="/media/private-attribution.jpg",
            original_file_name="private-attribution.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        db.add(photo)
        db.flush()
        db.add(
            ReceiptFact(
                id="finance-attribution-receipt",
                tenant_id="default",
                company_id="default",
                project_id=None,
                photo_id=photo.id,
                employee_id="E100",
                total_amount=12.5,
                receipt_timestamp=now,
                vendor_name="Private Vendor",
                summary_text="private receipt text must not leak",
            )
        )
        db.commit()

        payload = audit_finance_fact_attribution_payload(db, company_id="default", window_days=365, limit=10)

    assert payload["status"] == "ready_for_review"
    assert payload["mode"] == "dry_run"
    assert payload["totals"]["receipt_facts_scanned"] == 1
    assert payload["totals"]["suggested"] == 1
    assert payload["totals"]["employee_current_project_candidates"] == 1
    assert payload["candidates"][0]["candidate_project_id"] == "P100"
    assert payload["candidates"][0]["rules"] == ["employee_current_project"]
    assert payload["candidates"][0]["dry_run"] is True
    assert payload["guardrails"]["writes_database"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "E100" not in serialized
    assert "Private Vendor" not in serialized
    assert "private-attribution" not in serialized
    assert "raw_model_output" not in serialized


def test_finance_fact_attribution_dry_run_flags_conflicting_candidates(app_context):
    session_maker = app_context["session_maker"]
    now = utc_now()

    with session_maker() as db:
        db.add(
            Project(
                company_id="default",
                project_id="P101",
                project_name="Second Project",
                client_name="Second Client",
                location="Austin, TX",
            )
        )
        db.flush()
        photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="P101",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-conflict.jpg",
            image_url="/media/private-conflict.jpg",
            original_file_name="private-conflict.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        db.add(photo)
        db.flush()
        db.add(
            ReceiptFact(
                id="finance-attribution-conflict",
                tenant_id="default",
                company_id="default",
                project_id=None,
                photo_id=photo.id,
                employee_id="E100",
                receipt_timestamp=now,
            )
        )
        db.commit()

        payload = audit_finance_fact_attribution_payload(db, company_id="default", window_days=365, limit=10)

    assert payload["status"] == "needs_manual_review"
    assert payload["totals"]["suggested"] == 0
    assert payload["totals"]["ambiguous"] == 1
    assert payload["candidates"][0]["candidate_project_id"] is None
    assert payload["candidates"][0]["reason"] == "conflicting_project_candidates"


def test_finance_fact_attribution_apply_writes_rollback_and_rollback_restores(app_context, tmp_path):
    session_maker = app_context["session_maker"]
    now = utc_now()
    rollback_file = tmp_path / "finance-rollback.json"

    with session_maker() as db:
        photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="invoice",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-apply.jpg",
            image_url="/media/private-apply.jpg",
            original_file_name="private-apply.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        db.add(photo)
        db.flush()
        db.add(
            ReceiptFact(
                id="finance-attribution-apply",
                tenant_id="default",
                company_id="default",
                project_id=None,
                photo_id=photo.id,
                employee_id="E100",
                receipt_timestamp=now,
                vendor_name="Private Vendor",
            )
        )
        db.commit()

        apply_payload = apply_finance_fact_attribution_payload(
            db,
            company_id="default",
            window_days=365,
            limit=10,
            rollback_file=str(rollback_file),
        )
        db.commit()

        receipt = db.get(ReceiptFact, "finance-attribution-apply")
        assert receipt is not None
        assert receipt.project_id == "P100"
        assert apply_payload["status"] == "applied"
        assert apply_payload["totals"]["applied"] == 1
        assert rollback_file.exists()
        serialized_apply = json.dumps(apply_payload, ensure_ascii=False)
        assert "E100" not in serialized_apply
        assert "Private Vendor" not in serialized_apply
        assert "private-apply" not in serialized_apply

        rollback_payload = rollback_finance_fact_attribution_payload(db, rollback_file=str(rollback_file))
        db.commit()

        receipt = db.get(ReceiptFact, "finance-attribution-apply")
        assert receipt is not None
        assert receipt.project_id is None
        assert rollback_payload["status"] == "rolled_back"
        assert rollback_payload["totals"]["restored"] == 1


def test_finance_fact_attribution_rollback_skips_changed_current_project(app_context, tmp_path):
    session_maker = app_context["session_maker"]
    now = utc_now()
    rollback_file = tmp_path / "finance-rollback-changed.json"

    with session_maker() as db:
        db.add(
            Project(
                company_id="default",
                project_id="P102",
                project_name="Changed Project",
                client_name="Changed Client",
                location="Denver, CO",
            )
        )
        db.flush()
        photo = Photo(
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="invoice",
            photo_type=PhotoType.invoice,
            file_path="/tmp/private-rollback-skip.jpg",
            image_url="/media/private-rollback-skip.jpg",
            original_file_name="private-rollback-skip.jpg",
            captured_at_utc=now,
            created_at=now,
            approval_status=ApprovalStatus.pending,
            visibility=PhotoVisibility.internal,
        )
        db.add(photo)
        db.flush()
        db.add(
            ReceiptFact(
                id="finance-attribution-rollback-skip",
                tenant_id="default",
                company_id="default",
                project_id=None,
                photo_id=photo.id,
                employee_id="E100",
                receipt_timestamp=now,
            )
        )
        db.commit()

        apply_finance_fact_attribution_payload(
            db,
            company_id="default",
            window_days=365,
            limit=10,
            rollback_file=str(rollback_file),
        )
        receipt = db.get(ReceiptFact, "finance-attribution-rollback-skip")
        assert receipt is not None
        receipt.project_id = "P102"
        db.commit()

        rollback_payload = rollback_finance_fact_attribution_payload(db, rollback_file=str(rollback_file))
        db.commit()

        receipt = db.get(ReceiptFact, "finance-attribution-rollback-skip")
        assert receipt is not None
        assert receipt.project_id == "P102"
        assert rollback_payload["status"] == "no_changes"
        assert rollback_payload["totals"]["skipped"] == 1
        assert rollback_payload["skipped"][0]["reason"] == "current_project_changed"


def test_finance_summary_uses_redacted_receipt_metrics(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="finance",
            code="finance",
            title="Finance",
            default_language="en",
            forbidden_phrase_set_id="finance_evidence_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="finance_summary",
            slug="finance_summary",
            title="Finance summary",
            artifact_type="finance_summary",
            stage="expression",
            default_audience_id="finance",
        )
        version = ExpressionPromptVersion(
            id="finance_summary:v1",
            template_id="finance_summary",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:finance_summary:v1",
            scope_type="global",
            scope_id=None,
            template_id="finance_summary",
            prompt_version_id="finance_summary:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="finance_summary:finance:v1",
            artifact_type="finance_summary",
            audience_id="finance",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["artifact_type", "audience", "headline_metrics", "finance_surface", "body"],
                "properties": {
                    "artifact_type": {"type": "string", "const": "finance_summary"},
                    "audience": {"type": "string", "const": "finance"},
                    "headline_metrics": {"type": "object"},
                    "finance_surface": {"type": "object"},
                    "body": {"type": "object"},
                },
            },
            required_fact_paths_json=[
                "scope.company_id",
                "scope.project_id",
                "receipt_metrics.receipt_count",
                "receipt_metrics.total_amount",
                "receipt_metrics.receipts_missing_amount",
                "receipt_metrics.fuel_gallons_total",
            ],
            forbidden_claims_json=[
                {"pattern": "\\bemployee_id\\b"},
                {"pattern": "\\bpurchaser_name\\b"},
                {"pattern": "https?://"},
                {"pattern": "\\bfile_path\\b"},
            ],
            is_active=True,
        )
        phrases = [
            ExpressionForbiddenPhrase(
                id="finance_evidence_v1:employee",
                set_id="finance_evidence_zh_v1",
                audience_id="finance",
                language="en",
                phrase="(?i)\\bemployee_id\\b",
                match_type="regex",
                severity="error",
                is_active=True,
            ),
            ExpressionForbiddenPhrase(
                id="finance_evidence_v1:purchaser",
                set_id="finance_evidence_zh_v1",
                audience_id="finance",
                language="en",
                phrase="(?i)\\bpurchaser_name\\b",
                match_type="regex",
                severity="error",
                is_active=True,
            ),
        ]
        photo_one = Photo(
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.invoice,
            file_path="/tmp/receipt-secret-1.jpg",
            image_url="/media/photos/receipt-secret-1.jpg",
            original_file_name="receipt-secret-1.jpg",
            captured_at_utc=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        )
        photo_two = Photo(
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.invoice,
            file_path="/tmp/receipt-secret-2.jpg",
            image_url="/media/photos/receipt-secret-2.jpg",
            original_file_name="receipt-secret-2.jpg",
            captured_at_utc=datetime(2026, 6, 16, 10, 5, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 10, 5, tzinfo=timezone.utc),
        )
        db.add_all([audience, template, version, binding, contract, *phrases, photo_one, photo_two])
        db.flush()
        db.add_all(
            [
                ReceiptFact(
                    id="finance-receipt-1",
                    tenant_id="default",
                    company_id="default",
                    project_id="P100",
                    photo_id=photo_one.id,
                    vendor_name="North Fuel",
                    total_amount=57.12,
                    gallons=12.345,
                    purchaser_name="Alicia Field",
                    employee_id="E100",
                    receipt_timestamp=datetime(2026, 6, 16, 5, 1, tzinfo=timezone.utc),
                    currency_code="USD",
                    has_pump_photo=True,
                    summary_text="private receipt summary",
                    facts_json={"file_path": "/tmp/receipt-secret-1.jpg", "raw": "private"},
                    created_at=datetime(2026, 6, 16, 5, 2, tzinfo=timezone.utc),
                ),
                ReceiptFact(
                    id="finance-receipt-2",
                    tenant_id="default",
                    company_id="default",
                    project_id="P100",
                    photo_id=photo_two.id,
                    vendor_name="North Fuel",
                    total_amount=None,
                    gallons=None,
                    purchaser_name="Alicia Field",
                    employee_id="E100",
                    receipt_timestamp=datetime(2026, 6, 16, 5, 6, tzinfo=timezone.utc),
                    currency_code="USD",
                    has_pump_photo=False,
                    summary_text="another private summary",
                    facts_json={"image_url": "/media/photos/receipt-secret-2.jpg"},
                    created_at=datetime(2026, 6, 16, 5, 7, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        facts = build_finance_summary_facts(
            db,
            company_id="default",
            project_id="P100",
            window_days=1,
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        assert facts["receipt_metrics"]["receipt_count"] == 2
        assert facts["receipt_metrics"]["receipts_with_amount"] == 1
        assert facts["receipt_metrics"]["receipts_missing_amount"] == 1
        assert facts["receipt_metrics"]["total_amount"] == 57.12
        assert facts["receipt_metrics"]["fuel_gallons_total"] == 12.345
        serialized_facts = json.dumps(facts, ensure_ascii=False)
        assert "Alicia Field" not in serialized_facts
        assert "E100" not in serialized_facts
        assert "receipt-secret" not in serialized_facts
        assert "private receipt summary" not in serialized_facts
        assert "North Fuel" not in serialized_facts

        result = generate_finance_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="P100",
            window_days=1,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        snapshot = db.get(FactSnapshot, snapshot_id)
        assert artifact is not None
        assert snapshot is not None
        assert artifact.artifact_type == "finance_summary"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        assert artifact.structured_json["headline_metrics"]["total_amount"] == 57.12
        assert "missing_amount" in artifact.structured_json["status_flags"]
        serialized = json.dumps(artifact.structured_json, ensure_ascii=False)
        assert "Alicia Field" not in serialized
        assert "E100" not in serialized
        assert "receipt-secret" not in serialized
        assert "private receipt summary" not in serialized
        assert "North Fuel" not in serialized

        bad_payload = json.loads(json.dumps(artifact.structured_json, ensure_ascii=False))
        bad_payload["finance_surface"]["sections"][0]["lines"][0] = (
            "employee_id E100 purchaser_name Alicia Field https://example.invalid/file_path"
        )
        bad_payload["body"]["paragraphs"][0] = bad_payload["finance_surface"]["sections"][0]["lines"][0]
        audience = db.get(ExpressionAudience, "finance")
        contract = db.get(ExpressionOutputContract, "finance_summary:finance:v1")
        assert audience is not None
        assert contract is not None
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert any(error.startswith("finance_visibility:") for error in errors)
        assert "forbidden_phrase:(?i)\\bemployee_id\\b" in errors
        assert "forbidden_phrase:(?i)\\bpurchaser_name\\b" in errors


def test_executive_company_health_uses_aggregate_db_facts(app_context):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="executive",
            code="executive",
            title="Executive",
            default_language="en",
            forbidden_phrase_set_id="executive_overview_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="executive_company_health_summary",
            slug="executive_company_health_summary",
            title="Executive company health",
            artifact_type="executive_company_health_summary",
            stage="expression",
            default_audience_id="executive",
        )
        version = ExpressionPromptVersion(
            id="executive_company_health_summary:v1",
            template_id="executive_company_health_summary",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:executive_company_health_summary:v1",
            scope_type="global",
            scope_id=None,
            template_id="executive_company_health_summary",
            prompt_version_id="executive_company_health_summary:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="executive_company_health_summary:executive:v1",
            artifact_type="executive_company_health_summary",
            audience_id="executive",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["artifact_type", "audience", "headline_metrics", "company_health_surface", "body"],
                "properties": {
                    "artifact_type": {"type": "string", "const": "executive_company_health_summary"},
                    "audience": {"type": "string", "const": "executive"},
                    "headline_metrics": {"type": "object"},
                    "company_health_surface": {"type": "object"},
                    "body": {"type": "object"},
                },
            },
            required_fact_paths_json=[
                "scope.company_id",
                "project_portfolio.projects_total",
                "photo_activity.photos_current",
                "progress_reports.completed_current",
                "expression_readiness.shadow_invalid_current",
                "finance_receipt_coverage.receipt_count",
            ],
            forbidden_claims_json=[
                {"pattern": "\\bemployee_id\\b"},
                {"pattern": "\\bfile_path\\b"},
                {"pattern": "\\bshould\\b"},
                {"pattern": "\\bpriority\\b"},
                {"pattern": "\\brank(?:ing)?\\b"},
            ],
            is_active=True,
        )
        phrases = [
            ExpressionForbiddenPhrase(
                id="executive_overview_v1:employee",
                set_id="executive_overview_zh_v1",
                audience_id="executive",
                language="en",
                phrase="(?i)\\bemployee_id\\b",
                match_type="regex",
                severity="error",
                is_active=True,
            ),
            ExpressionForbiddenPhrase(
                id="executive_overview_v1:should",
                set_id="executive_overview_zh_v1",
                audience_id="executive",
                language="en",
                phrase="(?i)\\bshould\\b",
                match_type="regex",
                severity="error",
                is_active=True,
            ),
        ]
        project = Project(
            company_id="default",
            project_id="EXEC100",
            project_name="Executive Health Test",
            client_name="Client",
            location="Site",
        )
        project_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="EXEC100",
            photo_type=PhotoType.project,
            file_path="/tmp/executive-project.jpg",
            image_url="/media/photos/executive-project.jpg",
            original_file_name="executive-project.jpg",
            captured_at_utc=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            visibility=PhotoVisibility.client_visible,
        )
        receipt_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="EXEC100",
            photo_type=PhotoType.invoice,
            file_path="/tmp/executive-receipt.jpg",
            image_url="/media/photos/executive-receipt.jpg",
            original_file_name="executive-receipt.jpg",
            captured_at_utc=datetime(2026, 6, 16, 10, 5, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 10, 5, tzinfo=timezone.utc),
        )
        report = ProgressReport(
            id="executive-company-health-report-001",
            tenant_id="default",
            company_id="default",
            project_id="EXEC100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content="{}",
            completed_at=datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 16, 11, 0, tzinfo=timezone.utc),
        )
        db.add_all([audience, template, version, binding, contract, *phrases, project, project_photo, receipt_photo, report])
        db.flush()
        db.add(
            ReceiptFact(
                id="executive-receipt-1",
                tenant_id="default",
                company_id="default",
                project_id="EXEC100",
                photo_id=receipt_photo.id,
                vendor_name="Executive Fuel",
                total_amount=10.0,
                gallons=2.0,
                purchaser_name="Alicia Field",
                employee_id="E100",
                receipt_timestamp=datetime(2026, 6, 16, 10, 6, tzinfo=timezone.utc),
                currency_code="USD",
                has_pump_photo=True,
                summary_text="private receipt summary",
                facts_json={"file_path": "/tmp/executive-receipt.jpg"},
                created_at=datetime(2026, 6, 16, 10, 7, tzinfo=timezone.utc),
            )
        )
        db.commit()

    with session_maker() as db:
        facts = build_executive_company_health_facts(
            db,
            company_id="default",
            window_days=1,
            now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        )
        assert facts["project_portfolio"]["projects_total"] >= 1
        assert facts["photo_activity"]["photos_current"] >= 1
        assert facts["progress_reports"]["completed_current"] == 1
        assert facts["finance_receipt_coverage"]["receipt_count"] == 1
        serialized_facts = json.dumps(facts, ensure_ascii=False)
        assert "E100" not in serialized_facts
        assert "Alicia Field" not in serialized_facts
        assert "executive-receipt" not in serialized_facts
        assert "private receipt summary" not in serialized_facts

        result = generate_executive_company_health_shadow(
            db,
            app_settings=settings,
            company_id="default",
            window_days=1,
            use_ai=False,
        )
        db.commit()
        artifact_id = result.artifact.id
        snapshot_id = result.snapshot.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        snapshot = db.get(FactSnapshot, snapshot_id)
        assert artifact is not None
        assert snapshot is not None
        assert artifact.artifact_type == "executive_company_health_summary"
        assert artifact.audience_id == "executive"
        assert artifact.validation_status == "shadow_fallback_valid"
        assert artifact.validation_errors_json == []
        assert artifact.raw_model_output is None
        payload = artifact.structured_json
        assert payload["visibility"] == "shadow"
        assert payload["body"]["generation_path"] == "deterministic_fallback"
        assert len(payload["company_health_surface"]["cards"]) == 5
        for card in payload["company_health_surface"]["cards"]:
            assert card["fact_refs"]
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "E100" not in serialized
        assert "Alicia Field" not in serialized
        assert "executive-receipt" not in serialized
        assert "raw_model_output" not in serialized
        assert "should" not in serialized.lower()
        assert "priority" not in serialized.lower()
        assert "rank" not in serialized.lower()

        bad_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        bad_payload["company_health_surface"]["cards"][0]["lines"][0] = "employee_id E100 should be priority ranked"
        bad_payload["body"]["paragraphs"][0] = bad_payload["company_health_surface"]["cards"][0]["lines"][0]
        audience = db.get(ExpressionAudience, "executive")
        contract = db.get(ExpressionOutputContract, "executive_company_health_summary:executive:v1")
        assert audience is not None
        assert contract is not None
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=bad_payload,
            language="en",
            facts=snapshot.facts_json,
        )
        assert any(error.startswith("executive_visibility:") for error in errors)
        assert "forbidden_phrase:(?i)\\bemployee_id\\b" in errors
        assert "forbidden_phrase:(?i)\\bshould\\b" in errors


def test_executive_company_health_shadow_audit_flags_blocking_issues(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="executive",
            code="executive",
            title="Executive",
            default_language="en",
            forbidden_phrase_set_id="executive_overview_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="executive_company_health_summary",
            slug="executive_company_health_summary",
            title="Executive company health",
            artifact_type="executive_company_health_summary",
            stage="expression",
            default_audience_id="executive",
        )
        version = ExpressionPromptVersion(
            id="executive_company_health_summary:v1",
            template_id="executive_company_health_summary",
            version="v1",
            system_prompt="Runtime assembled.",
            user_prompt_template="{facts_json}\n{contract_schema_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:executive_company_health_summary:v1",
            scope_type="global",
            scope_id=None,
            template_id="executive_company_health_summary",
            prompt_version_id="executive_company_health_summary:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="executive_company_health_summary:executive:v1",
            artifact_type="executive_company_health_summary",
            audience_id="executive",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["artifact_type", "audience", "headline_metrics", "company_health_surface", "body"],
                "properties": {
                    "artifact_type": {"type": "string", "const": "executive_company_health_summary"},
                    "audience": {"type": "string", "const": "executive"},
                    "headline_metrics": {"type": "object"},
                    "company_health_surface": {"type": "object"},
                    "body": {"type": "object"},
                },
            },
            required_fact_paths_json=[
                "scope.company_id",
                "project_portfolio.projects_total",
                "photo_activity.photos_current",
                "progress_reports.completed_current",
                "expression_readiness.shadow_invalid_current",
                "finance_receipt_coverage.receipt_count",
            ],
            forbidden_claims_json=[],
            is_active=True,
        )
        db.add_all([audience, template, version, binding, contract])
        db.commit()

    with session_maker() as db:
        result = generate_executive_company_health_shadow(
            db,
            app_settings=settings,
            company_id="default",
            window_days=30,
            use_ai=False,
        )
        db.commit()
        good_snapshot_id = result.snapshot.id

    assert (
        audit_executive_company_health_shadow(
            company_id="default",
            limit=10,
            json_output=True,
            fail_on_warnings=False,
            settings_override=settings,
        )
        == 0
    )
    good_output = json.loads(capsys.readouterr().out)
    assert good_output["status"] == "pass"
    assert good_output["blocking_issues"] == 0

    with session_maker() as db:
        snapshot = db.get(FactSnapshot, good_snapshot_id)
        assert snapshot is not None
        bad_payload = {
            "artifact_type": "executive_company_health_summary",
            "audience": "executive",
            "visibility": "shadow",
            "overall_status": "green",
            "headline_metrics": {},
            "company_health_surface": {
                "cards": [
                    {
                        "card_id": "project_portfolio",
                        "status": "green",
                        "lines": ["employee_id E100 should be priority ranked"],
                        "fact_refs": [],
                        "metrics": {},
                        "rule_flags": [],
                    }
                ]
            },
            "body": {"paragraphs": ["employee_id E100 should be priority ranked"]},
        }
        db.add(
            ExpressionArtifact(
                id="bad-executive-health-audit-artifact",
                tenant_id="default",
                company_id="default",
                fact_snapshot_id=snapshot.id,
                prompt_version_id="executive_company_health_summary:v1",
                contract_id="executive_company_health_summary:executive:v1",
                scope_type="company_health_period",
                scope_key_json={"company_id": "default"},
                artifact_type="executive_company_health_summary",
                audience_id="executive",
                language="en",
                structured_json=bad_payload,
                validation_status="shadow_invalid",
                validation_errors_json=["synthetic"],
                model_used="deterministic_fallback",
                promoted=False,
            )
        )
        db.commit()

    assert (
        audit_executive_company_health_shadow(
            company_id="default",
            limit=10,
            json_output=True,
            fail_on_warnings=False,
            settings_override=settings,
        )
        == 1
    )
    bad_output = json.loads(capsys.readouterr().out)
    assert bad_output["status"] == "fail"
    assert bad_output["totals"]["shadow_invalid"] == 1
    assert bad_output["totals"]["missing_fact_refs"] == 1
    assert bad_output["totals"]["payload_denied_text"] == 1


def test_expression_registry_bootstrap_is_dry_run_safe_and_idempotent(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        dry_run = build_expression_registry_bootstrap_payload(db, apply=False)
        assert db.scalar(select(ExpressionAudience).where(ExpressionAudience.id == "employee")) is None

    assert dry_run["status"] == "dry_run"
    assert dry_run["guardrails"] == {
        "overwrites_existing_rows": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert dry_run["totals"]["planned_changes"] > 0
    assert dry_run["totals"]["created_rows"] == 0
    serialized_dry_run = json.dumps(dry_run, ensure_ascii=False)
    assert "Facts JSON" not in serialized_dry_run
    assert "raw_model_output" not in serialized_dry_run

    assert expression_bootstrap_registry(apply=True, json_output=True, settings_override=settings) == 0
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_output["status"] == "applied"
    assert apply_output["totals"]["created_rows"] == apply_output["totals"]["planned_changes"]
    assert apply_output["totals"]["created_rows"] > 0
    assert "Facts JSON" not in json.dumps(apply_output, ensure_ascii=False)

    assert audit_expression_registry(json_output=True, fail_on_warnings=False, settings_override=settings) == 0
    registry_output = json.loads(capsys.readouterr().out)
    assert registry_output["status"] == "pass"
    assert registry_output["totals"]["artifacts_checked"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert registry_output["totals"]["blocking_issues"] == 0

    assert expression_audit_prompt_catalog(json_output=True, settings_override=settings) == 0
    catalog_output = json.loads(capsys.readouterr().out)
    assert catalog_output["status"] == "pass"
    assert catalog_output["totals"]["artifacts_ready"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert "Facts JSON" not in json.dumps(catalog_output, ensure_ascii=False)

    assert expression_bootstrap_registry(apply=False, json_output=True, settings_override=settings) == 0
    second_dry_run = json.loads(capsys.readouterr().out)
    assert second_dry_run["status"] == "dry_run"
    assert second_dry_run["totals"]["planned_changes"] == 0
    assert second_dry_run["totals"]["created_rows"] == 0


def test_expression_registry_bootstrap_deduplicates_forbidden_phrases_by_value(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        db.add(
            ExpressionAudience(
                id="operations",
                code="operations",
                title="Operations",
                default_language="zh",
                forbidden_phrase_set_id="operations_health_zh_v1",
                is_active=True,
            )
        )
        db.add(
            ExpressionForbiddenPhrase(
                id="operations-health-existing-password",
                set_id="operations_health_zh_v1",
                audience_id="operations",
                language="en",
                phrase="password",
                match_type="literal",
                severity="error",
                is_active=True,
            )
        )
        db.commit()

    with session_maker() as db:
        payload = build_expression_registry_bootstrap_payload(db, apply=False)

    planned_ids = {action["id"] for action in payload["actions"]}
    assert "operations_health_zh_v1:5e884898da28" not in planned_ids


def test_expression_registry_audit_checks_prompt_contract_gate_chain(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audiences: dict[str, ExpressionAudience] = {}
        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
            audience_id = str(expectation["audience_id"])
            if audience_id not in audiences:
                audiences[audience_id] = ExpressionAudience(
                    id=audience_id,
                    code=audience_id,
                    title=audience_id.title(),
                    default_language="en",
                    forbidden_phrase_set_id=f"{audience_id}_phrases",
                    is_active=True,
                )
        db.add_all(audiences.values())
        db.flush()
        for audience_id, audience in audiences.items():
            db.add(
                ExpressionForbiddenPhrase(
                    id=f"{audience_id}_phrases:blocked",
                    set_id=str(audience.forbidden_phrase_set_id),
                    audience_id=audience_id,
                    language="en",
                    phrase="blocked",
                    match_type="literal",
                    severity="error",
                    is_active=True,
                )
            )
        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
            template_id = str(expectation["template_id"])
            artifact_type = str(expectation["artifact_type"])
            audience_id = str(expectation["audience_id"])
            db.add_all(
                [
                    ExpressionPromptTemplate(
                        id=template_id,
                        slug=template_id,
                        title=template_id,
                        artifact_type=artifact_type,
                        stage=str(expectation["stage"]),
                        default_audience_id=audience_id,
                    ),
                    ExpressionPromptVersion(
                        id=f"{template_id}:test-v1",
                        template_id=template_id,
                        version="test-v1",
                        system_prompt="System prompt.",
                        user_prompt_template="{facts_json}",
                        status="active",
                    ),
                    ExpressionPromptBinding(
                        id=f"global:{template_id}:test-v1",
                        scope_type="global",
                        scope_id=None,
                        template_id=template_id,
                        prompt_version_id=f"{template_id}:test-v1",
                        priority=100,
                    ),
                    ExpressionOutputContract(
                        id=f"{artifact_type}:{audience_id}:test-v1",
                        artifact_type=artifact_type,
                        audience_id=audience_id,
                        version="test-v1",
                        json_schema={"type": "object"},
                        required_fact_paths_json=list(expectation["required_fact_paths"]),
                        forbidden_claims_json=[
                            {"pattern": "blocked"}
                            for _ in range(max(1, int(expectation["min_forbidden_claims"])))
                        ],
                        is_active=True,
                    ),
                ]
            )
        db.commit()

    assert audit_expression_registry(json_output=True, fail_on_warnings=False, settings_override=settings) == 0
    pass_output = json.loads(capsys.readouterr().out)
    assert pass_output["status"] == "pass"
    assert pass_output["totals"]["blocking_issues"] == 0
    assert pass_output["artifacts"][0]["promotion_gate"]
    assert pass_output["artifacts"][0]["ai_policy"]

    assert audit_expression_audience_coverage(json_output=True, fail_on_warnings=False, settings_override=settings) == 0
    coverage_output = json.loads(capsys.readouterr().out)
    assert coverage_output["status"] == "pass"
    assert coverage_output["totals"]["audiences_expected"] == 6
    assert coverage_output["totals"]["audiences_ready"] == 6
    assert coverage_output["totals"]["artifacts_ready"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    project_manager_coverage = next(
        audience for audience in coverage_output["audiences"] if audience["audience_id"] == "project_manager"
    )
    assert project_manager_coverage["expected_artifacts"] == 5
    assert all(artifact["promotion_gate"] for artifact in project_manager_coverage["artifacts"])
    serialized_coverage = json.dumps(coverage_output, ensure_ascii=False)
    assert "System prompt." not in serialized_coverage
    assert "{facts_json}" not in serialized_coverage

    with session_maker() as db:
        db.add(
            ExpressionEvalRun(
                id="eval-run-layer-readiness-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=utc_now(),
                completed_at=utc_now(),
            )
        )
        db.commit()

    assert expression_audit_layer_readiness(json_output=True, fail_on_product_risk=False, settings_override=settings) == 0
    layer_output = json.loads(capsys.readouterr().out)
    assert layer_output["status"] == "pass"
    assert layer_output["schema_version"] == "expression_layer_readiness_v1"
    assert layer_output["guardrails"] == {
        "does_not_promote_artifacts": True,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert layer_output["totals"]["artifacts_expected"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert layer_output["totals"]["artifacts_shadow_verified"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert layer_output["totals"]["artifacts_blocked"] == 0
    assert layer_output["totals"]["artifacts_replay_covered"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert all(artifact["promotion_gate"] for artifact in layer_output["artifacts"])
    assert all(artifact["replay"]["status"] == "covered" for artifact in layer_output["artifacts"])
    assert "raw_model_output" not in json.dumps(layer_output, ensure_ascii=False)

    with session_maker() as db:
        broken = db.get(ExpressionOutputContract, "employee_contribution_narrative:employee:test-v1")
        assert broken is not None
        broken.required_fact_paths_json = ["scope.employee_id"]
        db.commit()

    assert audit_expression_registry(json_output=True, fail_on_warnings=False, settings_override=settings) == 1
    fail_output = json.loads(capsys.readouterr().out)
    assert fail_output["status"] == "fail"
    assert fail_output["totals"]["missing_required_fact_paths"] == 1
    assert any(
        artifact["artifact_type"] == "employee_contribution_narrative"
        and "missing_required_fact_paths" in artifact["issues"]
        for artifact in fail_output["artifacts"]
    )

    db_contract_deleted = False
    with session_maker() as db:
        broken = db.get(ExpressionOutputContract, "employee_contribution_narrative:employee:test-v1")
        assert broken is not None
        db.delete(broken)
        db.commit()
        db_contract_deleted = True
    assert db_contract_deleted
    assert audit_expression_audience_coverage(json_output=True, fail_on_warnings=False, settings_override=settings) == 1
    coverage_fail_output = json.loads(capsys.readouterr().out)
    employee_coverage = next(
        audience for audience in coverage_fail_output["audiences"] if audience["audience_id"] == "employee"
    )
    assert coverage_fail_output["totals"]["missing_contract"] == 1
    assert employee_coverage["missing_artifacts"] == ["employee_contribution_narrative"]
    assert "missing_active_contract" in employee_coverage["artifacts"][0]["issues"]


def test_surface_immutability_validators_reject_locked_field_drift():
    baseline = {
        "artifact_type": "client_progress_summary",
        "summary_surface": {
            "computed_at": "2026-06-16T00:00:00Z",
            "items": [
                {
                    "item_id": "client_photos",
                    "deterministic_summary": "Client-visible photos: 3.",
                    "fact_refs": [{"field_path": "client_visible_photo_metrics.photos_current", "observed_value": 3}],
                }
            ],
        },
        "body": {"paragraphs": ["Client-visible photos: 3."], "generation_path": "deterministic_fallback"},
        "validation": {"summary_surface_locked": True},
    }
    polished = _surface_summary_polished_payload(
        fallback_payload=baseline,
        candidate={"body": {"paragraphs": ["Polished client-safe wording with the same facts."]}},
    )
    assert polished["body"]["generation_path"] == "ai_polish"
    assert polished["summary_surface"] == baseline["summary_surface"]
    assert _surface_immutability_errors(
        artifact_type="client_progress_summary",
        baseline=baseline,
        candidate=polished,
        partial_candidate=False,
    ) == []

    tampered = json.loads(json.dumps(polished, ensure_ascii=False))
    tampered["summary_surface"]["items"][0]["deterministic_summary"] = "AI rewrote the locked surface."
    assert "client_summary:immutability:summary_surface:changed" in _surface_immutability_errors(
        artifact_type="client_progress_summary",
        baseline=baseline,
        candidate=tampered,
        partial_candidate=False,
    )

    finance_baseline = {
        "artifact_type": "finance_summary",
        "headline_metrics": {"receipt_count": 2, "total_amount": 10.0, "receipts_missing_amount": 0, "fuel_gallons_total": 0},
        "status_flags": ["missing_amount"],
        "finance_surface": {
            "computed_at": "2026-06-16T00:00:00Z",
            "sections": [
                {
                    "section_id": "receipt_totals",
                    "lines": ["Receipt count: 2."],
                    "fact_refs": [{"field_path": "receipt_metrics.receipt_count", "observed_value": 2}],
                }
            ],
        },
    }
    finance_candidate = json.loads(json.dumps(finance_baseline, ensure_ascii=False))
    finance_candidate["headline_metrics"]["receipt_count"] = 99
    assert "finance_summary:immutability:headline_metrics:changed" in _surface_immutability_errors(
        artifact_type="finance_summary",
        baseline=finance_baseline,
        candidate=finance_candidate,
        partial_candidate=True,
    )

    ops_baseline = {
        "artifact_type": "operations_health_summary",
        "overall_status": "amber",
        "health_surface": {
            "computed_at": "2026-06-16T00:00:00Z",
            "dimensions": [
                {
                    "dimension_id": "task_jobs",
                    "status": "amber",
                    "deterministic_summary": "Task jobs show 21 queued jobs.",
                    "fact_refs": [{"field_path": "task_jobs.queued_backlog", "observed_value": 21}],
                }
            ],
        },
    }
    body_only_candidate = {"body": {"paragraphs": ["Task jobs show 21 queued jobs."]}}
    assert _surface_immutability_errors(
        artifact_type="operations_health_summary",
        baseline=ops_baseline,
        candidate=body_only_candidate,
        partial_candidate=True,
    ) == []

    exec_baseline = {
        "artifact_type": "executive_company_health_summary",
        "overall_status": "green",
        "headline_metrics": {
            "projects_total": 4,
            "photos_current": 10,
            "completed_reports_current": 2,
            "shadow_invalid_current": 0,
            "receipt_count": 3,
        },
        "company_health_surface": {
            "computed_at": "2026-06-16T00:00:00Z",
            "cards": [
                {
                    "card_id": "project_portfolio",
                    "status": "green",
                    "lines": ["Project portfolio shows 4 projects."],
                    "fact_refs": [{"field_path": "project_portfolio.projects_total", "observed_value": 4}],
                }
            ],
        },
    }
    exec_candidate = json.loads(json.dumps(exec_baseline, ensure_ascii=False))
    exec_candidate["company_health_surface"]["cards"][0]["fact_refs"][0]["observed_value"] = 5
    assert "executive_company_health:immutability:fact_refs:changed" in _surface_immutability_errors(
        artifact_type="executive_company_health_summary",
        baseline=exec_baseline,
        candidate=exec_candidate,
        partial_candidate=False,
    )


def test_expression_target_roadmap_gap_audit_is_machine_readable_without_ai(capsys):
    summary = build_expression_target_roadmap_gap_audit(registry_expectations=EXPRESSION_REGISTRY_EXPECTATIONS)
    assert summary["schema_version"] == ROADMAP_SCHEMA_VERSION
    assert summary["status"] == "inform"
    assert summary["guardrails"]["uses_ai"] is False
    assert summary["totals"]["registry_artifacts"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert summary["totals"]["future_targets"] == len(EXPRESSION_FUTURE_ROADMAP_TARGETS)
    assert summary["totals"]["hard_bug"] == 0
    assert summary["totals"]["product_risk"] == 0
    assert summary["registry_gaps"] == []
    assert not any(
        gap["gap_id"].startswith("missing_immutability_validator:")
        for gap in summary["registry_gaps"]
    )
    assert summary["totals"]["future"] >= 3

    registry_types = {row["artifact_type"] for row in summary["registry_implementation"]}
    assert registry_types == set(EXPRESSION_ARTIFACT_IMPLEMENTATION)
    platform_target = next(
        target for target in summary["future_targets"] if target["target_id"] == "audience:platform_admin"
    )
    assert platform_target["gaps"] == ["audience_not_in_registry_expectations"]
    immutability_target = next(
        target for target in summary["future_targets"] if target["target_id"] == "capability:surface_immutability_validators"
    )
    assert immutability_target["ready"] is True
    assert immutability_target["gaps"] == []
    assert immutability_target["implemented_by"] == "surface_immutability"
    assert not any(
        gap["gap_id"] == "future_target:capability:surface_immutability_validators"
        for gap in summary["future_gaps"]
    )
    eval_target = next(
        target for target in summary["future_targets"] if target["target_id"] == "infrastructure:expression_eval_runs"
    )
    assert eval_target["ready"] is True
    assert eval_target["gaps"] == []
    assert eval_target["implemented_by"] == (
        "ExpressionEvalRun/ExpressionEvalItem + expression-golden-replay + expression-audit-eval-readiness"
    )
    assert not any(
        gap["gap_id"] == "future_target:infrastructure:expression_eval_runs"
        for gap in summary["future_gaps"]
    )
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "System prompt." not in serialized
    assert "{facts_json}" not in serialized

    assert audit_expression_target_roadmap(json_output=True, fail_on_hard_bugs=True, fail_on_product_risk=False) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["schema_version"] == ROADMAP_SCHEMA_VERSION

    assert audit_expression_target_roadmap(json_output=True, fail_on_hard_bugs=False, fail_on_product_risk=True) == 0
    product_risk_output = json.loads(capsys.readouterr().out)
    assert product_risk_output["status"] == "inform"


def test_expression_golden_replay_revalidates_shadow_artifact_without_ai(app_context, capsys):
    settings = app_context["settings"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            visibility_policy_json={"show_to": ["self"]},
            forbidden_phrase_set_id="employee_contribution_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="employee_contribution_narrative",
            slug="employee_contribution_narrative",
            title="Employee contribution narrative",
            artifact_type="employee_contribution_narrative",
            stage="expression",
            default_audience_id="employee",
        )
        version = ExpressionPromptVersion(
            id="employee_contribution_narrative:v1",
            template_id="employee_contribution_narrative",
            version="v1",
            system_prompt="只根据 facts 写中文。",
            user_prompt_template="{facts_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:employee_contribution_narrative:v1",
            scope_type="global",
            scope_id=None,
            template_id="employee_contribution_narrative",
            prompt_version_id="employee_contribution_narrative:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="employee_contribution_narrative:employee:v1",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["summary_line"],
                "properties": {"summary_line": {"type": "string"}},
            },
            required_fact_paths_json=["scope.employee_id", "scope.project_id"],
            forbidden_claims_json=["no ranking"],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="employee_contribution_zh_v1:performance",
            set_id="employee_contribution_zh_v1",
            audience_id="employee",
            language="zh",
            phrase="绩效",
            match_type="literal",
            severity="error",
            is_active=True,
        )
        snapshot = FactSnapshot(
            id="snapshot-golden-replay-000000000001",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            scope_type="employee_project_window",
            scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
            scope_key_hash="hash-golden-replay-e100",
            assembler_version="v1",
            source_manifest_json={"tables": ["photos"]},
            facts_json={
                "scope": {"employee_id": "E100", "project_id": "P100"},
                "counts": {"photos_current": 3},
            },
            fact_count=2,
            created_at=utc_now(),
        )
        artifact = ExpressionArtifact(
            id="artifact-golden-replay-000000000001",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id="employee_contribution_narrative:v1",
            contract_id=contract.id,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "最近 30 天有连续的现场记录。"},
            validation_status="promoted_valid",
            validation_errors_json=[],
            promoted=True,
        )
        db.add_all([audience, template, version, binding, contract, phrase, snapshot, artifact])
        db.commit()

    assert expression_golden_replay(
        company_id="default",
        artifact_type="employee_contribution_narrative",
        limit=10,
        json_output=True,
        settings_override=settings,
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["guardrails"]["uses_ai"] is False
    assert output["guardrails"]["mutates_promotion"] is False
    assert output["totals"] == {"cases": 1, "failed": 0, "passed": 1, "skipped": 0}
    assert output["artifact_type_counts"] == {
        "employee_contribution_narrative": {"cases": 1, "failed": 0, "passed": 1, "skipped": 0}
    }
    assert output["status"] == "pass"

    with session_maker() as db:
        run = db.scalar(
            select(ExpressionEvalRun)
            .where(ExpressionEvalRun.id == output["eval_run_id"])
        )
        assert run is not None
        assert run.run_type == "golden_replay"
        assert run.summary_json["artifact_type_counts"] == output["artifact_type_counts"]
        item = db.scalar(
            select(ExpressionEvalItem).where(ExpressionEvalItem.eval_run_id == run.id)
        )
        assert item is not None
        assert item.status == "pass"
        assert item.validator_name == "expression_payload"
        assert item.artifact_id == "artifact-golden-replay-000000000001"
        assert item.fact_snapshot_id == "snapshot-golden-replay-000000000001"
        assert item.expected_json["validation_status"] == "promoted_valid"

    assert expression_audit_eval_readiness(json_output=True, fail_on_missing_golden=True, settings_override=settings) == 0
    readiness_output = json.loads(capsys.readouterr().out)
    assert readiness_output["latest_golden_replay"]["id"] == output["eval_run_id"]
    assert readiness_output["latest_golden_replay"]["summary"]["artifact_type_counts"] == output["artifact_type_counts"]


def test_expression_revalidate_artifacts_dry_run_and_apply_only_touch_unpromoted(app_context, capsys):
    settings = app_context["settings"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            visibility_policy_json={"show_to": ["self"]},
            forbidden_phrase_set_id="employee_contribution_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="employee_contribution_narrative",
            slug="employee_contribution_narrative",
            title="Employee contribution narrative",
            artifact_type="employee_contribution_narrative",
            stage="expression",
            default_audience_id="employee",
        )
        version = ExpressionPromptVersion(
            id="employee_contribution_narrative:v1",
            template_id="employee_contribution_narrative",
            version="v1",
            system_prompt="只根据 facts 写中文。",
            user_prompt_template="{facts_json}",
            status="active",
        )
        binding = ExpressionPromptBinding(
            id="global:employee_contribution_narrative:v1",
            scope_type="global",
            scope_id=None,
            template_id="employee_contribution_narrative",
            prompt_version_id="employee_contribution_narrative:v1",
            priority=100,
        )
        contract = ExpressionOutputContract(
            id="employee_contribution_narrative:employee:v1",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            version="v1",
            json_schema={
                "type": "object",
                "required": ["summary_line"],
                "properties": {"summary_line": {"type": "string"}},
            },
            required_fact_paths_json=["scope.employee_id", "scope.project_id"],
            forbidden_claims_json=[],
            is_active=True,
        )
        phrase = ExpressionForbiddenPhrase(
            id="employee_contribution_zh_v1:performance",
            set_id="employee_contribution_zh_v1",
            audience_id="employee",
            language="zh",
            phrase="绩效",
            match_type="literal",
            severity="error",
            is_active=True,
        )
        snapshot = FactSnapshot(
            id="snapshot-revalidate-employee-000000000001",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            scope_type="employee_project_window",
            scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
            scope_key_hash="hash-revalidate-e100",
            assembler_version="v1",
            source_manifest_json={"tables": ["photos"]},
            facts_json={
                "scope": {"employee_id": "E100", "project_id": "P100"},
                "counts": {"photos_current": 3},
            },
            fact_count=2,
            created_at=utc_now(),
        )
        stale_shadow = ExpressionArtifact(
            id="artifact-revalidate-shadow-000000000001",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id="employee_contribution_narrative:v1",
            contract_id=contract.id,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "这是一条绩效说明。"},
            validation_status="shadow_valid",
            validation_errors_json=[],
            promoted=False,
        )
        promoted = ExpressionArtifact(
            id="artifact-revalidate-promoted-000000000001",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id="employee_contribution_narrative:v1",
            contract_id=contract.id,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "这条 promoted 也包含绩效，但工具不碰它。"},
            validation_status="promoted_valid",
            validation_errors_json=[],
            promoted=True,
        )
        db.add_all([audience, template, version, binding, contract, phrase, snapshot, stale_shadow, promoted])
        db.commit()

    assert (
        expression_revalidate_artifacts(
            artifact_type="employee_contribution_narrative",
            company_id="default",
            limit=20,
            apply=False,
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    dry_output = json.loads(capsys.readouterr().out)
    assert dry_output["status"] == "dry_run"
    assert dry_output["guardrails"]["touches_promoted_artifacts"] is False
    assert dry_output["totals"]["artifacts_selected"] == 1
    assert dry_output["totals"]["would_update"] == 1
    assert dry_output["runs"][0]["action"] == "would_mark_shadow_invalid"

    with session_maker() as db:
        assert db.get(ExpressionArtifact, "artifact-revalidate-shadow-000000000001").validation_status == "shadow_valid"
        assert db.get(ExpressionArtifact, "artifact-revalidate-promoted-000000000001").validation_status == "promoted_valid"

    assert (
        expression_revalidate_artifacts(
            artifact_type="employee_contribution_narrative",
            company_id="default",
            limit=20,
            apply=True,
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_output["status"] == "applied"
    assert apply_output["totals"]["updated"] == 1
    assert apply_output["runs"][0]["previous_validation_status"] == "shadow_valid"
    assert apply_output["runs"][0]["current_validation_status"] == "shadow_invalid"
    assert "raw_model_output" not in json.dumps(apply_output, ensure_ascii=False)

    with session_maker() as db:
        stale = db.get(ExpressionArtifact, "artifact-revalidate-shadow-000000000001")
        visible = db.get(ExpressionArtifact, "artifact-revalidate-promoted-000000000001")
        assert stale.validation_status == "shadow_invalid"
        assert any("forbidden_phrase:绩效" in error for error in stale.validation_errors_json)
        assert visible.validation_status == "promoted_valid"


def test_expression_eval_readiness_audit_is_db_backed_without_ai(app_context, capsys):
    settings = app_context["settings"]
    session_maker = app_context["session_maker"]

    assert expression_audit_eval_readiness(json_output=True, fail_on_missing_golden=False, settings_override=settings) == 0
    empty_output = json.loads(capsys.readouterr().out)
    assert empty_output["status"] == "pass"
    assert empty_output["guardrails"] == {
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert empty_output["totals"] == {"eval_items": 0, "eval_runs": 0, "golden_replay_runs": 0}

    assert expression_audit_eval_readiness(json_output=True, fail_on_missing_golden=True, settings_override=settings) == 1
    missing_output = json.loads(capsys.readouterr().out)
    assert missing_output["blocking_reasons"] == ["missing_golden_replay_run"]

    with session_maker() as db:
        base_time = utc_now()
        employee_failed_run = ExpressionEvalRun(
            id="eval-run-000000000000000000000000",
            company_id="default",
            run_type="golden_replay",
            status="fail",
            trigger_source="pytest",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            runner_version="eval_run_v1",
            input_manifest_json={"artifact_types": ["employee_contribution_narrative"]},
            summary_json={
                "cases": 1,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "artifact_type_counts": {
                    "employee_contribution_narrative": {"cases": 1, "passed": 0, "failed": 1, "skipped": 0}
                },
            },
            created_at=base_time - timedelta(seconds=20),
            started_at=base_time - timedelta(seconds=30),
            completed_at=base_time - timedelta(seconds=20),
        )
        employee_passed_run = ExpressionEvalRun(
            id="eval-run-000000000000000000000003",
            company_id="default",
            run_type="golden_replay",
            status="pass",
            trigger_source="pytest",
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            runner_version="eval_run_v1",
            input_manifest_json={"artifact_types": ["employee_contribution_narrative"]},
            summary_json={
                "cases": 2,
                "passed": 2,
                "failed": 0,
                "skipped": 0,
                "artifact_type_counts": {
                    "employee_contribution_narrative": {"cases": 2, "passed": 2, "failed": 0, "skipped": 0}
                },
            },
            created_at=base_time - timedelta(seconds=5),
            started_at=base_time - timedelta(seconds=10),
            completed_at=base_time - timedelta(seconds=5),
        )
        run = ExpressionEvalRun(
            id="eval-run-000000000000000000000001",
            company_id="default",
            run_type="golden_replay",
            status="pass",
            trigger_source="pytest",
            artifact_type="project_manager_decision_brief",
            audience_id="project_manager",
            runner_version="eval_run_v1",
            input_manifest_json={"artifact_types": ["project_manager_decision_brief"]},
            summary_json={
                "cases": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "artifact_type_counts": {
                    "project_manager_decision_brief": {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                },
            },
            created_at=base_time,
            started_at=base_time,
            completed_at=base_time,
        )
        item = ExpressionEvalItem(
            id="eval-item-000000000000000000000001",
            eval_run_id=run.id,
            case_key="project_manager_decision_brief:P100:baseline",
            status="pass",
            validator_name="decision_surface",
            expected_json={"validation_status": "shadow_fallback_valid"},
            actual_json={"validation_status": "shadow_fallback_valid"},
            metrics_json={"fact_refs": 5},
        )
        executive_run = ExpressionEvalRun(
            id="eval-run-000000000000000000000002",
            company_id="default",
            run_type="golden_replay",
            status="pass",
            trigger_source="pytest",
            artifact_type="executive_company_health_summary",
            audience_id="executive",
            runner_version="eval_run_v1",
            input_manifest_json={"artifact_types": ["executive_company_health_summary"]},
            summary_json={
                "cases": 2,
                "passed": 2,
                "failed": 0,
                "skipped": 0,
                "artifact_type_counts": {
                    "executive_company_health_summary": {"cases": 2, "passed": 2, "failed": 0, "skipped": 0}
                },
            },
            created_at=base_time + timedelta(seconds=1),
            started_at=base_time + timedelta(seconds=1),
            completed_at=base_time + timedelta(seconds=1),
        )
        db.add_all([employee_failed_run, employee_passed_run, run, item, executive_run])
        db.commit()

    assert expression_audit_eval_readiness(json_output=True, fail_on_missing_golden=True, settings_override=settings) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "pass"
    assert output["totals"] == {"eval_items": 1, "eval_runs": 4, "golden_replay_runs": 4}
    assert output["run_status_counts"] == {"fail": 1, "pass": 3}
    assert output["item_status_counts"] == {"pass": 1}
    assert output["latest_golden_replay"]["id"] == "eval-run-000000000000000000000002"
    assert output["latest_golden_replay"]["summary"]["artifact_type_counts"] == {
        "executive_company_health_summary": {"cases": 2, "passed": 2, "failed": 0, "skipped": 0}
    }
    assert output["replay_artifact_type_counts"] == {
        "employee_contribution_narrative": {"cases": 2, "failed": 0, "passed": 2, "skipped": 0},
        "executive_company_health_summary": {"cases": 2, "failed": 0, "passed": 2, "skipped": 0},
        "project_manager_decision_brief": {"cases": 1, "failed": 0, "passed": 1, "skipped": 0},
    }
    coverage = output["registry_replay_coverage"]
    assert coverage["schema_version"] == "registry_replay_coverage_v1"
    assert coverage["totals"] == {
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_covered": 3,
        "artifacts_with_failures": 0,
        "below_min_cases": 0,
        "missing_samples": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 3,
    }
    coverage_statuses = {artifact["artifact_type"]: artifact["status"] for artifact in coverage["artifacts"]}
    assert coverage_statuses["employee_contribution_narrative"] == "covered"
    assert coverage_statuses["project_manager_decision_brief"] == "covered"
    assert coverage_statuses["executive_company_health_summary"] == "covered"
    assert "raw_model_output" not in json.dumps(output, ensure_ascii=False)


def test_expression_promotion_surface_readiness_enforces_promoted_only_visibility(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = utc_now()

    with session_maker() as db:
        db.add_all(
            [
                ExpressionAudience(
                    id="employee",
                    code="employee",
                    title="Employee mobile contribution",
                    default_language="zh",
                    is_active=True,
                ),
                ExpressionAudience(
                    id="project_manager",
                    code="project_manager",
                    title="Project manager",
                    default_language="zh",
                    is_active=True,
                ),
                FactSnapshot(
                    id="snapshot-promotion-employee",
                    company_id="default",
                    employee_id="E100",
                    project_id="P100",
                    scope_type="employee_project_window",
                    scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
                    scope_key_hash="promotion-employee",
                    assembler_version="test",
                    source_manifest_json={"tables": ["photos"]},
                    facts_json={"scope": {"employee_id": "E100", "project_id": "P100"}},
                    fact_count=2,
                    coverage_json={},
                    created_at=base_time,
                ),
                FactSnapshot(
                    id="snapshot-promotion-manager",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="promotion-manager",
                    assembler_version="test",
                    source_manifest_json={"tables": ["photos", "progress_reports"]},
                    facts_json={"scope": {"project_id": "P100"}},
                    fact_count=1,
                    coverage_json={},
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-promotion-employee",
                    company_id="default",
                    fact_snapshot_id="snapshot-promotion-employee",
                    scope_type="employee_project_window",
                    scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
                    artifact_type="employee_contribution_narrative",
                    audience_id="employee",
                    language="zh",
                    structured_json={"summary_line": "最近记录窗口内共有 1 张现场照片。"},
                    raw_model_output='{"summary_line":"最近记录窗口内共有 1 张现场照片。"}',
                    rendered_markdown="最近记录窗口内共有 1 张现场照片。",
                    validation_status="promoted_valid",
                    validation_errors_json=[],
                    promoted=True,
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-promotion-manager-review",
                    company_id="default",
                    fact_snapshot_id="snapshot-promotion-manager",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="zh",
                    structured_json={"decision_surface": {"status": "steady"}, "fact_refs": []},
                    raw_model_output='{"decision_surface":{"status":"steady"}}',
                    rendered_markdown="项目状态卡",
                    validation_status="promoted_valid",
                    validation_errors_json=[],
                    promoted=True,
                    created_at=base_time,
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        payload = build_expression_promotion_surface_readiness_payload(db)

    assert payload["status"] == "pass"
    assert payload["guardrails"] == {
        "uses_ai": False,
        "writes_database": False,
        "uses_filesystem_scan": False,
        "raw_output_included": False,
        "does_not_promote_artifacts": True,
        "source_tables": ["expression_artifacts"],
    }
    artifacts = {item["artifact_type"]: item for item in payload["artifacts"]}
    assert artifacts["employee_contribution_narrative"]["readiness_status"] == "promotion_surface_verified"
    assert artifacts["employee_contribution_narrative"]["verified_surfaces"][0]["guardrails"][
        "requires_validation_status"
    ] == "promoted_valid"
    assert artifacts["employee_contribution_narrative"]["verified_surfaces"][0]["guardrails"][
        "shadow_artifacts_visible"
    ] is False
    assert artifacts["project_manager_decision_brief"]["readiness_status"] == "promotion_surface_verified"
    assert artifacts["project_manager_decision_brief"]["verified_surfaces"][0]["route"] == (
        "/api/mobile/projects/{project_id}/project-manager-status-card"
    )
    assert artifacts["project_manager_decision_brief"]["verified_surfaces"][0]["guardrails"][
        "shadow_artifacts_visible"
    ] is False
    assert "raw_model_output" not in json.dumps(payload, ensure_ascii=False)

    assert expression_audit_promotion_surfaces(json_output=True, fail_on_review=True, settings_override=settings) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "pass"
    assert output["totals"]["promotion_surface_needs_review"] == 0


def test_project_manager_status_card_contract_is_promoted_only_without_leaking_body(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with session_maker() as db:
        db.add_all(
            [
                ExpressionOutputContract(
                    id="project_manager_decision_brief:project_manager:status-card-test",
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    version="status-card-test",
                    json_schema={"type": "object"},
                    required_fact_paths_json=[
                        "scope.project_id",
                        "photo_metrics.photos_current",
                        "progress_report_metrics.completed_with_content",
                    ],
                    forbidden_claims_json=[{"pattern": "blocked"}],
                    is_active=True,
                ),
                FactSnapshot(
                    id="snapshot-status-card-good",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="snapshot-status-card-good",
                    assembler_version="test",
                    source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                    facts_json={
                        "scope": {"project_id": "P100"},
                        "photo_metrics": {"photos_current": 3},
                        "progress_report_metrics": {"completed_with_content": 1},
                    },
                    fact_count=3,
                    coverage_json={},
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-status-card-good-shadow",
                    company_id="default",
                    fact_snapshot_id="snapshot-status-card-good",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="en",
                    structured_json={
                        "decision_surface": {
                            "items": [
                                {
                                    "item_id": "evidence_volume_current",
                                    "category": "evidence_coverage",
                                    "severity": "info",
                                    "deterministic_summary": "The current project window has 3 database photos.",
                                    "fact_refs": [
                                        {
                                            "field_path": "photo_metrics.photos_current",
                                            "observed_value": 3,
                                        }
                                    ],
                                    "metrics": {"photos_current": 3},
                                }
                            ]
                        },
                        "body": {
                            "format": "markdown",
                            "paragraphs": ["The current project window has 3 database photos."],
                            "word_count": 8,
                            "generation_path": "deterministic_fallback",
                        },
                        "validation": {
                            "decision_surface_locked": True,
                            "ai_changed_decision_surface": False,
                        },
                    },
                    raw_model_output='{"body":{"paragraphs":["This raw text stays private."]}}',
                    rendered_markdown="hidden body",
                    validation_status="shadow_fallback_valid",
                    validation_errors_json=[],
                    promoted=False,
                    created_at=base_time,
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        payload = build_project_manager_status_card_contract_payload(
            db,
            company_id="default",
            sample_limit=10,
        )

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "project_manager_status_card_contract_v1"
    assert payload["guardrails"] == {
        "exposes_user_visible_content": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "server_computed_response": True,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["read_contract"]["state"] == "promoted_only_surface_declared"
    assert payload["read_contract"]["visible_artifact_filter"] == {
        "artifact_type": "project_manager_decision_brief",
        "audience_id": "project_manager",
        "promoted": True,
        "validation_status": "promoted_valid",
        "company_project_scoped": True,
    }
    assert payload["read_contract"]["visibility_rules"]["shadow_artifacts_visible"] is False
    assert payload["read_contract"]["visibility_rules"]["app_may_compute_metrics"] is False
    assert payload["totals"]["promoted_contract_issues"] == 0
    assert payload["samples"][0]["checks"]["fact_ref_count"] == 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "This raw text stays private" not in serialized
    assert "raw_model_output" not in serialized
    assert "should" not in serialized.lower()

    with session_maker() as db:
        db.add_all(
            [
                FactSnapshot(
                    id="snapshot-status-card-bad",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="snapshot-status-card-bad",
                    assembler_version="test",
                    source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                    facts_json={"scope": {"project_id": "P100"}, "photo_metrics": {"photos_current": 1}},
                    fact_count=2,
                    coverage_json={},
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-status-card-bad-promoted",
                    company_id="default",
                    fact_snapshot_id="snapshot-status-card-bad",
                    scope_type="project_window",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="en",
                    structured_json={
                        "decision_surface": {"items": []},
                        "body": {"paragraphs": ["This should stay out of user-visible routes."]},
                        "validation": {
                            "decision_surface_locked": False,
                            "ai_changed_decision_surface": True,
                        },
                    },
                    validation_status="promoted_valid",
                    validation_errors_json=[],
                    promoted=True,
                    created_at=base_time + timedelta(seconds=1),
                ),
            ]
        )
        db.commit()

    assert (
        expression_audit_project_manager_status_card_contract(
            company_id="default",
            sample_limit=10,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "fail"
    assert cli_output["totals"]["promoted_contract_issues"] == 1
    assert "promoted_artifacts_contract_issues" in cli_output["blockers"]
    serialized_cli = json.dumps(cli_output, ensure_ascii=False)
    assert "This should stay out" not in serialized_cli
    assert "raw_model_output" not in serialized_cli


def test_project_manager_status_card_promotion_preflight_promotes_only_after_guardrails(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    good_payload = {
        "decision_surface": {
            "items": [
                {
                    "item_id": "evidence_volume_current",
                    "category": "evidence_coverage",
                    "severity": "info",
                    "deterministic_summary": "The current project window has 4 database photos.",
                    "fact_refs": [
                        {
                            "field_path": "photo_metrics.photos_current",
                            "observed_value": 4,
                        }
                    ],
                    "metrics": {"photos_current": 4},
                }
            ]
        },
        "body": {
            "format": "markdown",
            "paragraphs": ["The current project window has 4 database photos."],
            "word_count": 8,
            "generation_path": "deterministic_fallback",
        },
        "validation": {
            "decision_surface_locked": True,
            "ai_changed_decision_surface": False,
        },
    }

    with session_maker() as db:
        db.add(
            ExpressionOutputContract(
                id="project_manager_decision_brief:project_manager:promotion-test",
                artifact_type="project_manager_decision_brief",
                audience_id="project_manager",
                version="promotion-test",
                json_schema={"type": "object"},
                required_fact_paths_json=["photo_metrics.photos_current"],
                forbidden_claims_json=[{"pattern": "blocked"}],
                is_active=True,
            )
        )
        db.add_all(
            [
                FactSnapshot(
                    id="snapshot-pm-promotion-old",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="snapshot-pm-promotion-old",
                    assembler_version="test",
                    source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                    facts_json={"photo_metrics": {"photos_current": 4}},
                    fact_count=1,
                    coverage_json={},
                    created_at=base_time,
                ),
                FactSnapshot(
                    id="snapshot-pm-promotion-new",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="snapshot-pm-promotion-new",
                    assembler_version="test",
                    source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                    facts_json={"photo_metrics": {"photos_current": 4}},
                    fact_count=1,
                    coverage_json={},
                    created_at=base_time + timedelta(seconds=1),
                ),
                ExpressionArtifact(
                    id="artifact-pm-promotion-old",
                    company_id="default",
                    fact_snapshot_id="snapshot-pm-promotion-old",
                    contract_id="project_manager_decision_brief:project_manager:promotion-test",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="en",
                    structured_json=good_payload,
                    raw_model_output='{"private":"old"}',
                    rendered_markdown="old private body",
                    validation_status="promoted_valid",
                    validation_errors_json=[],
                    promoted=True,
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-pm-promotion-new",
                    company_id="default",
                    fact_snapshot_id="snapshot-pm-promotion-new",
                    contract_id="project_manager_decision_brief:project_manager:promotion-test",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="en",
                    structured_json=good_payload,
                    raw_model_output='{"private":"new"}',
                    rendered_markdown="new private body",
                    validation_status="shadow_fallback_valid",
                    validation_errors_json=[],
                    promoted=False,
                    created_at=base_time + timedelta(seconds=1),
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        preflight = build_project_manager_status_card_promotion_preflight_payload(
            db,
            artifact_id="artifact-pm-promotion-new",
        )
        assert db.get(ExpressionArtifact, "artifact-pm-promotion-new").promoted is False

    assert preflight["status"] == "pass"
    assert preflight["guardrails"] == {
        "exposes_user_visible_content": False,
        "prompt_text_included": False,
        "promotes_artifact": False,
        "raw_output_included": False,
        "server_computed_response": True,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert preflight["target"]["artifact_type"] == "project_manager_decision_brief"
    assert preflight["target_sample"]["checks"]["fact_ref_count"] == 1
    assert preflight["contract_summary"]["verified_read_surfaces"] == 1
    serialized_preflight = json.dumps(preflight, ensure_ascii=False)
    assert "new private body" not in serialized_preflight
    assert "raw_model_output" not in serialized_preflight

    assert (
        expression_audit_project_manager_status_card_promotion(
            artifact_id="artifact-pm-promotion-new",
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    audit_output = json.loads(capsys.readouterr().out)
    assert audit_output["status"] == "pass"
    assert "raw_model_output" not in json.dumps(audit_output, ensure_ascii=False)

    assert (
        expression_promote_project_manager_status_card(
            artifact_id="artifact-pm-promotion-new",
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    promote_output = json.loads(capsys.readouterr().out)
    assert promote_output["status"] == "promoted"
    assert promote_output["artifact"]["validation_status"] == "promoted_valid"
    assert promote_output["artifact"]["promoted"] is True
    assert promote_output["artifact"]["supersedes_artifact_id"] == "artifact-pm-promotion-old"
    assert "raw_model_output" not in json.dumps(promote_output, ensure_ascii=False)

    with session_maker() as db:
        old_artifact = db.get(ExpressionArtifact, "artifact-pm-promotion-old")
        new_artifact = db.get(ExpressionArtifact, "artifact-pm-promotion-new")
        assert old_artifact.promoted is False
        assert new_artifact.promoted is True
        assert new_artifact.validation_status == "promoted_valid"


def test_project_manager_status_card_promotion_preflight_blocks_action_language(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with session_maker() as db:
        db.add(
            ExpressionOutputContract(
                id="project_manager_decision_brief:project_manager:promotion-block-test",
                artifact_type="project_manager_decision_brief",
                audience_id="project_manager",
                version="promotion-block-test",
                json_schema={"type": "object"},
                required_fact_paths_json=["photo_metrics.photos_current"],
                forbidden_claims_json=[{"pattern": "blocked"}],
                is_active=True,
            )
        )
        db.add_all(
            [
                FactSnapshot(
                    id="snapshot-pm-promotion-blocked",
                    company_id="default",
                    employee_id=None,
                    project_id="P100",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    scope_key_hash="snapshot-pm-promotion-blocked",
                    assembler_version="test",
                    source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                    facts_json={"photo_metrics": {"photos_current": 1}},
                    fact_count=1,
                    coverage_json={},
                    created_at=base_time,
                ),
                ExpressionArtifact(
                    id="artifact-pm-promotion-blocked",
                    company_id="default",
                    fact_snapshot_id="snapshot-pm-promotion-blocked",
                    contract_id="project_manager_decision_brief:project_manager:promotion-block-test",
                    scope_type="project_period",
                    scope_key_json={"project_id": "P100", "window_days": 30},
                    artifact_type="project_manager_decision_brief",
                    audience_id="project_manager",
                    language="en",
                    structured_json={
                        "decision_surface": {"items": []},
                        "body": {"paragraphs": ["This should remain private."]},
                        "validation": {
                            "decision_surface_locked": False,
                            "ai_changed_decision_surface": False,
                        },
                    },
                    raw_model_output='{"private":"blocked"}',
                    rendered_markdown="blocked private body",
                    validation_status="shadow_fallback_valid",
                    validation_errors_json=[],
                    promoted=False,
                    created_at=base_time,
                ),
            ]
        )
        db.commit()

    with session_maker() as db:
        preflight = build_project_manager_status_card_promotion_preflight_payload(
            db,
            artifact_id="artifact-pm-promotion-blocked",
        )

    assert preflight["status"] == "fail"
    assert "target_artifact_contract_issues" in preflight["blockers"]
    assert "action_language_hit" in preflight["target_sample"]["issues"]
    assert "decision_surface_items_empty" in preflight["target_sample"]["issues"]
    serialized_preflight = json.dumps(preflight, ensure_ascii=False)
    assert "This should remain private" not in serialized_preflight
    assert "raw_model_output" not in serialized_preflight

    assert (
        expression_promote_project_manager_status_card(
            artifact_id="artifact-pm-promotion-blocked",
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    promote_output = json.loads(capsys.readouterr().out)
    assert promote_output["status"] == "fail"
    assert "target_artifact_contract_issues" in promote_output["blockers"]
    assert "This should remain private" not in json.dumps(promote_output, ensure_ascii=False)

    with session_maker() as db:
        blocked = db.get(ExpressionArtifact, "artifact-pm-promotion-blocked")
        assert blocked.promoted is False
        assert blocked.validation_status == "shadow_fallback_valid"


def test_expression_promotion_readiness_summarizes_release_gates_without_writes(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with session_maker() as db:
        bootstrap = build_expression_registry_bootstrap_payload(db, apply=True)
        assert bootstrap["totals"]["created_rows"] > 0
        db.add(
            ExpressionEvalRun(
                id="eval-run-promotion-readiness-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        db.add(
            FactSnapshot(
                id="snapshot-promotion-readiness-good",
                company_id="default",
                employee_id=None,
                project_id="P100",
                scope_type="project_period",
                scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
                scope_key_hash="snapshot-promotion-readiness-good",
                assembler_version="test",
                source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                facts_json={
                    "scope": {"project_id": "P100"},
                    "photo_metrics": {"photos_current": 6},
                    "progress_report_metrics": {"completed_with_content": 1},
                },
                fact_count=2,
                coverage_json={},
                created_at=base_time,
            )
        )
        db.add(
            ExpressionArtifact(
                id="artifact-promotion-readiness-good",
                company_id="default",
                fact_snapshot_id="snapshot-promotion-readiness-good",
                contract_id="project_manager_decision_brief:project_manager:v1",
                scope_type="project_period",
                scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
                artifact_type="project_manager_decision_brief",
                audience_id="project_manager",
                language="en",
                structured_json={
                    "artifact_type": "project_manager_decision_brief",
                    "decision_surface": {
                        "items": [
                            {
                                "item_id": "evidence_volume_current",
                                "category": "evidence_coverage",
                                "severity": "info",
                                "deterministic_summary": "The current project window has 6 database photos.",
                                "fact_refs": [
                                    {"field_path": "photo_metrics.photos_current", "observed_value": 6}
                                ],
                                "metrics": {"photos_current": 6},
                            }
                        ]
                    },
                    "body": {
                        "paragraphs": ["The current project window has 6 database photos."],
                        "generation_path": "deterministic_fallback",
                    },
                    "validation": {"decision_surface_locked": True, "ai_changed_decision_surface": False},
                },
                raw_model_output='{"private":"do not show"}',
                rendered_markdown="private rendered body",
                validation_status="shadow_fallback_valid",
                validation_errors_json=[],
                promoted=False,
                created_at=base_time,
            )
        )
        db.commit()

    with session_maker() as db:
        payload = build_expression_promotion_readiness_payload(
            db,
            artifact_id="artifact-promotion-readiness-good",
            company_id="default",
            sample_limit=20,
        )
        artifact = db.get(ExpressionArtifact, "artifact-promotion-readiness-good")
        assert artifact is not None
        assert artifact.promoted is False

    assert payload["status"] == "pass"
    assert payload["guardrails"] == {
        "exposes_user_visible_content": False,
        "promotes_artifact": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "runs_shadow_generation": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["sections"]["registry_bootstrap"]["totals"]["planned_changes"] == 0
    assert payload["sections"]["prompt_catalog"]["status"] == "pass"
    assert payload["sections"]["contract_matrix"]["status"] == "pass"
    assert payload["sections"]["layer_readiness"]["status"] == "pass"
    assert payload["sections"]["layer_readiness"]["totals"]["artifacts_replay_covered"] == len(
        EXPRESSION_REGISTRY_EXPECTATIONS
    )
    assert payload["sections"]["promotion_surfaces"]["status"] == "pass"
    assert payload["artifact_preflight"]["status"] == "pass"
    assert payload["artifact_preflight"]["schema_version"] == "expression_artifact_promotion_preflight_v1"
    assert payload["artifact_preflight"]["project_manager_status_card_preflight"]["status"] == "pass"
    assert payload["artifact_preflight"]["checks"]["validator_error_count"] == 0
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private rendered body" not in serialized
    assert "do not show" not in serialized
    assert "raw_model_output" not in serialized
    assert "Facts JSON" not in serialized

    assert (
        expression_audit_promotion_readiness(
            artifact_id="artifact-promotion-readiness-good",
            company_id="default",
            sample_limit=20,
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert "raw_model_output" not in json.dumps(cli_output, ensure_ascii=False)


def test_expression_promotion_readiness_blocks_bad_artifact(app_context):
    session_maker = app_context["session_maker"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        db.add(
            ExpressionEvalRun(
                id="eval-run-promotion-readiness-bad-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        db.add(
            FactSnapshot(
                id="snapshot-promotion-readiness-bad",
                company_id="default",
                employee_id=None,
                project_id="P100",
                scope_type="project_period",
                scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
                scope_key_hash="snapshot-promotion-readiness-bad",
                assembler_version="test",
                source_manifest_json={"tables": ["projects", "photos", "progress_reports"]},
                facts_json={"photo_metrics": {"photos_current": 1}},
                fact_count=1,
                coverage_json={},
                created_at=base_time,
            )
        )
        db.add(
            ExpressionArtifact(
                id="artifact-promotion-readiness-bad",
                company_id="default",
                fact_snapshot_id="snapshot-promotion-readiness-bad",
                contract_id="project_manager_decision_brief:project_manager:v1",
                scope_type="project_period",
                scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
                artifact_type="project_manager_decision_brief",
                audience_id="project_manager",
                language="en",
                structured_json={
                    "decision_surface": {"items": []},
                    "body": {"paragraphs": ["This should never be visible."]},
                    "validation": {"decision_surface_locked": False, "ai_changed_decision_surface": False},
                },
                rendered_markdown="bad private rendered body",
                validation_status="shadow_fallback_valid",
                validation_errors_json=[],
                promoted=False,
                created_at=base_time,
            )
        )
        db.commit()

    with session_maker() as db:
        payload = build_expression_promotion_readiness_payload(
            db,
            artifact_id="artifact-promotion-readiness-bad",
            company_id="default",
            sample_limit=20,
        )

    assert payload["status"] == "fail"
    assert "artifact_preflight_not_ready" in payload["blockers"]
    assert payload["artifact_preflight"]["status"] == "fail"
    assert "target_artifact_contract_issues" in payload["artifact_preflight"]["blockers"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "This should never be visible" not in serialized
    assert "bad private rendered body" not in serialized


def test_expression_artifact_promotion_preflight_blocks_shadow_only_client_surface(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    artifact_id = ""

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        db.add(
            ExpressionEvalRun(
                id="eval-run-client-promotion-preflight-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        result = generate_client_progress_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        artifact_id = result.artifact.id
        db.commit()

    with session_maker() as db:
        preflight = build_expression_artifact_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        client_preflight = build_client_progress_summary_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        readiness = build_expression_promotion_readiness_payload(
            db,
            artifact_id=artifact_id,
            company_id="default",
            sample_limit=20,
        )

    assert preflight["status"] == "fail"
    assert "no_promoted_read_surface_declared" in preflight["blockers"]
    assert "artifact_type_not_project_manager_status_card" not in preflight["blockers"]
    assert preflight["visibility_policy"]["declared_promoted_read_surfaces"] == []
    assert preflight["visibility_policy"]["promotion_preflight_pass_does_not_create_user_visibility"] is True
    serialized = json.dumps(preflight, ensure_ascii=False)
    assert "Client Progress Summary" not in serialized
    assert "raw_model_output" not in serialized

    assert client_preflight["status"] == "fail"
    assert client_preflight["client_contract"]["facts_redaction_profile"] == "client_progress_v1"
    assert client_preflight["client_contract"]["approved_client_visible_photos_only"] is True
    assert client_preflight["client_contract"]["source_manifest_visibility"] == "client_visible"
    assert client_preflight["client_contract"]["source_manifest_approval_status"] == "approved"
    assert client_preflight["client_contract"]["coverage_redaction_profile"] == "client_progress_v1"
    assert client_preflight["client_contract"]["payload_visibility"] == "shadow"
    assert client_preflight["client_contract"]["payload_audience"] == "client"
    assert client_preflight["client_contract"]["disclaimer_present"] is True
    assert client_preflight["client_contract"]["summary_surface_locked"] is True
    assert client_preflight["client_contract"]["ai_changed_summary_surface"] is False
    assert client_preflight["client_contract"]["summary_item_count"] > 0
    assert client_preflight["client_contract"]["fact_ref_count"] > 0
    assert client_preflight["client_contract"]["items_missing_fact_refs"] == 0
    assert client_preflight["blockers"] == ["no_promoted_read_surface_declared"]
    assert "Client Progress Summary" not in json.dumps(client_preflight, ensure_ascii=False)

    assert readiness["status"] == "fail"
    assert "artifact_preflight_not_ready" in readiness["blockers"]
    assert "client_progress_summary_preflight_not_ready" in readiness["blockers"]
    assert "project_manager_status_card_preflight_not_ready" not in readiness["blockers"]
    assert readiness["artifact_preflight"]["artifact_type"] == "client_progress_summary"
    assert readiness["artifact_preflight"]["project_manager_status_card_preflight"] is None
    assert readiness["artifact_preflight"]["client_progress_summary_preflight"]["status"] == "fail"
    assert "no_promoted_read_surface_declared" in readiness["artifact_preflight"]["blockers"]

    assert (
        expression_audit_artifact_promotion(
            artifact_id=artifact_id,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "fail"
    assert "no_promoted_read_surface_declared" in cli_output["blockers"]
    assert "raw_model_output" not in json.dumps(cli_output, ensure_ascii=False)

    assert (
        expression_audit_client_progress_summary_promotion(
            artifact_id=artifact_id,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    client_cli_output = json.loads(capsys.readouterr().out)
    assert client_cli_output["status"] == "fail"
    assert client_cli_output["blockers"] == ["no_promoted_read_surface_declared"]
    assert client_cli_output["client_contract"]["summary_item_count"] > 0
    assert "raw_model_output" not in json.dumps(client_cli_output, ensure_ascii=False)


def test_expression_artifact_promotion_preflight_blocks_shadow_only_executive_surface(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    artifact_id = ""

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        db.add(
            ExpressionEvalRun(
                id="eval-run-executive-promotion-preflight-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        result = generate_executive_company_health_shadow(
            db,
            app_settings=settings,
            company_id="default",
            window_days=30,
            use_ai=False,
        )
        artifact_id = result.artifact.id
        db.commit()

    with session_maker() as db:
        preflight = build_expression_artifact_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        executive_preflight = build_executive_company_health_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        readiness = build_expression_promotion_readiness_payload(
            db,
            artifact_id=artifact_id,
            company_id="default",
            sample_limit=20,
        )

    assert preflight["status"] == "fail"
    assert preflight["blockers"] == ["no_promoted_read_surface_declared"]
    assert preflight["visibility_policy"]["declared_promoted_read_surfaces"] == []
    assert preflight["visibility_policy"]["promotion_preflight_pass_does_not_create_user_visibility"] is True
    serialized = json.dumps(preflight, ensure_ascii=False)
    assert "Company Health Summary" not in serialized
    assert "raw_model_output" not in serialized

    assert executive_preflight["status"] == "fail"
    assert executive_preflight["blockers"] == ["no_promoted_read_surface_declared"]
    contract = executive_preflight["executive_contract"]
    assert contract["facts_redaction_profile"] == "executive_company_health_v1"
    assert contract["db_only"] is True
    assert contract["no_disk_file_reads"] is True
    assert contract["no_employee_ranking"] is True
    assert contract["no_performance_scoring"] is True
    assert contract["source_manifest_redaction_profile"] == "executive_company_health_v1"
    assert contract["coverage_redaction_profile"] == "executive_company_health_v1"
    assert contract["payload_visibility"] == "shadow"
    assert contract["payload_audience"] == "executive"
    assert contract["company_health_surface_locked"] is True
    assert contract["ai_changed_company_health_surface"] is False
    assert contract["employee_ordering_disabled"] is True
    assert contract["performance_scoring_disabled"] is True
    assert contract["body_generation_path"] == "deterministic_fallback"
    assert contract["card_count"] > 0
    assert contract["fact_ref_count"] > 0
    assert contract["cards_missing_fact_refs"] == 0
    assert contract["unresolved_fact_refs"] == 0
    assert contract["mismatched_fact_refs"] == 0
    assert "Company Health Summary" not in json.dumps(executive_preflight, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(executive_preflight, ensure_ascii=False)

    assert readiness["status"] == "fail"
    assert "artifact_preflight_not_ready" in readiness["blockers"]
    assert "executive_company_health_preflight_not_ready" in readiness["blockers"]
    assert "client_progress_summary_preflight_not_ready" not in readiness["blockers"]
    assert "project_manager_status_card_preflight_not_ready" not in readiness["blockers"]
    assert readiness["artifact_preflight"]["artifact_type"] == "executive_company_health_summary"
    assert readiness["artifact_preflight"]["client_progress_summary_preflight"] is None
    assert readiness["artifact_preflight"]["project_manager_status_card_preflight"] is None
    assert readiness["artifact_preflight"]["executive_company_health_preflight"]["status"] == "fail"
    assert "no_promoted_read_surface_declared" in readiness["artifact_preflight"]["blockers"]

    assert (
        expression_audit_executive_company_health_promotion(
            artifact_id=artifact_id,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    executive_cli_output = json.loads(capsys.readouterr().out)
    assert executive_cli_output["status"] == "fail"
    assert executive_cli_output["blockers"] == ["no_promoted_read_surface_declared"]
    assert executive_cli_output["executive_contract"]["card_count"] > 0
    assert "Company Health Summary" not in json.dumps(executive_cli_output, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(executive_cli_output, ensure_ascii=False)


def test_expression_artifact_promotion_preflight_blocks_shadow_only_operations_surface(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    artifact_id = ""

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        db.add(
            ExpressionEvalRun(
                id="eval-run-operations-promotion-preflight-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        result = generate_operations_health_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            window_hours=24,
            use_ai=False,
        )
        artifact_id = result.artifact.id
        db.commit()

    with session_maker() as db:
        preflight = build_expression_artifact_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        operations_preflight = build_operations_health_summary_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        readiness = build_expression_promotion_readiness_payload(
            db,
            artifact_id=artifact_id,
            company_id="default",
            sample_limit=20,
        )

    assert preflight["status"] == "fail"
    assert preflight["blockers"] == ["no_promoted_read_surface_declared"]
    assert preflight["visibility_policy"]["declared_promoted_read_surfaces"] == []
    assert preflight["visibility_policy"]["promotion_preflight_pass_does_not_create_user_visibility"] is True
    serialized = json.dumps(preflight, ensure_ascii=False)
    assert "Operations Health Summary" not in serialized
    assert "raw_model_output" not in serialized

    assert operations_preflight["status"] == "fail"
    assert operations_preflight["blockers"] == ["no_promoted_read_surface_declared"]
    contract = operations_preflight["operations_contract"]
    assert contract["facts_redaction_profile"] == "operations_health_v1"
    assert contract["db_only"] is True
    assert contract["no_disk_file_reads"] is True
    assert contract["source_manifest_redaction_profile"] == "operations_health_v1"
    assert contract["coverage_redaction_profile"] == "operations_health_v1"
    assert contract["payload_visibility"] == "shadow"
    assert contract["payload_audience"] == "operations"
    assert contract["health_surface_locked"] is True
    assert contract["ai_changed_health_surface"] is False
    assert contract["body_generation_path"] == "deterministic_fallback"
    assert contract["dimension_count"] > 0
    assert contract["fact_ref_count"] > 0
    assert contract["dimensions_missing_fact_refs"] == 0
    assert contract["unresolved_fact_refs"] == 0
    assert contract["mismatched_fact_refs"] == 0
    assert "Operations Health Summary" not in json.dumps(operations_preflight, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(operations_preflight, ensure_ascii=False)

    assert readiness["status"] == "fail"
    assert "artifact_preflight_not_ready" in readiness["blockers"]
    assert "operations_health_summary_preflight_not_ready" in readiness["blockers"]
    assert "client_progress_summary_preflight_not_ready" not in readiness["blockers"]
    assert "executive_company_health_preflight_not_ready" not in readiness["blockers"]
    assert "project_manager_status_card_preflight_not_ready" not in readiness["blockers"]
    assert readiness["artifact_preflight"]["artifact_type"] == "operations_health_summary"
    assert readiness["artifact_preflight"]["client_progress_summary_preflight"] is None
    assert readiness["artifact_preflight"]["executive_company_health_preflight"] is None
    assert readiness["artifact_preflight"]["project_manager_status_card_preflight"] is None
    assert readiness["artifact_preflight"]["operations_health_summary_preflight"]["status"] == "fail"
    assert "no_promoted_read_surface_declared" in readiness["artifact_preflight"]["blockers"]

    assert (
        expression_audit_operations_health_summary_promotion(
            artifact_id=artifact_id,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    operations_cli_output = json.loads(capsys.readouterr().out)
    assert operations_cli_output["status"] == "fail"
    assert operations_cli_output["blockers"] == ["no_promoted_read_surface_declared"]
    assert operations_cli_output["operations_contract"]["dimension_count"] > 0
    assert "Operations Health Summary" not in json.dumps(operations_cli_output, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(operations_cli_output, ensure_ascii=False)


def test_expression_artifact_promotion_preflight_blocks_shadow_only_finance_surface(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    base_time = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    artifact_id = ""

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        db.add(
            ExpressionEvalRun(
                id="eval-run-finance-promotion-preflight-000000000001",
                company_id=None,
                run_type="golden_replay",
                status="pass",
                trigger_source="pytest",
                runner_version="eval_run_v1",
                input_manifest_json={"artifact_types": "all"},
                summary_json={
                    "cases": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "passed": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                    "failed": 0,
                    "skipped": 0,
                    "artifact_type_counts": {
                        str(expectation["artifact_type"]): {"cases": 1, "passed": 1, "failed": 0, "skipped": 0}
                        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS
                    },
                },
                started_at=base_time,
                completed_at=base_time,
            )
        )
        result = generate_finance_summary_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        artifact_id = result.artifact.id
        db.commit()

    with session_maker() as db:
        preflight = build_expression_artifact_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        finance_preflight = build_finance_summary_promotion_preflight_payload(
            db,
            artifact_id=artifact_id,
        )
        readiness = build_expression_promotion_readiness_payload(
            db,
            artifact_id=artifact_id,
            company_id="default",
            sample_limit=20,
        )

    assert preflight["status"] == "fail"
    assert preflight["blockers"] == ["no_promoted_read_surface_declared"]
    assert preflight["visibility_policy"]["declared_promoted_read_surfaces"] == []
    assert preflight["visibility_policy"]["promotion_preflight_pass_does_not_create_user_visibility"] is True
    serialized = json.dumps(preflight, ensure_ascii=False)
    assert "Finance Summary" not in serialized
    assert "raw_model_output" not in serialized

    assert finance_preflight["status"] == "fail"
    assert finance_preflight["blockers"] == ["no_promoted_read_surface_declared"]
    contract = finance_preflight["finance_contract"]
    assert contract["facts_redaction_profile"] == "finance_summary_v1"
    assert contract["db_only"] is True
    assert contract["source_manifest_redaction_profile"] == "finance_summary_v1"
    assert contract["coverage_redaction_profile"] == "finance_summary_v1"
    assert contract["payload_visibility"] == "shadow"
    assert contract["payload_audience"] == "finance"
    assert contract["finance_surface_locked"] is True
    assert contract["ai_changed_finance_surface"] is False
    assert contract["body_generation_path"] == "deterministic_fallback"
    assert contract["section_count"] > 0
    assert contract["fact_ref_count"] > 0
    assert contract["sections_missing_fact_refs"] == 0
    assert contract["unresolved_fact_refs"] == 0
    assert contract["mismatched_fact_refs"] == 0
    assert contract["headline_missing_count"] == 0
    assert contract["headline_mismatch_count"] == 0
    assert contract["headline_negative_count"] == 0
    assert "Finance Summary" not in json.dumps(finance_preflight, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(finance_preflight, ensure_ascii=False)

    assert readiness["status"] == "fail"
    assert "artifact_preflight_not_ready" in readiness["blockers"]
    assert "finance_summary_preflight_not_ready" in readiness["blockers"]
    assert "client_progress_summary_preflight_not_ready" not in readiness["blockers"]
    assert "executive_company_health_preflight_not_ready" not in readiness["blockers"]
    assert "operations_health_summary_preflight_not_ready" not in readiness["blockers"]
    assert "project_manager_status_card_preflight_not_ready" not in readiness["blockers"]
    assert readiness["artifact_preflight"]["artifact_type"] == "finance_summary"
    assert readiness["artifact_preflight"]["client_progress_summary_preflight"] is None
    assert readiness["artifact_preflight"]["executive_company_health_preflight"] is None
    assert readiness["artifact_preflight"]["operations_health_summary_preflight"] is None
    assert readiness["artifact_preflight"]["project_manager_status_card_preflight"] is None
    assert readiness["artifact_preflight"]["finance_summary_preflight"]["status"] == "fail"
    assert "no_promoted_read_surface_declared" in readiness["artifact_preflight"]["blockers"]

    assert (
        expression_audit_finance_summary_promotion(
            artifact_id=artifact_id,
            json_output=True,
            settings_override=settings,
        )
        == 1
    )
    finance_cli_output = json.loads(capsys.readouterr().out)
    assert finance_cli_output["status"] == "fail"
    assert finance_cli_output["blockers"] == ["no_promoted_read_surface_declared"]
    assert finance_cli_output["finance_contract"]["section_count"] > 0
    assert "Finance Summary" not in json.dumps(finance_cli_output, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(finance_cli_output, ensure_ascii=False)


def test_mobile_promoted_read_contracts_are_promoted_only_without_leaking_content(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        employee_result = generate_employee_contribution_shadow(
            db,
            app_settings=settings,
            company_id="default",
            employee_id="E100",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        manager_result = generate_project_manager_decision_brief_shadow(
            db,
            app_settings=settings,
            company_id="default",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        promote_expression_artifact(db, artifact_id=employee_result.artifact.id)
        promote_expression_artifact(db, artifact_id=manager_result.artifact.id)
        shadow_result = generate_employee_contribution_shadow(
            db,
            app_settings=settings,
            company_id="default",
            employee_id="E100",
            project_id="P100",
            window_days=30,
            use_ai=False,
        )
        shadow_result.artifact.structured_json = {"summary_line": "shadow content must stay hidden"}
        db.commit()

    with session_maker() as db:
        payload = build_mobile_promoted_read_contracts_payload(
            db,
            company_id="default",
            sample_limit=20,
        )

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "mobile_promoted_read_contracts:v1"
    assert payload["guardrails"] == {
        "uses_ai": False,
        "writes_database": False,
        "uses_filesystem_scan": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "exposes_user_visible_content": False,
        "promotes_artifact": False,
        "server_computed_response": True,
    }
    assert payload["totals"]["contracts_checked"] == 2
    assert payload["totals"]["contracts_ready"] == 2
    assert payload["totals"]["promoted_contract_issues"] == 0
    employee_contract = payload["contracts"]["employee_contribution"]
    manager_contract = payload["contracts"]["project_manager_status_card"]
    assert employee_contract["read_contract"]["visible_artifact_filter"] == {
        "artifact_type": "employee_contribution_narrative",
        "audience_id": "employee",
        "promoted": True,
        "validation_status": "promoted_valid",
        "company_employee_project_scoped": True,
    }
    assert employee_contract["read_contract"]["visibility_rules"]["shadow_artifacts_visible"] is False
    assert employee_contract["read_contract"]["visibility_rules"]["app_may_compute_metrics"] is False
    assert manager_contract["read_contract"]["visible_artifact_filter"]["validation_status"] == "promoted_valid"
    assert manager_contract["read_contract"]["visibility_rules"]["shadow_artifacts_visible"] is False
    assert manager_contract["read_contract"]["visibility_rules"]["app_may_compute_metrics"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "shadow content must stay hidden" not in serialized
    assert "raw_model_output" not in serialized

    assert (
        expression_audit_mobile_promoted_read_contracts(
            company_id="default",
            sample_limit=20,
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["contracts_ready"] == 2
    assert "shadow content must stay hidden" not in json.dumps(cli_output, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(cli_output, ensure_ascii=False)


def test_expression_prompt_catalog_readiness_checks_db_prompt_variables_without_leaking_text(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience_ids = sorted({str(expectation["audience_id"]) for expectation in EXPRESSION_REGISTRY_EXPECTATIONS})
        for audience_id in audience_ids:
            db.add(
                ExpressionAudience(
                    id=audience_id,
                    code=audience_id,
                    title=audience_id,
                    default_language="zh",
                    is_active=True,
                )
            )
        db.flush()
        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
            template_id = str(expectation["template_id"])
            artifact_type = str(expectation["artifact_type"])
            audience_id = str(expectation["audience_id"])
            version_id = f"{template_id}:catalog-test-v1"
            db.add_all(
                [
                    ExpressionPromptTemplate(
                        id=template_id,
                        slug=template_id,
                        title=template_id,
                        artifact_type=artifact_type,
                        stage=str(expectation["stage"]),
                        default_audience_id=audience_id,
                    ),
                    ExpressionPromptVersion(
                        id=version_id,
                        template_id=template_id,
                        version="catalog-test-v1",
                        system_prompt=(
                            "Use only database facts. Return strict JSON and do not add unsupported claims."
                        ),
                        user_prompt_template=(
                            "Facts snapshot:\n{facts_json}\n\nOutput contract:\n{contract_schema_json}"
                        ),
                        status="active",
                    ),
                    ExpressionPromptBinding(
                        id=f"global:{template_id}:catalog-test-v1",
                        scope_type="global",
                        scope_id=None,
                        template_id=template_id,
                        prompt_version_id=version_id,
                        priority=100,
                    ),
                    ExpressionOutputContract(
                        id=f"{artifact_type}:{audience_id}:catalog-test-v1",
                        artifact_type=artifact_type,
                        audience_id=audience_id,
                        version="catalog-test-v1",
                        json_schema={
                            "type": "object",
                            "required": ["artifact_type"],
                            "properties": {"artifact_type": {"type": "string", "const": artifact_type}},
                        },
                        required_fact_paths_json=list(expectation["required_fact_paths"]),
                        forbidden_claims_json=[{"pattern": "unsupported"}],
                        is_active=True,
                    ),
                ]
            )
        db.commit()

    with session_maker() as db:
        payload = build_expression_prompt_catalog_readiness_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "expression_prompt_catalog_readiness_v1"
    assert payload["guardrails"]["prompt_text_included"] is False
    assert payload["totals"]["artifacts_ready"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert payload["totals"]["blocking_issues"] == 0
    assert all(artifact["checks"]["has_facts_json_placeholder"] for artifact in payload["artifacts"])
    assert all(artifact["checks"]["has_contract_schema_placeholder"] for artifact in payload["artifacts"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Use only database facts" not in serialized
    assert "Facts snapshot" not in serialized
    assert "{facts_json}" not in serialized
    assert "raw_model_output" not in serialized

    with session_maker() as db:
        broken = db.get(ExpressionPromptVersion, "client_progress_summary:catalog-test-v1")
        assert broken is not None
        broken.user_prompt_template = "Facts snapshot:\n{facts_json}"
        db.commit()

    assert expression_audit_prompt_catalog(json_output=True, settings_override=settings) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "fail"
    assert output["totals"]["missing_contract_schema_placeholder"] == 1
    client_entry = next(
        artifact for artifact in output["artifacts"] if artifact["artifact_type"] == "client_progress_summary"
    )
    assert "missing_contract_schema_json_placeholder" in client_entry["issues"]
    assert "Facts snapshot" not in json.dumps(output, ensure_ascii=False)


def test_ai_prompt_surface_coverage_inventory_tracks_legacy_inline_prompts_without_leaking_text(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        build_expression_registry_bootstrap_payload(db, apply=True)
        seed_photo_field_analysis_prompt(db)
        seed_photo_invoice_analysis_prompt(db)
        seed_video_frame_insight_prompt(db)
        seed_progress_report_multi_image_prompt(db)
        seed_generated_report_markdown_legacy_prompt(db)
        db.commit()
        payload = build_ai_prompt_surface_coverage_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "ai_prompt_surface_coverage_v1"
    assert payload["guardrails"] == {
        "changes_runtime_prompt_behavior": False,
        "exposes_user_visible_content": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["totals"]["surfaces_total"] == (
        len(EXPRESSION_REGISTRY_EXPECTATIONS) + len(LEGACY_AI_PROMPT_SURFACE_EXPECTATIONS)
    )
    assert payload["totals"]["registry_backed"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert payload["totals"]["registry_ready"] >= len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert payload["totals"]["db_prompt_shadow_ready"] >= 5
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["totals"]["legacy_db_input_backed"] >= 1
    assert payload["totals"]["code_inline"] >= 1
    surfaces = {row["surface_id"]: row for row in payload["surfaces"]}
    assert surfaces["expression_registry:employee_contribution_narrative"]["coverage_status"] == "registry_backed"
    assert surfaces["photo_field_analysis"]["coverage_status"] == "db_prompt_shadow_ready"
    assert surfaces["photo_field_analysis"]["prompt_metadata"]["active_shadow_version_id"] == "photo_field_analysis:v1"
    assert surfaces["photo_invoice_analysis"]["coverage_status"] == "db_prompt_shadow_ready"
    assert surfaces["photo_invoice_analysis"]["prompt_metadata"]["active_shadow_version_id"] == "photo_invoice_analysis:v1"
    assert surfaces["video_frame_insight"]["coverage_status"] == "db_prompt_shadow_ready"
    assert surfaces["video_frame_insight"]["prompt_metadata"]["active_shadow_version_id"] == "video_frame_insight:v1"
    assert surfaces["progress_report_multi_image"]["coverage_status"] == "db_prompt_shadow_ready"
    assert surfaces["progress_report_multi_image"]["prompt_metadata"]["active_shadow_version_id"] == (
        "progress_report_multi_image:v1"
    )
    assert surfaces["generated_report_markdown_legacy"]["coverage_status"] == "db_prompt_shadow_ready"
    assert surfaces["generated_report_markdown_legacy"]["prompt_metadata"]["active_shadow_template_id"] == (
        "generated_report_markdown_legacy"
    )
    assert surfaces["generated_report_markdown_legacy"]["prompt_metadata"]["active_shadow_version_id"] == (
        "generated_report_markdown_legacy:v1"
    )
    assert surfaces["evidence_copilot_answer"]["coverage_status"] == "legacy_db_input_backed"
    assert surfaces["generic_text_translation"]["coverage_status"] == "code_inline"
    assert surfaces["photo_invoice_analysis"]["migration_gate"] == (
        "receipt_facts counts and numeric values match current production parser on sample"
    )
    assert surfaces["progress_report_multi_image"]["promotion_gate"] == (
        "do not replace report generation until registry shadow parity passes"
    )
    assert payload["migration_order"][0]["phase"] == "inventory"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Use only database facts" not in serialized
    assert "Facts snapshot" not in serialized
    assert "{facts_json}" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_ai_prompt_surface_coverage(json_output=True, settings_override=settings) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["surfaces_total"] == payload["totals"]["surfaces_total"]
    assert cli_output["totals"]["registry_backed"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    assert cli_output["totals"]["db_prompt_shadow_ready"] >= 5
    assert "Use only database facts" not in json.dumps(cli_output, ensure_ascii=False)
    assert "raw_model_output" not in json.dumps(cli_output, ensure_ascii=False)


def test_photo_field_analysis_db_prompt_shadow_matches_legacy_builder_without_runtime_cutover(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        seed_photo_field_analysis_prompt(db)
        db.commit()
        payload = build_photo_field_analysis_prompt_parity_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "photo_field_analysis_prompt_parity:v1"
    assert payload["guardrails"] == {
        "changes_runtime_prompt_behavior": False,
        "photo_types_covered": ["project"],
        "photo_types_not_covered": ["invoice", "video"],
        "prompt_text_included": False,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["registry"]["active_prompt_version_id"] == "photo_field_analysis:v1"
    assert payload["totals"]["cases_checked"] == 3
    assert payload["totals"]["matching_cases"] == 3
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["migration_gate"]["fallback"] == "build_ai_prompt_for_context remains the production prompt source"
    assert all(not case["issues"] for case in payload["cases"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "You are a field evidence triage analyst" not in serialized
    assert "Required JSON schema" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_photo_field_analysis_prompt_parity(json_output=True, settings_override=settings) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["prompt_digest_mismatch"] == 0
    assert "You are a field evidence triage analyst" not in json.dumps(cli_output, ensure_ascii=False)


def test_photo_invoice_analysis_db_prompt_shadow_matches_legacy_builder_without_runtime_cutover(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        seed_photo_invoice_analysis_prompt(db)
        db.commit()
        payload = build_photo_invoice_analysis_prompt_parity_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "photo_invoice_analysis_prompt_parity:v1"
    assert payload["guardrails"] == {
        "changes_runtime_prompt_behavior": False,
        "photo_types_covered": ["invoice"],
        "photo_types_not_covered": ["project", "video"],
        "prompt_text_included": False,
        "raw_output_included": False,
        "receipt_facts_mutated": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["registry"]["active_prompt_version_id"] == "photo_invoice_analysis:v1"
    assert payload["totals"]["cases_checked"] == 3
    assert payload["totals"]["matching_cases"] == 3
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["migration_gate"]["fallback"] == "build_ai_prompt_for_context remains the production invoice prompt source"
    assert all(not case["issues"] for case in payload["cases"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "You are an expense-evidence analyst" not in serialized
    assert "Required JSON schema" not in serialized
    assert "Extract receipt_facts only" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_photo_invoice_analysis_prompt_parity(json_output=True, settings_override=settings) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["prompt_digest_mismatch"] == 0
    assert "You are an expense-evidence analyst" not in json.dumps(cli_output, ensure_ascii=False)
    assert "Extract receipt_facts only" not in json.dumps(cli_output, ensure_ascii=False)


def test_video_frame_insight_db_prompt_shadow_matches_legacy_builder_without_worker_cutover(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        seed_video_frame_insight_prompt(db)
        db.commit()
        payload = build_video_frame_insight_prompt_parity_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "video_frame_insight_prompt_parity:v1"
    assert payload["guardrails"] == {
        "changes_runtime_prompt_behavior": False,
        "media_types_covered": ["video"],
        "photo_types_covered": ["project"],
        "photo_types_not_covered": ["invoice"],
        "prompt_text_included": False,
        "queues_video_task": False,
        "raw_output_included": False,
        "reads_video_file": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["registry"]["active_prompt_version_id"] == "video_frame_insight:v1"
    assert payload["totals"]["cases_checked"] == 3
    assert payload["totals"]["media_asset_builder_cases"] == 2
    assert payload["totals"]["matching_cases"] == 3
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["migration_gate"]["fallback"] == "build_media_asset_ai_prompt remains the production video frame prompt source"
    assert all(not case["issues"] for case in payload["cases"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "You are a field evidence triage analyst" not in serialized
    assert "Required JSON schema" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_video_frame_insight_prompt_parity(json_output=True, settings_override=settings) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["prompt_digest_mismatch"] == 0
    assert cli_output["totals"]["media_asset_builder_cases"] == 2
    assert "You are a field evidence triage analyst" not in json.dumps(cli_output, ensure_ascii=False)


def test_progress_report_multi_image_db_prompt_shadow_matches_legacy_builder_without_runtime_cutover(
    app_context,
    capsys,
):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        seed_progress_report_multi_image_prompt(db)
        db.commit()
        payload = build_progress_report_multi_image_prompt_parity_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "progress_report_multi_image_prompt_parity:v1"
    assert payload["guardrails"] == {
        "calls_generate_progress_report": False,
        "changes_runtime_prompt_behavior": False,
        "photo_types_covered": ["project"],
        "prompt_text_included": False,
        "raw_output_included": False,
        "reads_image_files": False,
        "report_types_covered": ["progress_report_multi_image"],
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["registry"]["active_prompt_version_id"] == "progress_report_multi_image:v1"
    assert payload["totals"]["cases_checked"] == 5
    assert payload["totals"]["matching_cases"] == 5
    assert payload["totals"]["custom_prompt_cases"] == 2
    assert payload["totals"]["existing_ai_context_cases"] == 2
    assert payload["totals"]["preferred_ai_hint_cases"] == 2
    assert payload["totals"]["three_photo_sequence_cases"] == 1
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["migration_gate"]["fallback"] == (
        "build_multi_image_progress_prompt remains the production progress report prompt source"
    )
    assert all(not case["issues"] for case in payload["cases"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "你是一个资深的工程监理" not in serialized
    assert "照片元数据" not in serialized
    assert "操作员追加说明" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_progress_report_multi_image_prompt_parity(
        json_output=True,
        settings_override=settings,
    ) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["prompt_digest_mismatch"] == 0
    assert cli_output["totals"]["cases_checked"] == 5
    assert "你是一个资深的工程监理" not in json.dumps(cli_output, ensure_ascii=False)


def test_generated_report_markdown_legacy_db_prompt_shadow_matches_legacy_builder_without_runtime_cutover(
    app_context,
    capsys,
):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        seed_generated_report_markdown_legacy_prompt(db)
        db.commit()
        payload = build_generated_report_markdown_legacy_prompt_parity_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "generated_report_markdown_legacy_prompt_parity:v1"
    assert payload["guardrails"] == {
        "calls_process_generated_report": False,
        "changes_runtime_prompt_behavior": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "reads_image_files": False,
        "target_runtime_table": "generated_reports",
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["registry"]["active_prompt_version_id"] == "generated_report_markdown_legacy:v1"
    assert payload["totals"]["cases_checked"] == 3
    assert payload["totals"]["matching_cases"] == 3
    assert payload["totals"]["custom_prompt_cases"] == 2
    assert payload["totals"]["tagged_photo_cases"] == 2
    assert payload["totals"]["blocking_issues"] == 0
    assert payload["migration_gate"]["fallback"] == (
        "build_report_markdown_prompt remains the production generated report prompt source"
    )
    assert all(not case["issues"] for case in payload["cases"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "You are a construction reporting assistant" not in serialized
    assert "Photo dataset (JSON)" not in serialized
    assert "raw_model_output" not in serialized
    assert "/fixtures/generated-report-photo" not in serialized

    assert expression_audit_generated_report_markdown_legacy_prompt_parity(
        json_output=True,
        settings_override=settings,
    ) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["prompt_digest_mismatch"] == 0
    assert cli_output["totals"]["cases_checked"] == 3
    assert "You are a construction reporting assistant" not in json.dumps(cli_output, ensure_ascii=False)


def test_expression_contract_matrix_summarizes_each_artifact_gate_without_prompt_text(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        audience_ids = sorted({str(expectation["audience_id"]) for expectation in EXPRESSION_REGISTRY_EXPECTATIONS})
        for audience_id in audience_ids:
            db.add(
                ExpressionAudience(
                    id=audience_id,
                    code=audience_id,
                    title=audience_id,
                    default_language="zh",
                    forbidden_phrase_set_id=f"{audience_id}_phrases",
                    is_active=True,
                )
            )
        db.flush()
        for audience_id in audience_ids:
            db.add(
                ExpressionForbiddenPhrase(
                    id=f"{audience_id}_phrases:matrix-blocked",
                    set_id=f"{audience_id}_phrases",
                    audience_id=audience_id,
                    language="zh",
                    phrase="blocked",
                    match_type="literal",
                    severity="error",
                    is_active=True,
                )
            )
        for expectation in EXPRESSION_REGISTRY_EXPECTATIONS:
            template_id = str(expectation["template_id"])
            artifact_type = str(expectation["artifact_type"])
            audience_id = str(expectation["audience_id"])
            version_id = f"{template_id}:matrix-test-v1"
            db.add_all(
                [
                    ExpressionPromptTemplate(
                        id=template_id,
                        slug=template_id,
                        title=template_id,
                        artifact_type=artifact_type,
                        stage=str(expectation["stage"]),
                        default_audience_id=audience_id,
                    ),
                    ExpressionPromptVersion(
                        id=version_id,
                        template_id=template_id,
                        version="matrix-test-v1",
                        system_prompt="Use only database facts and return strict JSON under the contract.",
                        user_prompt_template="Facts:\n{facts_json}\nContract:\n{contract_schema_json}",
                        status="active",
                    ),
                    ExpressionPromptBinding(
                        id=f"global:{template_id}:matrix-test-v1",
                        scope_type="global",
                        scope_id=None,
                        template_id=template_id,
                        prompt_version_id=version_id,
                        priority=100,
                    ),
                    ExpressionOutputContract(
                        id=f"{artifact_type}:{audience_id}:matrix-test-v1",
                        artifact_type=artifact_type,
                        audience_id=audience_id,
                        version="matrix-test-v1",
                        json_schema={
                            "type": "object",
                            "required": ["artifact_type"],
                            "properties": {"artifact_type": {"type": "string", "const": artifact_type}},
                        },
                        required_fact_paths_json=list(expectation["required_fact_paths"]),
                        forbidden_claims_json=[{"pattern": "blocked"}],
                        is_active=True,
                    ),
                ]
            )
        db.commit()

    with session_maker() as db:
        payload = build_expression_contract_matrix_payload(db)

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "expression_contract_matrix_v1"
    assert payload["guardrails"] == {
        "decision_or_ranking_generated": False,
        "prompt_text_included": False,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["totals"]["artifacts_ready"] == len(EXPRESSION_REGISTRY_EXPECTATIONS)
    artifacts = {artifact["artifact_type"]: artifact for artifact in payload["artifacts"]}
    manager = artifacts["project_manager_decision_brief"]
    assert manager["input_facts"]["source"] == "FactSnapshot.facts_json"
    assert manager["forbidden_content"]["policy_source"] == "ExpressionOutputContract + ExpressionForbiddenPhrase"
    assert manager["validator"]["name"] == "decision_surface"
    assert manager["fallback"]["deterministic_fallback"] is True
    assert manager["promotion"]["policy"] == "promoted_only_with_surface_guardrail"
    assert manager["promotion"]["verified_surfaces"][0]["route"] == (
        "/api/mobile/projects/{project_id}/project-manager-status-card"
    )
    employee = artifacts["employee_contribution_narrative"]
    assert employee["promotion"]["policy"] == "promoted_only_with_surface_guardrail"
    assert employee["promotion"]["verified_surfaces"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Use only database facts" not in serialized
    assert "{facts_json}" not in serialized
    assert "raw_model_output" not in serialized

    assert expression_audit_contract_matrix(json_output=True, settings_override=settings) == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["blocking_issues"] == 0


def test_expression_visibility_fact_boundary_audit_samples_client_and_executive_facts(app_context, capsys):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        project = Project(
            company_id="default",
            project_id="BOUNDARY100",
            project_name="Boundary Test",
            client_name="Client",
            location="Site",
        )
        visible_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="BOUNDARY100",
            photo_type=PhotoType.project,
            file_path="/tmp/client-visible-boundary.jpg",
            image_url="/media/photos/client-visible-boundary.jpg",
            original_file_name="client-visible-boundary.jpg",
            captured_at_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            visibility=PhotoVisibility.client_visible,
        )
        internal_photo = Photo(
            company_id="default",
            employee_id="E200",
            project_id="BOUNDARY100",
            photo_type=PhotoType.project,
            file_path="/tmp/internal-boundary.jpg",
            image_url="/media/photos/internal-boundary.jpg",
            original_file_name="internal-boundary.jpg",
            captured_at_utc=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            visibility=PhotoVisibility.internal,
        )
        db.add_all([project, visible_photo, internal_photo])
        db.commit()

    with session_maker() as db:
        payload = build_expression_visibility_fact_boundary_payload(
            db,
            company_id="default",
            window_days=30,
        )

    assert payload["status"] == "pass"
    assert payload["schema_version"] == "expression_visibility_fact_boundary_v1"
    assert payload["guardrails"] == {
        "prompt_text_included": False,
        "raw_output_included": False,
        "uses_ai": False,
        "uses_filesystem_scan": False,
        "writes_database": False,
    }
    assert payload["totals"] == {
        "boundaries_expected": 2,
        "boundaries_ready": 2,
        "blocking_issues": 0,
        "sample_denied_key_hits": 0,
        "sampled_boundaries": 2,
    }
    boundaries = {boundary["artifact_type"]: boundary for boundary in payload["boundaries"]}
    client_boundary = boundaries["client_progress_summary"]
    assert client_boundary["visibility_policy"]["client_visible_photos_only"] is True
    assert client_boundary["visibility_policy"]["approved_photos_only"] is True
    assert client_boundary["fact_source_policy"]["filesystem_scan_allowed"] is False
    assert client_boundary["sample"]["sampled"] is True
    assert client_boundary["sample"]["denied_key_hits"] == []
    executive_boundary = boundaries["executive_company_health_summary"]
    assert executive_boundary["visibility_policy"]["aggregate_only"] is True
    assert executive_boundary["visibility_policy"]["no_employee_ranking"] is True
    assert executive_boundary["visibility_policy"]["no_performance_scoring"] is True
    assert executive_boundary["sample"]["denied_key_hits"] == []
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "/tmp/client-visible-boundary.jpg" not in serialized
    assert "/tmp/internal-boundary.jpg" not in serialized
    assert "raw_model_output" not in serialized

    assert (
        expression_audit_visibility_fact_boundary(
            company_id="default",
            window_days=30,
            json_output=True,
            settings_override=settings,
        )
        == 0
    )
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "pass"
    assert cli_output["totals"]["sample_denied_key_hits"] == 0


def test_expression_production_gate_combines_registry_pm_and_executive(monkeypatch, capsys):
    def fake_registry(**kwargs):
        print(json.dumps({"status": "pass", "totals": {"blocking_issues": 0}}))
        return 0

    def fake_roadmap(**kwargs):
        print(
            json.dumps(
                {
                    "status": "inform",
                    "schema_version": "expression_roadmap_gap_v1",
                    "totals": {"gaps": 5, "hard_bug": 0, "product_risk": 3, "future": 2},
                }
            )
        )
        return 0

    def fake_eval_readiness(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "eval_runs": 1,
                        "eval_items": 2,
                        "golden_replay_runs": 1,
                    },
                    "latest_golden_replay": {
                        "summary": {
                            "artifact_type_counts": {
                                "project_manager_decision_brief": {
                                    "cases": 2,
                                    "passed": 2,
                                    "failed": 0,
                                    "skipped": 0,
                                }
                            }
                        }
                    },
                    "registry_replay_coverage": {
                        "schema_version": "registry_replay_coverage_v1",
                        "min_cases_per_artifact": 1,
                        "totals": {
                            "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                            "artifacts_covered": 1,
                            "artifacts_with_failures": 0,
                            "below_min_cases": 0,
                            "missing_samples": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 1,
                        },
                        "artifacts": [
                            {
                                "artifact_type": "project_manager_decision_brief",
                                "status": "covered",
                                "cases": 2,
                                "passed": 2,
                                "failed": 0,
                                "skipped": 0,
                            }
                        ],
                    },
                }
            )
        )
        return 0

    def fake_layer_readiness(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "artifacts_shadow_verified": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "artifacts_blocked": 0,
                        "artifacts_replay_covered": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "missing_replay_samples": 0,
                        "artifacts_with_replay_failures": 0,
                    },
                }
            )
        )
        return 0

    def fake_promotion_surfaces(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                            "promotion_surface_verified": 2,
                            "shadow_only_no_surface": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 2,
                        "promotion_surface_needs_review": 0,
                        "blocked": 0,
                        "unexpected_promoted_artifacts": 0,
                    },
                }
            )
        )
        return 0

    def fake_prompt_catalog(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "artifacts_ready": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "blocking_issues": 0,
                        "missing_active_version": 0,
                        "missing_active_binding": 0,
                        "missing_active_contract": 0,
                        "missing_facts_placeholder": 0,
                        "missing_contract_schema_placeholder": 0,
                        "prompt_format_errors": 0,
                    },
                }
            )
        )
        return 0

    def fake_contract_matrix(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "artifacts_ready": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "blocking_issues": 0,
                        "missing_prompt_catalog": 0,
                        "missing_validator": 0,
                        "missing_fallback": 0,
                        "missing_surface_policy": 0,
                    },
                }
            )
        )
        return 0

    def fake_visibility_fact_boundary(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "boundaries_ready": 2,
                        "blocking_issues": 0,
                        "sampled_boundaries": 2,
                        "sample_denied_key_hits": 0,
                    },
                }
            )
        )
        return 0

    def fake_pm_status_card_contract(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "totals": {
                        "artifacts_sampled": 2,
                        "promoted_artifacts_sampled": 0,
                        "promoted_contract_issues": 0,
                        "shadow_contract_issues": 0,
                        "declared_read_surfaces": 1,
                        "verified_read_surfaces": 1,
                        "blocking_issues": 0,
                    },
                }
            )
        )
        return 0

    def fake_pm_batch(**kwargs):
        print(
            json.dumps(
                {
                    "totals": {
                        "projects_selected": 3,
                        "errors": 0,
                        "shadow_invalid": 0,
                        "shadow_valid": 0,
                        "shadow_fallback_valid": 3,
                    }
                }
            )
        )
        return 0

    def fake_client_batch(**kwargs):
        print(
            json.dumps(
                {
                    "totals": {
                        "projects_selected": 2,
                        "errors": 0,
                        "shadow_invalid": 0,
                        "shadow_valid": 0,
                        "shadow_fallback_valid": 2,
                        "summary_items": 8,
                    }
                }
            )
        )
        return 0

    def fake_operations_batch(**kwargs):
        print(
            json.dumps(
                {
                    "totals": {
                        "companies_selected": 2,
                        "errors": 0,
                        "shadow_invalid": 0,
                        "shadow_valid": 0,
                        "shadow_fallback_valid": 2,
                        "dimensions": 10,
                    }
                }
            )
        )
        return 0

    def fake_finance_batch(**kwargs):
        window_days = int(kwargs.get("window_days") or 0)
        print(
            json.dumps(
                {
                    "totals": {
                        "projects_selected": 1 if window_days >= 365 else 0,
                        "errors": 0,
                        "shadow_invalid": 0,
                        "shadow_valid": 0,
                        "shadow_fallback_valid": 1 if window_days >= 365 else 0,
                        "receipt_count": 3 if window_days >= 365 else 0,
                    }
                }
            )
        )
        return 0

    def fake_finance_health(**kwargs):
        print(
            json.dumps(
                {
                    "status": "ready",
                    "totals": {
                        "receipt_fact_count": 3,
                        "projects_with_receipt_facts": 1,
                    },
                }
            )
        )
        return 0

    def fake_executive_audit(**kwargs):
        print(
            json.dumps(
                {
                    "status": "pass",
                    "blocking_issues": 0,
                    "totals": {
                        "artifacts_checked": 4,
                        "raw_model_output_present": 0,
                        "missing_fact_refs": 0,
                    },
                }
            )
        )
        return 0

    monkeypatch.setattr("app.cli.audit_expression_registry", fake_registry)
    monkeypatch.setattr("app.cli.audit_expression_target_roadmap", fake_roadmap)
    monkeypatch.setattr("app.cli.expression_audit_eval_readiness", fake_eval_readiness)
    monkeypatch.setattr("app.cli.expression_audit_layer_readiness", fake_layer_readiness)
    monkeypatch.setattr("app.cli.expression_audit_prompt_catalog", fake_prompt_catalog)
    monkeypatch.setattr("app.cli.expression_audit_contract_matrix", fake_contract_matrix)
    monkeypatch.setattr("app.cli.expression_audit_visibility_fact_boundary", fake_visibility_fact_boundary)
    monkeypatch.setattr("app.cli.expression_audit_promotion_surfaces", fake_promotion_surfaces)
    monkeypatch.setattr(
        "app.cli.expression_audit_project_manager_status_card_contract",
        fake_pm_status_card_contract,
    )
    monkeypatch.setattr("app.cli.expression_shadow_project_manager_brief_batch", fake_pm_batch)
    monkeypatch.setattr("app.cli.expression_shadow_client_progress_summary_batch", fake_client_batch)
    monkeypatch.setattr("app.cli.expression_shadow_operations_health_summary_batch", fake_operations_batch)
    monkeypatch.setattr("app.cli.expression_shadow_finance_summary_batch", fake_finance_batch)
    monkeypatch.setattr("app.cli.expression_audit_finance_fact_health", fake_finance_health)
    monkeypatch.setattr("app.cli.audit_executive_company_health_shadow", fake_executive_audit)

    assert (
        expression_audit_production_gate(
            company_id=None,
            project_limit=3,
            client_limit=2,
            operations_limit=2,
            finance_limit=1,
            executive_limit=4,
            window_days=30,
            finance_readiness_window_days=365,
            operations_window_hours=24,
            language="en",
            json_output=True,
            fail_on_warnings=False,
            min_project_samples=1,
            min_client_samples=1,
            min_operations_samples=1,
            min_finance_samples=0,
            min_finance_readiness_samples=1,
        )
        == 0
    )
    pass_output = json.loads(capsys.readouterr().out)
    assert pass_output["status"] == "pass"
    assert pass_output["blocking_reasons"] == []
    assert pass_output["gates"]["target_roadmap"]["hard_bug"] == 0
    assert pass_output["gates"]["target_roadmap"]["product_risk"] == 3
    assert pass_output["gates"]["eval_readiness"]["golden_replay_runs"] == 1
    assert pass_output["gates"]["eval_readiness"]["latest_artifact_type_counts"] == {
        "project_manager_decision_brief": {"cases": 2, "failed": 0, "passed": 2, "skipped": 0}
    }
    assert pass_output["gates"]["eval_readiness"]["registry_replay_coverage"]["totals"]["artifacts_covered"] == 1
    assert pass_output["gates"]["layer_readiness"] == {
        "status": "pass",
        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_shadow_verified": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "artifacts_blocked": 0,
        "artifacts_replay_covered": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "missing_replay_samples": 0,
        "artifacts_with_replay_failures": 0,
    }
    assert pass_output["gates"]["prompt_catalog"] == {
        "status": "pass",
        "artifacts_ready": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "blocking_issues": 0,
        "missing_active_version": 0,
        "missing_active_binding": 0,
        "missing_active_contract": 0,
        "missing_facts_placeholder": 0,
        "missing_contract_schema_placeholder": 0,
        "prompt_format_errors": 0,
    }
    assert pass_output["gates"]["contract_matrix"] == {
        "status": "pass",
        "artifacts_ready": len(EXPRESSION_REGISTRY_EXPECTATIONS),
        "blocking_issues": 0,
        "missing_prompt_catalog": 0,
        "missing_validator": 0,
        "missing_fallback": 0,
        "missing_surface_policy": 0,
    }
    assert pass_output["gates"]["visibility_fact_boundary"] == {
        "status": "pass",
        "boundaries_ready": 2,
        "blocking_issues": 0,
        "sampled_boundaries": 2,
        "sample_denied_key_hits": 0,
    }
    assert pass_output["gates"]["promotion_surfaces"] == {
        "status": "pass",
        "promotion_surface_verified": 2,
        "shadow_only_no_surface": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 2,
        "promotion_surface_needs_review": 0,
        "blocked": 0,
        "unexpected_promoted_artifacts": 0,
    }
    assert pass_output["gates"]["project_manager_status_card_contract"] == {
        "status": "pass",
        "artifacts_sampled": 2,
        "promoted_artifacts_sampled": 0,
        "promoted_contract_issues": 0,
        "shadow_contract_issues": 0,
        "declared_read_surfaces": 1,
        "verified_read_surfaces": 1,
        "blocking_issues": 0,
    }
    assert pass_output["gates"]["finance_summary_shadow_batch"]["projects_selected"] == 0
    assert pass_output["gates"]["finance_summary_readiness_shadow_batch"]["projects_selected"] == 1
    assert pass_output["gates"]["finance_fact_health"]["receipt_fact_count"] == 3

    def fake_pm_invalid(**kwargs):
        print(
            json.dumps(
                {
                    "totals": {
                        "projects_selected": 3,
                        "errors": 0,
                        "shadow_invalid": 1,
                        "shadow_valid": 2,
                        "shadow_fallback_valid": 0,
                    }
                }
            )
        )
        return 0

    monkeypatch.setattr("app.cli.expression_shadow_project_manager_brief_batch", fake_pm_invalid)
    assert (
        expression_audit_production_gate(
            company_id=None,
            project_limit=3,
            client_limit=2,
            operations_limit=2,
            finance_limit=1,
            executive_limit=4,
            window_days=30,
            finance_readiness_window_days=365,
            operations_window_hours=24,
            language="en",
            json_output=True,
            fail_on_warnings=False,
            min_project_samples=1,
            min_client_samples=1,
            min_operations_samples=1,
            min_finance_samples=0,
            min_finance_readiness_samples=1,
        )
        == 1
    )
    fail_output = json.loads(capsys.readouterr().out)
    assert fail_output["status"] == "fail"
    assert "project_manager_shadow_invalid" in fail_output["blocking_reasons"]

    monkeypatch.setattr("app.cli.expression_shadow_project_manager_brief_batch", fake_pm_batch)

    def fake_finance_readiness_empty(**kwargs):
        print(
            json.dumps(
                {
                    "totals": {
                        "projects_selected": 0,
                        "errors": 0,
                        "shadow_invalid": 0,
                        "shadow_valid": 0,
                        "shadow_fallback_valid": 0,
                        "receipt_count": 0,
                    }
                }
            )
        )
        return 0

    monkeypatch.setattr("app.cli.expression_shadow_finance_summary_batch", fake_finance_readiness_empty)
    assert (
        expression_audit_production_gate(
            company_id=None,
            project_limit=3,
            client_limit=2,
            operations_limit=2,
            finance_limit=1,
            executive_limit=4,
            window_days=30,
            finance_readiness_window_days=365,
            operations_window_hours=24,
            language="en",
            json_output=True,
            fail_on_warnings=False,
            min_project_samples=1,
            min_client_samples=1,
            min_operations_samples=1,
            min_finance_samples=0,
            min_finance_readiness_samples=1,
        )
        == 1
    )
    finance_fail_output = json.loads(capsys.readouterr().out)
    assert "finance_readiness_sample_too_small" in finance_fail_output["blocking_reasons"]

    def fake_roadmap_hard_bug(**kwargs):
        print(
            json.dumps(
                {
                    "status": "fail",
                    "schema_version": "expression_roadmap_gap_v1",
                    "totals": {"gaps": 1, "hard_bug": 1, "product_risk": 0, "future": 0},
                }
            )
        )
        return 1

    monkeypatch.setattr("app.cli.expression_shadow_finance_summary_batch", fake_finance_batch)
    monkeypatch.setattr("app.cli.audit_expression_target_roadmap", fake_roadmap_hard_bug)
    assert (
        expression_audit_production_gate(
            company_id=None,
            project_limit=3,
            client_limit=2,
            operations_limit=2,
            finance_limit=1,
            executive_limit=4,
            window_days=30,
            finance_readiness_window_days=365,
            operations_window_hours=24,
            language="en",
            json_output=True,
            fail_on_warnings=False,
            min_project_samples=1,
            min_client_samples=1,
            min_operations_samples=1,
            min_finance_samples=0,
            min_finance_readiness_samples=1,
        )
        == 1
    )
    roadmap_fail_output = json.loads(capsys.readouterr().out)
    assert "target_roadmap_hard_bug" in roadmap_fail_output["blocking_reasons"]

    def fake_layer_readiness_fail(**kwargs):
        print(
            json.dumps(
                {
                    "status": "fail",
                    "totals": {
                        "artifacts_expected": len(EXPRESSION_REGISTRY_EXPECTATIONS),
                        "artifacts_shadow_verified": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 1,
                        "artifacts_blocked": 1,
                        "artifacts_replay_covered": len(EXPRESSION_REGISTRY_EXPECTATIONS) - 1,
                        "missing_replay_samples": 1,
                        "artifacts_with_replay_failures": 0,
                    },
                }
            )
        )
        return 1

    monkeypatch.setattr("app.cli.audit_expression_target_roadmap", fake_roadmap)
    monkeypatch.setattr("app.cli.expression_audit_layer_readiness", fake_layer_readiness_fail)
    assert (
        expression_audit_production_gate(
            company_id=None,
            project_limit=3,
            client_limit=2,
            operations_limit=2,
            finance_limit=1,
            executive_limit=4,
            window_days=30,
            finance_readiness_window_days=365,
            operations_window_hours=24,
            language="en",
            json_output=True,
            fail_on_warnings=False,
            min_project_samples=1,
            min_client_samples=1,
            min_operations_samples=1,
            min_finance_samples=0,
            min_finance_readiness_samples=1,
        )
        == 1
    )
    layer_fail_output = json.loads(capsys.readouterr().out)
    assert "layer_readiness_failed" in layer_fail_output["blocking_reasons"]
    assert layer_fail_output["gates"]["layer_readiness"]["missing_replay_samples"] == 1


def test_employee_contribution_rejects_non_chinese_model_highlights(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    with session_maker() as db:
        db.add_all(
            [
                ExpressionAudience(
                    id="employee",
                    code="employee",
                    title="Employee mobile contribution",
                    default_language="zh",
                    forbidden_phrase_set_id="employee_contribution_zh_v1",
                    is_active=True,
                ),
                ExpressionPromptTemplate(
                    id="employee_contribution_narrative",
                    slug="employee_contribution_narrative",
                    title="Employee contribution narrative",
                    artifact_type="employee_contribution_narrative",
                    stage="expression",
                    default_audience_id="employee",
                ),
                ExpressionPromptVersion(
                    id="employee_contribution_narrative:v1",
                    template_id="employee_contribution_narrative",
                    version="v1",
                    system_prompt="只根据 facts 写中文。",
                    user_prompt_template="{facts_json}\n{contract_schema_json}",
                    status="active",
                ),
                ExpressionPromptBinding(
                    id="global:employee_contribution_narrative:v1",
                    scope_type="global",
                    scope_id=None,
                    template_id="employee_contribution_narrative",
                    prompt_version_id="employee_contribution_narrative:v1",
                    priority=100,
                ),
                ExpressionOutputContract(
                    id="employee_contribution_narrative:employee:v1",
                    artifact_type="employee_contribution_narrative",
                    audience_id="employee",
                    version="v1",
                    json_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "summary_line",
                            "contribution_explanation",
                            "strengths",
                            "suggestions",
                            "comparison_text",
                            "recent_highlights",
                            "disclaimer",
                        ],
                        "properties": {
                            "summary_line": {"type": "string"},
                            "contribution_explanation": {"type": "array", "items": {"type": "string"}},
                            "strengths": {"type": "array", "items": {"type": "string"}},
                            "suggestions": {"type": "array", "items": {"type": "string"}},
                            "comparison_text": {"type": "string"},
                            "recent_highlights": {"type": "array", "items": {"type": "object"}},
                            "disclaimer": {"type": "string", "const": FIXED_EMPLOYEE_DISCLAIMER},
                        },
                    },
                    is_active=True,
                ),
                Photo(
                    company_id="default",
                    employee_id="E100",
                    project_id="P100",
                    photo_type=PhotoType.project,
                    file_path="/tmp/test.jpg",
                    image_url="/media/photos/test.jpg",
                    original_file_name="test.jpg",
                    captured_at_utc=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
                    approval_status=ApprovalStatus.approved,
                    labeling_status="completed",
                    tag_json={"ai_summary": "Stored English site note", "labels": ["site"], "defects": []},
                ),
            ]
        )
        db.commit()

    class MockResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": json.dumps(
                    {
                        "summary_line": "最近 30 天有现场记录。",
                        "contribution_explanation": ["这些照片帮助团队了解现场。"],
                        "strengths": [],
                        "suggestions": ["后续可补充关键节点照片。"],
                        "comparison_text": "与上一窗口相比记录数量基本持平。",
                        "recent_highlights": [{"photo_id": 1, "text": "English only highlight"}],
                        "disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
                    },
                    ensure_ascii=False,
                )
            }

    monkeypatch.setattr("app.services.expression.requests.post", lambda *args, **kwargs: MockResponse())

    with session_maker() as db:
        result = generate_employee_contribution_shadow(
            db,
            app_settings=settings,
            company_id="default",
            employee_id="E100",
            project_id="P100",
            window_days=30,
            use_ai=True,
        )
        db.commit()
        artifact_id = result.artifact.id

    with session_maker() as db:
        artifact = db.get(ExpressionArtifact, artifact_id)
        assert artifact is not None
        assert artifact.validation_status == "shadow_fallback_valid"
        assert any("recent_highlights" in error for error in artifact.validation_errors_json)
        assert artifact.structured_json["recent_highlights"][0]["text"].startswith("照片")


def test_promote_expression_artifact_supersedes_existing_promoted_artifact(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience = ExpressionAudience(
            id="employee",
            code="employee",
            title="Employee mobile contribution",
            default_language="zh",
            forbidden_phrase_set_id="employee_contribution_zh_v1",
            is_active=True,
        )
        template = ExpressionPromptTemplate(
            id="employee_contribution_narrative",
            slug="employee_contribution_narrative",
            title="Employee contribution narrative",
            artifact_type="employee_contribution_narrative",
            stage="expression",
            default_audience_id="employee",
        )
        version = ExpressionPromptVersion(
            id="employee_contribution_narrative:v2",
            template_id="employee_contribution_narrative",
            version="v2",
            system_prompt="只根据 facts 写中文。",
            user_prompt_template="{facts_json}",
            status="active",
        )
        snapshot = FactSnapshot(
            id="snapshot-promote-001",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            scope_type="employee_project_window",
            scope_key_json={"employee_id": "E100", "project_id": "P100", "window_days": 30},
            scope_key_hash="hash-promote-e100-p100-30",
            assembler_version="employee_contribution_v1",
            source_manifest_json={"tables": ["photos"]},
            facts_json={"counts": {"photos_current": 2}},
            fact_count=2,
            coverage_json={"has_current_photos": True},
        )
        old_artifact = ExpressionArtifact(
            id="artifact-promote-old",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=version.id,
            contract_id=None,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "旧说明"},
            rendered_markdown="旧说明",
            validation_status="promoted_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "test"},
            promoted=True,
        )
        new_artifact = ExpressionArtifact(
            id="artifact-promote-new",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=version.id,
            contract_id=None,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id=audience.id,
            language="zh",
            structured_json={"summary_line": "新说明"},
            rendered_markdown="新说明",
            validation_status="shadow_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "test"},
            promoted=False,
        )
        db.add_all([audience, template, version, snapshot, old_artifact, new_artifact])
        db.commit()

    with session_maker() as db:
        promoted = promote_expression_artifact(db, artifact_id="artifact-promote-new")
        db.commit()
        promoted_id = promoted.id

    with session_maker() as db:
        promoted = db.get(ExpressionArtifact, promoted_id)
        old = db.get(ExpressionArtifact, "artifact-promote-old")
        assert promoted is not None
        assert promoted.promoted is True
        assert promoted.validation_status == "promoted_valid"
        assert promoted.supersedes_artifact_id == "artifact-promote-old"
        assert old is not None
        assert old.promoted is False


_EMPLOYEE_ZERO_CURRENT_PHOTO_PHRASES = (
    "这些照片",
    "这批记录",
    "连续记录",
    "这批照片",
)


def _seed_employee_contribution_validation_contract(db) -> tuple[ExpressionAudience, ExpressionOutputContract]:
    audience = ExpressionAudience(
        id="employee",
        code="employee",
        title="Employee mobile contribution",
        default_language="zh",
        forbidden_phrase_set_id="empty-test-set",
        is_active=True,
    )
    contract = ExpressionOutputContract(
        id="employee_contribution_narrative:employee:zero-photo-test",
        artifact_type="employee_contribution_narrative",
        audience_id="employee",
        version="zero-photo-test",
        json_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary_line",
                "contribution_explanation",
                "strengths",
                "suggestions",
                "comparison_text",
                "recent_highlights",
                "disclaimer",
            ],
            "properties": {
                "summary_line": {"type": "string"},
                "contribution_explanation": {"type": "array", "items": {"type": "string"}},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "suggestions": {"type": "array", "items": {"type": "string"}},
                "comparison_text": {"type": "string"},
                "recent_highlights": {"type": "array", "items": {"type": "object"}},
                "disclaimer": {"type": "string", "const": FIXED_EMPLOYEE_DISCLAIMER},
            },
        },
        is_active=True,
    )
    db.add_all([audience, contract])
    db.commit()
    return audience, contract


def test_expression_backend_candidates_adds_override_without_replacing_global_backends(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.expression_text_backend_url = "http://expression-only.test"
    settings.expression_text_backend_model = "custom-expression-model"

    global_backend = AIBackendNode(
        id="tenant-ollama",
        type=OLLAMA_TYPE,
        url="http://tenant-ai.test",
        model="tenant-base-model",
        weight=5,
        enabled=True,
    )
    resolve_calls: list[tuple[str | None, str]] = []

    def fake_resolve(db, app_settings, company_id):
        resolve_calls.append((app_settings.expression_text_backend_url, company_id))
        return [global_backend], "tenant"

    monkeypatch.setattr("app.services.expression.resolve_ai_backends_for_tenant", fake_resolve)

    with session_maker() as db:
        candidates = _expression_backend_candidates(
            db,
            app_settings=settings,
            company_id="default",
            preferred_model="preferred-expression-model",
        )

    assert [candidate.id for candidate in candidates] == [
        "expression-text-override",
        f"{global_backend.id}:expression:preferred-expression-model",
    ]
    assert candidates[0].url == "http://expression-only.test"
    assert candidates[0].model == "custom-expression-model"
    assert candidates[1].url == global_backend.url
    assert candidates[1].model == "preferred-expression-model"
    assert resolve_calls == [("http://expression-only.test", "default")]


def test_expression_backend_candidates_omits_override_when_url_unset(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.expression_text_backend_url = None
    settings.expression_text_backend_model = None

    global_backend = AIBackendNode(
        id="system-ollama",
        type=OLLAMA_TYPE,
        url="http://system-ai.test",
        model="system-base-model",
        weight=1,
        enabled=True,
    )
    monkeypatch.setattr(
        "app.services.expression.resolve_ai_backends_for_tenant",
        lambda db, app_settings, company_id: ([global_backend], "system"),
    )

    with session_maker() as db:
        candidates = _expression_backend_candidates(
            db,
            app_settings=settings,
            company_id="acme",
            preferred_model=None,
        )

    assert len(candidates) == 1
    assert candidates[0].id == f"{global_backend.id}:expression:{DEFAULT_EXPRESSION_TEXT_MODEL}"
    assert candidates[0].url == global_backend.url
    assert candidates[0].model == DEFAULT_EXPRESSION_TEXT_MODEL


def test_expression_backend_candidates_override_model_falls_back_to_preferred_then_default(app_context, monkeypatch):
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.expression_text_backend_url = "http://expression-only.test"
    settings.expression_text_backend_model = None
    monkeypatch.setattr(
        "app.services.expression.resolve_ai_backends_for_tenant",
        lambda db, app_settings, company_id: ([], "none"),
    )

    with session_maker() as db:
        preferred_candidates = _expression_backend_candidates(
            db,
            app_settings=settings,
            company_id="default",
            preferred_model="preferred-expression-model",
        )
        default_candidates = _expression_backend_candidates(
            db,
            app_settings=settings,
            company_id="default",
            preferred_model=None,
        )

    assert preferred_candidates[0].model == "preferred-expression-model"
    assert default_candidates[0].model == DEFAULT_EXPRESSION_TEXT_MODEL


@pytest.mark.parametrize("phrase", _EMPLOYEE_ZERO_CURRENT_PHOTO_PHRASES)
def test_employee_contribution_rejects_zero_current_photo_phrases(app_context, phrase):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        audience, contract = _seed_employee_contribution_validation_contract(db)
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload={
                "summary_line": "最近记录窗口内暂未看到新的项目现场照片。",
                "contribution_explanation": [f"说明包含{phrase}。"],
                "strengths": [],
                "suggestions": ["后续可继续补充关键节点照片。"],
                "comparison_text": "与上一记录窗口相比，本窗口未见新的现场照片。",
                "recent_highlights": [],
                "disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
            },
            language="zh",
            facts={"counts": {"photos_current": 0, "photos_previous": 2}},
        )

    assert f"employee_contribution:zero_current_photos:{phrase}" in errors


def test_employee_contribution_zero_current_fallback_passes_validation(app_context):
    session_maker = app_context["session_maker"]
    facts = {
        "counts": {"photos_current": 0, "photos_previous": 4, "active_days_current": 0, "completed_ai_current": 0},
        "trend": {"photo_delta": -4},
        "recent_highlights": [],
    }

    with session_maker() as db:
        audience, contract = _seed_employee_contribution_validation_contract(db)
        payload = build_employee_contribution_fallback(facts)
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload=payload,
            language="zh",
            facts=facts,
        )

    assert errors == []
    combined = json.dumps(payload, ensure_ascii=False)
    for phrase in _EMPLOYEE_ZERO_CURRENT_PHOTO_PHRASES:
        assert phrase not in combined


def test_employee_contribution_allows_current_photo_language_when_photos_present(app_context):
    session_maker = app_context["session_maker"]
    facts = {"counts": {"photos_current": 6, "photos_previous": 4}}

    with session_maker() as db:
        audience, contract = _seed_employee_contribution_validation_contract(db)
        errors = _validate_expression_payload(
            db,
            audience=audience,
            contract=contract,
            payload={
                "summary_line": "最近记录窗口内共有 6 张现场照片。",
                "contribution_explanation": ["这些照片为项目现场回看提供了连续记录。"],
                "strengths": ["这批记录补充了近期现场资料。"],
                "suggestions": ["后续可继续补充这批照片未覆盖的节点。"],
                "comparison_text": "与上一记录窗口相比，本窗口的现场照片数量有所增加。",
                "recent_highlights": [{"photo_id": 1, "text": "照片 1 已记录现场材料、设备或环境信息。"}],
                "disclaimer": FIXED_EMPLOYEE_DISCLAIMER,
            },
            language="zh",
            facts=facts,
        )

    assert not any(error.startswith("employee_contribution:zero_current_photos:") for error in errors)


def test_evidence_copilot_normalize_key_fits_column_limit():
    from app.services.evidence_copilot import _normalize_key

    assert _normalize_key("Tool & Brand!") == "tool_brand"
    long_key = _normalize_key("word " * 100)
    assert len(long_key) <= 160
    assert not long_key.endswith("_")
