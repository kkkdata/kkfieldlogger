from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user_optional, get_db, get_settings
from app.core.config import Settings
from app.core.security import hash_password, verify_password
from app.models import Company, CompanyApplication, CompanyApplicationStatus, User
from app.services.auth_tokens import consume_password_reset_token, create_password_reset_token
from app.services.audit import log_audit
from app.services.bootstrap import bootstrap_platform_data
from app.services.email import send_email
from app.services.login_throttle import check_login_rate_limit, clear_failed_logins, record_failed_login
from app.services.session import login_user, logout_user
from app.services.tenant import provision_company_workspace

router = APIRouter()
PUBLIC_DIR = Path(__file__).resolve().parents[2] / "static" / "public"
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _public_file(filename: str) -> FileResponse:
    media_type = "text/html; charset=utf-8" if filename.endswith(".html") else None
    return FileResponse(PUBLIC_DIR / filename, media_type=media_type)


def _setup_needed(db: Session, settings: Settings) -> bool:
    return settings.is_private_deployment and db.scalar(select(User.id).limit(1)) is None


def _setup_page_html(error: str | None = None) -> str:
    banner = f'<div class="banner visible error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>初始化 | KK Field Logger</title>
<link rel="icon" href="/static/img/kk-logo.png"><link rel="stylesheet" href="/site.css">
</head>
<body>
<div class="public-shell">
  <main class="plain-page">
    <p class="eyebrow">首次设置 / First-run setup</p>
    <h1>欢迎使用 KK Field Logger</h1>
    <p class="lead">花一分钟创建你的公司工作区和管理员账号。此页面只在系统初始化前可用。<br>
    Create your company workspace and admin account. This page is only available before initialization.</p>
    <section class="plain-card">
      {banner}
      <form method="post" action="/setup">
        <label><span>公司名称 / Company name</span>
          <input type="text" name="company_name" required maxlength="120"></label>
        <label><span>管理员姓名 / Admin name</span>
          <input type="text" name="display_name" required maxlength="120"></label>
        <label><span>管理员邮箱 / Admin email</span>
          <input type="email" name="email" required></label>
        <label><span>管理员用户名 / Admin username</span>
          <input type="text" name="username" required minlength="3" maxlength="60"></label>
        <label><span>密码（至少 10 位）/ Password (10+ chars)</span>
          <input type="password" name="password" required minlength="10"></label>
        <label><span>确认密码 / Confirm password</span>
          <input type="password" name="password_confirm" required minlength="10"></label>
        <button class="btn-primary submit-btn" type="submit">创建工作区 / Create workspace</button>
      </form>
    </section>
  </main>
</div>
</body>
</html>"""


@router.get("/setup", response_class=HTMLResponse)
def setup_page(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _setup_needed(db, settings):
        return _redirect("/login")
    return HTMLResponse(_setup_page_html())


@router.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    company_name: str = Form(...),
    display_name: str = Form(...),
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _setup_needed(db, settings):
        return _redirect("/login")
    if password != password_confirm:
        return HTMLResponse(_setup_page_html("两次输入的密码不一致 / Passwords do not match"), status_code=400)
    if len(password.strip()) < 10:
        return HTMLResponse(_setup_page_html("密码至少 10 位 / Password must be at least 10 characters"), status_code=400)
    try:
        company, _tenant, owner, _ = provision_company_workspace(
            db,
            company_name=company_name.strip(),
            display_name=display_name.strip(),
            email=email.strip().lower(),
            password=password.strip(),
            username=username.strip().lower(),
        )
    except ValueError as exc:
        db.rollback()
        return HTMLResponse(_setup_page_html(str(exc)), status_code=400)
    log_audit(
        db,
        action="private_setup_completed",
        target_type="company",
        target_id=company.company_id,
        actor_user_id=owner.id,
        detail_json={"company_name": company.company_name, "owner_username": owner.username},
        ip_address=request.client.host if request.client else None,
        company_id=company.company_id,
    )
    db.commit()
    return _redirect("/login?setup=done")


@router.get("/pricing")
def pricing_page():
    return _public_file("pricing.html")


@router.get("/plans")
def plans_alias():
    return _redirect("/pricing")


@router.get("/signup")
def signup_alias():
    return _redirect("/register")


@router.get("/apply")
def apply_alias():
    return _redirect("/register")


@router.get("/company/apply")
def company_apply_alias():
    return _redirect("/register")


@router.get("/favicon.ico")
def favicon():
    icon_path = PUBLIC_DIR / "favicon.ico"
    if not icon_path.exists():
        icon_path = Path(__file__).resolve().parents[2] / "static" / "img" / "kk-logo.png"
    return FileResponse(icon_path)


@router.get("/site.css")
def site_styles():
    return _public_file("site.css")


@router.get("/site.js")
def site_scripts():
    return _public_file("site.js")


@router.get("/register")
def register_page():
    return _public_file("register.html")


@router.get("/login")
def login_page(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if _setup_needed(db, settings):
        return _redirect("/setup")
    return _public_file("login.html")


@router.get("/about")
def about_page():
    return _public_file("about.html")


@router.get("/guide")
def guide_page():
    return _public_file("guide.html")


@router.get("/updates")
def updates_page():
    return _public_file("updates.html")


@router.get("/privacy")
def privacy_page():
    return _public_file("privacy.html")


@router.get("/terms")
def terms_page():
    return _public_file("terms.html")


@router.get("/support")
def support_page():
    return _public_file("support.html")


@router.get("/private-deployment")
def private_deployment_page():
    return _public_file("private-deployment.html")


@router.get("/delete-account")
def delete_account_page():
    return _public_file("delete-account.html")


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    token: str = Query(""),
    error: str = Query(""),
):
    return templates.TemplateResponse(
        request,
        "pages/auth/reset_password.html",
        {
            "request": request,
            "token": token,
            "error": error,
        },
    )


@router.post("/auth/register-company")
def register_company(
    request: Request,
    company_name: str = Form(...),
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    account_type: str | None = Form(None),
    company_id: str | None = Form(None),
    company_code: str | None = Form(None),
    username: str | None = Form(None),
    contact_phone: str | None = Form(None),
    contact_title: str | None = Form(None),
    website_url: str | None = Form(None),
    primary_use_case: str | None = Form(None),
    expected_users: str | None = Form(None),
    expected_projects: str | None = Form(None),
    company_intro: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    bootstrap_platform_data(db, settings)
    normalized_email = email.strip().lower()
    normalized_username = (username or normalized_email).strip().lower()
    normalized_intro = (company_intro or "").strip()
    normalized_account_type = "personal" if (account_type or "").strip().lower() == "personal" else "company"
    application_details = [
        ("Account type", normalized_account_type),
        ("Primary use case", primary_use_case),
        ("Expected users", expected_users),
        ("Expected active projects", expected_projects),
    ]
    detail_lines = [f"{label}: {str(value).strip()}" for label, value in application_details if str(value or "").strip()]
    if detail_lines:
        normalized_intro = f"{normalized_intro}\n\nApplication details:\n" + "\n".join(detail_lines)
    if len(password.strip()) < 10:
        return _redirect("/register?error=password_too_short")
    if not normalized_intro:
        return _redirect("/register?error=company_intro_required")
    if db.scalar(select(User).where(User.email == normalized_email, User.active.is_(True))) is not None:
        return _redirect("/register?error=account_exists")
    if db.scalar(select(User).where(User.username == normalized_username, User.active.is_(True))) is not None:
        return _redirect("/register?error=account_exists")
    if db.scalar(
        select(CompanyApplication).where(
            or_(
                CompanyApplication.contact_email == normalized_email,
                CompanyApplication.requested_username == normalized_username,
            ),
            CompanyApplication.status == CompanyApplicationStatus.pending,
        )
    ) is not None:
        return _redirect("/register?error=application_pending")
    application = CompanyApplication(
        public_id=str(uuid4()),
        company_name=company_name.strip(),
        requested_company_id=(company_id or "").strip() or None,
        requested_company_code=(company_code or "").strip() or None,
        contact_name=display_name.strip(),
        contact_email=normalized_email,
        contact_phone=(contact_phone or "").strip() or None,
        contact_title=(contact_title or "").strip() or None,
        website_url=(website_url or "").strip() or None,
        company_intro=normalized_intro,
        requested_username=normalized_username or None,
        password_hash=hash_password(password.strip()),
        status=CompanyApplicationStatus.pending,
    )
    db.add(application)
    # Deliberately no email on submission: the applicant is only emailed
    # after a human approves or rejects in the platform review queue.
    log_audit(
        db,
        action="company_application_submitted",
        target_type="company_application",
        target_id=str(application.id),
        actor_user_id=None,
        detail_json={
            "company_name": application.company_name,
            "requested_company_id": application.requested_company_id,
            "requested_company_code": application.requested_company_code,
            "contact_email": application.contact_email,
        },
        ip_address=request.client.host if request.client else None,
        company_id=None,
    )
    db.commit()
    return _redirect("/register?submitted=application_received")


@router.post("/auth/login")
def auth_login(
    request: Request,
    identifier: str | None = Form(None),
    username: str | None = Form(None),
    password: str = Form(...),
    next_url: str = Form("/portal"),
    db: Session = Depends(get_db),
):
    lookup = (identifier or username or "").strip().lower()
    check_login_rate_limit(request, lookup)
    user = db.scalar(select(User).where(or_(User.username == lookup, User.email == lookup)))
    company = db.scalar(select(Company).where(Company.company_id == user.company_id)) if user else None
    if user is None or not user.active or company is None or not company.active or not verify_password(password, user.password_hash):
        record_failed_login(request, lookup)
        log_audit(
            db,
            action="auth_login_failed",
            target_type="user",
            target_id=str(user.id) if user else None,
            detail_json={"identifier": lookup},
            ip_address=request.client.host if request.client else None,
            company_id=user.company_id if user else None,
        )
        db.commit()
        return _redirect("/login?error=invalid_credentials")

    clear_failed_logins(request, lookup)
    login_user(request, user.id)
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    log_audit(
        db,
        action="auth_login",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        ip_address=request.client.host if request.client else None,
        company_id=user.company_id,
    )
    db.commit()
    return _redirect(next_url or "/portal")


@router.post("/auth/logout")
def auth_logout(
    request: Request,
    next_url: str = Form("/login"),
    current_user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if current_user is not None:
        log_audit(
            db,
            action="auth_logout",
            target_type="user",
            target_id=str(current_user.id),
            actor_user_id=current_user.id,
            ip_address=request.client.host if request.client else None,
            company_id=current_user.company_id,
        )
        db.commit()
    logout_user(request)
    return _redirect(next_url or "/login")


@router.post("/auth/forgot-password")
def forgot_password(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower(), User.active.is_(True)))
    if user is not None:
        token = create_password_reset_token(db, user=user, settings=settings)
        reset_url = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
        send_email(
            db,
            settings=settings,
            to_email=user.email or email,
            subject="Reset your KK Field Logger password",
            html_content=(
                "<p>A password reset was requested for your account.</p>"
                f"<p><a href=\"{reset_url}\">Reset password</a></p>"
            ),
            action="password_reset_email_sent",
            company_id=user.company_id,
        )
        log_audit(
            db,
            action="password_reset_requested",
            target_type="user",
            target_id=str(user.id),
            actor_user_id=user.id,
            ip_address=request.client.host if request.client else None,
            company_id=user.company_id,
        )
        db.commit()
    return _redirect("/login?reset=requested")


@router.post("/auth/reset-password")
def reset_password(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if len(new_password) < 10:
        return _redirect(f"/reset-password?token={token}&error=password_too_short")

    user = consume_password_reset_token(db, token)
    if user is None:
        return _redirect("/reset-password?error=invalid_or_expired")

    user.password_hash = hash_password(new_password)
    db.add(user)
    log_audit(
        db,
        action="password_reset_completed",
        target_type="user",
        target_id=str(user.id),
        actor_user_id=user.id,
        ip_address=request.client.host if request.client else None,
        company_id=user.company_id,
    )
    db.commit()
    return _redirect("/login?reset=success")
