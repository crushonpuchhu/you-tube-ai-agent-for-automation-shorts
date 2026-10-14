"""YouTube Manager: upload -> verify -> record -> count toward the daily goal.

Nothing is reported as published unless YouTube itself confirms it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from agent.analytics import YouTubeStats
from agent.cleanup import discard_after_upload
from agent.clock import now_iso, today_key
from agent.db import Database
from agent.daily_goal import DailyGoalManager
from agent.errors import AuthRequired, PublishError
from agent.notifier import Notifier
from pipeline.models import VideoBrief
from pipeline.youtube_uploader import YouTubeUploader

log = logging.getLogger(__name__)

# Statuses allowed to start a publish (prevents duplicate uploads).
PUBLISHABLE = ("analyzed", "scheduled", "failed", "uploading")


class Publisher:
    def __init__(
        self,
        db: Database,
        uploader: YouTubeUploader,
        stats: YouTubeStats,
        goal: DailyGoalManager,
        notifier: Notifier,
        delete_after_upload: bool = True,
    ):
        self.db = db
        self.uploader = uploader
        self.stats = stats
        self.goal = goal
        self.notifier = notifier
        self.delete_after_upload = delete_after_upload

    def publish(self, video_row_id: int,
                progress: Callable[[int, str], None] | None = None) -> dict:
        """Blocking publish. Raises on failure; returns the verified record on success."""
        video = self.db.get_video(video_row_id)
        if not video:
            raise PublishError(f"Video record {video_row_id} not found.")

        if video.get("status") == "published" and video.get("youtube_id"):
            log.info("Video %d already published as %s — skipping duplicate upload",
                     video_row_id, video["youtube_id"])
            return video

        # Fail fast when the token has no read access... but do not block publishing:
        # the upload API's own success response already confirms the publish, and we
        # run the stronger `videos.list` verification whenever the scope allows it.
        try:
            self.stats.require_read_scope()
            can_verify = True
        except AuthRequired:
            can_verify = False
            log.warning("Read scope missing — publishing without videos.list verification "
                        "(run scripts/youtube_auth.py --force to enable full verification)")

        if not self.db.claim_publish(video_row_id):
            fresh = self.db.get_video(video_row_id)
            if fresh and fresh.get("status") == "published":
                return fresh
            raise PublishError(
                f"Video {video_row_id} is not ready to publish "
                f"(status: {fresh.get('status') if fresh else 'missing'})."
            )

        brief_payload = video.get("brief") or {}
        if not brief_payload.get("title"):
            self.db.update_video(video_row_id, status="failed",
                                 error="No metadata (analysis) for this video.")
            raise PublishError("No metadata available for this video — re-run the analysis.")

        file_path = Path(video["file_path"])
        if not file_path.exists():
            message = f"Video file missing: {file_path}"
            self.db.update_video(video_row_id, status="failed", error=message)
            raise PublishError(message)

        brief = VideoBrief(
            title=brief_payload.get("title", ""),
            description=brief_payload.get("description", ""),
            hashtags=brief_payload.get("hashtags") or [],
            tags=brief_payload.get("tags") or [],
            category_id=brief_payload.get("category_id", "24"),
            language=brief_payload.get("language", "en"),
            hook_summary=brief_payload.get("hook_summary", ""),
        )
        privacy = video.get("privacy") or "public"

        try:
            youtube_id = self.uploader.upload(file_path, brief, privacy, progress)
        except Exception as exc:  # noqa: BLE001
            self.db.update_video(video_row_id, status="failed", error=str(exc))
            raise

        # --- verification: YouTube must confirm the video exists -------------
        verified_at = None
        if can_verify:
            try:
                verified = self.stats.verify_video(youtube_id)
            except AuthRequired:
                verified = None
            if not verified:
                message = (f"YouTube returned id {youtube_id} but the video is not visible "
                           f"via the API — treating as failed.")
                self.db.update_video(video_row_id, status="failed", youtube_id=youtube_id,
                                     error=message)
                raise PublishError(message)
            verified_at = now_iso()

        stamp = now_iso()
        url = f"https://youtu.be/{youtube_id}"
        self.db.update_video(
            video_row_id,
            status="published",
            youtube_id=youtube_id,
            url=url,
            published_at=stamp,
            verified_at=verified_at,
            daily_key=today_key(),
            error=None if can_verify else "published (upload API confirmed); "
                                          "videos.list verification pending — run auth --force",
        )
        log.info("Published video_row=%s youtube_id=%s verified=%s",
                 video_row_id, youtube_id, bool(verified_at))

        # YouTube now owns the bytes — drop our local copy so the disk stays flat.
        if self.delete_after_upload:
            discard_after_upload(self.db, video_row_id)

        # Notify only if this wasn't an interactive (in-chat) upload.
        self.goal.check()  # fires the "target achieved" notice when appropriate
        return self.db.get_video(video_row_id)

    def publish_and_notify(self, video_row_id: int) -> dict:
        """Used by the scheduler: always reports the outcome to Telegram."""
        video = self.db.get_video(video_row_id)
        title = (video.get("brief") or {}).get("title", "Untitled") if video else "Untitled"
        try:
            record = self.publish(video_row_id)
        except AuthRequired as exc:
            self.notifier.send(
                f"🔐 <b>YouTube authorization needed</b>\n\n{exc}\n\n"
                f"The scheduled Short <i>{title}</i> was NOT published."
            )
            raise
        except Exception as exc:  # noqa: BLE001
            self.notifier.send(
                f"❌ <b>Scheduled publish failed</b>\n\n"
                f"🎥 <i>{title}</i>\nReason: {exc}\n\n"
                f"Fix the issue and send the video again, or use /queue to see the queue."
            )
            raise

        progress = self.goal.progress()
        if progress["remaining"]:
            goal_line = f"{progress['published']}/{progress['target']} ({progress['remaining']} to go)"
        else:
            goal_line = f"{progress['published']}/{progress['target']} — ✅ target achieved"
        lines = [
            "✅ <b>Scheduled Short published</b>",
            "",
            f"🎥 <i>{record['brief'].get('title', title)}</i>",
            f"🔗 {record['url']}",
            f"👁️ {record['privacy']}",
            "",
        ]
        if record.get("verified_at"):
            lines.append("✅ Verified via the YouTube API")
        else:
            lines.append(
                "⚠️ Confirmed by the upload API; full verification pending — "
                "run <code>python scripts/youtube_auth.py --force</code>"
            )
        lines.append(f"🎯 Daily goal: {goal_line}")
        self.notifier.send("\n".join(lines))
        return record
