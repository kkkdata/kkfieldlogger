from __future__ import annotations

import re
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.security import generate_password, hash_password
from app.models import (
    AIAnalysisLog,
    AuditLog,
    Company,
    CompanyApplication,
    CopilotConversation,
    CopilotMessage,
    CopilotMessageSource,
    Employee,
    EvidenceObservation,
    ExpressionArtifact,
    ExpressionEvalItem,
    ExpressionEvalRun,
    FactSnapshot,
    GeneratedReport,
    IPCamera,
    Invitation,
    MediaAnnotation,
    MediaAsset,
    Membership,
    MembershipStatus,
    PasswordResetToken,
    Photo,
    PhotoComment,
    PhotoLensObservation,
    PhotoLensObservationRun,
    PhotoObservationPromotion,
    PlanCode,
    ProgressReport,
    Project,
    ProjectMember,
    ReceiptFact,
    ReviewSession,
    ReviewTask,
    ReviewTaskEvent,
    Subscription,
    SubscriptionStatus,
    TaskJob,
    Tenant,
    TenantStatus,
    UsageCounter,
    User,
    UserRole,
)
from app.core.config import Settings
from app.services.audit import log_audit
from app.services.billing import ensure_subscription

SAFE_TENANT = re.compile(r"[^a-z0-9-]+")
COMPANY_CODE_WIDTH = 5
SCOPED_IDENTIFIER_LOCAL_WIDTH = 4
DEFAULT_COMPANY_CODE = "10000"
NUMERIC_IDENTIFIER = re.compile(r"^\d+$")


def normalize_company_id(value: str) -> str:
    base = SAFE_TENANT.sub("-", value.strip().lower()).strip("-")
    return base or "company"


def generate_company_id(db: Session, requested: str) -> str:
    base = normalize_company_id(requested)
    company_id = base
    suffix = 2
    while db.scalar(select(Company).where(Company.company_id == company_id)) is not None:
        company_id = f"{base}-{suffix}"
        suffix += 1
    return company_id


def get_default_company_id(settings: Settings) -> str:
    return settings.default_company_id


def normalize_company_code(value: str) -> str:
    normalized = "".join(char for char in str(value or "").strip() if char.isdigit())
    if len(normalized) != COMPANY_CODE_WIDTH:
        raise ValueError(f"Company code must be exactly {COMPANY_CODE_WIDTH} digits")
    return normalized


def generate_company_code(db: Session, preferred: str | None = None) -> str:
    used_codes = {
        code
        for code in db.scalars(select(Company.company_code).where(Company.company_code.is_not(None)))
        if code
    }
    if preferred:
        normalized_preferred = normalize_company_code(preferred)
        if normalized_preferred not in used_codes:
            return normalized_preferred

    next_value = int(DEFAULT_COMPANY_CODE)
    while True:
        candidate = f"{next_value:0{COMPANY_CODE_WIDTH}d}"
        if candidate not in used_codes:
            return candidate
        next_value += 1


def ensure_company_code(db: Session, company: Company, preferred: str | None = None) -> str:
    existing = (company.company_code or "").strip()
    if existing:
        normalized_existing = normalize_company_code(existing)
        company.company_code = normalized_existing
        return normalized_existing
    generated = generate_company_code(db, preferred=preferred)
    company.company_code = generated
    db.add(company)
    db.flush()
    return generated


def company_code_for_company_id(db: Session, company_id: str) -> str:
    company = db.scalar(select(Company).where(Company.company_id == company_id))
    if company is None:
        raise ValueError("Company not found")
    preferred = DEFAULT_COMPANY_CODE if company.company_id == "default" else None
    return ensure_company_code(db, company, preferred=preferred)


def normalize_company_scoped_identifier(raw_value: str, *, company_code: str, kind: str) -> str:
    normalized_company_code = normalize_company_code(company_code)
    cleaned = str(raw_value or "").strip()
    if not cleaned:
        raise ValueError(f"{kind.title()} ID is required")
    if not NUMERIC_IDENTIFIER.fullmatch(cleaned):
        return cleaned
    if len(cleaned) <= SCOPED_IDENTIFIER_LOCAL_WIDTH:
        return f"{normalized_company_code}{cleaned.zfill(SCOPED_IDENTIFIER_LOCAL_WIDTH)}"
    expected_length = COMPANY_CODE_WIDTH + SCOPED_IDENTIFIER_LOCAL_WIDTH
    if len(cleaned) == expected_length and cleaned.startswith(normalized_company_code):
        return cleaned
    if len(cleaned) == expected_length:
        raise ValueError(f"{kind.title()} ID must start with company code {normalized_company_code}")
    raise ValueError(
        f"{kind.title()} ID must be a {SCOPED_IDENTIFIER_LOCAL_WIDTH}-digit local code "
        f"or a {expected_length}-digit company-scoped code"
    )


def scoped_identifier_for_company(db: Session, *, company_id: str, raw_value: str, kind: str) -> str:
    company_code = company_code_for_company_id(db, company_id)
    return normalize_company_scoped_identifier(raw_value, company_code=company_code, kind=kind)


def _delete_where_any(db: Session, model, *conditions) -> None:
    active_conditions = [condition for condition in conditions if condition is not None]
    if not active_conditions:
        return
    if len(active_conditions) == 1:
        db.execute(delete(model).where(active_conditions[0]))
        return
    db.execute(delete(model).where(or_(*active_conditions)))


def _remove_tree_within(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_root:
        return
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        return
    shutil.rmtree(resolved_target, ignore_errors=True)


def delete_company_workspace(
    db: Session,
    *,
    app_settings: Settings,
    tenant_slug: str,
    actor_user: User,
    ip_address: str | None = None,
) -> dict[str, int | str]:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    company = db.scalar(select(Company).where(Company.company_id == tenant_slug))
    if tenant is None or company is None:
        raise ValueError("Tenant not found")
    if tenant.slug == "default" or company.company_id == "default":
        raise ValueError("The default workspace cannot be deleted")
    if tenant.status != TenantStatus.suspended or company.active:
        raise ValueError("Pause the tenant before permanent deletion")

    users = list(db.scalars(select(User).where(User.company_id == company.company_id)))
    employees = list(db.scalars(select(Employee).where(Employee.company_id == company.company_id)))
    projects = list(db.scalars(select(Project).where(Project.company_id == company.company_id)))
    photos = list(db.scalars(select(Photo).where(Photo.company_id == company.company_id)))
    media_assets = list(db.scalars(select(MediaAsset).where(MediaAsset.company_id == company.company_id)))
    generated_reports = list(db.scalars(select(GeneratedReport).where(GeneratedReport.company_id == company.company_id)))
    progress_reports = list(db.scalars(select(ProgressReport).where(ProgressReport.company_id == company.company_id)))
    cameras = list(db.scalars(select(IPCamera).where(IPCamera.company_id == company.company_id)))
    conversations = list(db.scalars(select(CopilotConversation).where(CopilotConversation.company_id == company.company_id)))
    applications = list(
        db.scalars(
            select(CompanyApplication).where(
                or_(
                    CompanyApplication.approved_company_id == company.company_id,
                    CompanyApplication.requested_company_id == company.company_id,
                )
            )
        )
    )

    user_ids = [user.id for user in users]
    project_ids = [project.project_id for project in projects]
    photo_ids = [photo.id for photo in photos]
    media_asset_ids = [asset.asset_id for asset in media_assets]
    conversation_ids = [conversation.id for conversation in conversations]
    application_ids = [application.id for application in applications]
    message_ids = list(
        db.scalars(select(CopilotMessage.id).where(CopilotMessage.conversation_id.in_(conversation_ids)))
    ) if conversation_ids else []

    filesystem_targets = [
        app_settings.photos_root / company.company_id,
        app_settings.reports_root / company.company_id,
        app_settings.media_assets_root / "_annotations" / company.company_id,
        app_settings.media_assets_root / "image" / company.company_id,
        app_settings.media_assets_root / "video" / company.company_id,
        app_settings.media_cold_storage_root / company.company_id,
        app_settings.camera_clips_root / company.company_id,
    ]
    filesystem_targets.extend(app_settings.media_frames_root / asset_id for asset_id in media_asset_ids)
    filesystem_targets.extend(app_settings.media_upload_chunks_root / asset_id for asset_id in media_asset_ids)

    _delete_where_any(db, CopilotMessageSource, CopilotMessageSource.message_id.in_(message_ids) if message_ids else None)
    _delete_where_any(db, CopilotMessage, CopilotMessage.conversation_id.in_(conversation_ids) if conversation_ids else None)
    db.execute(delete(CopilotConversation).where(CopilotConversation.company_id == company.company_id))
    db.execute(delete(ReceiptFact).where(ReceiptFact.company_id == company.company_id))
    db.execute(delete(EvidenceObservation).where(EvidenceObservation.company_id == company.company_id))
    _delete_where_any(
        db,
        PhotoComment,
        PhotoComment.photo_id.in_(photo_ids) if photo_ids else None,
        PhotoComment.user_id.in_(user_ids) if user_ids else None,
    )
    _delete_where_any(
        db,
        MediaAnnotation,
        MediaAnnotation.photo_id.in_(photo_ids) if photo_ids else None,
        MediaAnnotation.media_asset_id.in_(media_asset_ids) if media_asset_ids else None,
        MediaAnnotation.user_id.in_(user_ids) if user_ids else None,
    )
    _delete_where_any(
        db,
        AIAnalysisLog,
        AIAnalysisLog.photo_id.in_(photo_ids) if photo_ids else None,
        AIAnalysisLog.media_asset_id.in_(media_asset_ids) if media_asset_ids else None,
    )
    _delete_where_any(db, PasswordResetToken, PasswordResetToken.user_id.in_(user_ids) if user_ids else None)
    _delete_where_any(
        db,
        ProjectMember,
        ProjectMember.project_id.in_(project_ids) if project_ids else None,
        ProjectMember.user_id.in_(user_ids) if user_ids else None,
    )
    _delete_where_any(db, Membership, Membership.tenant_id == tenant.slug, Membership.user_id.in_(user_ids) if user_ids else None)
    db.execute(delete(Invitation).where(Invitation.tenant_id == tenant.slug))
    # Tables added after the original teardown was written; the expression
    # tables reference task_jobs so they must be cleared before TaskJob.
    db.execute(delete(ExpressionEvalItem).where(ExpressionEvalItem.company_id == company.company_id))
    db.execute(delete(ExpressionEvalRun).where(ExpressionEvalRun.company_id == company.company_id))
    db.execute(delete(ExpressionArtifact).where(ExpressionArtifact.company_id == company.company_id))
    db.execute(delete(FactSnapshot).where(FactSnapshot.company_id == company.company_id))
    _delete_where_any(
        db,
        PhotoObservationPromotion,
        PhotoObservationPromotion.photo_id.in_(photo_ids) if photo_ids else None,
    )
    _delete_where_any(
        db,
        PhotoLensObservation,
        PhotoLensObservation.photo_id.in_(photo_ids) if photo_ids else None,
    )
    _delete_where_any(
        db,
        PhotoLensObservationRun,
        PhotoLensObservationRun.photo_id.in_(photo_ids) if photo_ids else None,
    )
    db.execute(delete(ReviewTaskEvent).where(ReviewTaskEvent.company_id == company.company_id))
    db.execute(delete(ReviewTask).where(ReviewTask.company_id == company.company_id))
    db.execute(delete(ReviewSession).where(ReviewSession.company_id == company.company_id))
    db.execute(delete(TaskJob).where(TaskJob.company_id == company.company_id))
    db.execute(delete(GeneratedReport).where(GeneratedReport.company_id == company.company_id))
    db.execute(delete(ProgressReport).where(ProgressReport.company_id == company.company_id))
    db.execute(delete(IPCamera).where(IPCamera.company_id == company.company_id))
    db.execute(delete(UsageCounter).where(UsageCounter.company_id == company.company_id))
    db.execute(delete(Subscription).where(Subscription.company_id == company.company_id))
    _delete_where_any(
        db,
        AuditLog,
        AuditLog.company_id == company.company_id,
        AuditLog.actor_user_id.in_(user_ids) if user_ids else None,
    )
    _delete_where_any(
        db,
        CompanyApplication,
        CompanyApplication.id.in_(application_ids) if application_ids else None,
    )
    db.execute(delete(MediaAsset).where(MediaAsset.company_id == company.company_id))
    db.execute(delete(Photo).where(Photo.company_id == company.company_id))

    tenant.owner_user_id = None
    db.add(tenant)
    for user in users:
        if user.employee_id is not None:
            user.employee_id = None
            db.add(user)
    db.flush()

    db.execute(delete(Employee).where(Employee.company_id == company.company_id))
    db.execute(delete(Project).where(Project.company_id == company.company_id))
    db.delete(tenant)
    db.execute(delete(User).where(User.company_id == company.company_id))

    log_audit(
        db,
        action="tenant_deleted",
        target_type="tenant",
        target_id=tenant.slug,
        actor_user_id=actor_user.id,
        company_id=actor_user.company_id,
        ip_address=ip_address,
        detail_json={
            "deleted_company_id": company.company_id,
            "deleted_company_name": company.company_name,
            "user_count": len(users),
            "employee_count": len(employees),
            "project_count": len(projects),
            "photo_count": len(photos),
            "video_count": len([asset for asset in media_assets if str(asset.media_type.value if hasattr(asset.media_type, 'value') else asset.media_type) == "video"]),
            "media_asset_count": len(media_assets),
            "generated_report_count": len(generated_reports),
            "progress_report_count": len(progress_reports),
            "camera_count": len(cameras),
            "conversation_count": len(conversations),
            "application_count": len(applications),
        },
    )
    db.delete(company)
    db.commit()

    for target in filesystem_targets:
        _remove_tree_within(app_settings.media_root_path, target)

    return {
        "tenant_slug": tenant.slug,
        "company_name": company.company_name,
        "user_count": len(users),
        "employee_count": len(employees),
        "project_count": len(projects),
        "photo_count": len(photos),
        "video_count": len([asset for asset in media_assets if str(asset.media_type.value if hasattr(asset.media_type, 'value') else asset.media_type) == "video"]),
        "media_asset_count": len(media_assets),
    }


def provision_company_workspace(
    db: Session,
    *,
    company_name: str,
    display_name: str,
    email: str,
    password: str | None = None,
    password_hash: str | None = None,
    company_id: str | None = None,
    username: str | None = None,
    plan_id: PlanCode = PlanCode.free,
    company_code: str | None = None,
    user_role: UserRole = UserRole.owner,
    tenant_settings: dict | None = None,
) -> tuple[Company, Tenant, User, str]:
    normalized_company_name = company_name.strip()
    normalized_display_name = display_name.strip()
    normalized_email = email.strip().lower()
    normalized_username = (username or normalized_email).strip().lower()
    normalized_company_id = normalize_company_id(company_id or normalized_company_name)

    if not normalized_company_name:
        raise ValueError("Company name is required")
    if not normalized_display_name:
        raise ValueError("Administrator name is required")
    if not normalized_email:
        raise ValueError("Administrator email is required")
    if not normalized_username:
        raise ValueError("Administrator username is required")

    if db.scalar(select(Company).where(Company.company_id == normalized_company_id)) is not None:
        normalized_company_id = generate_company_id(db, normalized_company_id)
    if db.scalar(select(User).where(User.email == normalized_email, User.active.is_(True))) is not None:
        raise ValueError("Account email already exists")
    if db.scalar(select(User).where(User.username == normalized_username, User.active.is_(True))) is not None:
        raise ValueError("Account username already exists")

    if password_hash:
        normalized_password_hash = password_hash.strip()
        if not normalized_password_hash:
            raise ValueError("Password hash is required")
        effective_password = ""
        stored_password_hash = normalized_password_hash
    else:
        effective_password = (password or "").strip() or generate_password(14)
        if len(effective_password) < 10:
            raise ValueError("Password must be at least 10 characters")
        stored_password_hash = hash_password(effective_password)

    company = Company(
        company_id=normalized_company_id,
        company_code=generate_company_code(db, preferred=company_code),
        company_name=normalized_company_name,
        active=True,
    )
    db.add(company)
    tenant = Tenant(
        id=str(uuid4()),
        slug=normalized_company_id,
        name=normalized_company_name,
        status=TenantStatus.active,
        current_plan_id=plan_id,
        settings_json=dict(tenant_settings or {}) or None,
    )
    db.add(tenant)
    db.flush()

    owner_user = User(
        public_id=str(uuid4()),
        company_id=company.company_id,
        username=normalized_username,
        password_hash=stored_password_hash,
        role=user_role,
        display_name=normalized_display_name,
        email=normalized_email,
        active=True,
        is_verified=False,
    )
    db.add(owner_user)
    db.flush()

    db.add(
        Membership(
            tenant_id=company.company_id,
            user_id=owner_user.id,
            role=user_role.value,
            status=MembershipStatus.active,
        )
    )
    tenant.owner_user_id = owner_user.id
    db.add(tenant)
    ensure_subscription(
        db,
        company_id=company.company_id,
        plan_id=plan_id,
        status=SubscriptionStatus.active,
    )
    db.flush()
    return company, tenant, owner_user, effective_password
