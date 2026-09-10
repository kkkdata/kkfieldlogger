from __future__ import annotations

from sqlalchemy import select
from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Company, User, UserRole
from app.services.i18n import SUPPORTED_LANGUAGES, get_language, translator
from app.services.rbac import can_manage_companies, is_client, is_manager, is_tenant_admin, is_worker
from app.services.session import ensure_csrf_token, pop_flashes
from app.services.settings import get_system_settings
from app.services.tenant import SCOPED_IDENTIFIER_LOCAL_WIDTH


def _nav_section(title: str | None, items: list[dict[str, str]]) -> dict[str, object]:
    return {"title": title, "items": items}


def build_nav_sections(user: User | None, t) -> list[dict[str, object]]:
    if user is None:
        return []

    common = [{"label": t("nav_dashboard"), "href": "/portal"}]
    manager_common = [{"label": t("nav_today"), "href": "/portal/today"}] + common
    if can_manage_companies(user):
        return [
            _nav_section(
                t("nav_section_platform_ops"),
                manager_common
                + [
                    {"label": t("nav_photos"), "href": "/portal/photos"},
                    {"label": t("nav_platform"), "href": "/portal/platform"},
                    {"label": t("nav_billing"), "href": "/portal/billing"},
                    {"label": t("nav_ai_center"), "href": "/portal/ai-center"},
                      {"label": t("nav_ai_backends"), "href": "/portal/ai-backends"},
                      {"label": t("nav_audit_logs"), "href": "/portal/audit-logs"},
                      {"label": t("nav_settings"), "href": "/portal/settings"},
                  ],
              ),
            _nav_section(
                t("nav_section_tenant_inspection"),
                [
                    {"label": t("nav_invoices"), "href": "/portal/invoices"},
                    {"label": t("nav_map"), "href": "/portal/map"},
                    {"label": t("nav_projects"), "href": "/portal/projects"},
                    {"label": t("nav_employees"), "href": "/portal/employees"},
                    {"label": t("nav_users"), "href": "/portal/users"},
                    {"label": t("nav_gallery"), "href": "/portal/gallery"},
                    {"label": t("nav_reports"), "href": "/portal/reports"},
                    {"label": t("nav_copilot"), "href": "/portal/copilot"},
                ],
            ),
            _nav_section(
                t("nav_section_account"),
                [{"label": t("nav_profile"), "href": "/portal/profile"}],
            ),
        ]
    if is_tenant_admin(user):
        return [
            _nav_section(
                t("nav_section_workspace_ops"),
                manager_common
                + [
                    {"label": t("nav_photos"), "href": "/portal/photos"},
                    {"label": t("nav_billing"), "href": "/portal/billing"},
                    {"label": t("nav_projects"), "href": "/portal/projects"},
                    {"label": t("nav_employees"), "href": "/portal/employees"},
                    {"label": t("nav_users"), "href": "/portal/users"},
                    {"label": t("nav_ai_center"), "href": "/portal/ai-center"},
                    {"label": t("nav_ai_backends"), "href": "/portal/ai-backends"},
                    {"label": t("nav_reports"), "href": "/portal/reports"},
                    {"label": t("nav_ip_cameras"), "href": "/portal/ip-cameras"},
                    {"label": t("nav_audit_logs"), "href": "/portal/audit-logs"},
                    {"label": t("nav_settings"), "href": "/portal/settings"},
                ],
            ),
            _nav_section(
                t("nav_section_field_evidence"),
                [
                    {"label": t("nav_invoices"), "href": "/portal/invoices"},
                    {"label": t("nav_map"), "href": "/portal/map"},
                    {"label": t("nav_gallery"), "href": "/portal/gallery"},
                    {"label": t("nav_copilot"), "href": "/portal/copilot"},
                ],
            ),
            _nav_section(
                t("nav_section_account"),
                [{"label": t("nav_profile"), "href": "/portal/profile"}],
            ),
        ]
    if is_manager(user):
        return [
            _nav_section(
                t("nav_section_workspace_ops"),
                manager_common
                + [
                    {"label": t("nav_photos"), "href": "/portal/photos"},
                    {"label": t("nav_billing"), "href": "/portal/billing"},
                    {"label": t("nav_projects"), "href": "/portal/projects"},
                    {"label": t("nav_employees"), "href": "/portal/employees"},
                    {"label": t("nav_ai_center"), "href": "/portal/ai-center"},
                    {"label": t("nav_reports"), "href": "/portal/reports"},
                ],
            ),
            _nav_section(
                t("nav_section_field_evidence"),
                [
                    {"label": t("nav_invoices"), "href": "/portal/invoices"},
                    {"label": t("nav_map"), "href": "/portal/map"},
                    {"label": t("nav_gallery"), "href": "/portal/gallery"},
                    {"label": t("nav_copilot"), "href": "/portal/copilot"},
                ],
            ),
            _nav_section(
                t("nav_section_account"),
                [{"label": t("nav_profile"), "href": "/portal/profile"}],
            ),
        ]
    if is_client(user):
        return [
            _nav_section(
                t("nav_section_field_evidence"),
                common
                + [
                    {"label": t("nav_gallery"), "href": "/portal/gallery"},
                    {"label": t("nav_billing"), "href": "/portal/billing"},
                    {"label": t("nav_map"), "href": "/portal/map"},
                    {"label": t("nav_projects"), "href": "/portal/projects"},
                ],
            ),
            _nav_section(
                t("nav_section_account"),
                [{"label": t("nav_profile"), "href": "/portal/profile"}],
            ),
        ]
    if is_worker(user):
        return [
            _nav_section(
                t("nav_section_field_evidence"),
                common
                + [
                    {"label": t("nav_my_uploads"), "href": "/portal/photos"},
                    {"label": t("nav_billing"), "href": "/portal/billing"},
                    {"label": t("nav_ai_center"), "href": "/portal/ai-center"},
                    {"label": t("nav_map"), "href": "/portal/map"},
                ],
            ),
            _nav_section(
                t("nav_section_account"),
                [{"label": t("nav_profile"), "href": "/portal/profile"}],
            ),
        ]
    return [
        _nav_section(
            t("nav_section_account"),
            common + [{"label": t("nav_profile"), "href": "/portal/profile"}],
        ),
    ]


def build_nav_items(user: User | None, t) -> list[dict[str, str]]:
    sections = build_nav_sections(user, t)
    return [item for section in sections for item in section["items"]]


# Surfaces hidden in a private (NAS) deployment; the matching routes are
# blocked centrally in app.main.
PRIVATE_HIDDEN_NAV_HREFS = {"/portal/platform", "/portal/billing"}
# Platform-level surfaces that tenant admins get ONLY on a private box,
# where the workspace owner is the platform (AI backend config).
PRIVATE_ONLY_TENANT_NAV_HREFS = {"/portal/ai-backends"}


def filter_nav_sections_for_profile(sections: list[dict[str, object]], app_settings) -> list[dict[str, object]]:
    private = getattr(app_settings, "is_private_deployment", False)
    filtered: list[dict[str, object]] = []
    for section in sections:
        hrefs = {item.get("href") for item in section["items"]}
        if private:
            items = [item for item in section["items"] if item.get("href") not in PRIVATE_HIDDEN_NAV_HREFS]
        elif "/portal/platform" not in hrefs:
            items = [item for item in section["items"] if item.get("href") not in PRIVATE_ONLY_TENANT_NAV_HREFS]
        else:
            items = list(section["items"])
        if items:
            filtered.append({**section, "items": items})
    return filtered


def build_workspace_profile(user: User | None, t) -> dict[str, str]:
    if user is None:
        return {"mode": "guest", "label": "", "copy": ""}
    if can_manage_companies(user):
        return {
            "mode": "platform",
            "label": t("workspace_mode_platform_label"),
            "copy": t("workspace_mode_platform_copy"),
        }
    if user.role in {UserRole.owner, UserRole.admin}:
        return {
            "mode": "company",
            "label": t("workspace_mode_company_label"),
            "copy": t("workspace_mode_company_copy"),
        }
    if user.role == UserRole.manager:
        return {
            "mode": "finance",
            "label": t("workspace_mode_finance_label"),
            "copy": t("workspace_mode_finance_copy"),
        }
    if user.role == UserRole.project_manager:
        return {
            "mode": "project",
            "label": t("workspace_mode_project_label"),
            "copy": t("workspace_mode_project_copy"),
        }
    if is_worker(user):
        return {
            "mode": "worker",
            "label": t("workspace_mode_worker_label"),
            "copy": t("workspace_mode_worker_copy"),
        }
    if is_client(user):
        return {
            "mode": "client",
            "label": t("workspace_mode_client_label"),
            "copy": t("workspace_mode_client_copy"),
        }
    return {"mode": "general", "label": "", "copy": ""}


def build_template_context(
    request: Request,
    db: Session,
    app_settings: Settings,
    current_user: User | None,
    **context,
) -> dict:
    system_settings = get_system_settings(db, app_settings)
    current_language = get_language(request)
    t = translator(current_language)
    workspace_profile = build_workspace_profile(current_user, t)
    public_language_suffix = "" if current_language == "en" else f"?lang={current_language}"
    company_code = ""
    scoped_example = ""
    if current_user is not None:
        company = db.scalar(select(Company).where(Company.company_id == current_user.company_id))
        if company is not None and company.company_code:
            company_code = company.company_code
            scoped_example = f"{company_code}{1:0{SCOPED_IDENTIFIER_LOCAL_WIDTH}d}"
    return {
        "request": request,
        "app_settings": app_settings,
        "system_settings": system_settings,
        "current_user": current_user,
        "workspace_profile": workspace_profile,
        "company_code": company_code,
        "company_scoped_example": scoped_example,
        "nav_sections": filter_nav_sections_for_profile(build_nav_sections(current_user, t), app_settings),
        "nav_items": [
            item
            for section in filter_nav_sections_for_profile(build_nav_sections(current_user, t), app_settings)
            for item in section["items"]
        ],
        "flashes": pop_flashes(request),
        "csrf_token": ensure_csrf_token(request),
        "timezone_name": system_settings.get("timezone", app_settings.default_timezone),
        "current_language": current_language,
        "language_options": SUPPORTED_LANGUAGES,
        "portal_help_links": [
            {"label": t("nav_public_site"), "href": f"/{public_language_suffix}"},
            {"label": t("nav_guide"), "href": f"/guide{public_language_suffix}"},
            {"label": t("nav_updates"), "href": f"/updates{public_language_suffix}"},
            {"label": t("nav_privacy"), "href": f"/privacy{public_language_suffix}"},
            {"label": t("nav_support"), "href": f"/support{public_language_suffix}"},
        ],
        "t": t,
        **context,
    }
