"""IST-aware time helpers. All scheduling and daily-goal logic uses Asia/Kolkata."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def now_ist() -> datetime:
    return datetime.now(IST)


def today_key(dt: datetime | None = None) -> str:
    """IST calendar date as YYYY-MM-DD (used as the daily-goal key)."""
    return (dt or now_ist()).strftime("%Y-%m-%d")


def ensure_ist(dt: datetime) -> datetime:
    """Naive datetimes are treated as IST; aware ones are converted to IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def to_iso(dt: datetime) -> str:
    return ensure_ist(dt).isoformat()


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return ensure_ist(dt)


def now_iso() -> str:
    return to_iso(now_ist())


def seconds_until(dt: datetime) -> float:
    return (ensure_ist(dt) - now_ist()).total_seconds()


def hhmm_slots(raw: str) -> list[str]:
    """Parse '20:30,22:30' into ['20:30', '22:30'] (validating each)."""
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            datetime.strptime(part, "%H:%M")
        except ValueError:
            continue
        out.append(part)
    return out


def next_weekday(base: datetime, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Next occurrence of a weekday (0=Monday) at hour:minute, after `base`."""
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (weekday - base.weekday()) % 7
    candidate += timedelta(days=delta)
    if candidate <= base:
        candidate += timedelta(days=7)
    return candidate
