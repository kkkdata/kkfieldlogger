from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select

from app.core.security import hash_password
from app.models import AIAnalysisLog, AIAnalysisStatus, AIAnalysisType, AnnotationRole, AnnotationStatus, AnnotationType, AnnotationVisibility, ApprovalStatus, AuditLog, CameraApprovalStatus, Company, CompanyApplication, CompanyApplicationStatus, Employee, EvidenceObservation, ExpressionArtifact, ExpressionAudience, ExpressionPromptTemplate, ExpressionPromptVersion, FactSnapshot, GeneratedReport, IPCamera, MediaAnnotation, MediaAsset, MediaAssetStatus, MediaType, Membership, PasswordResetToken, Photo, PhotoComment, PhotoType, PhotoVisibility, PlanCode, ProgressReport, ProgressReportStatus, Project, ProjectMember, ProjectStatus, ReceiptFact, ReportStatus, Subscription, SystemSetting, TaskJob, TaskStatus, Tenant, TenantStatus, User, UserRole
from app.services.auth_tokens import create_password_reset_token
from app.services.ai_pipeline import normalize_ai_result
from app.services.tenant import provision_company_workspace

def _jpeg_bytes(tag: str) -> bytes:
    import io as _io

    from PIL import Image as _Image

    buffer = _io.BytesIO()
    _Image.new("RGB", (4, 4), "gray").save(buffer, "JPEG")
    # Trailing bytes after the JPEG EOI marker are ignored by decoders but make
    # each upload's checksum unique so the duplicate-upload guard stays out of
    # the way of tests that upload multiple photos.
    return buffer.getvalue() + tag.encode()


def _upload(client, api_key: str, filename: str, employee_id: str, project_id: str, photo_type: str, extra_data: dict | None = None):
    payload = {
        "employee_id": employee_id,
        "project_id": project_id,
        "photo_type": photo_type,
        "gps": "36.15398,-95.99277",
        "gps_lat": "36.15398",
        "gps_lon": "-95.99277",
        "heading": "135.1",
        "pitch": "4.2",
        "roll": "0.0",
        "location": "Site Alpha",
        "timestamp": "2026-03-18T12:00:00Z",
    }
    if extra_data:
        payload.update(extra_data)
    return client.post(
        "/upload",
        headers={"X-API-Key": api_key},
        data=payload,
        files={"photo": (filename, _jpeg_bytes(filename), "image/jpeg")},
    )


def _login(client, username: str, password: str):
    return client.post(
        "/portal/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _extract_js_csrf(html: str) -> str:
    match = re.search(r'const csrfToken = "([^"]+)"', html)
    assert match
    return match.group(1)


def test_mobile_diagnostic_log_upload_requires_api_key_and_writes_file(app_context):
    client = app_context["client"]
    settings = app_context["settings"]
    settings.mobile_diagnostic_log_rate_limit_per_hour = 1
    response = client.post(
        "/api/mobile/diagnostics/logs",
        json={"source": "test", "log_text": "network timeout\nX-API-Key: [REDACTED]"},
    )
    assert response.status_code == 401

    response = client.post(
        "/api/mobile/diagnostics/logs",
        headers={"X-API-Key": "api-key-e100"},
        json={
            "source": "manual_debug_screen",
            "created_at": "2026-06-15T20:00:00Z",
            "log_text": "network timeout\nbody=[omitted]",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    log_path = settings.logs_root / "mobile_diagnostics" / payload["file_name"]
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "employee_id=E100" in content
    assert "network timeout" in content

    limited = client.post(
        "/api/mobile/diagnostics/logs",
        headers={"X-API-Key": "api-key-e100"},
        json={"source": "manual_debug_screen", "log_text": "another log"},
    )
    assert limited.status_code == 429


def _provision_tenant_owner(
    session_maker,
    *,
    company_name: str,
    company_id: str,
    company_code: str,
    username: str,
    email: str,
    password: str = "TenantOwner123!",
) -> dict[str, str]:
    with session_maker() as db:
        company, _, owner, _ = provision_company_workspace(
            db,
            company_name=company_name,
            display_name=f"{company_name} Owner",
            email=email,
            password=password,
            company_id=company_id,
            company_code=company_code,
            username=username,
        )
        db.commit()
        return {
            "company_id": company.company_id,
            "company_code": company.company_code,
            "company_name": company.company_name,
            "username": owner.username,
            "password": password,
            "email": owner.email or "",
        }


def test_ai_model_json_parser_accepts_wrapped_json():
    payload = normalize_ai_result(
        "\n".join(
            [
                "Here is the requested JSON:",
                "```json",
                '{"ai_summary":"Visible breaker panel and tool cart.","labels":["electrical room","tool cart"],"defects":["none"]}',
                "```",
            ]
        )
    )

    assert payload["ai_summary"] == "Visible breaker panel and tool cart."
    assert payload["labels"] == ["electrical_room", "tool_cart"]
    assert payload["defects"] == []


def test_mobile_contract_and_soft_delete(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    upload_project = _upload(client, "api-key-e100", "visible-project.jpg", "E100", "P100", "project")
    assert upload_project.status_code == 200

    upload_invoice = _upload(client, "api-key-e100", "invoice-receipt.jpg", "E100", "invoice", "invoice")
    assert upload_invoice.status_code == 200

    project_payload = upload_project.json()["photo"]
    invoice_payload = upload_invoice.json()["photo"]
    assert project_payload["photo_type"] == "project"
    assert invoice_payload["photo_type"] == "invoice"
    assert project_payload["thumb_url"].startswith("http://testserver/media/")
    assert "/media/default/P100/E100/" in project_payload["image_url"]
    assert "/media/default/invoice/E100/" in invoice_payload["image_url"]
    assert project_payload["approval_status"] == "pending"
    assert project_payload["labeling_status"] == "pending"
    assert invoice_payload["labeling_status"] == "pending"
    assert upload_project.json()["queue"]["active_count"] >= 1
    assert upload_project.json()["photo"]["queue_info"]["active_count"] >= 1
    assert upload_project.json()["queue"]["estimated_completion_time"]
    assert upload_project.json()["queue"]["position_in_queue"] is not None
    assert "Estimated completion" in upload_project.json()["queue"]["queue_message"]
    assert "Estimated completion" in upload_project.json()["queue_message"]

    image_response = client.get(project_payload["image_url"])
    assert image_response.status_code == 200

    photos_me = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert photos_me.status_code == 200
    payload = photos_me.json()
    assert len(payload) == 2
    assert payload[0]["captured_at_utc"].endswith("Z")
    assert payload[0]["image_url"].startswith("http://testserver/media/")
    assert payload[0]["media_url"] == payload[0]["image_url"]
    assert payload[0]["media_kind"] == "photo"
    assert payload[0]["media_asset_id"] == str(payload[0]["id"])
    assert payload[0]["duration_seconds"] == 0.0
    assert any(item["queue_info"] for item in payload)
    assert payload[0]["queue_info"]["estimated_completion_time"]
    assert payload[0]["queue_info"]["position_in_queue"] is not None

    with session_maker() as db:
        records = list(db.scalars(select(Photo).order_by(Photo.id)))
        assert len(records) == 2
        assert records[0].employee_id == "E100"

    deleted = client.delete(f"/photo/{project_payload['id']}", headers={"X-API-Key": "api-key-e100"})
    assert deleted.status_code == 200

    with session_maker() as db:
        deleted_record = db.scalar(select(Photo).where(Photo.id == project_payload["id"]))
        assert deleted_record.deleted is True
        assert deleted_record.deleted_at is not None

    after_delete = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert after_delete.status_code == 200
    remaining_ids = {item["id"] for item in after_delete.json()}
    assert project_payload["id"] not in remaining_ids
    assert invoice_payload["id"] in remaining_ids


def test_mobile_upload_note_and_ai_pipeline(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload_response = _upload(
        client,
        "api-key-e100",
        "ai-note-photo.jpg",
        "E100",
        "P100",
        "project",
        extra_data={"note": "North wall crack near loading bay"},
    )
    assert upload_response.status_code == 200

    upload_payload = upload_response.json()["photo"]
    assert upload_payload["note"] == "North wall crack near loading bay"
    assert upload_payload["tag_json"] is None
    assert upload_payload["labeling_status"] == "pending"

    photos_me = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert photos_me.status_code == 200
    listed_photo = next(item for item in photos_me.json() if item["id"] == upload_payload["id"])
    assert listed_photo["note"] == "North wall crack near loading bay"
    assert listed_photo["labeling_status"] == "completed"
    assert "Mock field photo" in listed_photo["tag_json"]["ai_summary"]
    assert isinstance(listed_photo["tag_json"]["labels"], list)
    assert listed_photo["tag_json"]["defects"] == []

    with session_maker() as db:
        stored_photo = db.scalar(select(Photo).where(Photo.id == upload_payload["id"]))
        assert stored_photo is not None
        assert stored_photo.note == "North wall crack near loading bay"
        assert stored_photo.labeling_status == "completed"
        assert "Mock field photo" in stored_photo.tag_json["ai_summary"]
        assert isinstance(stored_photo.tag_json["labels"], list)
        assert stored_photo.tag_json["defects"] == []


def test_private_profile_hides_platform_surfaces(app_context):
    client = app_context["client"]
    settings = app_context["settings"]
    settings.deployment_profile = "private"
    try:
        assert client.get("/portal/platform").status_code == 404
        assert client.get("/portal/billing").status_code == 404
        assert client.get("/register").status_code == 404
        assert client.post("/api/v2/auth/register", json={}).status_code == 404
        assert client.post("/api/v2/auth/switch-tenant", json={}).status_code == 404

        login = _login(client, "admin", "AdminPass123!")
        assert login.status_code == 200
        today = client.get("/portal/today")
        assert today.status_code == 200
        assert 'href="/portal/platform"' not in today.text
        assert 'href="/portal/billing"' not in today.text

        # users already exist, so the setup wizard must refuse to run
        setup = client.get("/setup", follow_redirects=False)
        assert setup.status_code == 303
        assert setup.headers["location"] == "/login"
    finally:
        settings.deployment_profile = "saas"
        client.cookies.clear()

    assert client.get("/register").status_code == 200


def test_retention_pass_prunes_old_rows(app_context):
    from datetime import timedelta

    from app.core.time import utc_now
    from app.services.retention import run_retention_pass

    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    old = utc_now() - timedelta(days=400)

    with session_maker() as db:
        db.add(
            AuditLog(
                action="retention_test_old",
                target_type="test",
                target_id="1",
                company_id="default",
                created_at=old,
            )
        )
        db.add(
            TaskJob(
                public_id=str(uuid4()),
                company_id="default",
                task_type="photo_ai_pipeline",
                status=TaskStatus.completed,
                priority=100,
                created_at=old,
                updated_at=old,
            )
        )
        db.add(
            AIAnalysisLog(
                id=str(uuid4()),
                photo_id=None,
                media_asset_id=None,
                analysis_type=AIAnalysisType.deep_analysis,
                prompt_used="retention test",
                model_used="test",
                status=AIAnalysisStatus.rejected,
                result_data={},
                created_at=old,
            )
        )
        db.commit()

    summary = run_retention_pass(session_maker, settings)
    assert summary["audit_logs_deleted"] >= 1
    assert summary["completed_jobs_deleted"] >= 1
    assert summary["superseded_ai_logs_deleted"] >= 1

    with session_maker() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "retention_test_old")) is None


def test_today_workbench_is_manager_landing_page(app_context):
    client = app_context["client"]

    login = _login(client, "admin", "AdminPass123!")
    assert login.status_code == 200
    assert str(login.request.url).endswith("/portal/today")
    assert "Handled automatically" in login.text

    today = client.get("/portal/today")
    assert today.status_code == 200
    assert "Project pulse" in today.text

    classic = client.get("/portal?classic=1")
    assert classic.status_code == 200
    assert "/portal/today" not in str(classic.request.url)


def test_mobile_upload_recovers_unknown_project_id_to_assigned_project(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    response = _upload(client, "api-key-e100", "wrong-project.jpg", "E100", "10010001", "project")
    assert response.status_code == 200
    payload = response.json()["photo"]
    assert payload["project_id"] == "P100"

    with session_maker() as db:
        stored_photo = db.scalar(select(Photo).where(Photo.id == payload["id"]))
        assert stored_photo.project_id == "P100"
        assert stored_photo.metadata_json["original_project_id"] == "10010001"
        assert "/P100/" in stored_photo.image_url
        assert "/P100/" in stored_photo.thumb_url
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "mobile_upload_project_recovered",
                AuditLog.target_id == str(payload["id"]),
            )
        )
        assert audit is not None
        assert audit.detail_json["original_project_id"] == "10010001"
        assert audit.detail_json["resolved_project_id"] == "P100"


def test_mobile_upload_unknown_project_id_still_rejected_without_assignment(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        employee = db.scalar(select(Employee).where(Employee.employee_id == "E100"))
        employee.project_id = None
        db.commit()

    response = _upload(client, "api-key-e100", "wrong-project-2.jpg", "E100", "10010001", "project")
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown project_id"


def test_mobile_project_work_evidence_returns_processed_employee_project_data(app_context):
    client = app_context["client"]

    upload_response = _upload(
        client,
        "api-key-e100",
        "work-evidence-photo.jpg",
        "E100",
        "P100",
        "project",
        extra_data={"note": "South trench conduit staged"},
    )
    assert upload_response.status_code == 200
    photo_id = upload_response.json()["photo"]["id"]

    photos_me = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert photos_me.status_code == 200

    evidence = client.get(
        "/api/mobile/projects/P100/work-evidence?language=zh&limit=10",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert evidence.status_code == 200
    payload = evidence.json()
    assert payload["employee"]["employee_id"] == "E100"
    assert payload["project"]["project_id"] == "P100"
    assert payload["summary"]["photo_count"] >= 1
    assert payload["summary"]["labeling_counts"]["completed"] >= 1
    assert isinstance(payload["summary"]["top_labels"], list)
    item = next(item for item in payload["items"] if item["photo_id"] == photo_id)
    assert item["note"] == "South trench conduit staged"
    assert item["media_url"].startswith("http://testserver/media/")
    assert item["ai_summary"]
    assert isinstance(item["labels"], list)
    assert isinstance(item["recommended_actions"], list)

    cross_company = client.get(
        "/api/mobile/projects/P200/work-evidence",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert cross_company.status_code == 404


def test_mobile_project_contribution_returns_latest_validated_expression(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        db.add(
            ExpressionAudience(
                id="employee",
                code="employee",
                title="Employee mobile contribution",
                default_language="zh",
                forbidden_phrase_set_id="employee_contribution_zh_v1",
                is_active=True,
            )
        )
        db.add(
            ExpressionPromptTemplate(
                id="employee_contribution_narrative",
                slug="employee_contribution_narrative",
                title="Employee contribution narrative",
                artifact_type="employee_contribution_narrative",
                stage="expression",
                default_audience_id="employee",
            )
        )
        db.add(
            ExpressionPromptVersion(
                id="employee_contribution_narrative:v2",
                template_id="employee_contribution_narrative",
                version="v2",
                system_prompt="只根据 facts 写中文。",
                user_prompt_template="{facts_json}",
                status="active",
            )
        )
        snapshot = FactSnapshot(
            id="snapshot-mobile-contribution-001",
            tenant_id="default",
            company_id="default",
            employee_id="E100",
            project_id="P100",
            scope_type="employee_project_window",
            scope_key_json={"company_id": "default", "employee_id": "E100", "project_id": "P100", "window_days": 30},
            scope_key_hash="default-e100-p100-30",
            assembler_version="employee_contribution_v1",
            source_manifest_json={"tables": ["photos", "evidence_observations"]},
            facts_json={
                "counts": {
                    "photos_current": 4,
                    "photos_previous": 2,
                    "active_days_current": 2,
                    "active_days_previous": 1,
                    "completed_ai_current": 4,
                    "completed_ai_previous": 2,
                },
                "trend": {"photo_delta": 2, "active_day_delta": 1, "completed_ai_delta": 2},
                "coverage": {"has_current_photos": True, "has_completed_ai": True, "has_recent_highlights": True},
            },
            fact_count=12,
            coverage_json={"has_current_photos": True},
        )
        manager_snapshot = FactSnapshot(
            id="snapshot-mobile-manager-status-card-001",
            tenant_id="default",
            company_id="default",
            employee_id=None,
            project_id="P100",
            scope_type="project_period",
            scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
            scope_key_hash="default-p100-manager-card-30",
            assembler_version="project_manager_decision_brief_v1",
            source_manifest_json={"tables": ["projects", "photos", "progress_reports", "expression_artifacts"]},
            facts_json={
                "scope": {"company_id": "default", "project_id": "P100"},
                "photo_metrics": {"photos_current": 4, "active_days_current": 2},
                "progress_report_metrics": {"completed_with_content": 1},
            },
            fact_count=6,
            coverage_json={"has_current_photos": True},
        )
        artifact = ExpressionArtifact(
            id="artifact-mobile-contribution-001",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id="employee_contribution_narrative:v2",
            contract_id=None,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            language="zh",
            structured_json={
                "summary_line": "最近记录窗口内共有 4 张现场照片，覆盖 2 个记录日。",
                "contribution_explanation": ["这些照片为项目现场回看提供了连续记录。"],
                "strengths": ["这批记录补充了近期现场资料。"],
                "suggestions": ["后续可继续补充关键工序照片。"],
                "comparison_text": "与上一记录窗口相比，本窗口的现场照片数量有所增加。",
                "recent_highlights": [{"photo_id": 1, "text": "照片 1 已记录现场材料、设备或环境信息。"}],
                "disclaimer": "该说明仅用于现场记录参考，不是绩效评分。",
            },
            raw_model_output='{"hidden":"raw"}',
            rendered_markdown="最近记录窗口内共有 4 张现场照片，覆盖 2 个记录日。",
            validation_status="promoted_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "ollama-test"},
            promoted=True,
        )
        shadow_artifact = ExpressionArtifact(
            id="artifact-mobile-contribution-shadow",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id="employee_contribution_narrative:v2",
            contract_id=None,
            scope_type=snapshot.scope_type,
            scope_key_json=snapshot.scope_key_json,
            artifact_type="employee_contribution_narrative",
            audience_id="employee",
            language="zh",
            structured_json={"summary_line": "这个 shadow artifact 不应该出现在 APP。"},
            raw_model_output='{"hidden":"shadow"}',
            rendered_markdown="这个 shadow artifact 不应该出现在 APP。",
            validation_status="shadow_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "ollama-test"},
            promoted=False,
        )
        current_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="/tmp/dashboard-current.jpg",
            image_url="/media/default/P100/E100/dashboard-current.jpg",
            original_file_name="dashboard-current.jpg",
            captured_at_utc=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            labeling_status="completed",
        )
        previous_photo = Photo(
            company_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="/tmp/dashboard-previous.jpg",
            image_url="/media/default/P100/E100/dashboard-previous.jpg",
            original_file_name="dashboard-previous.jpg",
            captured_at_utc=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            approval_status=ApprovalStatus.approved,
            labeling_status="completed",
        )
        report = ProgressReport(
            id="progress-report-dashboard-001",
            tenant_id="default",
            company_id="default",
            project_id="P100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content='{"executive_summary":"Stored report"}',
            completed_at=datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc),
        )
        markdown_shadow_artifact = ExpressionArtifact(
            id="artifact-dashboard-markdown-shadow",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=None,
            contract_id=None,
            scope_type="progress_report",
            scope_key_json={"company_id": "default", "project_id": "P100", "report_id": report.id},
            artifact_type="generated_report_markdown",
            audience_id="project_manager",
            language="en",
            structured_json={"sections": [{"markdown": "shadow markdown body must not leak"}]},
            rendered_markdown="shadow markdown body must not leak",
            validation_status="shadow_valid",
            validation_errors_json=[],
            model_used="qwen2.5:7b-instruct",
            backend_profile_json={"backend_id": "ollama-test"},
            promoted=False,
        )
        manager_shadow_artifact = ExpressionArtifact(
            id="artifact-dashboard-manager-shadow",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=None,
            contract_id=None,
            scope_type="project_period",
            scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
            artifact_type="project_manager_decision_brief",
            audience_id="project_manager",
            language="en",
            structured_json={
                "body": {"paragraphs": ["decision brief body must not leak"]},
                "decision_surface": {"items": []},
            },
            rendered_markdown="decision brief body must not leak",
            validation_status="shadow_fallback_valid",
            validation_errors_json=[],
            model_used="deterministic_fallback",
            backend_profile_json={"backend_id": "deterministic_fallback"},
            promoted=False,
        )
        manager_promoted_artifact = ExpressionArtifact(
            id="artifact-dashboard-manager-promoted",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=manager_snapshot.id,
            prompt_version_id="project_manager_decision_brief:v1",
            contract_id="project_manager_decision_brief:project_manager:v1",
            scope_type="project_period",
            scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
            artifact_type="project_manager_decision_brief",
            audience_id="project_manager",
            language="en",
            structured_json={
                "artifact_type": "project_manager_decision_brief",
                "artifact_version": "1.0.0",
                "audience": "project_manager",
                "visibility": "promoted",
                "project_id": "P100",
                "as_of": "2026-06-15T13:00:00+00:00",
                "decision_surface": {
                    "computed_at": "2026-06-15T13:00:00+00:00",
                    "items": [
                        {
                            "item_id": "evidence_volume_current",
                            "category": "evidence_coverage",
                            "severity": "info",
                            "deterministic_summary": "The current project window has 4 database photos.",
                            "fact_refs": [
                                {"field_path": "photo_metrics.photos_current", "observed_value": 4}
                            ],
                            "metrics": {"photos_current": 4},
                        }
                    ],
                },
                "body": {
                    "format": "markdown",
                    "paragraphs": ["The current project window has 4 database photos."],
                    "word_count": 8,
                    "generation_path": "deterministic_fallback",
                },
                "provenance": {
                    "source_tables": ["projects", "photos", "progress_reports"],
                    "fact_snapshot_id": manager_snapshot.id,
                    "assembler_version": "project_manager_decision_brief_v1",
                },
                "validation": {
                    "decision_surface_locked": True,
                    "ai_changed_decision_surface": False,
                },
            },
            raw_model_output='{"hidden":"manager raw"}',
            rendered_markdown="- The current project window has 4 database photos.",
            validation_status="promoted_valid",
            validation_errors_json=[],
            model_used="deterministic_fallback",
            backend_profile_json={"backend_id": "deterministic_fallback"},
            promoted=True,
        )
        client_shadow_artifact = ExpressionArtifact(
            id="artifact-dashboard-client-shadow",
            tenant_id="default",
            company_id="default",
            fact_snapshot_id=snapshot.id,
            prompt_version_id=None,
            contract_id=None,
            scope_type="client_project_period",
            scope_key_json={"company_id": "default", "project_id": "P100", "window_days": 30},
            artifact_type="client_progress_summary",
            audience_id="client",
            language="en",
            structured_json={
                "body": {"paragraphs": ["client progress body must not leak"]},
                "summary_surface": {"items": []},
            },
            rendered_markdown="client progress body must not leak",
            validation_status="shadow_fallback_valid",
            validation_errors_json=[],
            model_used="deterministic_fallback",
            backend_profile_json={"backend_id": "deterministic_fallback"},
            promoted=False,
        )
        db.add_all(
            [
                snapshot,
                manager_snapshot,
                artifact,
                shadow_artifact,
                current_photo,
                previous_photo,
                report,
                markdown_shadow_artifact,
                manager_shadow_artifact,
                manager_promoted_artifact,
                client_shadow_artifact,
            ]
        )
        db.commit()

    response = client.get("/api/mobile/projects/P100/contribution", headers={"X-API-Key": "api-key-e100"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "available"
    assert payload["employee"]["employee_id"] == "E100"
    assert payload["project"]["project_id"] == "P100"
    assert payload["contribution"]["summary_line"].startswith("最近记录窗口内共有 4 张")
    assert payload["contribution"]["summary"].startswith("最近记录窗口内共有 4 张")
    assert payload["contribution"]["comparison"] == "与上一记录窗口相比，本窗口的现场照片数量有所增加。"
    assert payload["contribution"]["highlights"][0]["photo_id"] == 1
    assert payload["contribution"]["sections"]["contribution_explanation"] == [
        "这些照片为项目现场回看提供了连续记录。"
    ]
    assert payload["contribution"]["sections"]["strengths"] == ["这批记录补充了近期现场资料。"]
    assert payload["contribution"]["sections"]["suggestions"] == ["后续可继续补充关键工序照片。"]
    assert payload["facts_summary"]["photos_current"] == 4
    assert payload["facts_summary"]["photo_delta"] == 2
    assert payload["artifact"]["artifact_id"] == "artifact-mobile-contribution-001"
    assert payload["artifact"]["prompt_version_id"] == "employee_contribution_narrative:v2"
    assert payload["artifact"]["validation_status"] == "promoted_valid"
    assert payload["artifact"]["promoted"] is True
    assert "shadow artifact" not in payload["contribution"]["summary_line"]
    assert "raw_model_output" not in payload["artifact"]

    dashboard = client.get("/api/mobile/projects/P100/contribution-dashboard", headers={"X-API-Key": "api-key-e100"})
    assert dashboard.status_code == 200
    dashboard_payload = dashboard.json()
    assert dashboard_payload["contract_version"] == "mobile_contribution_dashboard:v1"
    assert dashboard_payload["status"] == "ready"
    assert dashboard_payload["guardrails"]["uses_promoted_artifacts_only"] is True
    assert dashboard_payload["guardrails"]["exposes_shadow_content"] is False
    modules = {module["module_id"]: module for module in dashboard_payload["modules"]}
    assert modules["deterministic_metrics"]["status"] == "ready"
    assert modules["deterministic_metrics"]["source"] == "db_metric"
    assert modules["deterministic_metrics"]["content"]["photos_current"] >= 1
    assert modules["deterministic_metrics"]["content"]["completed_ai_current"] >= 1
    assert modules["employee_contribution"]["status"] == "ready"
    assert modules["employee_contribution"]["source"] == "promoted_artifact"
    assert modules["employee_contribution"]["content"]["summary_line"].startswith("最近记录窗口内共有 4 张")
    assert modules["employee_contribution"]["provenance"]["artifact_id"] == "artifact-mobile-contribution-001"
    assert modules["project_management_status"]["status"] == "partial"
    assert modules["project_management_status"]["content"]["reports"]["completed_with_content"] >= 1
    assert set(dashboard_payload["ai_slots"].keys()) == {"2", "3", "4", "5", "6"}
    assert dashboard_payload["ai_slots"]["3"]["artifact_type"] == "generated_report_markdown"
    assert dashboard_payload["ai_slots"]["3"]["content"] is None
    assert dashboard_payload["ai_slots"]["3"]["shadow_summary"]["validation_status_counts"]["shadow_valid"] >= 1
    assert dashboard_payload["ai_slots"]["4"]["artifact_type"] == "project_manager_decision_brief"
    assert dashboard_payload["ai_slots"]["4"]["content"] is None
    assert dashboard_payload["ai_slots"]["4"]["shadow_summary"]["validation_status_counts"]["shadow_fallback_valid"] >= 1
    assert dashboard_payload["ai_slots"]["5"]["artifact_type"] == "client_progress_summary"
    assert dashboard_payload["ai_slots"]["5"]["content"] is None
    assert dashboard_payload["ai_slots"]["5"]["shadow_summary"]["validation_status_counts"]["shadow_fallback_valid"] >= 1
    serialized_dashboard = json.dumps(dashboard_payload, ensure_ascii=False)
    assert "shadow markdown body must not leak" not in serialized_dashboard
    assert "decision brief body must not leak" not in serialized_dashboard
    assert "client progress body must not leak" not in serialized_dashboard
    assert "raw_model_output" not in serialized_dashboard

    manager_card = client.get(
        "/api/mobile/projects/P100/project-manager-status-card",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert manager_card.status_code == 200
    manager_payload = manager_card.json()
    assert manager_payload["contract_version"] == "project_manager_status_card:v1"
    assert manager_payload["status"] == "available"
    assert manager_payload["guardrails"]["uses_promoted_artifacts_only"] is True
    assert manager_payload["guardrails"]["exposes_shadow_content"] is False
    assert manager_payload["guardrails"]["client_should_not_compute_metrics"] is True
    assert manager_payload["artifact"]["artifact_id"] == "artifact-dashboard-manager-promoted"
    assert manager_payload["artifact"]["validation_status"] == "promoted_valid"
    assert manager_payload["artifact"]["promoted"] is True
    assert manager_payload["status_card"]["decision_surface"]["items"][0]["fact_refs"][0]["observed_value"] == 4
    assert manager_payload["status_card"]["body"]["paragraphs"] == [
        "The current project window has 4 database photos."
    ]
    serialized_manager = json.dumps(manager_payload, ensure_ascii=False)
    assert "decision brief body must not leak" not in serialized_manager
    assert "manager raw" not in serialized_manager
    assert "raw_model_output" not in serialized_manager

    manager_not_ready = client.get(
        "/api/mobile/projects/P100/project-manager-status-card?window_days=60",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert manager_not_ready.status_code == 200
    assert manager_not_ready.json()["status"] == "not_ready"

    not_ready = client.get("/api/mobile/projects/P100/contribution?window_days=60", headers={"X-API-Key": "api-key-e100"})
    assert not_ready.status_code == 200
    assert not_ready.json()["status"] == "not_ready"

    cross_company = client.get("/api/mobile/projects/P200/contribution", headers={"X-API-Key": "api-key-e100"})
    assert cross_company.status_code == 404
    dashboard_cross_company = client.get(
        "/api/mobile/projects/P200/contribution-dashboard",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert dashboard_cross_company.status_code == 404
    manager_cross_company = client.get(
        "/api/mobile/projects/P200/project-manager-status-card",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert manager_cross_company.status_code == 404


def test_mobile_video_listing_returns_media_fields_and_image_thumbnail(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    upload_response = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "media_kind": "video",
            "timestamp": "2026-03-18T12:10:00Z",
        },
        files={"photo": ("field-walkthrough.mp4", b"fake-video-bytes", "video/mp4")},
    )
    assert upload_response.status_code == 200
    uploaded_photo = upload_response.json()["photo"]
    assert uploaded_photo["media_kind"] == "video"
    assert urlparse(uploaded_photo["media_url"]).path.endswith(".mp4")
    assert urlparse(uploaded_photo["thumb_url"]).path.endswith(".jpg")

    photos_me = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert photos_me.status_code == 200
    video_item = next(item for item in photos_me.json() if item["id"] == uploaded_photo["id"])
    assert video_item["media_kind"] == "video"
    assert video_item["media_asset_id"]
    assert urlparse(video_item["media_url"]).path.endswith(".mp4")
    assert urlparse(video_item["thumb_url"]).path.endswith(".jpg")
    assert video_item["duration_seconds"] == 0.0
    with session_maker() as db:
        asset = db.get(MediaAsset, video_item["media_asset_id"])
        assert asset is not None
        assert str((asset.metadata_json or {}).get("legacy_photo_id")) == str(uploaded_photo["id"])

    thumbnail_response = client.get(video_item["thumb_url"])
    assert thumbnail_response.status_code == 200
    assert thumbnail_response.headers["content-type"].startswith("image/")


def test_portal_roles_and_client_visibility(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    visible_upload = _upload(client, "api-key-e100", "client-visible.jpg", "E100", "P100", "project")
    hidden_upload = _upload(client, "api-key-e100", "internal-only.jpg", "E100", "P100", "project")
    other_project_upload = _upload(client, "api-key-e200", "other-project.jpg", "E200", "P200", "project")

    assert visible_upload.status_code == 200
    assert hidden_upload.status_code == 200
    assert other_project_upload.status_code == 200

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200
    assert "Dashboard" in manager_login.text

    restricted = client.get("/portal/users")
    assert restricted.status_code == 403

    manager_employees = client.get("/portal/employees")
    assert manager_employees.status_code == 200
    assert "Create Mobile Employee" in manager_employees.text
    assert "Alicia Field" in manager_employees.text
    assert "Jordan Invoice" not in manager_employees.text

    employee_csrf = _extract_csrf(manager_employees.text)
    create_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employee_csrf,
            "employee_id": "E110",
            "name": "New Field Worker",
            "role_name": "worker",
            "project_id": "P100",
        },
        follow_redirects=True,
    )
    assert create_employee.status_code == 200
    assert "New Field Worker" in create_employee.text

    manager_photos = client.get("/portal/photos")
    assert manager_photos.status_code == 200
    assert "client-visible.jpg" in manager_photos.text
    assert "internal-only.jpg" in manager_photos.text
    assert "other-project.jpg" not in manager_photos.text

    csrf_token = _extract_csrf(manager_photos.text)
    visible_id = visible_upload.json()["photo"]["id"]

    approve = client.post(
        f"/portal/photos/{visible_id}/approve",
        data={"csrf_token": csrf_token, "next_url": "/portal/photos"},
        follow_redirects=True,
    )
    assert approve.status_code == 200

    set_visible = client.post(
        f"/portal/photos/{visible_id}/visibility",
        data={
            "csrf_token": csrf_token,
            "next_url": "/portal/photos",
            "visibility": "client_visible",
            "featured": "on",
            "note": "Ready for client review",
        },
        follow_redirects=True,
    )
    assert set_visible.status_code == 200

    client.cookies.clear()
    client_login = _login(client, "client", "ClientPass123!")
    assert client_login.status_code == 200

    gallery = client.get("/portal/gallery")
    assert gallery.status_code == 200
    assert "client-visible.jpg" in gallery.text
    assert "internal-only.jpg" not in gallery.text
    assert "other-project.jpg" not in gallery.text
    assert "invoice-receipt.jpg" not in gallery.text

    client.cookies.clear()
    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    admin_users = client.get("/portal/users")
    assert admin_users.status_code == 200
    assert "Create Web User" in admin_users.text

    with session_maker() as db:
        created_employee = db.scalar(select(Employee).where(Employee.employee_id == "E110"))
        assert created_employee is not None
        assert created_employee.company_id == "default"
        assert created_employee.tenant_id == "default"
        assert created_employee.project_id == "P100"
        visible_record = db.scalar(select(Photo).where(Photo.id == visible_id))
        assert visible_record.approval_status == ApprovalStatus.approved
        assert visible_record.visibility == PhotoVisibility.client_visible


def test_company_admin_user_creation_cannot_escalate_to_owner(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    tenant = _provision_tenant_owner(
        session_maker,
        company_name="User Boundary Builders",
        company_id="user-boundary",
        company_code="10150",
        username="user-boundary-owner",
        email="user-boundary-owner@example.com",
        password="UserBoundary123!",
    )
    with session_maker() as db:
        db.add(
            Project(
                tenant_id=tenant["company_id"],
                company_id=tenant["company_id"],
                project_id="101500001",
                project_name="Boundary Project",
                client_name="Boundary Client",
                location="Austin",
                status=ProjectStatus.active,
            )
        )
        db.commit()

    owner_login = _login(client, tenant["username"], tenant["password"])
    assert owner_login.status_code == 200
    users_page = client.get("/portal/users")
    assert users_page.status_code == 200
    csrf_token = _extract_csrf(users_page.text)

    create_owner = client.post(
        "/portal/users",
        data={
            "csrf_token": csrf_token,
            "username": "tenant-owner-escalation",
            "display_name": "Owner Escalation",
            "email": "tenant-owner-escalation@example.com",
            "role": "owner",
            "password": "OwnerEscalation123!",
        },
        follow_redirects=True,
    )
    assert create_owner.status_code == 200
    assert "tenant-owner-escalation" not in create_owner.text

    create_pm = client.post(
        "/portal/users",
        data={
            "csrf_token": csrf_token,
            "username": "tenant-pm-ok",
            "display_name": "Tenant PM OK",
            "email": "tenant-pm-ok@example.com",
            "role": "project_manager",
            "password": "TenantPmOk123!",
            "project_ids": ["101500001"],
        },
        follow_redirects=True,
    )
    assert create_pm.status_code == 200
    assert "tenant-pm-ok" in create_pm.text

    with session_maker() as db:
        assert db.scalar(select(User).where(User.username == "tenant-owner-escalation")) is None
        project_manager = db.scalar(select(User).where(User.username == "tenant-pm-ok"))
        assert project_manager is not None
        assert project_manager.company_id == tenant["company_id"]
        assert project_manager.role == UserRole.project_manager
        assert db.scalar(
            select(ProjectMember).where(
                ProjectMember.user_id == project_manager.id,
                ProjectMember.project_id == "101500001",
            )
        ) is not None


def test_password_management_map_stats_comments_and_metadata(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload_response = _upload(
        client,
        "api-key-e100",
        "metadata-photo.jpg",
        "E100",
        "P100",
        "project",
        extra_data={
            "device_model": "Pixel 9",
            "os_version": "Android 16",
            "app_version": "2.4.1",
        },
    )
    assert upload_response.status_code == 200
    photo_id = upload_response.json()["photo"]["id"]
    assert upload_response.json()["photo"]["device_model"] == "Pixel 9"
    assert upload_response.json()["photo"]["thumb_url"].startswith("http://testserver/media/")

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    profile_page = client.get("/portal/profile")
    assert profile_page.status_code == 200
    profile_csrf = _extract_csrf(profile_page.text)

    change_password = client.post(
        "/portal/user/change-password",
        data={
            "csrf_token": profile_csrf,
            "current_password": "ManagerPass123!",
            "new_password": "ManagerPass456!",
        },
        follow_redirects=True,
    )
    assert change_password.status_code == 200
    client.cookies.clear()

    manager_relogin = _login(client, "manager", "ManagerPass456!")
    assert manager_relogin.status_code == 200

    photos_page = client.get("/portal/photos")
    photos_csrf = _extract_csrf(photos_page.text)
    comment_response = client.post(
        f"/portal/photos/{photo_id}/comments",
        data={"csrf_token": photos_csrf, "next_url": "/portal/photos", "comment": "Manager review comment"},
        follow_redirects=True,
    )
    assert comment_response.status_code == 200
    assert "Manager review comment" in comment_response.text

    approval_response = client.post(
        f"/portal/photos/{photo_id}/approval",
        data={"csrf_token": photos_csrf, "next_url": "/portal/photos", "approval_status": "approved"},
        follow_redirects=True,
    )
    assert approval_response.status_code == 200

    stats_overview = client.get("/portal/stats/overview")
    assert stats_overview.status_code == 200
    assert stats_overview.json()["total_photos"] >= 1
    assert stats_overview.json()["photos_today"] >= 1

    stats_projects = client.get("/portal/stats/projects")
    assert stats_projects.status_code == 200
    assert any(item["project_id"] == "P100" for item in stats_projects.json())

    map_page = client.get("/portal/map")
    assert map_page.status_code == 200

    map_photos = client.get("/portal/map/photos?project_id=P100")
    assert map_photos.status_code == 200
    map_items = map_photos.json()
    assert any(item["photo_id"] == photo_id for item in map_items)
    matching_item = next(item for item in map_items if item["photo_id"] == photo_id)
    assert matching_item["media_kind"] == "photo"
    assert matching_item["preview_url"]

    client.cookies.clear()
    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    billing_page = client.get("/portal/billing")
    assert billing_page.status_code == 200
    assert "Billing" in billing_page.text
    assert "built-in method copy" not in billing_page.text
    users_page = client.get("/portal/users")
    users_csrf = _extract_csrf(users_page.text)
    reset_password = client.post(
        "/portal/users/3/reset-password",
        data={"csrf_token": users_csrf, "new_password": "ClientReset456!"},
        follow_redirects=True,
    )
    assert reset_password.status_code == 200

    client.cookies.clear()
    client_login = _login(client, "client", "ClientReset456!")
    assert client_login.status_code == 200

    with session_maker() as db:
        record = db.scalar(select(Photo).where(Photo.id == photo_id))
        assert record.device_model == "Pixel 9"
        assert record.os_version == "Android 16"
        assert record.app_version == "2.4.1"
        assert record.approval_status == ApprovalStatus.approved


def test_portal_ai_backends_and_reprocess_actions(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    first_upload = _upload(client, "api-key-e100", "reprocess-a.jpg", "E100", "P100", "project")
    second_upload = _upload(client, "api-key-e100", "reprocess-b.jpg", "E100", "P100", "project")
    assert first_upload.status_code == 200
    assert second_upload.status_code == 200
    first_photo_id = first_upload.json()["photo"]["id"]
    second_photo_id = second_upload.json()["photo"]["id"]

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    ai_backends_page = client.get("/portal/ai-backends")
    assert ai_backends_page.status_code == 200
    assert "AI Max Concurrent Requests" in ai_backends_page.text
    assert "built-in method copy" not in ai_backends_page.text
    ai_backends_csrf = _extract_csrf(ai_backends_page.text)

    update_concurrency = client.post(
        "/portal/ai-backends/settings",
        data={
            "csrf_token": ai_backends_csrf,
            "ai_max_concurrent_requests": "4",
            "ai_enable_dynamic_fallback": "on",
        },
        follow_redirects=True,
    )
    assert update_concurrency.status_code == 200
    assert "AI max concurrent requests saved: 4. Dynamic fallback is enabled." in update_concurrency.text

    create_backend = client.post(
        "/portal/ai-backends",
        data={
            "csrf_token": ai_backends_csrf,
            "backend_id": "tenant-gemini",
            "backend_type": "gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-1.5-flash",
            "api_key": "tenant-key",
            "weight": "2",
            "enabled": "on",
        },
        follow_redirects=True,
    )
    assert create_backend.status_code == 200
    assert "tenant-gemini" in create_backend.text

    test_backend = client.post(
        "/portal/ai-backends/test",
        data={
            "csrf_token": ai_backends_csrf,
            "backend_id": "tenant-gemini",
            "backend_type": "gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-1.5-flash",
            "weight": "2",
            "enabled": "on",
            "existing_backend_id": "tenant-gemini",
            "custom_prompt": "Return a concise JSON test summary.",
        },
        follow_redirects=True,
    )
    assert test_backend.status_code == 200
    assert "responded successfully" in test_backend.text

    with session_maker() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "default"))
        assert tenant is not None
        assert tenant.settings_json is not None
        assert tenant.settings_json["ai_backends"][0]["id"] == "tenant-gemini"
        concurrency_setting = db.get(SystemSetting, "ai_max_concurrent_requests")
        assert concurrency_setting is not None
        assert concurrency_setting.value == "4"
        fallback_setting = db.get(SystemSetting, "ai_enable_dynamic_fallback")
        assert fallback_setting is not None
        assert fallback_setting.value == "true"

    client.cookies.clear()
    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert "Re-run AI Analysis" in photos_page.text
    photos_csrf = _extract_csrf(photos_page.text)

    single_reprocess = client.post(
        f"/portal/photos/{first_photo_id}/reprocess",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "prompt": "Focus on structural defects first.",
        },
        follow_redirects=True,
    )
    assert single_reprocess.status_code == 200
    assert "Queued AI reprocessing" in single_reprocess.text

    bulk_reprocess = client.post(
        "/portal/photos/reprocess",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "prompt": "Highlight any safety concerns.",
            "photo_ids": [str(first_photo_id), str(second_photo_id)],
        },
        follow_redirects=True,
    )
    assert bulk_reprocess.status_code == 200
    assert "Queued AI reprocessing" in bulk_reprocess.text

    semantic_search = client.get("/portal/photos?semantic_query=reprocess-a.jpg")
    assert semantic_search.status_code == 200
    first_index = semantic_search.text.find("reprocess-a.jpg")
    second_index = semantic_search.text.find("reprocess-b.jpg")
    assert first_index != -1
    assert second_index != -1
    assert first_index < second_index

    confidence_filter = client.get("/portal/photos?ai_keyword=Mock+field+photo&confidence=high")
    assert confidence_filter.status_code == 200
    assert "reprocess-a.jpg" in confidence_filter.text
    assert "reprocess-b.jpg" not in confidence_filter.text

    with session_maker() as db:
        refreshed = list(db.scalars(select(Photo).where(Photo.id.in_([first_photo_id, second_photo_id]))))
        assert len(refreshed) == 2
        assert all(photo.labeling_status == "completed" for photo in refreshed)
        bulk_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "bulk_photo_reprocess_requested",
                AuditLog.company_id == "default",
            )
        )
        assert bulk_audit is not None
        assert bulk_audit.detail_json["source"] == "portal_bulk"


def test_portal_photo_bulk_review_annotations_and_recycle_bin(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    first_upload = _upload(client, "api-key-e100", "bulk-tools-a.jpg", "E100", "P100", "project")
    second_upload = _upload(client, "api-key-e100", "bulk-tools-b.jpg", "E100", "P100", "project")
    assert first_upload.status_code == 200
    assert second_upload.status_code == 200

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert "Approval still matters" in photos_page.text
    assert "Approve and Publish to Client" in photos_page.text
    assert "Notes and Comments" in photos_page.text
    assert "AI Summary Timeline" in photos_page.text
    photos_csrf = _extract_csrf(photos_page.text)
    first_photo_id = first_upload.json()["photo"]["id"]
    second_photo_id = second_upload.json()["photo"]["id"]

    review_response = client.post(
        "/portal/photos/bulk/review",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "bulk_visibility": "client_visible",
            "bulk_approval_status": "approved",
            "photo_ids": [str(first_photo_id), str(second_photo_id)],
        },
        follow_redirects=True,
    )
    assert review_response.status_code == 200
    assert "Updated review settings for 2 photos." in review_response.text

    annotate_response = client.post(
        "/portal/photos/bulk/annotate",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "bulk_note": "Batch note for client publishing.",
            "bulk_comment": "Batch manager comment.",
            "photo_ids": [str(first_photo_id), str(second_photo_id)],
        },
        follow_redirects=True,
    )
    assert annotate_response.status_code == 200
    assert "Updated notes/comments for 2 photos." in annotate_response.text

    delete_response = client.post(
        "/portal/photos/bulk/delete",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "photo_ids": [str(first_photo_id), str(second_photo_id)],
        },
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "Moved 2 photos to the recycle bin." in delete_response.text

    with session_maker() as db:
        photos = list(db.scalars(select(Photo).where(Photo.id.in_([first_photo_id, second_photo_id]))))
        assert len(photos) == 2
        assert all(photo.visibility == PhotoVisibility.client_visible for photo in photos)
        assert all(photo.approval_status == ApprovalStatus.approved for photo in photos)
        assert all(photo.note == "Batch note for client publishing." for photo in photos)
        assert all(photo.deleted for photo in photos)
        comments = list(db.scalars(select(PhotoComment).where(PhotoComment.photo_id.in_([first_photo_id, second_photo_id]))))
        assert len(comments) == 2
        assert all(comment.comment == "Batch manager comment." for comment in comments)


def test_portal_report_generation_and_download(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    first_upload = _upload(client, "api-key-e100", "report-a.jpg", "E100", "P100", "project")
    second_upload = _upload(client, "api-key-e100", "report-b.jpg", "E100", "P100", "project")
    assert first_upload.status_code == 200
    assert second_upload.status_code == 200

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    photos_csrf = _extract_csrf(photos_page.text)
    first_photo_id = first_upload.json()["photo"]["id"]
    second_photo_id = second_upload.json()["photo"]["id"]

    report_response = client.post(
        "/portal/reports/generate",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "prompt": "Generate a field inspection summary with key findings and recommended actions.",
            "photo_ids": [str(first_photo_id), str(second_photo_id)],
        },
        follow_redirects=True,
    )
    assert report_response.status_code == 200
    assert "Queued report generation" in report_response.text
    assert "Open AI Processing Center for live progress or Report History for completed downloads." in report_response.text
    assert "Download PDF Report" in report_response.text

    ai_center_page = client.get("/portal/ai-center")
    assert ai_center_page.status_code == 200
    assert "AI Processing Center" in ai_center_page.text
    assert "built-in method copy" not in ai_center_page.text

    ai_center_data = client.get("/portal/ai-center/data")
    assert ai_center_data.status_code == 200
    ai_center_payload = ai_center_data.json()
    assert ai_center_payload["summary"]["total_tasks"] >= 1
    assert any(item["task_type"] == "report_generation" for item in ai_center_payload["items"])

    reports_page = client.get("/portal/reports")
    assert reports_page.status_code == 200
    assert "Report History" in reports_page.text


def test_portal_project_prompts_and_progress_report_views(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    def fake_multi_image_completion(*, backends, prompt, images):
        assert "scaffolding and helmets" in prompt
        assert "standing water and PPE" in prompt
        assert "existing_single_image_ai" in prompt
        assert "paper shredder" in prompt
        assert "黑色矩形设备" in prompt
        assert "preferred_sequence_label" in prompt
        return (
            json.dumps(
                {
                    "executive_summary": "Framing advanced between the compared images, but helmet compliance still needs follow-up.",
                    "overall_progress_percent": 58,
                    "overall_status": "at_risk",
                    "confidence_level": "medium",
                    "manager_brief": "Progress is visible, but PPE compliance still requires correction before the next work cycle.",
                    "key_changes": [
                        "Framing advanced from the earlier photo to the later photo.",
                    ],
                    "work_completed": [
                        "Structural framing moved forward.",
                    ],
                    "work_remaining": [
                        "Close the helmet compliance gap.",
                    ],
                    "safety_risks": [
                        "Visible helmet compliance issue.",
                    ],
                    "quality_risks": [],
                    "recommended_actions": [
                        "Re-brief the crew and verify PPE before the next shift.",
                    ],
                    "timeline_observations": [
                        {
                            "photo_id": 1,
                            "captured_at": "2026-04-04T10:00:00Z",
                            "observation": "Earlier image shows the baseline framing stage.",
                            "progress_signal": "Baseline state captured.",
                            "risk_signal": "PPE gap is visible.",
                        },
                        {
                            "photo_id": 2,
                            "captured_at": "2026-04-04T11:00:00Z",
                            "observation": "Later image shows added framing and continued activity.",
                            "progress_signal": "Work clearly advanced.",
                            "risk_signal": "Helmet compliance still needs follow-up.",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            "ollama:llava:latest",
            [{"backend_id": "ollama-test", "status": "success"}],
        )

    def fake_markdown_completion(*, backends, prompt):
        return (
            json.dumps(
                {
                    "zh": {
                        "executive_summary": "两张照片显示现场有推进，但 PPE 仍需跟进。",
                        "overall_progress_percent": 58,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "现场在推进，但 PPE 仍需立即纠偏。",
                        "key_changes": ["框架施工较早期照片已有推进。"],
                        "work_completed": ["结构框架继续向前推进。"],
                        "work_remaining": ["补齐安全帽执行与现场确认。"],
                        "safety_risks": ["仍可见 PPE 执行缺口。"],
                        "quality_risks": [],
                        "recommended_actions": ["下个班次前完成 PPE 复核。"],
                        "timeline_observations": [
                            {
                                "photo_id": photo_ids[0],
                                "captured_at": "2026-04-04T10:00:00Z",
                                "observation": "较早照片展示了初始框架状态。",
                                "progress_signal": "基线状态已记录。",
                                "risk_signal": "PPE 缺口可见。",
                            },
                            {
                                "photo_id": photo_ids[1],
                                "captured_at": "2026-04-04T11:00:00Z",
                                "observation": "较晚照片显示框架继续推进。",
                                "progress_signal": "工作有明确进展。",
                                "risk_signal": "安全帽执行仍需跟进。",
                            },
                        ],
                    },
                    "en": {
                        "executive_summary": "Framing advanced between the compared images, but helmet compliance still needs follow-up.",
                        "overall_progress_percent": 58,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "Progress is visible, but PPE compliance still requires correction before the next work cycle.",
                        "key_changes": ["Framing advanced from the earlier photo to the later photo."],
                        "work_completed": ["Structural framing moved forward."],
                        "work_remaining": ["Close the helmet compliance gap."],
                        "safety_risks": ["Visible helmet compliance issue."],
                        "quality_risks": [],
                        "recommended_actions": ["Re-brief the crew and verify PPE before the next shift."],
                        "timeline_observations": [
                            {
                                "photo_id": photo_ids[0],
                                "captured_at": "2026-04-04T10:00:00Z",
                                "observation": "Earlier image shows the baseline framing stage.",
                                "progress_signal": "Baseline state captured.",
                                "risk_signal": "PPE gap is visible.",
                            },
                            {
                                "photo_id": photo_ids[1],
                                "captured_at": "2026-04-04T11:00:00Z",
                                "observation": "Later image shows added framing and continued activity.",
                                "progress_signal": "Work clearly advanced.",
                                "risk_signal": "Helmet compliance still needs follow-up.",
                            },
                        ],
                    },
                    "es": {
                        "executive_summary": "Las fotos muestran avance, pero el cumplimiento de EPP aún requiere seguimiento.",
                        "overall_progress_percent": 58,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "Hay progreso visible, pero el EPP necesita corrección inmediata.",
                        "key_changes": ["La estructura avanzó frente a la foto inicial."],
                        "work_completed": ["El trabajo de estructura avanzó."],
                        "work_remaining": ["Cerrar la brecha de cumplimiento de casco."],
                        "safety_risks": ["Sigue visible una brecha de EPP."],
                        "quality_risks": [],
                        "recommended_actions": ["Reforzar la charla de seguridad antes del siguiente turno."],
                        "timeline_observations": [
                            {
                                "photo_id": photo_ids[0],
                                "captured_at": "2026-04-04T10:00:00Z",
                                "observation": "La foto temprana muestra el estado base.",
                                "progress_signal": "Estado base documentado.",
                                "risk_signal": "Se observa una brecha de EPP.",
                            },
                            {
                                "photo_id": photo_ids[1],
                                "captured_at": "2026-04-04T11:00:00Z",
                                "observation": "La foto posterior muestra más avance de estructura.",
                                "progress_signal": "El trabajo avanzó con claridad.",
                                "risk_signal": "El casco todavía requiere seguimiento.",
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            "ollama:qwen2.5:7b-instruct",
            [{"backend_id": "ollama-text", "status": "success"}],
        )

    monkeypatch.setattr("app.services.reports.generate_multi_image_completion", fake_multi_image_completion)
    monkeypatch.setattr("app.services.reports.generate_markdown_completion", fake_markdown_completion)
    monkeypatch.setattr(
        "app.services.reports._existing_progress_ai_contexts",
        lambda db, photos: [
            "[existing_single_image_ai photo_id=1] summary=A black paper shredder is plugged into a wall outlet on the floor. | labels=paper shredder"
        ],
    )
    monkeypatch.setattr(
        "app.services.reports._preferred_sequence_ai_hints",
        lambda db, photos: [
            "[preferred_sequence_label] paper shredder appears consistently across 2 selected photos；可参考中文既有描述：一台黑色的碎纸机插在一个地上的插座上。"
        ],
    )

    first_file = settings.photos_root / "portal-progress-1.jpg"
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_bytes(b"\xff\xd8\xff\xe0portal-progress-1")
    second_file = settings.photos_root / "portal-progress-2.jpg"
    second_file.write_bytes(b"\xff\xd8\xff\xe0portal-progress-2")

    with session_maker() as db:
        first = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(first_file),
            storage_path=str(first_file),
            image_url="/media/default/P100/E100/portal-progress-1.jpg",
            original_file_name="portal-progress-1.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc),
        )
        second = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(second_file),
            storage_path=str(second_file),
            image_url="/media/default/P100/E100/portal-progress-2.jpg",
            original_file_name="portal-progress-2.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 4, 4, 11, 0, tzinfo=timezone.utc),
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        photo_ids = [first.id, second.id]

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    project_page = client.get("/portal/projects/P100")
    assert project_page.status_code == 200
    project_csrf = _extract_csrf(project_page.text)
    assert "Image / Video AI Prompt" in project_page.text

    update_project = client.post(
        "/portal/projects/P100/update",
        data={
            "csrf_token": project_csrf,
            "project_name": "Substation Retrofit",
            "client_name": "Northwind Power",
            "location": "Tulsa, OK",
            "status_value": "active",
            "image_video_ai_prompt": "Focus on scaffolding and helmets.",
            "billing_receipt_ai_prompt": "Extract receipt totals.",
        },
        follow_redirects=True,
    )
    assert update_project.status_code == 200
    assert "Focus on scaffolding and helmets." in update_project.text

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    photos_csrf = _extract_csrf(photos_page.text)
    assert "Deep AI Progress Compare" in photos_page.text

    compare_response = client.post(
        "/portal/reports/compare-progress",
        data={
            "csrf_token": photos_csrf,
            "next_url": "/portal/photos",
            "prompt": "Focus on standing water and PPE.",
            "photo_ids": [str(photo_ids[0]), str(photo_ids[1])],
        },
        follow_redirects=True,
    )
    assert compare_response.status_code == 200
    assert "Progress Evolution Report" in compare_response.text
    assert "Focus on standing water and PPE." in compare_response.text

    with session_maker() as db:
        report = db.scalar(select(ProgressReport).where(ProgressReport.project_id == "P100"))
        assert report is not None
        assert report.status == ProgressReportStatus.completed
        report_id = report.id

    reports_page = client.get("/portal/reports?tab=progress")
    assert reports_page.status_code == 200
    assert "AI Progress Reports" in reports_page.text
    assert report_id in reports_page.text

    detail_page = client.get(f"/portal/reports/progress/{report_id}")
    assert detail_page.status_code == 200
    assert "Progress Evolution Report" in detail_page.text
    assert "Manager Brief" in detail_page.text
    assert "58%" in detail_page.text

    language_switch = client.post(
        "/portal/language",
        data={"language": "zh", "next_url": f"/portal/reports/progress/{report_id}"},
        follow_redirects=True,
    )
    assert language_switch.status_code == 200
    assert "进度演变报告" in language_switch.text
    assert "两张照片显示现场有推进" in language_switch.text

    with session_maker() as db:
        project = db.scalar(select(Project).where(Project.project_id == "P100"))
        assert project is not None
        assert project.image_video_ai_prompt == "Focus on scaffolding and helmets."
        assert project.billing_receipt_ai_prompt == "Extract receipt totals."
    reports_data = client.get("/portal/reports/data")
    assert reports_data.status_code == 200
    progress_items = reports_data.json()["progress_items"]
    assert any(item["report_id"] == report_id for item in progress_items)
    progress_item = next(item for item in progress_items if item["report_id"] == report_id)
    assert progress_item["structured_report"]["overall_progress_percent"] == 58
    assert progress_item["custom_prompt"] == "Focus on standing water and PPE."

def test_portal_ip_camera_crud_actions(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    tenant = _provision_tenant_owner(
        session_maker,
        company_name="Camera CRUD Builders",
        company_id="camera-crud",
        company_code="10110",
        username="camera-crud-owner",
        email="camera-crud-owner@example.com",
        password="CameraCrud123!",
    )

    owner_login = _login(client, tenant["username"], tenant["password"])
    assert owner_login.status_code == 200
    camera_page = client.get("/portal/ip-cameras")
    assert camera_page.status_code == 200
    assert "IP Cameras" in camera_page.text
    csrf_token = _extract_csrf(camera_page.text)

    create_camera = client.post(
        "/portal/ip-cameras",
        data={
            "csrf_token": csrf_token,
            "name": "North Yard Cam",
            "protocol": "rtsp",
            "stream_url": "rtsp://camera.local/live",
            "is_enabled": "on",
        },
        follow_redirects=True,
    )
    assert create_camera.status_code == 200
    assert "North Yard Cam" in create_camera.text

    with session_maker() as db:
        camera = db.scalar(select(IPCamera).where(IPCamera.company_id == tenant["company_id"]))
        assert camera is not None
        assert camera.approval_status == CameraApprovalStatus.pending
        camera_id = camera.id

    monkeypatch.setattr(
        "app.services.camera_scheduler.probe_ip_camera_stream",
        lambda camera, app_settings: {"status": "online", "message": "Camera stream is reachable."},
    )
    test_camera = client.post(
        f"/portal/ip-cameras/{camera_id}/test",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert test_camera.status_code == 200
    assert "ready for platform review" in test_camera.text.lower()

    client.cookies.clear()
    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    assert "North Yard Cam" in platform_page.text
    platform_csrf = _extract_csrf(platform_page.text)
    approve_camera = client.post(
        f"/portal/platform/ip-cameras/{camera_id}/approve",
        data={
            "csrf_token": platform_csrf,
            "review_notes": "Approved for tenant testing.",
            "next_url": "/portal/platform",
        },
        follow_redirects=True,
    )
    assert approve_camera.status_code == 200

    with session_maker() as db:
        camera = db.get(IPCamera, camera_id)
        assert camera is not None
        assert camera.approval_status == CameraApprovalStatus.approved
        assert camera.review_notes == "Approved for tenant testing."

    client.cookies.clear()
    owner_login = _login(client, tenant["username"], tenant["password"])
    assert owner_login.status_code == 200
    refreshed_camera_page = client.get("/portal/ip-cameras")
    assert refreshed_camera_page.status_code == 200
    csrf_token = _extract_csrf(refreshed_camera_page.text)

    update_camera = client.post(
        f"/portal/ip-cameras/{camera_id}/update",
        data={
            "csrf_token": csrf_token,
            "name": "North Yard Cam 2",
            "protocol": "rtmp",
            "stream_url": "rtmp://ingest.local/live/camera-2",
        },
        follow_redirects=True,
    )
    assert update_camera.status_code == 200
    assert "North Yard Cam 2" in update_camera.text

    with session_maker() as db:
        camera = db.get(IPCamera, camera_id)
        assert camera is not None
        assert camera.approval_status == CameraApprovalStatus.pending
        assert camera.review_notes is None

    delete_camera = client.post(
        f"/portal/ip-cameras/{camera_id}/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert delete_camera.status_code == 200
    assert "deleted" in delete_camera.text.lower()

    with session_maker() as db:
        assert db.get(IPCamera, camera_id) is None


def test_portal_ip_camera_bulk_actions_and_health_summary(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    tenant = _provision_tenant_owner(
        session_maker,
        company_name="Camera Bulk Electric",
        company_id="camera-bulk",
        company_code="10111",
        username="camera-bulk-owner",
        email="camera-bulk-owner@example.com",
        password="CameraBulk123!",
    )

    with session_maker() as db:
        first = IPCamera(
            company_id=tenant["company_id"],
            name="Bulk Cam 1",
            stream_url="rtsp://bulk-1",
            protocol="rtsp",
            is_enabled=True,
            status="offline",
            consecutive_failures=3,
            approval_status=CameraApprovalStatus.pending,
        )
        second = IPCamera(
            company_id=tenant["company_id"],
            name="Bulk Cam 2",
            stream_url="rtsp://bulk-2",
            protocol="rtsp",
            is_enabled=True,
            status="error",
            consecutive_failures=4,
            approval_status=CameraApprovalStatus.pending,
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        camera_ids = [first.id, second.id]

    owner_login = _login(client, tenant["username"], tenant["password"])
    assert owner_login.status_code == 200

    camera_page = client.get("/portal/ip-cameras")
    assert camera_page.status_code == 200
    assert "Bulk Camera Actions" in camera_page.text
    csrf_token = _extract_csrf(camera_page.text)

    health_response = client.get("/portal/ip-cameras/health-summary")
    assert health_response.status_code == 200
    health_payload = health_response.json()
    assert health_payload["attention_count"] >= 2
    assert health_payload["pending_review_cameras"] == 2

    disable_response = client.post(
        "/portal/ip-cameras/bulk",
        data={"csrf_token": csrf_token, "bulk_action": "disable", "camera_ids": camera_ids},
        follow_redirects=True,
    )
    assert disable_response.status_code == 200

    with session_maker() as db:
        cameras = list(db.scalars(select(IPCamera).where(IPCamera.id.in_(camera_ids)).order_by(IPCamera.id)))
        assert cameras
        assert all(camera.is_enabled is False for camera in cameras)

    enable_response = client.post(
        "/portal/ip-cameras/bulk",
        data={"csrf_token": csrf_token, "bulk_action": "enable", "camera_ids": camera_ids},
        follow_redirects=True,
    )
    assert enable_response.status_code == 200

    def fake_test(db, camera, settings):
        camera.status = "online"
        camera.error_log = None
        camera.consecutive_failures = 0
        db.add(camera)
        db.flush()
        return {"status": "online", "message": "ok"}

    monkeypatch.setattr("app.api.routes.portal.test_ip_camera_connection", fake_test)

    test_response = client.post(
        "/portal/ip-cameras/bulk",
        data={"csrf_token": csrf_token, "bulk_action": "test", "camera_ids": camera_ids},
        follow_redirects=True,
    )
    assert test_response.status_code == 200
    assert "0 failed" in test_response.text

    with session_maker() as db:
        cameras = list(db.scalars(select(IPCamera).where(IPCamera.id.in_(camera_ids)).order_by(IPCamera.id)))
        assert cameras
        assert all(str(camera.status.value if hasattr(camera.status, "value") else camera.status) == "online" for camera in cameras)
        assert all(camera.consecutive_failures == 0 for camera in cameras)
        assert all(camera.approval_status == CameraApprovalStatus.pending for camera in cameras)

    delete_response = client.post(
        "/portal/ip-cameras/bulk",
        data={"csrf_token": csrf_token, "bulk_action": "delete", "camera_ids": camera_ids},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200

    with session_maker() as db:
        remaining = list(db.scalars(select(IPCamera).where(IPCamera.id.in_(camera_ids))))
        assert remaining == []


def test_portal_photo_ai_history_routes(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload_response = _upload(
        client,
        "api-key-e100",
        "history-portal.jpg",
        "E100",
        "P100",
        "project",
        extra_data={"note": "Portal AI history"},
    )
    assert upload_response.status_code == 200
    photo_id = upload_response.json()["photo"]["id"]

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert "AI Analysis History" in photos_page.text
    csrf_token = _extract_csrf(photos_page.text)

    logs_response = client.get(f"/portal/photos/{photo_id}/ai_logs")
    assert logs_response.status_code == 200
    payload = logs_response.json()
    assert payload["photo_id"] == photo_id
    assert len(payload["logs"]) == 1
    assert payload["primary_log_id"] == payload["logs"][0]["id"]
    assert payload["confidence_label"] == "Low"
    log_id = payload["logs"][0]["id"]

    promote_response = client.post(
        f"/portal/ai_logs/{log_id}/promote",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert promote_response.status_code == 200
    promoted_payload = promote_response.json()
    assert promoted_payload["primary_log_id"] != log_id
    assert promoted_payload["confidence_label"] == "Medium"

    reject_response = client.patch(
        f"/portal/ai_logs/{promoted_payload['primary_log_id']}/status",
        headers={"X-CSRF-Token": csrf_token},
        json={"status": "rejected"},
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["log"]["status"] == "rejected"
    assert reject_response.json()["confidence_label"] == "Low"

    with session_maker() as db:
        analysis_log = db.get(AIAnalysisLog, log_id)
        assert analysis_log is not None
        assert analysis_log.status == AIAnalysisStatus.active
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.tag_json["ai_summary"] is not None


def test_portal_ai_center_tracks_recent_photo_tasks(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload_response = _upload(client, "api-key-e100", "ai-center-photo.jpg", "E100", "P100", "project")
    assert upload_response.status_code == 200
    with session_maker() as db:
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        db.add(
            TaskJob(
                public_id="annotation-ai-center-job",
                tenant_id="default",
                company_id="default",
                created_by_user_id=manager.id,
                task_type="annotation_transcription",
                status=TaskStatus.dead_letter,
                payload_json={"annotation_ids": ["annotation-ai-center"]},
                related_type="media_annotation",
                related_id="annotation-ai-center",
                last_error="Synthetic transcription failure for AI Center coverage.",
            )
        )
        db.commit()

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200
    payload_response = client.get("/portal/ai-center/data")
    assert payload_response.status_code == 200
    payload = payload_response.json()
    assert payload["summary"]["total_tasks"] == len(payload["items"])
    assert payload["summary"]["category_counts"]["annotation"] == 1
    assert payload["summary"]["dead_letter_tasks"] >= 1
    annotation_item = next(item for item in payload["items"] if item["task_id"] == "annotation-ai-center-job")
    assert annotation_item["task_category"] == "annotation"
    assert annotation_item["last_error"] == "Synthetic transcription failure for AI Center coverage."
    assert annotation_item["can_retry"] is True

    ai_center_page = client.get("/portal/ai-center")
    assert ai_center_page.status_code == 200
    assert "Retry Task" in ai_center_page.text
    retry_response = client.post(
        "/portal/ai-center/tasks/annotation-ai-center-job/retry",
        data={"csrf_token": _extract_js_csrf(ai_center_page.text)},
        follow_redirects=True,
    )
    assert retry_response.status_code == 200
    assert "Task has been requeued for another worker attempt." in retry_response.text

    with session_maker() as db:
        retry_job = db.scalar(select(TaskJob).where(TaskJob.public_id == "annotation-ai-center-job"))
        assert retry_job is not None
        assert retry_job.status == TaskStatus.queued
        assert retry_job.attempt_count == 0
        assert retry_job.last_error is None


def test_portal_ai_history_uses_selected_language(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_path = settings.photos_root / "portal-ai-language.jpg"
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    photo_path.write_bytes(b"\xff\xd8\xff\xe0portal-ai-language")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/portal-ai-language.jpg",
            original_file_name="portal-ai-language.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={
                "ai_summary": "English progress summary",
                "ai_summary_translations": {
                    "zh": "\u4e2d\u6587\u8fdb\u5ea6\u6458\u8981",
                    "en": "English progress summary",
                    "es": "Resumen de progreso en espa\u00f1ol",
                },
                "labels": ["progress"],
                "defects": [],
            },
        )
        db.add(photo)
        db.flush()
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        db.add(
            AIAnalysisLog(
                id="portal-ai-history-log",
                photo_id=photo.id,
                analysis_type=AIAnalysisType.fast_screen,
                prompt_used="prompt",
                model_used="ollama:llava:latest",
                result_data={
                    "ai_summary": "English progress summary",
                    "ai_summary_translations": {
                        "zh": "\u4e2d\u6587\u8fdb\u5ea6\u6458\u8981",
                        "en": "English progress summary",
                        "es": "Resumen de progreso en espa\u00f1ol",
                    },
                    "labels": ["progress"],
                    "defects": [],
                },
                status=AIAnalysisStatus.active,
                created_by=str(manager.id),
            )
        )
        db.commit()
        photo_id = photo.id

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    switch_language = client.post(
        "/portal/language",
        data={"language": "zh", "next_url": "/portal/photos"},
        follow_redirects=True,
    )
    assert switch_language.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert "\u4e2d\u6587\u8fdb\u5ea6\u6458\u8981" in photos_page.text

    history_response = client.get(f"/portal/photos/{photo_id}/ai_logs")
    assert history_response.status_code == 200
    payload = history_response.json()
    assert payload["snapshot"]["ai_summary"] == "\u4e2d\u6587\u8fdb\u5ea6\u6458\u8981"
    assert payload["logs"][0]["result_data"]["ai_summary"] == "\u4e2d\u6587\u8fdb\u5ea6\u6458\u8981"


def test_portal_recycle_bin_permanent_delete_removes_files_and_ai_data(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_path = settings.photos_root / "default" / "P100" / "E100" / "portal-delete-me.jpg"
    thumb_path = settings.photos_root / "default" / "P100" / "E100" / "thumb_portal-delete-me.jpg"
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    photo_path.write_bytes(b"\xff\xd8\xff\xe0portal-delete-me")
    thumb_path.write_bytes(b"\xff\xd8\xff\xe0portal-delete-thumb")

    with session_maker() as db:
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="/media/default/P100/E100/portal-delete-me.jpg",
            thumb_url="/media/default/P100/E100/thumb_portal-delete-me.jpg",
            original_file_name="portal-delete-me.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
        )
        db.add(photo)
        db.flush()
        db.add(
            AIAnalysisLog(
                id="portal-delete-log",
                photo_id=photo.id,
                analysis_type=AIAnalysisType.fast_screen,
                prompt_used="prompt",
                model_used="ollama:llava:latest",
                result_data={"ai_summary": "Mock field photo", "labels": ["project"], "defects": []},
                status=AIAnalysisStatus.active,
                created_by=str(manager.id),
            )
        )
        db.add(PhotoComment(photo_id=photo.id, user_id=manager.id, comment="Delete this test photo"))
        db.add(
            MediaAnnotation(
                id="portal-delete-annotation",
                photo_id=photo.id,
                user_id=manager.id,
                role_at_time=AnnotationRole.manager,
                annotation_type=AnnotationType.text,
                content_text="Delete note",
                visibility=AnnotationVisibility.public,
                status=AnnotationStatus.completed,
            )
        )
        db.add(
            TaskJob(
                public_id="portal-delete-task",
                tenant_id="default",
                company_id="default",
                created_by_user_id=manager.id,
                task_type="photo_ai_pipeline",
                status=TaskStatus.queued,
                priority=100,
                attempt_count=0,
                max_attempts=3,
                available_at=datetime.now(timezone.utc),
                payload_json={"photo_id": photo.id},
                related_type="photo",
                related_id=str(photo.id),
            )
        )
        db.add(
            EvidenceObservation(
                id="portal-delete-observation",
                tenant_id="default",
                company_id="default",
                project_id="P100",
                photo_id=photo.id,
                source_kind="photo",
                observation_type="safety_note",
                normalized_key="safety_note",
                title="Delete observation",
                content_text="This structured observation should be deleted with the photo.",
                severity="medium",
                confidence=0.87,
                observed_at=datetime.now(timezone.utc),
                metadata_json={"source": "integration-test"},
            )
        )
        db.add(
            ReceiptFact(
                id="portal-delete-receipt",
                tenant_id="default",
                company_id="default",
                project_id="P100",
                photo_id=photo.id,
                vendor_name="Delete Fuel Stop",
                total_amount=57.12,
                gallons=12.5,
                unit_price=4.57,
                fuel_type="diesel",
                employee_id="E100",
                summary_text="Temporary receipt fact used for recycle-bin coverage.",
                facts_json={"source": "integration-test"},
            )
        )
        db.commit()
        photo_id = photo.id

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200
    photos_page = client.get("/portal/photos")
    csrf_token = _extract_csrf(photos_page.text)

    delete_response = client.post(
        f"/portal/photos/{photo_id}/delete",
        data={"csrf_token": csrf_token, "next_url": "/portal/photos"},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200

    recycle_page = client.get("/portal/photos?recycle_bin=true")
    assert recycle_page.status_code == 200
    assert "portal-delete-me.jpg" in recycle_page.text

    recycle_csrf = _extract_csrf(recycle_page.text)
    destroy_response = client.post(
        f"/portal/photos/{photo_id}/destroy",
        data={"csrf_token": recycle_csrf, "next_url": "/portal/photos?recycle_bin=true"},
        follow_redirects=True,
    )
    assert destroy_response.status_code == 200

    with session_maker() as db:
        assert db.get(Photo, photo_id) is None
        assert db.get(AIAnalysisLog, "portal-delete-log") is None
        assert db.get(MediaAnnotation, "portal-delete-annotation") is None
        assert db.get(EvidenceObservation, "portal-delete-observation") is None
        assert db.get(ReceiptFact, "portal-delete-receipt") is None
        assert db.scalar(select(PhotoComment).where(PhotoComment.photo_id == photo_id)) is None
        assert db.scalar(select(TaskJob).where(TaskJob.related_type == "photo", TaskJob.related_id == str(photo_id))) is None

    assert not photo_path.exists()
    assert not thumb_path.exists()

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    center_page = client.get("/portal/ai-center")
    assert center_page.status_code == 200
    assert "AI Processing Center" in center_page.text
    assert "Report Tasks" in center_page.text
    assert "Photo Tasks" in center_page.text

    center_payload = client.get("/portal/ai-center/data")
    assert center_payload.status_code == 200
    payload = center_payload.json()
    assert payload["summary"]["total_tasks"] == 0
    assert payload["items"] == []
    assert "system_health" in payload
    assert "cpu_percent" in payload["system_health"]


def test_portal_ai_center_includes_camera_tasks_and_health_summary(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        camera = IPCamera(
            company_id="default",
            name="Attention Camera",
            stream_url="rtsp://attention.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="error",
            error_log="camera offline",
            consecutive_failures=5,
            approval_status=CameraApprovalStatus.approved,
        )
        db.add(camera)
        db.flush()
        asset = MediaAsset(
            asset_id="camera-asset-1",
            tenant_id="default",
            company_id="default",
            media_type=MediaType.video,
            source="ip_camera",
            file_path="C:/tmp/camera-asset-1.mp4",
            original_file_name="camera-asset-1.mp4",
            mime_type="video/mp4",
            file_size=128,
            checksum="camera-asset-1",
            duration_seconds=15.0,
            status=MediaAssetStatus.processing,
            metadata_json={"camera_id": camera.id, "camera_name": camera.name},
        )
        job = TaskJob(
            public_id="camera-task-1",
            tenant_id="default",
            company_id="default",
            task_type="video_media_pipeline",
            status=TaskStatus.running,
            priority=120,
            attempt_count=1,
            max_attempts=3,
            payload_json={"asset_id": asset.asset_id, "source": "ip_camera"},
            related_type="media_asset",
            related_id=asset.asset_id,
        )
        db.add_all([asset, job])
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    center_page = client.get("/portal/ai-center")
    assert center_page.status_code == 200
    assert "Camera Tasks" in center_page.text
    assert "Camera Health Summary" in center_page.text

    center_payload = client.get("/portal/ai-center/data")
    assert center_payload.status_code == 200
    payload = center_payload.json()
    assert payload["camera_health"]["attention_count"] >= 1
    assert any(item["task_category"] == "camera" for item in payload["items"])
    assert any(item.get("source_label") == "IP Camera" for item in payload["items"])
    assert any(item.get("camera_attention") is True for item in payload["items"])
    assert payload["system_health"]["global_queue_active_jobs"] >= 1


def test_portal_ai_center_categorizes_progress_reports_copilot_and_dead_letter(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        progress_report = ProgressReport(
            id=str(uuid4()),
            tenant_id="default",
            company_id="default",
            created_by_user_id=manager.id,
            project_id="P100",
            status=ProgressReportStatus.completed,
            source_photo_ids=[],
            report_content="completed report",
            completed_at=datetime.now(timezone.utc),
        )
        db.add(progress_report)
        db.flush()
        db.add_all(
            [
                TaskJob(
                    public_id=str(uuid4()),
                    tenant_id="default",
                    company_id="default",
                    created_by_user_id=manager.id,
                    task_type="progress_compare_generation",
                    status=TaskStatus.completed,
                    priority=120,
                    attempt_count=1,
                    max_attempts=3,
                    payload_json={"report_id": progress_report.id},
                    related_type="progress_report",
                    related_id=progress_report.id,
                ),
                TaskJob(
                    public_id=str(uuid4()),
                    tenant_id="default",
                    company_id="default",
                    created_by_user_id=manager.id,
                    task_type="copilot_chat_generation",
                    status=TaskStatus.completed,
                    priority=110,
                    attempt_count=1,
                    max_attempts=3,
                    payload_json={"question": "recent risks"},
                    related_type="copilot_message",
                    related_id="copilot-message-1",
                ),
                TaskJob(
                    public_id=str(uuid4()),
                    tenant_id="default",
                    company_id="default",
                    created_by_user_id=manager.id,
                    task_type="photo_ai_pipeline",
                    status=TaskStatus.dead_letter,
                    priority=100,
                    attempt_count=3,
                    max_attempts=3,
                    payload_json={"photo_id": 999999},
                    related_type="photo",
                    related_id="999999",
                    last_error="permanent failure",
                ),
            ]
        )
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path="/tmp/annotation-source.jpg",
            storage_path="/tmp/annotation-source.jpg",
            image_url="/media/default/P100/E100/annotation-source.jpg",
            thumb_url="/media/default/P100/E100/thumb_annotation-source.jpg",
            original_file_name="annotation-source.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
        )
        db.add(photo)
        db.flush()
        annotation = MediaAnnotation(
            id=str(uuid4()),
            photo_id=photo.id,
            media_asset_id=None,
            user_id=manager.id,
            role_at_time=AnnotationRole.manager,
            annotation_type=AnnotationType.voice,
            content_text=None,
            audio_file_path="/tmp/voice.wav",
            visibility=AnnotationVisibility.public,
            status=AnnotationStatus.processing,
        )
        db.add(annotation)
        db.add(
            TaskJob(
                public_id=str(uuid4()),
                tenant_id="default",
                company_id="default",
                created_by_user_id=manager.id,
                task_type="annotation_transcription",
                status=TaskStatus.running,
                priority=110,
                attempt_count=1,
                max_attempts=3,
                payload_json={"annotation_ids": [annotation.id]},
                related_type="media_annotation",
                related_id=annotation.id,
            )
        )
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    center_page = client.get("/portal/ai-center")
    assert center_page.status_code == 200
    assert "Copilot Tasks" in center_page.text

    payload = client.get("/portal/ai-center/data").json()
    assert payload["summary"]["total_tasks"] >= 3
    assert payload["summary"]["failed_tasks"] >= 1
    assert any(item["task_type"] == "progress_compare_generation" and item["task_category"] == "report" for item in payload["items"])
    assert any(item["task_type"] == "copilot_chat_generation" and item["task_category"] == "copilot" for item in payload["items"])
    progress_item = next(item for item in payload["items"] if item["task_type"] == "progress_compare_generation")
    assert progress_item["related_url"].startswith("/portal/reports/progress/")
    annotation_item = next(item for item in payload["items"] if item["task_type"] == "annotation_transcription")
    assert annotation_item["task_category"] == "annotation"
    assert annotation_item["related_title"] == "annotation-source.jpg"
    assert annotation_item["related_url"].startswith("/portal/photos?focus_photo_id=")


def test_portal_media_preview_and_storage_monitor(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    video_path = settings.media_assets_root / "video" / "default" / "asset-preview-1" / "preview-video.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-video-bytes")
    preview_root = settings.media_frames_root / "asset-preview-1" / "preview"
    preview_root.mkdir(parents=True, exist_ok=True)
    preview_frame = preview_root / "preview_01.jpg"
    preview_frame.write_bytes(b"fake-jpeg")

    with session_maker() as db:
        subscription = db.scalar(select(Subscription).where(Subscription.company_id == "default"))
        assert subscription is not None
        subscription.plan_id = PlanCode.free
        threshold_setting = db.get(SystemSetting, "storage_warning_threshold_percent")
        assert threshold_setting is not None
        threshold_setting.value = "80"
        db.add_all([subscription, threshold_setting])
        db.add(
            MediaAsset(
                asset_id="asset-preview-1",
                tenant_id="default",
                company_id="default",
                media_type=MediaType.video,
                source="manual_upload",
                file_path=str(video_path),
                original_file_name="preview-video.mp4",
                mime_type="video/mp4",
                file_size=450 * 1024 * 1024,
                checksum="asset-preview-1",
                duration_seconds=12.0,
                status=MediaAssetStatus.completed,
                metadata_json={
                    "preview_frames": ["preview_01.jpg"],
                    "thumbnail_frame": "preview_01.jpg",
                    "timeline_markers": [
                        {
                            "time_seconds": 3.0,
                            "time_label": "00:03",
                            "label": "Crack near roof seam",
                            "summary": "Frame shows a crack near the roof seam.",
                            "defects": ["Crack"],
                            "has_defect": True,
                        }
                    ],
                    "ai_snapshot": {
                        "ai_summary": "Roof seam crack visible in the short clip.",
                        "labels": ["camera", "roof"],
                        "defects": ["Crack"],
                    },
                },
            )
        )
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    media_page = client.get("/portal/media/asset-preview-1")
    assert media_page.status_code == 200
    assert "Video Preview" in media_page.text
    assert "Crack near roof seam" in media_page.text
    assert "Media Annotations" in media_page.text
    assert "Add Secondary Description" in media_page.text

    media_stream = client.get("/portal/media/asset-preview-1/stream")
    assert media_stream.status_code == 200
    assert media_stream.headers["content-type"].startswith("video/")

    preview_response = client.get("/portal/media/asset-preview-1/preview/preview_01.jpg")
    assert preview_response.status_code == 200

    ai_backends_page = client.get("/portal/ai-backends")
    assert ai_backends_page.status_code == 200
    assert "AI Max Concurrent Requests" in ai_backends_page.text

    ai_center_payload = client.get("/portal/ai-center/data")
    assert ai_center_payload.status_code == 200
    assert ai_center_payload.json()["storage_monitor"]["warning"] is True
    assert "system_health" in ai_center_payload.json()


def test_portal_media_stream_rate_limit(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    settings.portal_media_rate_limit_per_minute = 1

    video_path = settings.media_assets_root / "video" / "default" / "asset-rate-limit-1" / "rate-limit.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-video-bytes")

    with session_maker() as db:
        db.add(
            MediaAsset(
                asset_id="asset-rate-limit-1",
                tenant_id="default",
                company_id="default",
                media_type=MediaType.video,
                source="manual_upload",
                file_path=str(video_path),
                original_file_name="rate-limit.mp4",
                mime_type="video/mp4",
                file_size=video_path.stat().st_size,
                checksum="rate-limit-checksum",
                duration_seconds=12.0,
                status=MediaAssetStatus.completed,
            )
        )
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    first_response = client.get("/portal/media/asset-rate-limit-1/stream")
    assert first_response.status_code == 200
    second_response = client.get("/portal/media/asset-rate-limit-1/stream")
    assert second_response.status_code == 429


def test_portal_camera_health_dashboard_page(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    tenant = _provision_tenant_owner(
        session_maker,
        company_name="Camera Health Services",
        company_id="camera-health",
        company_code="10112",
        username="camera-health-owner",
        email="camera-health-owner@example.com",
        password="CameraHealth123!",
    )

    with session_maker() as db:
        camera = IPCamera(
            company_id=tenant["company_id"],
            name="Health Dashboard Camera",
            stream_url="rtsp://health.local/live",
            protocol="rtsp",
            is_enabled=True,
            status="error",
            consecutive_failures=5,
            error_log="stream timeout",
            approval_status=CameraApprovalStatus.approved,
        )
        db.add(camera)
        db.flush()
        db.add(
            AuditLog(
                action="ip_camera_rtsp_capture_failed",
                target_type="ip_camera",
                target_id=str(camera.id),
                company_id=tenant["company_id"],
                detail_json={"error": "stream timeout"},
            )
        )
        db.commit()

    owner_login = _login(client, tenant["username"], tenant["password"])
    assert owner_login.status_code == 200

    health_page = client.get("/portal/ip-cameras/health")
    assert health_page.status_code == 200
    assert "Camera Health Dashboard" in health_page.text
    assert "Health Dashboard Camera" in health_page.text
    assert "Recent Camera Failures" in health_page.text

    health_summary = client.get("/portal/ip-cameras/health-summary")
    assert health_summary.status_code == 200
    payload = health_summary.json()
    assert payload["attention_count"] >= 1
    assert payload["recent_failures"]
    assert payload["recent_failures"][0]["camera_name"] == "Health Dashboard Camera"

    client.cookies.clear()
    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    assert client.get("/portal/ip-cameras").status_code == 403
    assert client.get("/portal/ip-cameras/health").status_code == 403


def test_portal_language_switching(app_context):
    client = app_context["client"]

    login_page = client.get("/portal/login")
    assert login_page.status_code == 200
    assert "Management Access" in login_page.text

    switch_zh = client.post(
        "/portal/language",
        data={"language": "zh", "next_url": "/portal/login"},
        follow_redirects=True,
    )
    assert switch_zh.status_code == 200
    assert "管理访问" in switch_zh.text

    _login(client, "admin", "AdminPass123!")
    dashboard_zh = client.get("/portal")
    assert "仪表板" in dashboard_zh.text
    assert "查看企业申请、租户状态和失败任务。" in dashboard_zh.text

    switch_es = client.post(
        "/portal/language",
        data={"language": "es", "next_url": "/portal"},
        follow_redirects=True,
    )
    assert switch_es.status_code == 200
    assert "Panel" in switch_es.text


def test_saas_registration_auth_and_password_reset(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    assert "K&K Data Service Inc." in pricing.text
    terms = client.get("/terms")
    assert terms.status_code == 200
    public_home = client.get("/", headers={"accept": "text/html"})
    assert public_home.status_code == 200
    assert "KK Field Logger" in public_home.text

    register = client.post(
        "/auth/register-company",
        data={
            "company_name": "South Ridge Engineering",
            "display_name": "Tenant Admin",
            "email": "tenant@example.com",
            "password": "TenantPass123!",
            "contact_phone": "+1 713-555-2222",
            "company_intro": "We want a reviewed pilot before workspace activation.",
        },
        follow_redirects=False,
    )
    assert register.status_code == 303
    assert register.headers["location"] == "/register?submitted=application_received"

    with session_maker() as db:
        application = db.scalar(select(CompanyApplication).where(CompanyApplication.contact_email == "tenant@example.com"))
        assert application is not None
        assert application.status == CompanyApplicationStatus.pending
        assert db.scalar(select(Company).where(Company.company_name == "South Ridge Engineering")) is None

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    platform_csrf = _extract_csrf(platform_page.text)

    approve = client.post(
        f"/portal/platform/applications/{application.id}/approve",
        data={"csrf_token": platform_csrf, "plan_id": "free"},
        follow_redirects=True,
    )
    assert approve.status_code == 200
    assert "South Ridge Engineering" in approve.text

    client.cookies.clear()
    auth_login = client.post(
        "/auth/login",
        data={"identifier": "tenant@example.com", "password": "TenantPass123!"},
        follow_redirects=True,
    )
    assert auth_login.status_code == 200
    assert "Dashboard" in auth_login.text

    forgot = client.post(
        "/auth/forgot-password",
        data={"email": "tenant@example.com"},
        follow_redirects=False,
    )
    assert forgot.status_code == 303

    with session_maker() as db:
        company = db.scalar(select(Company).where(Company.company_name == "South Ridge Engineering"))
        assert company is not None
        subscription = db.scalar(select(Subscription).where(Subscription.company_id == company.company_id))
        assert subscription is not None
        assert subscription.plan_id.value == "free"
        tenant_user = db.scalar(select(User).where(User.email == "tenant@example.com"))
        assert tenant_user is not None
        assert tenant_user.company_id == company.company_id
        token_entry = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == tenant_user.id))
        assert token_entry is not None

    with session_maker() as db:
        tenant_user = db.scalar(select(User).where(User.email == "tenant@example.com"))
        assert tenant_user is not None
        raw_token = create_password_reset_token(db, user=tenant_user, settings=settings)
        db.commit()

    reset_page = client.get(f"/reset-password?token={raw_token}")
    assert reset_page.status_code == 200

    reset = client.post(
        "/auth/reset-password",
        data={"token": raw_token, "new_password": "TenantPass456!"},
        follow_redirects=False,
    )
    assert reset.status_code == 303

    client.cookies.clear()
    relogin = client.post(
        "/auth/login",
        data={"identifier": "tenant@example.com", "password": "TenantPass456!"},
        follow_redirects=True,
    )
    assert relogin.status_code == 200
    assert "Dashboard" in relogin.text


def test_portal_copilot_page_is_available_for_manager(app_context):
    client = app_context["client"]

    manager_login = _login(client, "manager", "ManagerPass123!")
    assert manager_login.status_code == 200

    response = client.get("/portal/copilot")
    assert response.status_code == 200
    assert "Evidence Copilot" in response.text
    assert "Ask Copilot" in response.text
    assert "built-in method copy" not in response.text


def test_default_company_code_prefixes_new_numeric_ids_and_keeps_legacy_employee_ids(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        default_company = db.scalar(select(Company).where(Company.company_id == "default"))
        assert default_company is not None
        assert default_company.company_code == "10000"

    login_response = _login(client, "admin", "AdminPass123!")
    assert login_response.status_code == 200

    projects_page = client.get("/portal/projects")
    assert projects_page.status_code == 200
    assert 'action="/portal/projects"' in projects_page.text
    projects_csrf = _extract_csrf(projects_page.text)
    create_project = client.post(
        "/portal/projects",
        data={
            "csrf_token": projects_csrf,
            "project_id": "12",
            "project_name": "Prefixed Numeric Project",
            "client_name": "Testing Client",
            "location": "Austin, TX",
            "status_value": "active",
        },
        follow_redirects=True,
    )
    assert create_project.status_code == 200
    assert "100000012" in create_project.text
    assert "company code 10000" in projects_page.text

    employees_page = client.get("/portal/employees")
    assert employees_page.status_code == 200
    employees_csrf = _extract_csrf(employees_page.text)
    create_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employees_csrf,
            "employee_id": "34",
            "name": "Prefixed Numeric Worker",
            "role_name": "worker",
            "project_id": "12",
        },
        follow_redirects=True,
    )
    assert create_employee.status_code == 200
    assert "100000034" in create_employee.text
    assert "company code 10000" in employees_page.text

    with session_maker() as db:
        project = db.scalar(select(Project).where(Project.project_id == "100000012"))
        employee = db.scalar(select(Employee).where(Employee.employee_id == "100000034"))
        assert project is not None
        assert project.company_id == "default"
        assert employee is not None
        assert employee.company_id == "default"
        assert employee.project_id == "100000012"

        legacy_employee = db.scalar(select(Employee).where(Employee.employee_id == "888"))
        if legacy_employee is None:
            db.add(
                Employee(
                    company_id="default",
                    tenant_id="default",
                    employee_id="888",
                    name="Legacy Test Employee 888",
                    api_key="api-key-888",
                    role="worker",
                    active=True,
                    project_id="100000012",
                )
            )
            db.commit()

    legacy_upload = _upload(client, "api-key-888", "legacy-888.jpg", "888", "100000012", "project")
    assert legacy_upload.status_code == 200
    assert legacy_upload.json()["photo"]["employee_id"] == "888"
    assert legacy_upload.json()["photo"]["project_id"] == "100000012"


def test_multitenant_enterprise_setup_and_data_isolation_end_to_end(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    platform_csrf = _extract_csrf(platform_page.text)

    create_company = client.post(
        "/portal/platform/companies",
        data={
            "csrf_token": platform_csrf,
            "company_name": "Granite Peak Builders",
            "display_name": "Granite Owner",
            "email": "granite-owner@example.com",
            "password": "GranitePass123!",
            "company_code": "10003",
            "username": "granite-owner",
        },
        follow_redirects=True,
    )
    assert create_company.status_code == 200
    assert "Granite Peak Builders" in create_company.text

    with session_maker() as db:
        company = db.scalar(select(Company).where(Company.company_name == "Granite Peak Builders"))
        assert company is not None
        assert company.company_code == "10003"
        owner = db.scalar(select(User).where(User.email == "granite-owner@example.com"))
        assert owner is not None
        assert owner.company_id == company.company_id

    client.cookies.clear()
    owner_login = _login(client, "granite-owner", "GranitePass123!")
    assert owner_login.status_code == 200

    projects_page = client.get("/portal/projects")
    assert projects_page.status_code == 200
    projects_csrf = _extract_csrf(projects_page.text)

    create_primary_project = client.post(
        "/portal/projects",
        data={
            "csrf_token": projects_csrf,
            "project_id": "GP100",
            "project_name": "Granite Tower Core",
            "client_name": "Granite Client",
            "location": "Houston, TX",
            "image_video_ai_prompt": "Focus on scaffolding, hard hats, and fall protection.",
            "billing_receipt_ai_prompt": "Extract vendor, gallons, total spend, and pump evidence.",
            "status_value": "active",
        },
        follow_redirects=True,
    )
    assert create_primary_project.status_code == 200
    assert "Granite Tower Core" in create_primary_project.text

    create_secondary_project = client.post(
        "/portal/projects",
        data={
            "csrf_token": projects_csrf,
            "project_id": "GP200",
            "project_name": "Granite Yard Expansion",
            "client_name": "Granite Client",
            "location": "Dallas, TX",
            "image_video_ai_prompt": "Track material staging and standing water risk.",
            "billing_receipt_ai_prompt": "Flag unusual fuel volume or missing pump imagery.",
            "status_value": "active",
        },
        follow_redirects=True,
    )
    assert create_secondary_project.status_code == 200
    assert "Granite Yard Expansion" in create_secondary_project.text

    employees_page = client.get("/portal/employees")
    assert employees_page.status_code == 200
    employees_csrf = _extract_csrf(employees_page.text)

    create_primary_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employees_csrf,
            "employee_id": "G888",
            "name": "Granite Worker A",
            "role_name": "worker",
            "project_id": "GP100",
        },
        follow_redirects=True,
    )
    assert create_primary_employee.status_code == 200
    assert "Granite Worker A" in create_primary_employee.text

    create_secondary_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employees_csrf,
            "employee_id": "G889",
            "name": "Granite Worker B",
            "role_name": "worker",
            "project_id": "GP200",
        },
        follow_redirects=True,
    )
    assert create_secondary_employee.status_code == 200
    assert "Granite Worker B" in create_secondary_employee.text

    users_page = client.get("/portal/users")
    assert users_page.status_code == 200
    users_csrf = _extract_csrf(users_page.text)

    create_manager = client.post(
        "/portal/users",
        data={
            "csrf_token": users_csrf,
            "username": "granite-pm",
            "display_name": "Granite PM",
            "role": "project_manager",
            "password": "GranitePm123!",
            "email": "granite-pm@example.com",
            "project_ids": "GP100",
        },
        follow_redirects=True,
    )
    assert create_manager.status_code == 200
    assert "Granite PM" in create_manager.text

    with session_maker() as db:
        primary_project = db.scalar(select(Project).where(Project.project_id == "GP100"))
        secondary_project = db.scalar(select(Project).where(Project.project_id == "GP200"))
        primary_employee = db.scalar(select(Employee).where(Employee.employee_id == "G888"))
        secondary_employee = db.scalar(select(Employee).where(Employee.employee_id == "G889"))
        manager = db.scalar(select(User).where(User.username == "granite-pm"))
        assert primary_project is not None
        assert secondary_project is not None
        assert primary_project.company_id == company.company_id
        assert primary_project.image_video_ai_prompt == "Focus on scaffolding, hard hats, and fall protection."
        assert secondary_project.billing_receipt_ai_prompt == "Flag unusual fuel volume or missing pump imagery."
        assert primary_employee is not None
        assert secondary_employee is not None
        assert primary_employee.company_id == company.company_id
        assert primary_employee.project_id == "GP100"
        assert secondary_employee.project_id == "GP200"
        assert manager is not None
        primary_employee_api_key = primary_employee.api_key
        secondary_employee_api_key = secondary_employee.api_key
        membership = db.scalar(select(ProjectMember).where(ProjectMember.user_id == manager.id, ProjectMember.project_id == "GP100"))
        assert membership is not None
        assert db.scalar(select(ProjectMember).where(ProjectMember.user_id == manager.id, ProjectMember.project_id == "GP200")) is None

    primary_upload = _upload(client, primary_employee_api_key, "granite-core-a.jpg", "G888", "GP100", "project")
    secondary_upload = _upload(client, secondary_employee_api_key, "granite-yard-b.jpg", "G889", "GP200", "project")
    assert primary_upload.status_code == 200
    assert secondary_upload.status_code == 200
    primary_photo_id = primary_upload.json()["photo"]["id"]
    secondary_photo_id = secondary_upload.json()["photo"]["id"]

    primary_listing = client.get("/photos/me", headers={"X-API-Key": primary_employee_api_key})
    assert primary_listing.status_code == 200
    primary_items = primary_listing.json()
    assert {item["id"] for item in primary_items} == {primary_photo_id}

    secondary_listing = client.get("/photos/me", headers={"X-API-Key": secondary_employee_api_key})
    assert secondary_listing.status_code == 200
    secondary_items = secondary_listing.json()
    assert {item["id"] for item in secondary_items} == {secondary_photo_id}

    client.cookies.clear()
    manager_login = _login(client, "granite-pm", "GranitePm123!")
    assert manager_login.status_code == 200

    manager_projects = client.get("/portal/projects")
    assert manager_projects.status_code == 200
    assert "Granite Tower Core" in manager_projects.text
    assert "Granite Yard Expansion" not in manager_projects.text

    manager_employees = client.get("/portal/employees")
    assert manager_employees.status_code == 200
    assert "Granite Worker A" in manager_employees.text
    assert "Granite Worker B" not in manager_employees.text

    manager_photos = client.get("/portal/photos")
    assert manager_photos.status_code == 200
    assert "granite-core-a.jpg" in manager_photos.text
    assert "granite-yard-b.jpg" not in manager_photos.text

    manager_api_login = client.post(
        "/api/v2/auth/login",
        json={"email": "granite-pm@example.com", "password": "GranitePm123!"},
    )
    assert manager_api_login.status_code == 200

    manager_annotation = client.post(
        "/api/v2/annotations/text",
        json={
            "target_ids": [{"photo_id": primary_photo_id}],
            "content_text": "Please confirm the scaffold bracing before client walkthrough.",
            "visibility": "public",
        },
    )
    assert manager_annotation.status_code == 201
    parent_annotation_id = manager_annotation.json()["annotation_ids"][0]

    employee_reply = client.post(
        "/api/v2/annotations/text",
        headers={"X-API-Key": primary_employee_api_key},
        json={
            "target_id": primary_photo_id,
            "body_text": "Crew confirmed and tightened the brace this afternoon.",
            "parent_id": parent_annotation_id,
            "created_at": "2026-04-05T18:30:00Z",
        },
    )
    assert employee_reply.status_code == 201

    employee_detail = client.get(
        f"/api/v2/media/{primary_photo_id}/detail",
        headers={"X-API-Key": primary_employee_api_key},
    )
    assert employee_detail.status_code == 200
    detail_payload = employee_detail.json()
    assert detail_payload["id"] == primary_photo_id
    assert detail_payload["authorized_employee_ids"] == ["G888"]
    assert detail_payload["annotations"][0]["body_text"] == "Please confirm the scaffold bracing before client walkthrough."
    assert any(
        child["body_text"] == "Crew confirmed and tightened the brace this afternoon."
        for child in detail_payload["annotations"][0]["children"]
    )

    blocked_same_company_other_employee = client.get(
        f"/api/v2/media/{primary_photo_id}/detail",
        headers={"X-API-Key": secondary_employee_api_key},
    )
    assert blocked_same_company_other_employee.status_code == 404

    blocked_same_company_annotation = client.post(
        "/api/v2/annotations/text",
        headers={"X-API-Key": secondary_employee_api_key},
        json={"target_id": primary_photo_id, "body_text": "Should not be allowed"},
    )
    assert blocked_same_company_annotation.status_code == 404

    client.cookies.clear()
    default_manager_login = _login(client, "manager", "ManagerPass123!")
    assert default_manager_login.status_code == 200

    default_projects = client.get("/portal/projects")
    assert default_projects.status_code == 200
    assert "Granite Tower Core" not in default_projects.text
    assert "Granite Yard Expansion" not in default_projects.text

    default_employees = client.get("/portal/employees")
    assert default_employees.status_code == 200
    assert "Granite Worker A" not in default_employees.text
    assert "Granite Worker B" not in default_employees.text

    default_photos = client.get("/portal/photos")
    assert default_photos.status_code == 200
    assert "granite-core-a.jpg" not in default_photos.text
    assert "granite-yard-b.jpg" not in default_photos.text

    default_manager_detail = client.get(f"/api/v2/media/{primary_photo_id}/detail")
    assert default_manager_detail.status_code == 404

    default_employee_detail = client.get(
        f"/api/v2/media/{primary_photo_id}/detail",
        headers={"X-API-Key": "api-key-e100"},
    )
    assert default_employee_detail.status_code == 404

    default_employee_listing = client.get("/photos/me", headers={"X-API-Key": "api-key-e100"})
    assert default_employee_listing.status_code == 200
    assert primary_photo_id not in {item["id"] for item in default_employee_listing.json()}

    with session_maker() as db:
        assert db.scalar(select(MediaAnnotation).where(MediaAnnotation.id == parent_annotation_id)) is not None
        granite_photo = db.scalar(select(Photo).where(Photo.id == primary_photo_id))
        assert granite_photo is not None
        assert granite_photo.company_id == company.company_id
        assert granite_photo.project_id == "GP100"


def test_public_private_deployment_page_and_platform_company_provisioning_flow(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    private_page = client.get("/private-deployment")
    assert private_page.status_code == 200
    assert "K&K Data Service Inc." in private_page.text

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    dashboard_page = client.get("/portal")
    assert dashboard_page.status_code == 200
    assert '/static/css/admin-console.css?v=20260808-console-reset-v3' in dashboard_page.text
    assert 'class="page-stack dashboard-shell ops-page dashboard-console-page"' in dashboard_page.text
    assert 'class="metric-grid dashboard-metric-grid console-stat-strip"' in dashboard_page.text
    assert 'class="panel hero-panel"' not in dashboard_page.text
    assert "Platform Operations" in dashboard_page.text
    assert "Tenant Inspection" in dashboard_page.text
    assert "Review applications, tenant status, and failed jobs." in dashboard_page.text

    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    assert "Open Company Workspace" in platform_page.text
    platform_csrf = _extract_csrf(platform_page.text)

    create_company = client.post(
        "/portal/platform/companies",
        data={
            "csrf_token": platform_csrf,
            "company_name": "Harbor Grid Services",
            "company_id": "harbor-grid",
            "company_code": "10002",
            "plan_id": "free",
            "display_name": "Harbor Owner",
            "email": "harbor-owner@example.com",
            "username": "harbor-owner",
            "password": "HarborOwner123!",
        },
        follow_redirects=True,
    )
    assert create_company.status_code == 200
    assert "Harbor Grid Services" in create_company.text
    assert "10002" in create_company.text
    assert "harbor-owner" in create_company.text

    with session_maker() as db:
        company = db.scalar(select(Company).where(Company.company_id == "harbor-grid"))
        owner = db.scalar(select(User).where(User.username == "harbor-owner"))
        assert company is not None
        assert company.company_code == "10002"
        assert owner is not None
        assert owner.company_id == "harbor-grid"
        assert owner.role == UserRole.owner

    client.cookies.clear()
    owner_login = _login(client, "harbor-owner", "HarborOwner123!")
    assert owner_login.status_code == 200

    projects_page = client.get("/portal/projects")
    assert projects_page.status_code == 200
    projects_csrf = _extract_csrf(projects_page.text)

    create_project = client.post(
        "/portal/projects",
        data={
            "csrf_token": projects_csrf,
            "project_id": "1",
            "project_name": "Harbor Fuel Yard",
            "client_name": "Harbor Client",
            "location": "Houston, TX",
            "image_video_ai_prompt": "Check scaffold and yard safety.",
            "billing_receipt_ai_prompt": "Extract gallons, totals, and pump evidence.",
            "status_value": "active",
        },
        follow_redirects=True,
    )
    assert create_project.status_code == 200
    assert "100020001" in create_project.text

    employees_page = client.get("/portal/employees")
    assert employees_page.status_code == 200
    employees_csrf = _extract_csrf(employees_page.text)

    create_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employees_csrf,
            "employee_id": "7",
            "name": "Harbor Field Tech",
            "role_name": "worker",
            "project_id": "100020001",
        },
        follow_redirects=True,
    )
    assert create_employee.status_code == 200
    assert "100020007" in create_employee.text

    update_key = client.post(
        "/portal/employees/100020007/update-key",
        data={
            "csrf_token": employees_csrf,
            "api_key": "harbor-key-07",
        },
        follow_redirects=True,
    )
    assert update_key.status_code == 200
    assert "Employee API key updated." in update_key.text

    users_page = client.get("/portal/users")
    assert users_page.status_code == 200
    assert 'value="super_admin"' not in users_page.text
    assert 'value="owner"' not in users_page.text
    assert 'value="manager"' not in users_page.text
    users_csrf = _extract_csrf(users_page.text)

    create_pm_without_project = client.post(
        "/portal/users",
        data={
            "csrf_token": users_csrf,
            "username": "harbor-pm-empty",
            "display_name": "Harbor PM Empty",
            "role": "project_manager",
            "password": "HarborPmEmpty123!",
            "email": "harbor-pm-empty@example.com",
        },
        follow_redirects=True,
    )
    assert create_pm_without_project.status_code == 200
    assert "Select at least one project for that role before creating or saving the user." in create_pm_without_project.text

    create_owner_attempt = client.post(
        "/portal/users",
        data={
            "csrf_token": users_csrf,
            "username": "harbor-owner-2",
            "display_name": "Harbor Owner Two",
            "role": "owner",
            "password": "HarborOwnerTwo123!",
            "email": "harbor-owner-2@example.com",
        },
        follow_redirects=True,
    )
    assert create_owner_attempt.status_code == 200
    assert "You do not have permission to create that role from this workspace." in create_owner_attempt.text

    billing_page = client.get("/portal/billing")
    assert billing_page.status_code == 200
    assert "/portal/billing/subscription" not in billing_page.text
    assert "Read-only Subscription View" in billing_page.text
    assert "/portal/ai-backends" not in billing_page.text

    ai_backends_page = client.get("/portal/ai-backends")
    assert ai_backends_page.status_code == 403

    create_pm = client.post(
        "/portal/users",
        data={
            "csrf_token": users_csrf,
            "username": "harbor-pm",
            "display_name": "Harbor PM",
            "role": "project_manager",
            "password": "HarborPm123!",
            "email": "harbor-pm@example.com",
            "project_ids": "100020001",
        },
        follow_redirects=True,
    )
    assert create_pm.status_code == 200
    assert "Harbor PM" in create_pm.text

    with session_maker() as db:
        project = db.scalar(select(Project).where(Project.project_id == "100020001"))
        employee = db.scalar(select(Employee).where(Employee.employee_id == "100020007"))
        pm_user = db.scalar(select(User).where(User.username == "harbor-pm"))
        assert project is not None
        assert project.company_id == "harbor-grid"
        assert employee is not None
        assert employee.company_id == "harbor-grid"
        assert employee.api_key == "harbor-key-07"
        assert pm_user is not None
        assert pm_user.company_id == "harbor-grid"
        membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == "100020001",
                ProjectMember.user_id == pm_user.id,
            )
        )
        assert membership is not None

    upload = _upload(
        client,
        "harbor-key-07",
        "harbor-yard-photo.jpg",
        "100020007",
        "100020001",
        "project",
    )
    assert upload.status_code == 200

    client.cookies.clear()
    pm_login = _login(client, "harbor-pm", "HarborPm123!")
    assert pm_login.status_code == 200
    pm_projects = client.get("/portal/projects")
    assert pm_projects.status_code == 200
    assert "Harbor Fuel Yard" in pm_projects.text
    pm_photos = client.get("/portal/photos")
    assert pm_photos.status_code == 200
    assert "harbor-yard-photo.jpg" in pm_photos.text


def test_company_application_review_and_tenant_pause_flow(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    register_page = client.get("/register")
    assert register_page.status_code == 200
    assert "review" in register_page.text.lower()

    submit_application = client.post(
        "/auth/register-company",
        data={
            "company_name": "Pending Review Electric",
            "display_name": "Pending Admin",
            "email": "pending-admin@example.com",
            "password": "PendingAdmin123!",
            "company_id": "pending-electric",
            "company_code": "10004",
            "username": "pending-admin",
            "contact_phone": "+1 713-555-0001",
            "contact_title": "Operations Lead",
            "website_url": "https://pending.example.com",
            "company_intro": "We need a controlled pilot for site evidence, billing review, and manager oversight.",
        },
        follow_redirects=False,
    )
    assert submit_application.status_code == 303
    assert submit_application.headers["location"] == "/register?submitted=application_received"

    with session_maker() as db:
        application = db.scalar(select(CompanyApplication).where(CompanyApplication.contact_email == "pending-admin@example.com"))
        assert application is not None
        assert application.status == CompanyApplicationStatus.pending
        assert db.scalar(select(Company).where(Company.company_id == "pending-electric")) is None
        assert db.scalar(select(User).where(User.username == "pending-admin")) is None

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    assert "Pending Review Electric" in platform_page.text
    platform_csrf = _extract_csrf(platform_page.text)

    approve_application = client.post(
        f"/portal/platform/applications/{application.id}/approve",
        data={
            "csrf_token": platform_csrf,
            "plan_id": "free",
            "review_notes": "Approved for a supervised tenant pilot.",
        },
        follow_redirects=True,
    )
    assert approve_application.status_code == 200
    assert "Pending Review Electric" in approve_application.text
    assert "10004" in approve_application.text
    assert f"/portal/platform/applications/{application.id}/approve" not in approve_application.text
    assert f"/portal/platform/applications/{application.id}/reject" not in approve_application.text

    with session_maker() as db:
        approved_application = db.get(CompanyApplication, application.id)
        company = db.scalar(select(Company).where(Company.company_id == "pending-electric"))
        owner = db.scalar(select(User).where(User.username == "pending-admin"))
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "pending-electric"))
        assert approved_application is not None
        assert approved_application.status == CompanyApplicationStatus.approved
        assert approved_application.review_notes == "Approved for a supervised tenant pilot."
        assert company is not None
        assert company.company_code == "10004"
        assert owner is not None
        assert owner.company_id == "pending-electric"
        assert tenant is not None
        assert tenant.status == TenantStatus.active

    client.cookies.clear()
    owner_login = _login(client, "pending-admin", "PendingAdmin123!")
    assert owner_login.status_code == 200
    assert "Dashboard" in owner_login.text

    projects_page = client.get("/portal/projects")
    assert projects_page.status_code == 200
    projects_csrf = _extract_csrf(projects_page.text)
    create_project = client.post(
        "/portal/projects",
        data={
            "csrf_token": projects_csrf,
            "project_id": "15",
            "project_name": "Pending Test Yard",
            "client_name": "Pending Client",
            "location": "Austin, TX",
            "image_video_ai_prompt": "Check PPE and perimeter safety.",
            "billing_receipt_ai_prompt": "Extract gallons and total amount.",
            "status_value": "active",
        },
        follow_redirects=True,
    )
    assert create_project.status_code == 200
    assert "100040015" in create_project.text

    employees_page = client.get("/portal/employees")
    employees_csrf = _extract_csrf(employees_page.text)
    create_employee = client.post(
        "/portal/employees",
        data={
            "csrf_token": employees_csrf,
            "employee_id": "16",
            "name": "Pending Field Worker",
            "role_name": "worker",
            "project_id": "100040015",
        },
        follow_redirects=True,
    )
    assert create_employee.status_code == 200
    assert "100040016" in create_employee.text

    with session_maker() as db:
        employee = db.scalar(select(Employee).where(Employee.employee_id == "100040016"))
        assert employee is not None
        employee_key = employee.api_key

    upload = _upload(client, employee_key, "pending-yard.jpg", "100040016", "100040015", "project")
    assert upload.status_code == 200

    client.cookies.clear()
    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    assert "pending-admin@example.com" in platform_page.text
    assert "Pending Review Electric" in platform_page.text
    assert "10004" in platform_page.text
    platform_csrf = _extract_csrf(platform_page.text)

    suspend_tenant = client.post(
        "/portal/platform/tenants/pending-electric/status",
        data={"csrf_token": platform_csrf, "tenant_status": "suspended"},
        follow_redirects=True,
    )
    assert suspend_tenant.status_code == 200

    with session_maker() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == "pending-electric"))
        company = db.scalar(select(Company).where(Company.company_id == "pending-electric"))
        assert tenant is not None
        assert tenant.status == TenantStatus.suspended
        assert company is not None
        assert company.active is False

    client.cookies.clear()
    suspended_owner_login = _login(client, "pending-admin", "PendingAdmin123!")
    assert suspended_owner_login.status_code == 200
    assert str(suspended_owner_login.request.url).endswith("/portal/login")
    assert "Sign in" in suspended_owner_login.text
    assert "Dashboard" not in suspended_owner_login.text

    blocked_mobile = client.get("/photos/me", headers={"X-API-Key": employee_key})
    assert blocked_mobile.status_code == 401


def test_platform_tenant_search_and_pagination(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        for index in range(105):
            provision_company_workspace(
                db,
                company_name=f"Tenant Batch {index:03d}",
                display_name=f"Batch Owner {index:03d}",
                email=f"tenant-batch-{index:03d}@example.com",
                password="BatchOwner123!",
                company_id=f"tenant-batch-{index:03d}",
                company_code=f"{10100 + index:05d}",
                username=f"tenant-batch-{index:03d}",
            )
        total_tenants = db.scalar(select(func.count()).select_from(Tenant)) or 0
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    first_page = client.get("/portal/platform")
    assert first_page.status_code == 200
    assert f"Showing 1-100 of {total_tenants} tenants." in first_page.text
    assert f"Page 1 of {((total_tenants - 1) // 100) + 1}" in first_page.text

    second_page = client.get("/portal/platform?page=2")
    assert second_page.status_code == 200
    assert f"Showing 101-{total_tenants} of {total_tenants} tenants." in second_page.text
    assert "tenant-batch-000" in second_page.text

    search_page = client.get("/portal/platform?q=tenant-batch-104")
    assert search_page.status_code == 200
    assert "Showing 1-1 of 1 tenants." in search_page.text
    assert "tenant-batch-104" in search_page.text
    assert "tenant-batch-050" not in search_page.text


def test_platform_page_renders_activity_copy_and_anchor_targets(app_context):
    client = app_context["client"]

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    platform_page = client.get("/portal/platform")
    assert platform_page.status_code == 200
    assert 'class="page-stack ops-page platform-console-page"' in platform_page.text
    assert 'class="console-stat-strip platform-stat-strip"' in platform_page.text
    assert '<section class="panel" id="tenant-applications">' in platform_page.text
    assert '<section class="panel table-panel" id="tenant-list">' in platform_page.text
    assert "<built-in method copy" not in platform_page.text


def test_portal_legacy_deep_links_redirect_to_live_pages(app_context):
    client = app_context["client"]

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200

    expected_locations = {
        "/portal/dashboard": "/portal",
        "/portal/system-health": "/portal/ai-center#system-health",
        "/portal/tenant-applications": "/portal/platform#tenant-applications",
        "/portal/companies": "/portal/platform#tenant-list",
    }
    for path, expected_location in expected_locations.items():
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == expected_location


def test_photos_bulk_actions_expose_dangerous_action_confirmations(app_context):
    client = app_context["client"]
    upload_response = _upload(client, "api-key-e100", "bulk-confirm.jpg", "E100", "P100", "project")
    assert upload_response.status_code == 200

    login_response = _login(client, "manager", "ManagerPass123!")
    assert login_response.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert 'id="bulk-photo-tools-form"' in photos_page.text
    assert "photos-bulk-panel" in photos_page.text
    assert "data-confirm-publish" in photos_page.text
    assert "Client users may see them immediately" in photos_page.text
    assert "This may consume queue capacity" in photos_page.text
    assert '/static/css/app.css?v=20260808-interface-refresh' in photos_page.text
    assert '/static/js/portal.js?v=20260808-interface-refresh' in photos_page.text
    assert '/static/css/photos-overlays.css?v=20260808-style-extraction' in photos_page.text
    assert '/static/css/photos-workbench.css?v=20260808-workbench-responsive' in photos_page.text
    assert '/static/js/photos-workbench.js?v=20260808-main-modules' in photos_page.text
    assert '/static/js/photos-reports.js?v=20260808-script-extraction' in photos_page.text
    assert 'id="photos-workbench-config" type="application/json"' in photos_page.text
    assert 'class="photos-workbench-header"' in photos_page.text
    assert 'href="#photos-filters"' in photos_page.text
    assert 'id="photos-results"' in photos_page.text
    assert 'id="image-modal" role="dialog" aria-modal="true"' in photos_page.text
    assert 'href="/portal/photos" class="nav-link active" aria-current="page"' in photos_page.text
    fallback_asset = client.get("/static/img/media-unavailable.svg")
    assert fallback_asset.status_code == 200
    assert "Media unavailable" in fallback_asset.text
    portal_js = client.get("/static/js/portal.js")
    assert portal_js.status_code == 200
    assert "applyImageFallback" in portal_js.text
    assert "initSidebar" in portal_js.text
    assert "initImageFallbacks" in portal_js.text
    assert "initLightbox" in portal_js.text
    assert "body.classList.add(\"modal-open\")" in portal_js.text

    portal_css = client.get("/static/css/app.css")
    assert portal_css.status_code == 200
    assert "--layer-lightbox: 1400" in portal_css.text
    assert "--layer-drawer: 1500" in portal_css.text
    assert "z-index: var(--layer-lightbox)" in portal_css.text
    assert "z-index: var(--layer-drawer)" in portal_css.text

    workbench_css = client.get("/static/css/photos-workbench.css")
    assert workbench_css.status_code == 200
    assert ".photos-workbench-nav" in workbench_css.text
    assert "@media (max-width: 960px)" in workbench_css.text

    overlays_css = client.get("/static/css/photos-overlays.css")
    assert overlays_css.status_code == 200
    assert ".ai-history-overlay" in overlays_css.text
    assert ".bulk-progress-bar" in overlays_css.text

    reports_js = client.get("/static/js/photos-reports.js")
    assert reports_js.status_code == 200
    assert "report_status_poll_failed" in reports_js.text
    assert "progress_report_status_poll_failed" in reports_js.text

    workbench_js = client.get("/static/js/photos-workbench.js")
    assert workbench_js.status_code == 200
    assert "photos-workbench-config" in workbench_js.text
    assert "{{" not in workbench_js.text


def test_project_manager_without_project_assignment_sees_clear_photo_empty_state(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        pm_user = User(
            company_id="default",
            username="unassigned-pm",
            password_hash=hash_password("UnassignedPm123!"),
            role=UserRole.project_manager,
            display_name="Unassigned PM",
            email="unassigned-pm@example.com",
            active=True,
            is_verified=True,
        )
        db.add(pm_user)
        db.commit()

    login_response = _login(client, "unassigned-pm", "UnassignedPm123!")
    assert login_response.status_code == 200

    photos_page = client.get("/portal/photos")
    assert photos_page.status_code == 200
    assert "No projects are assigned to this manager yet." in photos_page.text
    assert "Ask your company administrator to assign at least one project" in photos_page.text


def test_platform_can_delete_suspended_tenant_without_touching_other_tenants(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]
    keep_frames_dir = None

    with session_maker() as db:
        _, _, target_owner, _ = provision_company_workspace(
            db,
            company_name="Delete Me Field Services",
            display_name="Delete Owner",
            email="delete-owner@example.com",
            password="DeleteOwner123!",
            company_id="delete-me",
            company_code="10006",
            username="delete-owner",
        )
        _, _, keep_owner, _ = provision_company_workspace(
            db,
            company_name="Keep Me Electric",
            display_name="Keep Owner",
            email="keep-owner@example.com",
            password="KeepOwner123!",
            company_id="keep-me",
            company_code="10007",
            username="keep-owner",
        )
        target_project = Project(
            company_id="delete-me",
            tenant_id="delete-me",
            project_id="100060001",
            project_name="Delete Yard",
            client_name="Delete Client",
            location="Houston, TX",
            status=ProjectStatus.active,
            created_by=target_owner.id,
        )
        keep_project = Project(
            company_id="keep-me",
            tenant_id="keep-me",
            project_id="100070001",
            project_name="Keep Yard",
            client_name="Keep Client",
            location="Dallas, TX",
            status=ProjectStatus.active,
            created_by=keep_owner.id,
        )
        db.add_all([target_project, keep_project])
        db.flush()

        target_employee = Employee(
            company_id="delete-me",
            tenant_id="delete-me",
            employee_id="100060011",
            name="Delete Worker",
            api_key="delete-key-11",
            role="worker",
            active=True,
            project_id=target_project.project_id,
        )
        keep_employee = Employee(
            company_id="keep-me",
            tenant_id="keep-me",
            employee_id="100070011",
            name="Keep Worker",
            api_key="keep-key-11",
            role="worker",
            active=True,
            project_id=keep_project.project_id,
        )
        target_pm = User(
            company_id="delete-me",
            username="delete-pm",
            password_hash=hash_password("DeletePm123!"),
            role=UserRole.project_manager,
            display_name="Delete PM",
            email="delete-pm@example.com",
            active=True,
        )
        db.add_all([target_employee, keep_employee, target_pm])
        db.flush()
        db.add(
            ProjectMember(
                project_id=target_project.project_id,
                user_id=target_pm.id,
                role_in_project="project_manager",
            )
        )
        db.commit()

    target_photo = _upload(client, "delete-key-11", "delete-photo.jpg", "100060011", "100060001", "project")
    target_video = client.post(
        "/upload",
        headers={"X-API-Key": "delete-key-11"},
        data={
            "employee_id": "100060011",
            "project_id": "100060001",
            "photo_type": "project",
            "media_kind": "video",
            "timestamp": "2026-03-18T12:10:00Z",
        },
        files={"photo": ("delete-video.mp4", b"fake-video-bytes", "video/mp4")},
    )
    keep_photo = _upload(client, "keep-key-11", "keep-photo.jpg", "100070011", "100070001", "project")
    keep_video = client.post(
        "/upload",
        headers={"X-API-Key": "keep-key-11"},
        data={
            "employee_id": "100070011",
            "project_id": "100070001",
            "photo_type": "project",
            "media_kind": "video",
            "timestamp": "2026-03-18T12:11:00Z",
        },
        files={"photo": ("keep-video.mp4", b"fake-video-bytes", "video/mp4")},
    )
    assert target_photo.status_code == 200
    assert target_video.status_code == 200
    assert keep_photo.status_code == 200
    assert keep_video.status_code == 200

    with session_maker() as db:
        target_media_assets = list(db.scalars(select(MediaAsset).where(MediaAsset.company_id == "delete-me")))
        keep_media_assets = list(db.scalars(select(MediaAsset).where(MediaAsset.company_id == "keep-me")))
        assert target_media_assets
        assert keep_media_assets

        target_frames_dir = settings.media_frames_root / target_media_assets[0].asset_id
        target_frames_dir.mkdir(parents=True, exist_ok=True)
        (target_frames_dir / "preview").mkdir(parents=True, exist_ok=True)
        (target_frames_dir / "preview" / "frame_00001.jpg").write_bytes(b"target-frame")

        keep_frames_dir = settings.media_frames_root / keep_media_assets[0].asset_id
        keep_frames_dir.mkdir(parents=True, exist_ok=True)
        (keep_frames_dir / "preview").mkdir(parents=True, exist_ok=True)
        (keep_frames_dir / "preview" / "frame_00001.jpg").write_bytes(b"keep-frame")

        target_report_path = settings.reports_root / "delete-me" / "delete-report.pdf"
        target_report_path.parent.mkdir(parents=True, exist_ok=True)
        target_report_path.write_bytes(b"delete-report")
        keep_report_path = settings.reports_root / "keep-me" / "keep-report.pdf"
        keep_report_path.parent.mkdir(parents=True, exist_ok=True)
        keep_report_path.write_bytes(b"keep-report")

        db.add(
            GeneratedReport(
                public_id=str(uuid4()),
                tenant_id="delete-me",
                company_id="delete-me",
                created_by_user_id=target_owner.id,
                status=ReportStatus.completed,
                title="Delete report",
                prompt="Delete report prompt",
                source_photo_ids=[target_photo.json()["photo"]["id"]],
                markdown_content="Delete report body",
                file_path=str(target_report_path),
                mime_type="application/pdf",
            )
        )
        db.add(
            GeneratedReport(
                public_id=str(uuid4()),
                tenant_id="keep-me",
                company_id="keep-me",
                created_by_user_id=keep_owner.id,
                status=ReportStatus.completed,
                title="Keep report",
                prompt="Keep report prompt",
                source_photo_ids=[keep_photo.json()["photo"]["id"]],
                markdown_content="Keep report body",
                file_path=str(keep_report_path),
                mime_type="application/pdf",
            )
        )
        db.add(
            TaskJob(
                public_id=str(uuid4()),
                tenant_id="delete-me",
                company_id="delete-me",
                created_by_user_id=target_owner.id,
                task_type="tenant_cleanup_probe",
                status=TaskStatus.queued,
                payload_json={"tenant": "delete-me"},
                related_type="tenant",
                related_id="delete-me",
            )
        )
        db.add(
            TaskJob(
                public_id=str(uuid4()),
                tenant_id="keep-me",
                company_id="keep-me",
                created_by_user_id=keep_owner.id,
                task_type="tenant_cleanup_probe",
                status=TaskStatus.queued,
                payload_json={"tenant": "keep-me"},
                related_type="tenant",
                related_id="keep-me",
            )
        )
        db.commit()

    admin_login = _login(client, "admin", "AdminPass123!")
    assert admin_login.status_code == 200
    platform_page = client.get("/portal/platform?q=delete-me")
    assert platform_page.status_code == 200
    platform_csrf = _extract_csrf(platform_page.text)

    suspend_target = client.post(
        "/portal/platform/tenants/delete-me/status",
        data={
            "csrf_token": platform_csrf,
            "next_url": "/portal/platform?q=delete-me",
            "tenant_status": "suspended",
        },
        follow_redirects=True,
    )
    assert suspend_target.status_code == 200

    delete_tenant = client.post(
        "/portal/platform/tenants/delete-me/delete",
        data={
            "csrf_token": platform_csrf,
            "next_url": "/portal/platform?q=delete-me",
            "confirm_slug": "delete-me",
        },
        follow_redirects=True,
    )
    assert delete_tenant.status_code == 200
    assert "/portal/platform/tenants/delete-me/delete" not in delete_tenant.text
    assert "Showing 0-0 of 0 tenants." in delete_tenant.text
    assert "No tenants match the current filter." in delete_tenant.text

    with session_maker() as db:
        assert db.scalar(select(Company).where(Company.company_id == "delete-me")) is None
        assert db.scalar(select(Tenant).where(Tenant.slug == "delete-me")) is None
        assert db.scalar(select(User).where(User.company_id == "delete-me")) is None
        assert db.scalar(select(Employee).where(Employee.company_id == "delete-me")) is None
        assert db.scalar(select(Project).where(Project.company_id == "delete-me")) is None
        assert db.scalar(select(Photo).where(Photo.company_id == "delete-me")) is None
        assert db.scalar(select(MediaAsset).where(MediaAsset.company_id == "delete-me")) is None
        assert db.scalar(select(GeneratedReport).where(GeneratedReport.company_id == "delete-me")) is None
        assert db.scalar(select(TaskJob).where(TaskJob.company_id == "delete-me")) is None
        assert db.scalar(select(ProjectMember).join(User, User.id == ProjectMember.user_id).where(User.company_id == "delete-me")) is None
        assert db.scalar(select(Membership).where(Membership.tenant_id == "delete-me")) is None

        assert db.scalar(select(Company).where(Company.company_id == "keep-me")) is not None
        assert db.scalar(select(Tenant).where(Tenant.slug == "keep-me")) is not None
        assert db.scalar(select(Employee).where(Employee.company_id == "keep-me")) is not None
        assert db.scalar(select(Project).where(Project.company_id == "keep-me")) is not None
        assert db.scalar(select(Photo).where(Photo.company_id == "keep-me")) is not None
        assert db.scalar(select(MediaAsset).where(MediaAsset.company_id == "keep-me")) is not None
        assert db.scalar(select(GeneratedReport).where(GeneratedReport.company_id == "keep-me")) is not None
        assert db.scalar(select(TaskJob).where(TaskJob.company_id == "keep-me")) is not None

    assert not (settings.photos_root / "delete-me").exists()
    assert not (settings.reports_root / "delete-me").exists()
    assert not (settings.media_assets_root / "video" / "delete-me").exists()
    assert (settings.photos_root / "keep-me").exists()
    assert (settings.reports_root / "keep-me").exists()
    assert keep_frames_dir is not None
    assert keep_frames_dir.exists()
