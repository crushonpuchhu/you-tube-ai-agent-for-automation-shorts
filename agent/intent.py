"""Natural-language publishing instructions from a video caption or chat message.

Understands things like:
  "Upload this Short now."
  "Schedule this video for tomorrow at 6:30 PM IST."
  "Find a suitable publishing time for this video and schedule it."
  "Schedule this video for the best available time based on my channel's performance."
  "Upload this video immediately and track its performance."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from agent.clock import ensure_ist, now_ist

MODE_NOW = "now"
MODE_BEST = "best"
MODE_WHEN = "when"
MODE_NONE = "none"

_BEST_MARKERS = (
    "best time", "best available", "best slot", "best time slot", "suitable time",
    "good time", "recommended time", "right time", "optimal time", "optimal time slot",
    "auto schedule", "auto-schedule", "schedule for me", "channel performance",
    "based on my channel", "analytics", "peak time", "peak hour", "whenever is best",
    "find a suitable", "find a good time", "suggest a time", "recommend a time",
)
_NOW_MARKERS = (
    "now", "immediately", "immediate", "right away", "asap", "asap.", "right now",
    "publish this", "upload this", "post this", "go live with it", "today itself",
)
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::|\.)?(\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE
)
_24H_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RELATIVE_RE = re.compile(r"\bin\s+(\d+)\s*(hours?|hrs?|minutes?|mins?)\b", re.IGNORECASE)


@dataclass
class PublishIntent:
    mode: str = MODE_NONE          # now | best | when | none
    at: datetime | None = None     # explicit IST datetime (mode == when)
    day: date | None = None        # preferred day when no explicit time
    track: bool = False            # "track its performance"
    matched: str = ""              # what triggered the decision
    notes: list[str] = field(default_factory=list)

    @property
    def is_explicit(self) -> bool:
        return self.mode in (MODE_NOW, MODE_BEST, MODE_WHEN)


def _parse_time(text: str) -> time | None:
    match = _TIME_RE.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower().replace(".", "")
        if meridiem.startswith("p") and hour != 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
        return None

    match = _24H_RE.search(text)
    if match:
        return time(int(match.group(1)), int(match.group(2)))
    return None


def _parse_day(text: str, now: datetime) -> date | None:
    lowered = text.lower()
    if "tomorrow" in lowered:
        return (now + timedelta(days=1)).date()
    if "today" in lowered or "tonight" in lowered:
        return now.date()
    if "day after tomorrow" in lowered:
        return (now + timedelta(days=2)).date()

    for word, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            delta = (weekday - now.weekday()) % 7
            target = now + timedelta(days=delta)
            when = _parse_time(text)
            # "monday at 9am" said on Monday before 9am still means today.
            if target.date() == now.date() and when and when < now.time():
                target += timedelta(days=7)
            return target.date()
    return None


def parse_intent(text: str | None) -> PublishIntent:
    """Extract a publishing instruction from free text (English, IST)."""
    intent = PublishIntent()
    if not text or not text.strip():
        return intent

    raw = text.strip()
    lowered = raw.lower()
    intent.track = bool(re.search(r"\btrack (its|the|performance|stats|analytics)", lowered))
    intent.matched = raw

    # 1) Best-time requests take priority ("schedule ... best available time ...")
    for marker in _BEST_MARKERS:
        if marker in lowered:
            intent.mode = MODE_BEST
            intent.day = _parse_day(lowered, now_ist())
            intent.matched = marker
            return intent

    # 2) Explicit relative time ("in 2 hours")
    match = _RELATIVE_RE.search(lowered)
    if match and any(word in lowered for word in ("schedule", "publish", "upload", "post", "at")):
        amount = int(match.group(1))
        delta = timedelta(hours=amount if match.group(2).lower().startswith("h") else 0,
                          minutes=0 if match.group(2).lower().startswith("h") else amount)
        intent.mode = MODE_WHEN
        intent.at = now_ist() + delta
        intent.matched = match.group(0)
        return intent

    # 3) Explicit day/time ("tomorrow at 6:30 PM", "today 20:00", "on monday 9 am")
    day = _parse_day(lowered, now_ist())
    when = _parse_time(lowered)
    if day is not None:
        intent.day = day
        if when is not None:
            candidate = datetime.combine(day, when, tzinfo=now_ist().tzinfo)
            intent.mode = MODE_WHEN
            intent.at = ensure_ist(candidate)
            intent.matched = "day+time"
            return intent
        if re.search(r"\b(schedule|publish|upload|post|at)\b", lowered):
            intent.mode = MODE_BEST  # day chosen, time left to the scheduler
            intent.matched = "day"
            return intent
    elif when is not None and re.search(r"\b(at|by|around)\b", lowered):
        candidate = datetime.combine(now_ist().date(), when, tzinfo=now_ist().tzinfo)
        if candidate <= now_ist():
            candidate += timedelta(days=1)
        intent.mode = MODE_WHEN
        intent.at = ensure_ist(candidate)
        intent.matched = "time"
        return intent

    # 4) Immediate publishing
    for marker in _NOW_MARKERS:
        if re.search(rf"\b{re.escape(marker.rstrip('.'))}\b", lowered):
            intent.mode = MODE_NOW
            intent.matched = marker
            return intent

    # 5) Bare "schedule ..." with no parsable time -> let the engine choose
    if re.search(r"\bschedule\b", lowered):
        intent.mode = MODE_BEST
        intent.matched = "schedule"
        intent.notes.append("No time given — a recommended slot will be chosen.")
        return intent

    return intent


def describe_intent(intent: PublishIntent) -> str:
    if intent.mode == MODE_NOW:
        return "🚀 Publish immediately"
    if intent.mode == MODE_WHEN and intent.at:
        return f"⏰ Scheduled for {intent.at.strftime('%a %d %b, %I:%M %p IST').lstrip('0')}"
    if intent.mode == MODE_BEST:
        day = intent.day.strftime("%a %d %b") if intent.day else "best available day"
        return f"🧠 Best available time ({day})"
    return "❓ No publishing instruction in the caption"
