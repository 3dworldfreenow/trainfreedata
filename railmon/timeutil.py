from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_ist() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def ist_to_utc(d: date, hhmm: str, day_offset: int = 0) -> datetime | None:
    """Combine an IST calendar date + 'HH:MM[:SS]' (+ day offset) into naive UTC."""
    try:
        h, m, *_ = hhmm.split(":")
        local = datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST) + timedelta(days=day_offset)
    except (ValueError, AttributeError):
        return None
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def age_minutes(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int((utcnow() - dt).total_seconds() // 60)
