"""Intelligent, IST-based publishing-time recommendations.

Two evidence sources, always labelled so the user knows which one was used:

* "channel"  — this channel's own published-video performance (YouTube Analytics)
* "research" — published industry guidance for Indian/IST audiences, used only
               when there is not enough channel data yet.

No single time is hard-coded as universally optimal: candidate windows are
filtered by the daily goal, spacing between videos, and what is still in the future.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.clock import ensure_ist, now_ist
from agent.db import Database

log = logging.getLogger(__name__)

# (start_hour, end_hour, label, why) — general IST/India Shorts guidance,
# used only when the channel has no data of its own yet.
RESEARCH_WINDOWS = [
    (7, 9, "Morning commute", "Indian audiences scroll heavily on the way to work/college."),
    (12, 14, "Lunch break", "Strong mid-day check-in window for short-form content."),
    (18, 21, "Evening prime", "Highest overall short-form consumption window in India."),
    (21, 23, "Night scroll", "Long wind-down scrolling session before sleep."),
]

WEEKEND_WINDOWS = [
    (9, 11, "Lazy weekend morning", "Weekend mornings start later and scroll longer."),
    (15, 17, "Afternoon break", "Free-time browsing during the weekend afternoon."),
    (19, 22, "Weekend evening", "Peak leisure scrolling on Saturday/Sunday nights."),
]

MIN_SAMPLES_CHANNEL_DATA = 5  # videos needed before we trust our own data


@dataclass
class Slot:
    run_at: datetime
    source: str                 # "channel" | "research"
    label: str
    reason: str
    confidence: str             # high | medium | low
    alternatives: list[datetime] = field(default_factory=list)

    @property
    def source_line(self) -> str:
        if self.source == "channel":
            return "📈 <b>Built from your channel's own performance data</b>"
        return "📌 <b>General recommendation</b> — your channel doesn't have enough history yet"


def _hours_booked(db: Database) -> list[datetime]:
    """Datetimes already used by published or scheduled videos."""
    booked: list[datetime] = []
    for status in ("published", "scheduled", "uploading"):
        for video in db.list_videos(status=status, limit=200):
            for stamp in (video.get("published_at"), video.get("scheduled_for")):
                if stamp:
                    try:
                        booked.append(ensure_ist(datetime.fromisoformat(stamp)))
                    except ValueError:
                        pass
    for task in db.list_tasks(status="pending", limit=100):
        if task.get("kind") == "publish_video":
            try:
                booked.append(ensure_ist(datetime.fromisoformat(task["run_at"])))
            except ValueError:
                pass
    return booked


def channel_hour_performance(db: Database) -> dict[int, dict]:
    """{publish_hour: {"avg_views": float, "n": int}} from our own published Shorts."""
    latest = db.latest_stats()
    by_hour: dict[int, list[float]] = {}
    for video in db.list_videos(status="published", limit=100):
        if not video.get("youtube_id"):
            continue
        stats = latest.get(video["youtube_id"])
        if not stats:
            continue
        try:
            published = ensure_ist(datetime.fromisoformat(video["published_at"]))
        except (ValueError, TypeError):
            continue
        views = float(stats.get("views") or 0)
        if views <= 0:
            continue
        by_hour.setdefault(published.hour, []).append(views)

    return {
        hour: {"avg_views": sum(values) / len(values), "n": len(values)}
        for hour, values in by_hour.items()
    }


def _candidate_hours(db: Database) -> tuple[list[int], str]:
    performance = channel_hour_performance(db)
    total_samples = sum(v["n"] for v in performance.values())

    if total_samples >= MIN_SAMPLES_CHANNEL_DATA and performance:
        ordered = sorted(performance.items(), key=lambda kv: kv[1]["avg_views"], reverse=True)
        hours = [hour for hour, _ in ordered]
        log.info("Scheduling from channel data (%d videos, %d hour buckets)",
                 total_samples, len(hours))
        return hours, "channel"

    windows = WEEKEND_WINDOWS if now_ist().weekday() >= 5 else RESEARCH_WINDOWS
    hours: list[int] = []
    for start, end, _label, _why in windows:
        hours.extend(range(start, end))
    log.info("Scheduling from general research guidance (%d samples on channel)",
             total_samples)
    return hours, "research"


def _window_label(hour: int) -> str:
    for start, end, label, why in RESEARCH_WINDOWS + WEEKEND_WINDOWS:
        if start <= hour < end:
            return f"{label}: {why}"
    return "Chosen from your channel's observed performance."


def recommend(
    db: Database,
    preferred_day: datetime | None = None,
    daily_goal: int = 2,
    min_gap_hours: float = 4.0,
    min_lead_minutes: int = 30,
) -> Slot:
    """Pick the next sensible publishing slot (never in the past, always spaced)."""
    now = now_ist()
    hours, source = _candidate_hours(db)
    booked = _hours_booked(db)

    if preferred_day is None:
        base_day = now.date()
    elif isinstance(preferred_day, datetime):
        base_day = preferred_day.date()
    else:  # a plain `datetime.date`
        base_day = preferred_day
    days_to_try: list[datetime] = [
        datetime.combine(base_day, datetime.min.time()) + timedelta(days=offset)
        for offset in range(0, 5)
    ]

    def _slots_used(day) -> int:
        """How many Shorts are already published or scheduled on that day."""
        key = day.strftime("%Y-%m-%d")
        published = db.count_videos("published", daily_key=key)
        scheduled = len([
            v for v in db.list_videos(status="scheduled", limit=100)
            if (v.get("scheduled_for") or "").startswith(key)
        ])
        return published + scheduled

    candidates: list[datetime] = []
    for day in days_to_try:
        if _slots_used(day.date()) >= daily_goal:
            continue  # that day's publishing budget is already used up
        for hour in hours:
            for minute in (0, 30):
                slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
                if slot < now + timedelta(minutes=min_lead_minutes):
                    continue
                if any(abs((slot - b).total_seconds()) < min_gap_hours * 3600 for b in booked):
                    continue
                candidates.append(slot)
        if candidates:
            break

    if not candidates:
        # Everything is packed: fall back to tomorrow's first research window.
        fallback = days_to_try[1] + timedelta(hours=18)
        return Slot(
            run_at=fallback,
            source=source,
            label=_window_label(fallback.hour),
            reason="No slot satisfied the spacing rules today — pushed to tomorrow evening.",
            confidence="low",
        )

    # Channel mode: the hours list is already ordered by real performance.
    chosen = candidates[0]
    alternatives = [c for c in candidates[1:4]]
    performance = channel_hour_performance(db)
    bucket = performance.get(chosen.hour, {})
    if source == "channel" and bucket:
        reason = (
            f"Your Shorts published around {chosen.strftime('%I:%M %p').lstrip('0')} IST "
            f"average {int(bucket['avg_views']):,} views across {bucket['n']} video(s)."
        )
        confidence = "high" if bucket.get("n", 0) >= 3 else "medium"
    else:
        reason = _window_label(chosen.hour)
        confidence = "medium"

    return Slot(
        run_at=chosen,
        source=source,
        label=_window_label(chosen.hour),
        reason=reason,
        confidence=confidence,
        alternatives=alternatives,
    )


def describe(slot: Slot) -> str:
    when = slot.run_at.strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
    lines = [
        f"🗓 <b>Recommended slot</b>\n{when}",
        slot.source_line,
        f"💡 {slot.reason}",
        f"🎯 Confidence: {slot.confidence}",
    ]
    if slot.alternatives:
        alts = ", ".join(
            a.strftime("%a %I:%M %p").replace(" 0", " ") for a in slot.alternatives[:3]
        )
        lines.append(f"🔀 Other good options: {alts}")
    return "\n".join(lines)
