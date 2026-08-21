from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc_timestamp(value: str | datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_local_display(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "-"
    local_value = value.astimezone(ZoneInfo(timezone_name))
    return f"{local_value.strftime('%Y-%m-%d %H:%M:%S')} {timezone_name}"


def build_date_range(
    start_date: str | None,
    end_date: str | None,
    timezone_name: str,
) -> tuple[datetime | None, datetime | None]:
    zone = ZoneInfo(timezone_name)
    start_dt = None
    end_dt = None
    if start_date:
        start_local = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=zone)
        start_dt = start_local.astimezone(timezone.utc)
    if end_date:
        end_local = datetime.combine(date.fromisoformat(end_date), time.min, tzinfo=zone) + timedelta(days=1)
        end_dt = end_local.astimezone(timezone.utc)
    return start_dt, end_dt

