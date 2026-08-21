from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.errors import MobileApiError

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Plan, PlanCode, Subscription, SubscriptionStatus, UsageCounter

PLAN_DEFINITIONS = (
    {
        "plan_id": PlanCode.free,
        "display_name": "Free",
        "monthly_price_cents": 0,
        "monthly_photo_limit": 62,
        "daily_upload_limit": 2,
        "storage_limit_mb": 512,
    },
    {
        "plan_id": PlanCode.starter,
        "display_name": "Starter",
        "monthly_price_cents": 10,
        "monthly_photo_limit": 620,
        "daily_upload_limit": 20,
        "storage_limit_mb": 2048,
    },
    {
        "plan_id": PlanCode.business,
        "display_name": "Business",
        "monthly_price_cents": 1000,
        "monthly_photo_limit": 1000000,
        "daily_upload_limit": 1000000,
        "storage_limit_mb": 51200,
    },
    {
        "plan_id": PlanCode.basic,
        "display_name": "Basic (Legacy)",
        "monthly_price_cents": 900,
        "monthly_photo_limit": 5000,
        "daily_upload_limit": 200,
        "storage_limit_mb": 10240,
    },
    {
        "plan_id": PlanCode.pro,
        "display_name": "Pro (Legacy)",
        "monthly_price_cents": 2900,
        "monthly_photo_limit": 50000,
        "daily_upload_limit": 5000,
        "storage_limit_mb": 102400,
    },
)


def month_key_for_timezone(timezone_name: str) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    return now.strftime("%Y-%m")


def usage_date_for_timezone(timezone_name: str) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    return now.strftime("%Y-%m-%d")


def ensure_plans(db: Session) -> None:
    for definition in PLAN_DEFINITIONS:
        existing = db.scalar(select(Plan).where(Plan.plan_id == definition["plan_id"]))
        if existing is not None:
            continue
        db.add(Plan(**definition, active=True))
    db.flush()


def ensure_subscription(db: Session, *, company_id: str, plan_id: PlanCode, status: SubscriptionStatus) -> Subscription:
    subscription = db.scalar(select(Subscription).where(Subscription.company_id == company_id))
    if subscription is not None:
        return subscription
    subscription = Subscription(company_id=company_id, plan_id=plan_id, status=status)
    db.add(subscription)
    db.flush()
    return subscription


def get_company_subscription(db: Session, company_id: str) -> Subscription:
    subscription = db.scalar(select(Subscription).where(Subscription.company_id == company_id))
    if subscription is None:
        raise HTTPException(status_code=503, detail="Subscription is not configured")
    return subscription


def get_plan(db: Session, plan_id: PlanCode) -> Plan:
    plan = db.scalar(select(Plan).where(Plan.plan_id == plan_id, Plan.active.is_(True)))
    if plan is None:
        raise HTTPException(status_code=503, detail="Plan is not configured")
    return plan


def _reset_daily_usage_if_needed(db: Session, *, counter: UsageCounter, usage_date: str) -> UsageCounter:
    if counter.usage_date == usage_date:
        return counter
    counter.usage_date = usage_date
    counter.uploaded_count = 0
    db.add(counter)
    db.flush()
    return counter


def get_usage_counter(db: Session, *, company_id: str, month_key: str, usage_date: str) -> UsageCounter:
    counter = db.scalar(
        select(UsageCounter).where(
            UsageCounter.company_id == company_id,
            UsageCounter.month_key == month_key,
        )
    )
    if counter is not None:
        return _reset_daily_usage_if_needed(db, counter=counter, usage_date=usage_date)

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            pg_insert(UsageCounter)
            .values(
                company_id=company_id,
                month_key=month_key,
                usage_date=usage_date,
                photos_this_month=0,
                uploaded_count=0,
                storage_used=0,
                updated_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=[UsageCounter.company_id, UsageCounter.month_key],
                set_={"updated_at": datetime.now(timezone.utc)},
            )
        )
        db.flush()
        counter = db.scalar(
            select(UsageCounter).where(
                UsageCounter.company_id == company_id,
                UsageCounter.month_key == month_key,
            )
        )
        if counter is None:
            raise HTTPException(status_code=503, detail="Usage counter is unavailable")
        return _reset_daily_usage_if_needed(db, counter=counter, usage_date=usage_date)

    counter = UsageCounter(
        company_id=company_id,
        month_key=month_key,
        usage_date=usage_date,
        photos_this_month=0,
        uploaded_count=0,
        storage_used=0,
    )
    db.add(counter)
    db.flush()
    return counter


def get_company_usage_summary(db: Session, *, company_id: str, timezone_name: str) -> dict[str, int | str]:
    month_key = month_key_for_timezone(timezone_name)
    usage_date = usage_date_for_timezone(timezone_name)
    subscription = get_company_subscription(db, company_id)
    plan = get_plan(db, subscription.plan_id)
    counter = get_usage_counter(db, company_id=company_id, month_key=month_key, usage_date=usage_date)
    return {
        "month_key": month_key,
        "usage_date": usage_date,
        "photos_this_month": counter.photos_this_month,
        "uploaded_count": counter.uploaded_count,
        "storage_used": counter.storage_used,
        "plan_id": plan.plan_id.value,
        "monthly_photo_limit": plan.monthly_photo_limit,
        "daily_upload_limit": plan.daily_upload_limit,
        "storage_limit_mb": plan.storage_limit_mb,
    }


def _seconds_until_local_midnight(timezone_name: str) -> int:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


def enforce_upload_limit(db: Session, *, company_id: str, timezone_name: str) -> UsageCounter:
    month_key = month_key_for_timezone(timezone_name)
    usage_date = usage_date_for_timezone(timezone_name)
    subscription = get_company_subscription(db, company_id)
    if subscription.status != SubscriptionStatus.active:
        raise MobileApiError(
            status_code=403,
            code="SUBSCRIPTION_INACTIVE",
            message="Subscription is not active",
            message_zh="公司订阅已停用，请联系公司管理员",
            message_es="La suscripción de la empresa está inactiva",
            retryable=False,
        )
    plan = get_plan(db, subscription.plan_id)
    counter = get_usage_counter(db, company_id=company_id, month_key=month_key, usage_date=usage_date)
    if counter.uploaded_count >= plan.daily_upload_limit:
        # Retryable with Retry-After: the offline queue should keep the photo
        # and try again after the daily counter resets, not abort the sync.
        raise MobileApiError(
            status_code=403,
            code="QUOTA_EXCEEDED",
            message="Daily upload limit reached",
            message_zh=f"今日上传额度（{plan.daily_upload_limit} 张）已用完，照片将在明天自动重传",
            message_es="Se alcanzó el límite diario de subidas, se reintentará mañana",
            retryable=True,
            retry_after_seconds=_seconds_until_local_midnight(timezone_name),
        )
    return counter


def record_upload_usage(db: Session, *, company_id: str, timezone_name: str, file_size: int = 0) -> UsageCounter:
    counter = get_usage_counter(
        db,
        company_id=company_id,
        month_key=month_key_for_timezone(timezone_name),
        usage_date=usage_date_for_timezone(timezone_name),
    )
    counter.photos_this_month += 1
    counter.uploaded_count += 1
    counter.storage_used += file_size
    db.add(counter)
    return counter
