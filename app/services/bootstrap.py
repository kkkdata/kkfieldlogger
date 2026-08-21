from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Company, Membership, MembershipStatus, PlanCode, SubscriptionStatus, Tenant, TenantStatus, User
from app.services.billing import ensure_plans, ensure_subscription
from app.services.tenant import DEFAULT_COMPANY_CODE, ensure_company_code


def bootstrap_platform_data(db: Session, settings: Settings) -> None:
    company = db.scalar(select(Company).where(Company.company_id == settings.default_company_id))
    if company is None:
        db.add(
            Company(
                company_id=settings.default_company_id,
                company_code=DEFAULT_COMPANY_CODE,
                company_name=settings.default_company_name,
                active=True,
            )
        )
        db.flush()
        company = db.scalar(select(Company).where(Company.company_id == settings.default_company_id))

    companies = list(db.scalars(select(Company).order_by(Company.id.asc())))
    for company in companies:
        ensure_company_code(
            db,
            company,
            preferred=DEFAULT_COMPANY_CODE if company.company_id == settings.default_company_id else None,
        )

    ensure_plans(db)
    ensure_subscription(
        db,
        company_id=settings.default_company_id,
        plan_id=PlanCode.business,
        status=SubscriptionStatus.active,
    )
    companies = list(db.scalars(select(Company)))
    for company in companies:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == company.company_id))
        if tenant is None:
            db.add(
                Tenant(
                    id=str(uuid4()),
                    slug=company.company_id,
                    name=company.company_name,
                    status=TenantStatus.active if company.active else TenantStatus.suspended,
                    current_plan_id=PlanCode.business if company.company_id == settings.default_company_id else None,
                )
            )
    db.flush()

    users = list(db.scalars(select(User)))
    for user in users:
        if user.public_id is None:
            user.public_id = str(uuid4())
        if user.updated_at is None:
            user.updated_at = user.created_at
        membership = db.scalar(
            select(Membership).where(Membership.tenant_id == user.company_id, Membership.user_id == user.id)
        )
        if membership is None:
            db.add(
                Membership(
                    tenant_id=user.company_id,
                    user_id=user.id,
                    role=user.role.value,
                    status=MembershipStatus.active,
                )
            )
    db.commit()
