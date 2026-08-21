from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import create_engine_from_settings, create_session_maker
from app.main import create_app
from app.models import Company, Employee, PlanCode, Project, ProjectMember, ProjectStatus, Subscription, SubscriptionStatus, SystemSetting, User, UserRole
from app.services.bootstrap import bootstrap_platform_data


@pytest.fixture(autouse=True)
def mock_ai_requests(monkeypatch):
    def build_mock_embedding(text: str) -> list[float]:
        vector = [0.0] * 768
        for token in (text or "").lower().replace("\n", " ").split():
            index = sum(ord(char) for char in token) % 768
            vector[index] += 1.0
        if not any(vector):
            vector[0] = 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    class MockResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None, **kwargs):
        assert timeout in {60, 180}
        if url.endswith("/api/embeddings"):
            assert json is not None
            assert json["prompt"]
            return MockResponse({"embedding": build_mock_embedding(json["prompt"])})
        if url.endswith("/api/generate"):
            assert json is not None
            if json.get("images"):
                assert json["images"]
                return MockResponse(
                    {
                        "response": '{"ai_summary":"Mock field photo","labels":["project","construction"],"defects":[]}'
                    }
                )
            prompt = json.get("prompt") or ""
            if '"translations":{"zh"' in prompt or '"translations": {"zh"' in prompt:
                return MockResponse(
                    {
                        "response": (
                            '{"source_language":"en","translations":'
                            '{"zh":"模拟现场照片","en":"Mock field photo","es":"Foto de campo simulada"}}'
                        )
                    }
                )
            return MockResponse(
                {
                    "response": "\n".join(
                        [
                            "# Mock Inspection Report",
                            "",
                            "## Executive Summary",
                            "A concise automated report generated from the selected field photos.",
                            "",
                            "## Key Findings",
                            "- Mock field photo evidence was reviewed.",
                            f"- Prompt context: {prompt[:80]}",
                            "",
                            "## Recommended Actions",
                            "- Review the cited defects and validate remediation timing.",
                        ]
                    )
                }
            )
        if ":embedContent" in url:
            assert json is not None
            text_parts = json["content"]["parts"]
            joined_text = " ".join(part["text"] for part in text_parts if "text" in part)
            return MockResponse({"embedding": {"values": build_mock_embedding(joined_text)}})
        if ":generateContent" in url:
            response_mime_type = ((json or {}).get("generationConfig") or {}).get("responseMimeType")
            if response_mime_type == "text/plain":
                return MockResponse(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "text": "# Mock Inspection Report\n\n## Executive Summary\nGenerated from Gemini test backend.\n\n## Key Findings\n- Mock field photo evidence was reviewed.\n"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            return MockResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": '{"ai_summary":"Mock field photo","labels":["project","construction"],"defects":[]}'
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        raise AssertionError(f"Unexpected AI backend URL: {url}")

    monkeypatch.setattr("app.services.ai_pipeline.requests.post", fake_post)

    def fake_render_markdown_report_pdf(markdown_text: str, title: str) -> bytes:
        content = f"%PDF-1.4\n% Mock PDF\nTitle: {title}\n\n{markdown_text}\n".encode("utf-8")
        return content

    monkeypatch.setattr("app.services.reports.render_markdown_report_pdf", fake_render_markdown_report_pdf)


@pytest.fixture()
def app_context(tmp_path: Path):
    db_path = tmp_path / "kklogger.db"
    media_root = tmp_path / "kkdata"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        media_root=str(media_root),
        public_base_url="http://testserver",
        session_secret="test-secret",
        ai_min_auditable_image_bytes=0,
    )
    engine = create_engine_from_settings(settings)
    Base.metadata.create_all(engine)
    session_maker = create_session_maker(engine)

    with session_maker() as db:
        bootstrap_platform_data(db, settings)
        db.add(
            Company(
                company_id="acme",
                company_code="10001",
                company_name="Acme Utilities",
                active=True,
            )
        )
        db.add(
            Subscription(
                company_id="acme",
                plan_id=PlanCode.free,
                status=SubscriptionStatus.active,
            )
        )
        db.add(
            SystemSetting(
                key="ai_backends",
                value=json.dumps(
                    [
                        {
                            "id": "ollama-test",
                            "type": "ollama",
                            "url": "http://ai.test",
                            "model": "llava:latest",
                            "weight": 1,
                            "enabled": True,
                        }
                    ]
                ),
            )
        )
        project_primary = Project(
            company_id="default",
            project_id="P100",
            project_name="Substation Retrofit",
            client_name="Northwind Power",
            location="Tulsa, OK",
            status=ProjectStatus.active,
        )
        project_secondary = Project(
            company_id="acme",
            project_id="P200",
            project_name="Bridge Inspection",
            client_name="County Works",
            location="Wichita, KS",
            status=ProjectStatus.active,
        )
        db.add_all([project_primary, project_secondary])
        db.flush()

        employee_primary = Employee(
            company_id="default",
            employee_id="E100",
            name="Alicia Field",
            api_key="api-key-e100",
            role="worker",
            active=True,
            project_id="P100",
        )
        employee_secondary = Employee(
            company_id="acme",
            employee_id="E200",
            name="Jordan Invoice",
            api_key="api-key-e200",
            role="worker",
            active=True,
            project_id="P200",
        )
        db.add_all([employee_primary, employee_secondary])

        admin = User(
            company_id="default",
            username="admin",
            password_hash=hash_password("AdminPass123!"),
            role=UserRole.platform_super_admin,
            display_name="System Admin",
            email="admin@example.com",
            active=True,
        )
        manager = User(
            company_id="default",
            username="manager",
            password_hash=hash_password("ManagerPass123!"),
            role=UserRole.project_manager,
            display_name="Project Manager",
            email="manager@example.com",
            active=True,
        )
        client_user = User(
            company_id="default",
            username="client",
            password_hash=hash_password("ClientPass123!"),
            role=UserRole.client,
            display_name="Client User",
            email="client@example.com",
            active=True,
        )
        worker_user = User(
            company_id="default",
            username="worker",
            password_hash=hash_password("WorkerPass123!"),
            role=UserRole.worker,
            display_name="Worker Portal",
            email="worker@example.com",
            employee_id="E100",
            active=True,
        )
        db.add_all([admin, manager, client_user, worker_user])
        db.flush()

        db.add_all(
            [
                ProjectMember(project_id="P100", user_id=manager.id, role_in_project="project_manager"),
                ProjectMember(project_id="P100", user_id=client_user.id, role_in_project="client"),
            ]
        )
        db.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        yield {
            "client": client,
            "session_maker": session_maker,
            "settings": settings,
        }
