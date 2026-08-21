from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlalchemy import func, select

from app.models import AIAnalysisLog, AIAnalysisStatus, AIAnalysisType, AnnotationRole, AnnotationStatus, AnnotationType, AnnotationVisibility, AuditLog, CopilotConversation, CopilotMessage, CopilotMessageStatus, Employee, EvidenceObservation, GeneratedReport, IPCamera, MediaAnnotation, MediaAsset, MediaAssetStatus, MediaType, Membership, MembershipStatus, Photo, PhotoComment, PhotoType, ProgressReport, ProgressReportStatus, Project, ReceiptFact, ReportStatus, ReviewSession, ReviewTask, ReviewTaskEvent, User, UserRole
from app.services.auth_tokens import create_password_reset_token
from app.services.job_queue import process_due_jobs


def _jpeg_bytes(tag: str) -> bytes:
    import io as _io

    from PIL import Image as _Image

    buffer = _io.BytesIO()
    _Image.new("RGB", (4, 4), "gray").save(buffer, "JPEG")
    # Trailing bytes after the JPEG EOI marker are ignored by decoders but keep
    # each upload's checksum unique so the duplicate-upload guard does not
    # collapse distinct test uploads.
    return buffer.getvalue() + tag.encode()


def test_api_v2_register_login_me_and_tenant_switch(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    register = client.post(
        "/api/v2/auth/register",
        json={
            "tenant_name": "River Delta Civil",
            "email": "river-admin@example.com",
            "display_name": "River Admin",
            "password": "RiverPass123!",
        },
    )
    assert register.status_code == 202
    payload = register.json()
    assert payload["status"] == "pending_review"
    assert payload["company"]["name"] == "River Delta Civil"
    assert payload["contact"]["email"] == "river-admin@example.com"

    with session_maker() as db:
        user = db.scalar(select(User).where(User.email == "river-admin@example.com"))
        assert user is None


def test_api_v2_rate_limit_and_public_requests(app_context):
    client = app_context["client"]

    for _ in range(6):
        failed = client.post(
            "/api/v2/auth/login",
            json={"email": "limit-test@example.com", "password": "wrong-password"},
        )
        assert failed.status_code == 401

    blocked = client.post(
        "/api/v2/auth/login",
        json={"email": "limit-test@example.com", "password": "wrong-password"},
    )
    assert blocked.status_code == 429

    delete_request = client.post(
        "/api/v2/public/delete-account-request",
        json={"email": "public@example.com", "reason": "Please remove the test account data."},
    )
    assert delete_request.status_code == 200

    support_request = client.post(
        "/api/v2/public/support-request",
        json={
            "email": "public@example.com",
            "subject": "Portal help",
            "message": "Need help with onboarding and project access configuration.",
        },
    )
    assert support_request.status_code == 200


def test_api_v2_forgot_and_reset_password(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    forgot = client.post("/api/v2/auth/forgot-password", json={"email": "admin@example.com"})
    assert forgot.status_code == 200

    with session_maker() as db:
        user = db.scalar(select(User).where(User.email == "admin@example.com"))
        assert user is not None
        token = create_password_reset_token(db, user=user, settings=settings)
        db.commit()

    reset = client.post(
        "/api/v2/auth/reset-password",
        json={"token": token, "new_password": "AdminPass789!"},
    )
    assert reset.status_code == 200

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "admin@example.com", "password": "AdminPass789!"},
    )
    assert login.status_code == 200


def test_api_v2_project_manager_employee_permissions(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    create_employee = client.post(
        "/api/v2/employees",
        json={
            "employee_id": "E120",
            "name": "Project Scoped Worker",
            "role_name": "worker",
            "project_id": "P100",
        },
    )
    assert create_employee.status_code == 201
    payload = create_employee.json()["employee"]
    assert payload["project_id"] == "P100"
    assert payload["company_id"] == "default"
    assert payload["tenant_id"] == "default"

    blocked = client.post(
        "/api/v2/employees",
        json={
            "employee_id": "E121",
            "name": "Cross Tenant Worker",
            "role_name": "worker",
            "project_id": "P200",
        },
    )
    assert blocked.status_code == 403

    with session_maker() as db:
        created_employee = db.scalar(select(Employee).where(Employee.employee_id == "E120"))
        assert created_employee is not None
        assert created_employee.project_id == "P100"
        assert created_employee.company_id == "default"

    client.post("/api/v2/auth/logout")

    admin_login = client.post(
        "/api/v2/auth/login",
        json={"email": "admin@example.com", "password": "AdminPass123!"},
    )
    assert admin_login.status_code == 200

    assign = client.post(
        "/api/v2/employees/E120/assign-project",
        json={"project_id": None},
    )
    assert assign.status_code == 200
    assert assign.json()["employee"]["project_id"] is None


def test_api_v2_management_mobile_contract_and_review_actions(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    first_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-07-08T10:00:00Z",
            "note": "Panel rough-in photo",
        },
        files={"photo": ("management-review-a.jpg", _jpeg_bytes("management-a"), "image/jpeg")},
    )
    second_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-07-08T10:05:00Z",
            "note": "Conduit staging photo",
        },
        files={"photo": ("management-review-b.jpg", _jpeg_bytes("management-b"), "image/jpeg")},
    )
    assert first_upload.status_code == 200
    assert second_upload.status_code == 200
    first_photo_id = first_upload.json()["photo"]["id"]

    anonymous = client.get("/api/v2/management/projects/overview")
    assert anonymous.status_code == 401

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    overview = client.get("/api/v2/management/projects/overview?window_days=365")
    assert overview.status_code == 200
    overview_payload = overview.json()
    assert overview_payload["contract_version"] == "management_projects_overview:v1"
    assert overview_payload["guardrails"]["employee_api_key_allowed"] is False
    project_row = next(item for item in overview_payload["projects"] if item["project"]["project_id"] == "P100")
    assert project_row["counts"]["uploaded_count"] >= 2
    assert project_row["counts"]["pending_internal_count"] >= 2

    summary = client.get("/api/v2/management/projects/P100/review-summary?window_days=365")
    assert summary.status_code == 200
    summary_payload = summary.json()
    assert summary_payload["counts"]["uploaded_count"] >= 2
    assert summary_payload["status_flags"]["needs_review"] is True
    assert summary_payload["recent_photos"]
    assert "file_path" not in summary_payload["recent_photos"][0]

    inbox = client.get("/api/v2/management/projects/P100/review-inbox?status_filter=pending")
    assert inbox.status_code == 200
    inbox_payload = inbox.json()
    inbox_ids = {item["id"] for item in inbox_payload["items"]}
    assert first_photo_id in inbox_ids

    health = client.get("/api/v2/management/projects/P100/upload-health?window_days=365")
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload["contract_version"] == "management_upload_health:v1"
    assert health_payload["upload_counts"]["uploaded_count"] >= 2
    assert "photo_ai_queue_company_snapshot" in health_payload

    action = client.post(
        "/api/v2/management/review-actions",
        json={
            "photo_ids": [first_photo_id],
            "action": "approve_client_visible",
            "comment": "Ready for client review.",
        },
    )
    assert action.status_code == 200
    assert action.json()["updated_photo_ids"] == [first_photo_id]
    assert action.json()["comment_ids"]

    with session_maker() as db:
        photo = db.get(Photo, first_photo_id)
        assert photo is not None
        assert photo.approval_status.value == "approved"
        assert photo.visibility.value == "client_visible"
        comment = db.scalar(select(PhotoComment).where(PhotoComment.photo_id == first_photo_id))
        assert comment is not None
        assert comment.comment == "Ready for client review."
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "management_review_action"))
        assert audit is not None
        assert audit.detail_json["action"] == "approve_client_visible"

    client.post("/api/v2/auth/logout")
    client_login = client.post(
        "/api/v2/auth/login",
        json={"email": "client@example.com", "password": "ClientPass123!"},
    )
    assert client_login.status_code == 200
    forbidden = client.get("/api/v2/management/projects/overview")
    assert forbidden.status_code == 403


def test_api_v2_management_review_task_workflow_and_mobile_completion(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        db.add(
            Employee(
                company_id="default",
                employee_id="E101",
                name="Second Field Worker",
                api_key="api-key-e101",
                role="worker",
                active=True,
                project_id="P100",
            )
        )
        db.commit()

    source_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-07-08T11:00:00Z",
            "note": "Initial panel overview",
        },
        files={"photo": ("review-task-source.jpg", _jpeg_bytes("review-source"), "image/jpeg")},
    )
    cross_company_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e200"},
        data={
            "employee_id": "E200",
            "project_id": "P200",
            "photo_type": "project",
            "timestamp": "2026-07-08T11:01:00Z",
            "note": "Other company photo",
        },
        files={"photo": ("review-task-cross.jpg", _jpeg_bytes("review-cross"), "image/jpeg")},
    )
    assert source_upload.status_code == 200
    assert cross_company_upload.status_code == 200
    source_photo_id = source_upload.json()["photo"]["id"]
    cross_photo_id = cross_company_upload.json()["photo"]["id"]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    create_task = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "related_photo_id": source_photo_id,
            "task_type": "retake_photo",
            "assigned_employee_id": "E101",
            "message": "Capture a clearer overview of the panel label.",
        },
    )
    assert create_task.status_code == 200
    task_payload = create_task.json()["task"]
    task_id = task_payload["public_id"]
    assert task_payload["assigned_employee_id"] == "E101"
    assert task_payload["status"] == "open"

    blocked_cross_project_photo = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "related_photo_id": cross_photo_id,
            "task_type": "retake_photo",
            "assigned_employee_id": "E101",
            "message": "Capture a clearer overview of the panel label.",
        },
    )
    assert blocked_cross_project_photo.status_code == 404

    blocked_language = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "related_photo_id": source_photo_id,
            "task_type": "clarify_photo",
            "assigned_employee_id": "E101",
            "message": "This is urgent and must be fixed.",
        },
    )
    assert blocked_language.status_code == 400

    tasks = client.get("/api/v2/management/projects/P100/review-tasks?status_filter=active")
    assert tasks.status_code == 200
    assert any(item["public_id"] == task_id for item in tasks.json()["items"])

    cancelled_task = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "related_photo_id": source_photo_id,
            "task_type": "clarify_photo",
            "assigned_employee_id": "E101",
            "message": "Check the visible panel label.",
        },
    )
    assert cancelled_task.status_code == 200
    cancelled_task_id = cancelled_task.json()["task"]["public_id"]
    cancel_response = client.post(
        f"/api/v2/management/review-tasks/{cancelled_task_id}/actions",
        json={"action": "cancel"},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["task"]["status"] == "cancelled"

    client.post("/api/v2/auth/logout")
    client_login = client.post(
        "/api/v2/auth/login",
        json={"email": "client@example.com", "password": "ClientPass123!"},
    )
    assert client_login.status_code == 200
    forbidden_client = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "task_type": "clarify_photo",
            "assigned_employee_id": "E101",
            "message": "Check the visible label text.",
        },
    )
    assert forbidden_client.status_code == 403
    client.post("/api/v2/auth/logout")

    worker_login = client.post(
        "/api/v2/auth/login",
        json={"email": "worker@example.com", "password": "WorkerPass123!"},
    )
    assert worker_login.status_code == 200
    forbidden_worker = client.get("/api/v2/management/projects/P100/review-tasks")
    assert forbidden_worker.status_code == 403
    client.post("/api/v2/auth/logout")

    e100_tasks = client.get("/api/mobile/tasks", headers={"X-API-Key": "api-key-e100"})
    assert e100_tasks.status_code == 200
    assert all(item["public_id"] != task_id for item in e100_tasks.json()["items"])

    e101_tasks = client.get("/api/mobile/tasks", headers={"X-API-Key": "api-key-e101"})
    assert e101_tasks.status_code == 200
    assert task_id in [item["public_id"] for item in e101_tasks.json()["items"]]

    blocked_cancelled_completion = client.post(
        f"/api/mobile/tasks/{cancelled_task_id}/actions",
        headers={"X-API-Key": "api-key-e101"},
        json={"action": "complete", "message": "Panel label text checked."},
    )
    assert blocked_cancelled_completion.status_code == 400

    missing_completion = client.post(
        f"/api/mobile/tasks/{task_id}/actions",
        headers={"X-API-Key": "api-key-e101"},
        json={"action": "complete", "message": "New overview has been captured."},
    )
    assert missing_completion.status_code == 400

    completion_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e101"},
        data={
            "employee_id": "E101",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-07-08T11:10:00Z",
            "note": "Clearer panel label overview",
        },
        files={"photo": ("review-task-completion.jpg", _jpeg_bytes("review-completion"), "image/jpeg")},
    )
    assert completion_upload.status_code == 200
    completion_photo_id = completion_upload.json()["photo"]["id"]

    blocked_other_employee_photo = client.post(
        f"/api/mobile/tasks/{task_id}/actions",
        headers={"X-API-Key": "api-key-e101"},
        json={
            "action": "complete",
            "message": "New overview has been captured.",
            "completion_photo_id": source_photo_id,
        },
    )
    assert blocked_other_employee_photo.status_code == 400

    complete = client.post(
        f"/api/mobile/tasks/{task_id}/actions",
        headers={"X-API-Key": "api-key-e101"},
        json={
            "action": "complete",
            "message": "New overview has been captured.",
            "completion_photo_id": completion_photo_id,
        },
    )
    assert complete.status_code == 200
    assert complete.json()["task"]["status"] == "completed"
    assert complete.json()["task"]["completion_photo_id"] == completion_photo_id

    login_again = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login_again.status_code == 200
    clarify = client.post(
        "/api/v2/management/review-tasks",
        json={
            "project_id": "P100",
            "related_photo_id": source_photo_id,
            "task_type": "clarify_photo",
            "assigned_employee_id": "E100",
            "message": "Check the visible label text.",
        },
    )
    assert clarify.status_code == 200
    clarify_id = clarify.json()["task"]["public_id"]
    client.post("/api/v2/auth/logout")

    clarify_complete = client.post(
        f"/api/mobile/tasks/{clarify_id}/actions",
        headers={"X-API-Key": "api-key-e100"},
        json={"action": "complete", "message": "The visible label reads feeder A."},
    )
    assert clarify_complete.status_code == 200
    assert clarify_complete.json()["task"]["status"] == "completed"
    assert clarify_complete.json()["task"]["completion_photo_id"] is None

    with session_maker() as db:
        task = db.scalar(select(ReviewTask).where(ReviewTask.public_id == task_id))
        assert task is not None
        assert task.status.value == "completed"
        assert task.completion_photo_id == completion_photo_id
        event_types = {
            event.event_type.value
            for event in db.scalars(select(ReviewTaskEvent).where(ReviewTaskEvent.task_public_id == task_id))
        }
        assert {"created", "completed"}.issubset(event_types)
        assert db.scalar(select(AuditLog).where(AuditLog.action == "review_task_created")) is not None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "review_task_employee_action")) is not None


def test_api_v2_management_review_session_closeout_is_independent(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-07-08T12:00:00Z",
            "note": "Closeout evidence",
        },
        files={"photo": ("review-session-source.jpg", _jpeg_bytes("review-session"), "image/jpeg")},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo"]["id"]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    current = client.post(
        "/api/v2/management/projects/P100/review-sessions/current",
        json={"review_date": "2026-07-08"},
    )
    assert current.status_code == 200
    assert current.json()["created"] is True
    session_id = current.json()["session"]["public_id"]

    close = client.post(f"/api/v2/management/review-sessions/{session_id}/close", json={})
    assert close.status_code == 200
    close_payload = close.json()["session"]
    assert close_payload["status"] == "closed"
    assert close_payload["summary"]["closeout_effects"]["auto_approves_photos"] is False
    assert close_payload["summary"]["closeout_effects"]["auto_sets_client_visible"] is False

    with session_maker() as db:
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.approval_status.value == "pending"
        assert photo.visibility.value == "internal"
        session = db.scalar(select(ReviewSession).where(ReviewSession.public_id == session_id))
        assert session is not None
        assert session.status.value == "closed"
        assert isinstance(session.summary_json, dict)
        assert session.summary_json["photo_counts"]["total"] >= 1
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "review_session_closed"))
        assert audit is not None


def test_api_v2_bulk_reprocess(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    file_one = settings.photos_root / "api-reprocess-1.jpg"
    file_one.parent.mkdir(parents=True, exist_ok=True)
    file_one.write_bytes(b"\xff\xd8\xff\xe0api-reprocess-1")
    file_two = settings.photos_root / "api-reprocess-2.jpg"
    file_two.write_bytes(b"\xff\xd8\xff\xe0api-reprocess-2")

    with session_maker() as db:
        first = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(file_one),
            storage_path=str(file_one),
            image_url="/media/default/P100/E100/api-reprocess-1.jpg",
            original_file_name="api-reprocess-1.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "old", "labels": [], "defects": []},
        )
        second = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(file_two),
            storage_path=str(file_two),
            image_url="/media/default/P100/E100/api-reprocess-2.jpg",
            original_file_name="api-reprocess-2.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "old", "labels": [], "defects": []},
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        photo_ids = [first.id, second.id]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    reprocess = client.post(
        "/api/v2/photos/bulk-reprocess",
        json={"photo_ids": photo_ids, "prompt": "Prioritize visible site hazards."},
    )
    assert reprocess.status_code == 200
    assert reprocess.json()["queued_count"] == 2
    assert sorted(reprocess.json()["queued_photo_ids"]) == sorted(photo_ids)

    with session_maker() as db:
        refreshed = list(db.scalars(select(Photo).where(Photo.id.in_(photo_ids)).order_by(Photo.id)))
        assert len(refreshed) == 2
        assert all(photo.labeling_status == "completed" for photo in refreshed)
        assert all("Mock field photo" in photo.tag_json["ai_summary"] for photo in refreshed)
        assert all(isinstance(photo.tag_json["labels"], list) for photo in refreshed)
        assert all(photo.tag_json["defects"] == [] for photo in refreshed)
        bulk_audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "bulk_photo_reprocess_requested",
                AuditLog.company_id == "default",
            )
        )
        assert bulk_audit is not None
        assert bulk_audit.detail_json["photo_ids"]
        assert bulk_audit.detail_json["custom_prompt_supplied"] is True


def test_api_v2_semantic_search(app_context):
    client = app_context["client"]

    crack_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-04-02T12:00:00Z",
            "note": "North wall crack near loading bay",
        },
        files={"photo": ("semantic-crack.jpg", _jpeg_bytes("semantic-crack"), "image/jpeg")},
    )
    clear_upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-04-02T12:05:00Z",
            "note": "Conduit staging near south gate",
        },
        files={"photo": ("semantic-clear.jpg", _jpeg_bytes("semantic-clear"), "image/jpeg")},
    )
    assert crack_upload.status_code == 200
    assert clear_upload.status_code == 200

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    search = client.post(
        "/api/v2/photos/search",
        json={"query": "wall crack loading bay", "limit": 5},
    )
    assert search.status_code == 200
    results = search.json()["results"]
    assert len(results) >= 2
    assert results[0]["photo"]["id"] == crack_upload.json()["photo"]["id"]
    assert results[0]["similarity"] >= results[1]["similarity"]

    filtered = client.get("/api/v2/photos?ai_keyword=mock+field&confidence=low")
    assert filtered.status_code == 200
    filtered_results = filtered.json()["results"]
    assert filtered_results
    assert filtered_results[0]["confidence_level"] == "low"


def test_api_v2_photo_ai_logs_and_reject(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    upload = client.post(
        "/upload",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "employee_id": "E100",
            "project_id": "P100",
            "photo_type": "project",
            "timestamp": "2026-04-03T09:00:00Z",
            "note": "First AI history photo",
        },
        files={"photo": ("ai-history.jpg", _jpeg_bytes("ai-history"), "image/jpeg")},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo"]["id"]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    logs_response = client.get(f"/api/v2/photos/{photo_id}/ai_logs")
    assert logs_response.status_code == 200
    payload = logs_response.json()
    assert payload["photo_id"] == photo_id
    assert len(payload["logs"]) == 1
    log_id = payload["logs"][0]["id"]
    assert payload["primary_log_id"] == log_id
    assert payload["confidence_level"] == "low"

    promote_response = client.post(f"/api/v2/ai_logs/{log_id}/promote")
    assert promote_response.status_code == 200
    promote_payload = promote_response.json()
    assert promote_payload["primary_log_id"] != log_id
    assert promote_payload["confidence_level"] == "medium"

    reject_response = client.patch(
        f"/api/v2/ai_logs/{promote_payload['primary_log_id']}/status",
        json={"status": "rejected"},
    )
    assert reject_response.status_code == 200
    reject_payload = reject_response.json()
    assert reject_payload["log"]["status"] == "rejected"
    assert reject_payload["snapshot"]["ai_summary"] is not None
    assert reject_payload["confidence_level"] == "low"

    with session_maker() as db:
        analysis_log = db.get(AIAnalysisLog, log_id)
        assert analysis_log is not None
        assert analysis_log.status == AIAnalysisStatus.active
        photo = db.get(Photo, photo_id)
        assert photo is not None
        assert photo.tag_json["ai_summary"] is not None
        assert isinstance(photo.tag_json.get("labels"), list)


def test_api_v2_generate_report_and_download(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    first_file = settings.photos_root / "report-photo-1.jpg"
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_bytes(b"\xff\xd8\xff\xe0report-photo-1")
    second_file = settings.photos_root / "report-photo-2.jpg"
    second_file.write_bytes(b"\xff\xd8\xff\xe0report-photo-2")

    with session_maker() as db:
        first = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(first_file),
            storage_path=str(first_file),
            image_url="/media/default/P100/E100/report-photo-1.jpg",
            original_file_name="report-photo-1.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "Wall crack near loading bay", "labels": ["defect"], "defects": ["crack"]},
            note="North wall crack near loading bay",
        )
        second = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(second_file),
            storage_path=str(second_file),
            image_url="/media/default/P100/E100/report-photo-2.jpg",
            original_file_name="report-photo-2.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "Conduit staging area", "labels": ["site"], "defects": []},
            note="South gate conduit staging",
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        photo_ids = [first.id, second.id]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    response = client.post(
        "/api/v2/reports/generate",
        json={
            "photo_ids": photo_ids,
            "prompt": "Generate a weekly inspection report focused on visible defects and recommended actions.",
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["report_id"]
    assert payload["status_url"].endswith(payload["report_id"])

    status_response = client.get(payload["status_url"])
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "completed"
    assert status_payload["download_url"]

    download_response = client.get(status_payload["download_url"])
    assert download_response.status_code == 200
    assert download_response.headers["content-type"].startswith("application/pdf")
    assert download_response.content.startswith(b"%PDF-1.4")

    with session_maker() as db:
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == payload["report_id"]))
        assert report is not None
        assert report.status == ReportStatus.completed
        assert report.file_path is not None
        assert report.markdown_content is not None
        requested_audit = db.scalar(
            select(AuditLog).where(AuditLog.action == "report_generation_requested", AuditLog.company_id == "default")
        )
        completed_audit = db.scalar(
            select(AuditLog).where(AuditLog.action == "report_generation_completed", AuditLog.company_id == "default")
        )
        assert requested_audit is not None
        assert completed_audit is not None


def test_api_v2_compare_progress_report(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    def fake_multi_image_completion(*, backends, prompt, images):
        assert len(images) == 2
        assert "guardrails and helmets" in prompt
        assert "standing water and staging" in prompt
        assert "photo_id" in prompt
        return (
            json.dumps(
                {
                    "executive_summary": "Structural framing advanced between the two photos, but edge protection still needs attention.",
                    "overall_progress_percent": 62,
                    "overall_status": "at_risk",
                    "confidence_level": "medium",
                    "manager_brief": "Visible progress is real, but guardrail completion is lagging and should be corrected before the next stage.",
                    "key_changes": [
                        "Framing coverage increased across the compared views.",
                        "Material staging became more organized in the later photo.",
                    ],
                    "work_completed": [
                        "Structural framing advanced.",
                    ],
                    "work_remaining": [
                        "Install full edge protection.",
                    ],
                    "safety_risks": [
                        "Missing guardrail on the exposed edge.",
                    ],
                    "quality_risks": [],
                    "recommended_actions": [
                        "Finish guardrail installation before additional elevated work.",
                    ],
                    "timeline_observations": [
                        {
                            "photo_id": 1,
                            "captured_at": "2026-04-04T08:00:00Z",
                            "observation": "Earlier frame shows the workface before the additional framing was installed.",
                            "progress_signal": "Baseline framing stage.",
                            "risk_signal": "Open edge lacks full protection.",
                        },
                        {
                            "photo_id": 2,
                            "captured_at": "2026-04-04T09:00:00Z",
                            "observation": "Later frame shows more framing progress and clearer material organization.",
                            "progress_signal": "Meaningful forward progress is visible.",
                            "risk_signal": "Edge protection is still incomplete.",
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
                        "executive_summary": "两张照片之间的结构施工有推进，但临边防护仍需关注。",
                        "overall_progress_percent": 62,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "现场推进属实，但防护栏补齐滞后，进入下一阶段前应先纠正。",
                        "key_changes": ["对比视图中结构覆盖继续增加。", "后续照片的材料摆放更有序。"],
                        "work_completed": ["结构框架继续推进。"],
                        "work_remaining": ["补齐完整的临边防护。"],
                        "safety_risks": ["暴露边缘仍缺少防护栏。"],
                        "quality_risks": [],
                        "recommended_actions": ["继续高处作业前先完成防护栏安装。"],
                        "timeline_observations": [
                            {
                                "photo_id": 1,
                                "captured_at": "2026-04-04T08:00:00Z",
                                "observation": "较早画面显示加装框架前的工作面。",
                                "progress_signal": "基线框架阶段。",
                                "risk_signal": "开放边缘缺少完整保护。",
                            },
                            {
                                "photo_id": 2,
                                "captured_at": "2026-04-04T09:00:00Z",
                                "observation": "较晚画面显示更多框架推进和更清晰的材料整理。",
                                "progress_signal": "现场存在明确推进。",
                                "risk_signal": "边缘保护仍不完整。",
                            },
                        ],
                    },
                    "en": {
                        "executive_summary": "Structural framing advanced between the two photos, but edge protection still needs attention.",
                        "overall_progress_percent": 62,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "Visible progress is real, but guardrail completion is lagging and should be corrected before the next stage.",
                        "key_changes": ["Framing coverage increased across the compared views.", "Material staging became more organized in the later photo."],
                        "work_completed": ["Structural framing advanced."],
                        "work_remaining": ["Install full edge protection."],
                        "safety_risks": ["Missing guardrail on the exposed edge."],
                        "quality_risks": [],
                        "recommended_actions": ["Finish guardrail installation before additional elevated work."],
                        "timeline_observations": [
                            {
                                "photo_id": 1,
                                "captured_at": "2026-04-04T08:00:00Z",
                                "observation": "Earlier frame shows the workface before the additional framing was installed.",
                                "progress_signal": "Baseline framing stage.",
                                "risk_signal": "Open edge lacks full protection.",
                            },
                            {
                                "photo_id": 2,
                                "captured_at": "2026-04-04T09:00:00Z",
                                "observation": "Later frame shows more framing progress and clearer material organization.",
                                "progress_signal": "Meaningful forward progress is visible.",
                                "risk_signal": "Edge protection is still incomplete.",
                            },
                        ],
                    },
                    "es": {
                        "executive_summary": "La estructura avanzó entre las dos fotos, pero la protección del borde aún requiere atención.",
                        "overall_progress_percent": 62,
                        "overall_status": "at_risk",
                        "confidence_level": "medium",
                        "manager_brief": "El avance es real, pero la baranda sigue retrasada y debe corregirse antes de la siguiente etapa.",
                        "key_changes": ["La cobertura de estructura aumentó entre las vistas.", "El acopio quedó más ordenado en la foto posterior."],
                        "work_completed": ["La estructura avanzó."],
                        "work_remaining": ["Instalar protección completa en el borde."],
                        "safety_risks": ["Falta baranda en el borde expuesto."],
                        "quality_risks": [],
                        "recommended_actions": ["Completar la baranda antes de más trabajo elevado."],
                        "timeline_observations": [
                            {
                                "photo_id": 1,
                                "captured_at": "2026-04-04T08:00:00Z",
                                "observation": "La imagen inicial muestra el frente antes del nuevo avance.",
                                "progress_signal": "Etapa base del frente.",
                                "risk_signal": "El borde abierto no tiene protección completa.",
                            },
                            {
                                "photo_id": 2,
                                "captured_at": "2026-04-04T09:00:00Z",
                                "observation": "La imagen posterior muestra más avance y mejor organización de materiales.",
                                "progress_signal": "Hay avance visible.",
                                "risk_signal": "La protección del borde sigue incompleta.",
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

    first_file = settings.photos_root / "progress-photo-1.jpg"
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_bytes(b"\xff\xd8\xff\xe0progress-photo-1")
    second_file = settings.photos_root / "progress-photo-2.jpg"
    second_file.write_bytes(b"\xff\xd8\xff\xe0progress-photo-2")

    with session_maker() as db:
        project = db.scalar(select(Project).where(Project.project_id == "P100"))
        assert project is not None
        project.image_video_ai_prompt = "Focus on guardrails and helmets."
        first = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(first_file),
            storage_path=str(first_file),
            image_url="/media/default/P100/E100/progress-photo-1.jpg",
            original_file_name="progress-photo-1.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
            gps_lat=36.15398,
            gps_lon=-95.99277,
            pitch=4.5,
            heading=135.1,
            labeling_status="completed",
            tag_json={"ai_summary": "Earlier stage", "labels": ["progress"], "defects": []},
        )
        second = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(second_file),
            storage_path=str(second_file),
            image_url="/media/default/P100/E100/progress-photo-2.jpg",
            original_file_name="progress-photo-2.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 4, 4, 9, 0, tzinfo=timezone.utc),
            gps_lat=36.15399,
            gps_lon=-95.99275,
            pitch=5.0,
            heading=134.5,
            labeling_status="completed",
            tag_json={"ai_summary": "Later stage", "labels": ["progress"], "defects": ["missing guardrail"]},
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        photo_ids = [first.id, second.id]

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    response = client.post(
        "/api/v2/reports/compare-progress",
        json={"photo_ids": photo_ids, "prompt": "Focus on standing water and staging."},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] in {"pending", "processing", "completed"}
    assert payload["report_id"]

    status_response = client.get(f"/api/v2/reports/compare-progress/{payload['report_id']}")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "completed"
    assert "Progress Evolution Report" in status_payload["report_content"]
    assert len(status_payload["photos"]) == 2
    assert status_payload["structured_report"]["overall_progress_percent"] == 62
    assert status_payload["structured_report"]["overall_status"] == "at_risk"
    assert status_payload["structured_report"]["timeline_observations"][0]["photo_id"] == photo_ids[0]
    assert status_payload["custom_prompt"] == "Focus on standing water and staging."

    with session_maker() as db:
        report = db.get(ProgressReport, payload["report_id"])
        assert report is not None
        assert report.status == ProgressReportStatus.completed
        assert report.project_id == "P100"
        assert report.source_photo_ids == photo_ids


def test_api_v2_media_video_upload_supports_chunked_processing(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]

    def fake_extract(video_path, output_dir, *, fps=1):
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(1, 5):
            frame_path = output_dir / f"frame_{index:05d}.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0video-frame")
            frames.append(frame_path)
        return frames

    monkeypatch.setattr("app.services.media_pipeline.extract_video_frames", fake_extract)
    monkeypatch.setattr(
        "app.services.media_pipeline.select_representative_video_frames",
        lambda frame_paths, **kwargs: frame_paths[:4],
    )

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    first_chunk = client.post(
        "/api/v2/media/upload",
        data={
            "media_type": "video",
            "source": "app_video",
            "chunk_index": "0",
            "total_chunks": "2",
            "metadata_json": '{"project_id":"P100","note":"Walkthrough clip"}',
        },
        files={"upload": ("walkthrough.mp4", b"video-part-1", "video/mp4")},
    )
    assert first_chunk.status_code == 200
    first_payload = first_chunk.json()
    assert first_payload["status"] == "uploading"
    assert first_payload["upload_complete"] is False
    asset_id = first_payload["asset_id"]

    second_chunk = client.post(
        "/api/v2/media/upload",
        data={
            "media_type": "video",
            "source": "app_video",
            "upload_id": asset_id,
            "chunk_index": "1",
            "total_chunks": "2",
            "metadata_json": '{"project_id":"P100","note":"Walkthrough clip"}',
        },
        files={"upload": ("walkthrough.mp4", b"video-part-2", "video/mp4")},
    )
    assert second_chunk.status_code == 200
    second_payload = second_chunk.json()
    assert second_payload["asset_id"] == asset_id
    assert second_payload["status"] == "processing"
    assert second_payload["upload_complete"] is True

    with session_maker() as db:
        asset = db.get(MediaAsset, asset_id)
        assert asset is not None
        assert asset.media_type == MediaType.video
        assert asset.status == MediaAssetStatus.completed
        assert isinstance(asset.metadata_json, dict)
        assert asset.metadata_json["selected_frame_count"] == 4
        assert asset.metadata_json["ai_snapshot"]["ai_summary"] == "Mock field photo"
        assert isinstance(asset.metadata_json["ai_snapshot"]["labels"], list)

        logs = list(
            db.scalars(
                select(AIAnalysisLog)
                .where(AIAnalysisLog.media_asset_id == asset_id)
                .order_by(AIAnalysisLog.created_at.asc())
            )
        )
        assert len(logs) == 4
        assert all(log.photo_id is None for log in logs)
        assert all(log.media_asset_id == asset_id for log in logs)
        assert all(log.analysis_type == AIAnalysisType.video_insight for log in logs)


def test_api_v2_rtmp_recording_webhook_ingests_video_asset(app_context, monkeypatch):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    def fake_extract(video_path, output_dir, *, fps=1):
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(1, 5):
            frame_path = output_dir / f"frame_{index:05d}.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0rtmp-frame")
            frames.append(frame_path)
        return frames

    monkeypatch.setattr("app.services.media_pipeline.extract_video_frames", fake_extract)
    monkeypatch.setattr(
        "app.services.media_pipeline.select_representative_video_frames",
        lambda frame_paths, **kwargs: frame_paths[:4],
    )

    recording_path = settings.media_root_path / "rtmp-test-recording.mp4"
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    recording_path.write_bytes(b"mock-rtmp-recording")

    with session_maker() as db:
        camera = IPCamera(
            company_id="default",
            name="Gate Cam",
            stream_url="rtmp://nginx/live/gate-cam",
            protocol="rtmp",
            is_enabled=True,
        )
        db.add(camera)
        db.commit()
        db.refresh(camera)
        camera_id = camera.id

    webhook_response = client.post(
        "/api/v2/webhooks/rtmp_recording_done",
        headers={"X-Webhook-Token": settings.camera_webhook_token},
        json={
            "camera_id": camera_id,
            "file_path": str(recording_path),
            "metadata_json": {"note": "nginx recording"},
        },
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["status"] == "processing"

    process_due_jobs(session_maker, settings, max_jobs=10, worker_name="test-webhook")

    with session_maker() as db:
        asset = db.get(MediaAsset, payload["asset_id"])
        assert asset is not None
        assert asset.status == MediaAssetStatus.completed
        assert isinstance(asset.metadata_json, dict)
        assert asset.metadata_json["ai_snapshot"]["ai_summary"] == "Mock field photo"
        assert isinstance(asset.metadata_json["ai_snapshot"]["labels"], list)
        assert asset.metadata_json["camera_id"] == camera_id

        camera = db.get(IPCamera, camera_id)
        assert camera is not None
        assert camera.status.value == "online"


def test_api_v2_rtmp_recording_webhook_rejects_disallowed_ip(app_context):
    client = app_context["client"]
    settings = app_context["settings"]

    settings.camera_webhook_ip_allowlist = "10.0.0.5"
    response = client.post(
        "/api/v2/webhooks/rtmp_recording_done",
        headers={"X-Webhook-Token": settings.camera_webhook_token},
        json={"company_id": "default", "file_path": "C:/missing.mp4"},
    )
    assert response.status_code == 403


def test_api_v2_retry_report(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_file = settings.photos_root / "retry-report-photo.jpg"
    photo_file.parent.mkdir(parents=True, exist_ok=True)
    photo_file.write_bytes(b"\xff\xd8\xff\xe0retry-report-photo")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_file),
            storage_path=str(photo_file),
            image_url="/media/default/P100/E100/retry-report-photo.jpg",
            original_file_name="retry-report-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "Initial report evidence", "labels": ["inspection"], "defects": []},
            note="Retry report note",
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)
        photo_id = photo.id

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    create_response = client.post(
        "/api/v2/reports/generate",
        json={"photo_ids": [photo_id], "prompt": "Generate a concise inspection report."},
    )
    assert create_response.status_code == 202
    report_id = create_response.json()["report_id"]

    with session_maker() as db:
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == report_id))
        assert report is not None
        report.status = ReportStatus.failed
        report.error_message = "Synthetic failure"
        db.add(report)
        db.commit()

    retry_response = client.post(f"/api/v2/reports/{report_id}/retry")
    assert retry_response.status_code == 202
    assert retry_response.json()["report_id"] == report_id

    with session_maker() as db:
        report = db.scalar(select(GeneratedReport).where(GeneratedReport.public_id == report_id))
        assert report is not None
        assert report.status == ReportStatus.completed
        retry_audit = db.scalar(select(AuditLog).where(AuditLog.action == "report_generation_retried"))
        assert retry_audit is not None


def test_api_v2_media_annotations_tree_and_visibility(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    media_file = settings.media_assets_root / "video" / "default" / "annot-tree-1" / "annot-tree-1.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"annotation-media")

    with session_maker() as db:
        asset = MediaAsset(
            asset_id="annot-tree-1",
            company_id="default",
            tenant_id="default",
            media_type=MediaType.video,
            source="manual_upload",
            file_path=str(media_file),
            original_file_name="annot-tree-1.mp4",
            mime_type="video/mp4",
            status=MediaAssetStatus.completed,
        )
        db.add(asset)
        db.commit()

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    root_response = client.post(
        "/api/v2/annotations/text",
        json={
            "target_ids": [{"media_asset_id": "annot-tree-1"}],
            "content_text": "Manager secondary description",
            "visibility": "public",
        },
    )
    assert root_response.status_code == 201
    root_annotation_id = root_response.json()["annotation_ids"][0]

    reply_response = client.post(
        "/api/v2/annotations/text",
        json={
            "target_ids": [{"media_asset_id": "annot-tree-1"}],
            "content_text": "Employee follow-up reply",
            "visibility": "public",
            "parent_id": root_annotation_id,
        },
    )
    assert reply_response.status_code == 201

    manager_only_response = client.post(
        "/api/v2/annotations/text",
        json={
            "target_ids": [{"media_asset_id": "annot-tree-1"}],
            "content_text": "Manager-only note",
            "visibility": "manager_only",
        },
    )
    assert manager_only_response.status_code == 201

    manager_tree = client.get("/api/v2/annotations/annot-tree-1?target_type=media_asset")
    assert manager_tree.status_code == 200
    manager_items = manager_tree.json()["items"]
    assert len(manager_items) == 2
    assert manager_items[0]["content_text"] == "Manager secondary description"
    assert manager_items[0]["children"][0]["content_text"] == "Employee follow-up reply"
    manager_detail = client.get("/api/v2/media/annot-tree-1/detail")
    assert manager_detail.status_code == 200
    detail_payload = manager_detail.json()
    assert detail_payload["media"]["asset_id"] == "annot-tree-1"
    assert len(detail_payload["annotations"]) == 1
    assert detail_payload["annotations"][0]["content_text"] == "Manager secondary description"
    assert detail_payload["annotation_tree"]["media_asset_id"] == "annot-tree-1"

    client.post("/api/v2/auth/logout")
    worker_login = client.post(
        "/api/v2/auth/login",
        json={"email": "worker@example.com", "password": "WorkerPass123!"},
    )
    assert worker_login.status_code == 200

    worker_tree = client.get("/api/v2/annotations/annot-tree-1?target_type=media_asset")
    assert worker_tree.status_code == 200
    worker_items = worker_tree.json()["items"]
    assert len(worker_items) == 1
    assert worker_items[0]["content_text"] == "Manager secondary description"
    assert worker_items[0]["children"][0]["content_text"] == "Employee follow-up reply"


def test_api_v2_media_annotations_include_multilingual_voice_variants(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    media_file = settings.media_assets_root / "video" / "default" / "annot-tree-voice-1" / "annot-tree-voice-1.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"annotation-media-voice")

    with session_maker() as db:
        asset = MediaAsset(
            asset_id="annot-tree-voice-1",
            company_id="default",
            tenant_id="default",
            media_type=MediaType.video,
            source="manual_upload",
            file_path=str(media_file),
            original_file_name="annot-tree-voice-1.mp4",
            mime_type="video/mp4",
            status=MediaAssetStatus.completed,
        )
        manager = db.scalar(select(User).where(User.username == "manager"))
        assert manager is not None
        db.add(asset)
        db.flush()
        db.add(
            MediaAnnotation(
                id="annot-voice-1",
                media_asset_id=asset.asset_id,
                user_id=manager.id,
                role_at_time=AnnotationRole.manager,
                annotation_type=AnnotationType.voice,
                content_text="public hazard near forklift lane",
                source_language="en",
                translations_json={
                    "zh": "叉车通道附近存在公开危险",
                    "en": "public hazard near forklift lane",
                    "es": "peligro público cerca del carril de montacargas",
                },
                visibility=AnnotationVisibility.public,
                status=AnnotationStatus.completed,
            )
        )
        db.commit()

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "worker@example.com", "password": "WorkerPass123!"},
    )
    assert login.status_code == 200

    response = client.get("/api/v2/media/annot-tree-voice-1/detail")
    assert response.status_code == 200
    payload = response.json()
    assert payload["annotations"][0]["transcript_language"] == "en"
    assert payload["annotations"][0]["content_text_zh"] == "叉车通道附近存在公开危险"
    assert payload["annotations"][0]["content_text_en"] == "public hazard near forklift lane"
    assert payload["annotations"][0]["content_text_es"] == "peligro público cerca del carril de montacargas"


def test_api_v2_media_detail_and_binary_routes_support_x_api_key(app_context):
    client = app_context["client"]
    settings = app_context["settings"]
    session_maker = app_context["session_maker"]

    media_file = settings.media_assets_root / "video" / "default" / "app-media-1" / "app-media-1.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"mock-video-stream")
    preview_dir = settings.media_frames_root / "app-media-1" / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_file = preview_dir / "frame_00001.jpg"
    preview_file.write_bytes(b"\xff\xd8\xff\xe0preview-frame")

    with session_maker() as db:
        asset = MediaAsset(
            asset_id="app-media-1",
            company_id="default",
            tenant_id="default",
            media_type=MediaType.video,
            source="app_video",
            file_path=str(media_file),
            original_file_name="app-media-1.mp4",
            mime_type="video/mp4",
            status=MediaAssetStatus.completed,
            metadata_json={
                "preview_frames": ["frame_00001.jpg"],
                "thumbnail_frame": "frame_00001.jpg",
                "timeline_markers": [{"time_seconds": 0, "label": "Opening frame"}],
            },
        )
        db.add(asset)
        db.commit()

    headers = {"X-API-Key": "api-key-e100"}
    detail_response = client.get("/api/v2/media/app-media-1/detail", headers=headers)
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["media_kind"] == "video"
    assert detail_payload["media_url"] == "http://testserver/api/v2/media/app-media-1/stream"
    assert detail_payload["thumb_url"] == "http://testserver/api/v2/media/app-media-1/preview/frame_00001.jpg"
    assert detail_payload["media"]["stream_url"] == "http://testserver/api/v2/media/app-media-1/stream"
    assert detail_payload["media"]["thumbnail_url"] == "http://testserver/api/v2/media/app-media-1/preview/frame_00001.jpg"
    assert detail_payload["media"]["preview_items"][0]["url"] == "http://testserver/api/v2/media/app-media-1/preview/frame_00001.jpg"

    stream_response = client.get("/api/v2/media/app-media-1/stream", headers=headers)
    assert stream_response.status_code == 200
    assert stream_response.content == b"mock-video-stream"
    assert stream_response.headers["content-type"].startswith("video/mp4")

    preview_response = client.get("/api/v2/media/app-media-1/preview/frame_00001.jpg", headers=headers)
    assert preview_response.status_code == 200
    assert preview_response.content == b"\xff\xd8\xff\xe0preview-frame"
    assert preview_response.headers["content-type"].startswith("image/jpeg")


def test_api_v2_voice_annotations_support_mixed_targets(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_file = settings.photos_root / "voice-group-photo.jpg"
    photo_file.parent.mkdir(parents=True, exist_ok=True)
    photo_file.write_bytes(b"\xff\xd8voice-group-photo")
    media_file = settings.media_assets_root / "video" / "default" / "voice-group-asset" / "voice-group-asset.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"voice-group-video")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_file),
            storage_path=str(photo_file),
            image_url="/media/default/P100/E100/voice-group-photo.jpg",
            original_file_name="voice-group-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
        )
        asset = MediaAsset(
            asset_id="voice-group-asset",
            company_id="default",
            tenant_id="default",
            media_type=MediaType.video,
            source="manual_upload",
            file_path=str(media_file),
            original_file_name="voice-group-asset.mp4",
            mime_type="video/mp4",
            status=MediaAssetStatus.completed,
        )
        db.add(photo)
        db.add(asset)
        db.commit()
        db.refresh(photo)

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200
    created_at = "2026-04-04T10:15:30Z"

    response = client.post(
        "/api/v2/annotations/voice",
        data={
            "target_ids": '[{"photo_id": %d}, {"media_asset_id": "voice-group-asset"}]' % photo.id,
            "source": "app_voice",
            "created_at": created_at,
        },
        files={"audio": ("voice-note.webm", b"fake-audio-body", "audio/webm")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["target_count"] == 2
    assert len(payload["annotation_ids"]) == 2
    assert payload["status"] == "processing"

    with session_maker() as db:
        annotations = list(
            db.scalars(
                select(MediaAnnotation).where(MediaAnnotation.id.in_(payload["annotation_ids"]))
            )
        )
        assert len(annotations) == 2
        assert {annotation.photo_id for annotation in annotations if annotation.photo_id is not None} == {photo.id}
        assert {annotation.media_asset_id for annotation in annotations if annotation.media_asset_id is not None} == {"voice-group-asset"}
        assert len({annotation.audio_file_path for annotation in annotations}) == 1
        assert all(annotation.visibility == AnnotationVisibility.public for annotation in annotations)
        assert {
            annotation.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            for annotation in annotations
        } == {created_at}


def test_api_v2_mobile_employee_annotation_and_numeric_detail_compatibility(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_file = settings.photos_root / "mobile-annotation-photo.jpg"
    photo_file.parent.mkdir(parents=True, exist_ok=True)
    photo_file.write_bytes(b"\xff\xd8mobile-annotation-photo")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_file),
            storage_path=str(photo_file),
            image_url="/media/default/P100/E100/mobile-annotation-photo.jpg",
            thumb_url="/media/default/P100/E100/mobile-annotation-thumb.jpg",
            original_file_name="mobile-annotation-photo.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime.now(timezone.utc),
            labeling_status="completed",
            tag_json={"ai_summary": "Manager asked for a follow-up.", "labels": ["inspection"], "defects": []},
        )
        db.add(photo)
        db.commit()
        db.refresh(photo)

    manager_login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert manager_login.status_code == 200
    root_response = client.post(
        "/api/v2/annotations/text",
        json={
            "target_ids": [{"photo_id": photo.id}],
            "content_text": "Please verify this area.",
            "visibility": "public",
        },
    )
    assert root_response.status_code == 201
    parent_id = root_response.json()["annotation_ids"][0]
    client.post("/api/v2/auth/logout")

    voice_response = client.post(
        "/api/v2/annotations/voice",
        headers={"X-API-Key": "api-key-e100"},
        data={
            "target_ids": f"[{photo.id}]",
            "source": "batch_selection",
            "created_at": "2026-04-04T23:13:10Z",
            "parent_id": parent_id,
        },
        files={"audio": ("live_test_voice.wav", b"RIFF....WAVEfmt", "audio/wav")},
    )
    assert voice_response.status_code == 201
    assert voice_response.json()["target_count"] == 1

    text_response = client.post(
        "/api/v2/annotations/text",
        headers={"X-API-Key": "api-key-e100"},
        json={
            "target_id": photo.id,
            "body_text": "employee side test reply",
            "parent_id": parent_id,
            "created_at": "2026-04-04T23:13:20Z",
        },
    )
    assert text_response.status_code == 201

    detail_response = client.get(f"/api/v2/media/{photo.id}/detail", headers={"X-API-Key": "api-key-e100"})
    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert payload["id"] == photo.id
    assert payload["media_kind"] == "photo"
    assert payload["media_asset_id"] == str(photo.id)
    assert payload["media_url"].startswith("http://testserver/media/")
    assert payload["ai_conclusion"] == "Manager asked for a follow-up."
    assert payload["authorized_employee_ids"] == ["E100"]
    assert payload["can_reply"] is True
    assert payload["annotations"][0]["body_text"] == "Please verify this area."
    assert payload["annotations"][0]["author_role"] == "manager"
    assert any(child["body_text"] == "employee side test reply" for child in payload["annotations"][0]["children"])
    voice_child = next(child for child in payload["annotations"][0]["children"] if child["annotation_type"] == "voice")
    assert voice_child["audio_url"].startswith("/api/v2/annotations/")
    assert voice_child["transcript_status"] in {"processing", "failed"}
    assert voice_child["processing_status"] == voice_child["status"]
    assert voice_child["worker_status"] in {"queued", "failed", "dead_letter", "running", "completed"}
    assert "error_message" in voice_child

    audio_head_response = client.head(voice_child["audio_url"], headers={"X-API-Key": "api-key-e100"})
    assert audio_head_response.status_code == 200
    assert audio_head_response.headers["content-type"] == "audio/wav"

    annotations_response = client.get(f"/api/v2/annotations/{photo.id}", headers={"X-API-Key": "api-key-e100"})
    assert annotations_response.status_code == 200
    assert annotations_response.json()["items"][0]["content_text"] == "Please verify this area."


def test_api_v2_copilot_conversation_and_message_flow(app_context):
    client = app_context["client"]
    session_maker = app_context["session_maker"]
    settings = app_context["settings"]

    photo_path = settings.photos_root / "default" / "P100" / "E100" / "copilot-source.jpg"
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    photo_path.write_bytes(b"\xff\xd8\xff\xe0copilot-source")

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P100",
            photo_type=PhotoType.project,
            file_path=str(photo_path),
            storage_path=str(photo_path),
            image_url="http://testserver/media/default/P100/E100/copilot-source.jpg",
            thumb_url="http://testserver/media/default/P100/E100/copilot-source.jpg",
            original_file_name="copilot-source.jpg",
            mime_type="image/jpeg",
            gps="36.10,-95.90",
            gps_lat=36.10,
            gps_lon=-95.90,
            location="North work zone",
            heading=120.0,
            pitch=4.0,
            roll=0.0,
            captured_at_utc=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc),
            note="Visible standing water near the access path.",
            tag_json={
                "ai_summary": "Standing water remains visible near the active work path.",
                "labels": ["water", "construction"],
                "defects": ["standing water hazard"],
            },
            labeling_status="completed",
        )
        db.add(photo)
        db.flush()

        from app.services.evidence_copilot import sync_photo_evidence_bundle

        sync_photo_evidence_bundle(db, photo)
        db.commit()
        photo_id = photo.id

    login = client.post(
        "/api/v2/auth/login",
        json={"email": "manager@example.com", "password": "ManagerPass123!"},
    )
    assert login.status_code == 200

    backends_response = client.get("/api/v2/copilot/backends")
    assert backends_response.status_code == 200
    assert backends_response.json()["items"]

    create_response = client.post(
        "/api/v2/copilot/conversations",
        json={"project_id": "P100", "preferred_mode": "executive"},
    )
    assert create_response.status_code == 201
    conversation_id = create_response.json()["conversation"]["id"]

    message_response = client.post(
        f"/api/v2/copilot/conversations/{conversation_id}/messages",
        json={
            "content_text": "What are the highest safety risks this week on this project?",
            "language": "en",
            "preferred_mode": "executive",
        },
    )
    assert message_response.status_code == 202
    assistant_message_id = message_response.json()["assistant_message"]["id"]

    process_due_jobs(app_context["session_maker"], settings, 5, "test-copilot")

    conversation_response = client.get(f"/api/v2/copilot/conversations/{conversation_id}")
    assert conversation_response.status_code == 200
    payload = conversation_response.json()
    assert len(payload["messages"]) == 2
    messages_by_role = {message["role"]: message for message in payload["messages"]}
    assert set(messages_by_role) == {"user", "assistant"}
    assert messages_by_role["assistant"]["status"] == "completed"
    assert isinstance(messages_by_role["assistant"]["sources"], list)

    message_detail = client.get(f"/api/v2/copilot/messages/{assistant_message_id}")
    assert message_detail.status_code == 200
    assert message_detail.json()["status"] == "completed"

    with session_maker() as db:
        assert db.scalar(select(func.count()).select_from(EvidenceObservation).where(EvidenceObservation.photo_id == photo_id)) >= 1
        conversation = db.get(CopilotConversation, conversation_id)
        assert conversation is not None
        messages = list(db.scalars(select(CopilotMessage).where(CopilotMessage.conversation_id == conversation_id)))
        assert len(messages) == 2
        assert any(message.status == CopilotMessageStatus.completed for message in messages if message.role == "assistant")


def test_invoice_evidence_bundle_assigns_receipt_to_employee_project(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="invoice",
            photo_type=PhotoType.invoice,
            file_path="/tmp/invoice-employee-project.jpg",
            image_url="/media/default/invoice/E100/invoice-employee-project.jpg",
            original_file_name="invoice-employee-project.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            tag_json={
                "ai_summary": "Fuel receipt shows a visible total.",
                "receipt_facts": {"vendor": "Fuel Stop", "total_amount": "42.15"},
            },
            labeling_status="completed",
        )
        db.add(photo)
        db.flush()

        from app.services.evidence_copilot import sync_photo_evidence_bundle

        sync_photo_evidence_bundle(db, photo)
        db.commit()

        receipt = db.scalar(select(ReceiptFact).where(ReceiptFact.photo_id == photo.id))
        assert receipt is not None
        assert receipt.project_id == "P100"
        observation = db.scalar(
            select(EvidenceObservation).where(
                EvidenceObservation.photo_id == photo.id,
                EvidenceObservation.observation_type == "summary",
            )
        )
        assert observation is not None
        assert observation.project_id == "P100"


def test_invoice_evidence_bundle_leaves_project_empty_on_conflicting_project_facts(app_context):
    session_maker = app_context["session_maker"]

    with session_maker() as db:
        db.add(
            Project(
                company_id="default",
                project_id="P101",
                project_name="Conflicting Project",
                client_name="Conflict Client",
                location="Austin, TX",
            )
        )
        db.flush()
        photo = Photo(
            company_id="default",
            tenant_id="default",
            employee_id="E100",
            project_id="P101",
            photo_type=PhotoType.invoice,
            file_path="/tmp/invoice-conflict.jpg",
            image_url="/media/default/P101/E100/invoice-conflict.jpg",
            original_file_name="invoice-conflict.jpg",
            mime_type="image/jpeg",
            captured_at_utc=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            tag_json={
                "ai_summary": "Fuel receipt shows a visible total.",
                "receipt_facts": {"vendor": "Fuel Stop", "total_amount": "42.15"},
            },
            labeling_status="completed",
        )
        db.add(photo)
        db.flush()

        from app.services.evidence_copilot import sync_photo_evidence_bundle

        sync_photo_evidence_bundle(db, photo)
        db.commit()

        receipt = db.scalar(select(ReceiptFact).where(ReceiptFact.photo_id == photo.id))
        assert receipt is not None
        assert receipt.project_id is None
        observations = list(db.scalars(select(EvidenceObservation).where(EvidenceObservation.photo_id == photo.id)))
        assert observations
        assert {observation.project_id for observation in observations} == {None}
