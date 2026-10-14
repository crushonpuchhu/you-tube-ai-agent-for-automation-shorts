"""Daily Shorts goal tracking (IST) with Telegram reminders."""

from __future__ import annotations

import logging
from datetime import datetime

from agent.clock import now_ist, today_key
from agent.db import (
    STATUS_ANALYZED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_PUBLISHED,
    STATUS_RECEIVED,
    STATUS_SCHEDULED,
    STATUS_UPLOADING,
    Database,
)
from agent.notifier import Notifier

log = logging.getLogger(__name__)

#: Everything that still counts as "in the pipeline" for today's goal: it was
#: accepted and has not reached a terminal state yet.
OPEN_STATUSES = (
    STATUS_RECEIVED, STATUS_DOWNLOADING, STATUS_PROCESSING, STATUS_ANALYZED,
    STATUS_SCHEDULED, STATUS_UPLOADING, STATUS_FAILED,
)


class DailyGoalManager:
    def __init__(self, db: Database, notifier: Notifier, target: int = 2,
                 reminder_times: list[str] | None = None):
        self.db = db
        self.notifier = notifier
        self.target = target
        self.reminder_times = reminder_times or ["20:30", "22:30"]

    # ------------------------------------------------------------------ state
    def progress(self, key: str | None = None) -> dict:
        key = key or today_key()
        goal = self.db.get_goal(key, self.target)
        published = self.db.count_videos(STATUS_PUBLISHED, daily_key=key)
        in_progress = self.db.count_videos(OPEN_STATUSES, daily_key=key)
        scheduled_today = [
            v for v in self.db.list_videos(status=STATUS_SCHEDULED, limit=100)
            if (v.get("scheduled_for") or "").startswith(key)
        ]
        target = int(goal.get("target") or self.target)
        remaining = max(0, target - published)
        return {
            "key": key,
            "target": target,
            "published": published,
            "in_progress": in_progress,
            "scheduled": len(scheduled_today),
            "scheduled_titles": [v.get("brief", {}).get("title") or "Untitled"
                                 for v in scheduled_today],
            "remaining": remaining,
            "achieved": published >= target,
            "goal_row": goal,
        }

    def status_html(self, key: str | None = None) -> str:
        p = self.progress(key)
        bar_filled = min(p["published"], p["target"])
        bar = "🟩" * bar_filled + "⬜" * max(0, p["target"] - bar_filled)
        now = now_ist()
        lines = [
            f"🎯 <b>Daily Shorts goal — {p['key']} (IST)</b>",
            "",
            f"{bar}  {p['published']}/{p['target']} published",
            f"📤 Accepted today (in progress): {p['in_progress']}",
            f"⏱ Scheduled for today: {p['scheduled']}",
        ]
        if p["scheduled_titles"]:
            for title in p["scheduled_titles"][:5]:
                lines.append(f"   • {title}")
        if p["remaining"]:
            lines.append(f"📉 Remaining to hit target: <b>{p['remaining']}</b>")
        else:
            lines.append("🎉 Target achieved for today!")
        lines.append(f"🕘 Current time: {now.strftime('%I:%M %p IST').lstrip('0')}")
        return "\n".join(lines)

    # ------------------------------------------------------------- monitoring
    def check(self) -> None:
        """Called by the background scheduler: sends at most one message per slot."""
        now = now_ist()
        key = today_key(now)
        progress = self.progress(key)
        goal = progress["goal_row"]
        clock = now.strftime("%H:%M")

        if progress["achieved"]:
            if not goal.get("achieved_notified"):
                self.notifier.send(
                    f"🎉 <b>Daily target achieved!</b>\n\n"
                    f"You published {progress['published']} out of "
                    f"{progress['target']} Shorts for {key}. "
                    f"Target complete — anything else today is a bonus."
                )
                self.db.update_goal(key, achieved_notified=1)
            return

        sent = set((goal.get("reminder_sent") or "").split(","))
        sent.discard("")
        due_slots = [slot for slot in self.reminder_times if clock >= slot and slot not in sent]
        if not due_slots:
            return

        slot = due_slots[0]
        message = self._reminder_text(progress, now)
        if self.notifier.send(message):
            sent.add(slot)
            self.db.update_goal(key, reminder_sent=",".join(sorted(sent)))

    def _reminder_text(self, p: dict, now: datetime) -> str:
        remaining = p["remaining"]
        plural = "Short" if remaining == 1 else "Shorts"
        verb = "is" if remaining == 1 else "are"
        lines = [
            f"⏰ <b>Daily goal reminder</b> ({now.strftime('%I:%M %p IST').lstrip('0')})",
            "",
            f"You have published {p['published']} out of your {p['target']} daily Shorts. "
            f"{remaining} more {plural} {verb} needed to complete today's target.",
        ]
        if p["scheduled"]:
            lines.append(
                f"✅ {p['scheduled']} video(s) are already scheduled for today and will "
                f"count automatically once YouTube confirms the upload."
            )
        lines.append(f"You still have time — the day ends at 23:59 IST ({24 - now.hour}h left).")
        lines.append("Send me a video and say “upload now”, or let me pick the best slot.")
        return "\n".join(lines)
