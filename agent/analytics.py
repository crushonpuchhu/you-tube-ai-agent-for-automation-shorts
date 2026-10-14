"""YouTube integration: publication verification, statistics and Analytics API.

All calls are blocking — run them from worker threads.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from agent.clock import now_ist
from agent.errors import AuthRequired

log = logging.getLogger(__name__)

DATA_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def _is_scope_error(exc: HttpError) -> bool:
    if exc.resp.status not in (401, 403):
        return False
    content = (exc.content or b"").decode("utf-8", "ignore").lower()
    return "insufficient" in content or "scope" in content


class YouTubeStats:
    """Wraps the YouTube Data API + YouTube Analytics API using the bot's OAuth token."""

    def __init__(self, credentials_provider: Callable[[], object],
                 scope_checker: Callable[[], list[str]] | None = None):
        self._creds = credentials_provider
        self._scope_checker = scope_checker

    # ------------------------------------------------------------- services
    def youtube(self):
        return build("youtube", "v3", credentials=self._creds(), cache_discovery=False)

    def youtube_analytics(self):
        return build("youtubeAnalytics", "v2", credentials=self._creds(), cache_discovery=False)

    def require_read_scope(self) -> None:
        """Fail fast when the stored token predates the read-only scope."""
        if self._scope_checker is not None:
            missing = [s for s in self._scope_checker() if "readonly" in s]
            if missing:
                raise AuthRequired(
                    "The YouTube token is missing read access. "
                    "Run: python scripts/youtube_auth.py --force"
                )
            return
        creds = self._creds()
        try:
            if hasattr(creds, "has_scopes") and not creds.has_scopes([DATA_SCOPE]):
                raise AuthRequired(
                    "The YouTube token is missing the read-only scope. "
                    "Run: python scripts/youtube_auth.py --force"
                )
        except AuthRequired:
            raise
        except Exception:  # noqa: BLE001 - provider may be a mock
            pass

    # ----------------------------------------------------------- verification
    def verify_video(self, video_id: str, attempts: int = 4, delay: float = 4.0) -> dict | None:
        """Confirm YouTube actually has the video. Returns its snippet/status or None.

        This is the only accepted source of truth for "published" — we never
        assume an upload succeeded from a local response alone.
        """
        self.require_read_scope()
        for attempt in range(attempts):
            try:
                response = (
                    self.youtube()
                    .videos()
                    .list(part="snippet,status,contentDetails", id=video_id)
                    .execute()
                )
            except HttpError as exc:
                if _is_scope_error(exc):
                    raise AuthRequired(
                        "YouTube refused read access (insufficient scope). "
                        "Run: python scripts/youtube_auth.py --force"
                    ) from exc
                if exc.resp.status in (403, 429, 500, 503) and attempt < attempts - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise

            items = response.get("items") or []
            if items:
                item = items[0]
                return {
                    "id": item.get("id"),
                    "title": (item.get("snippet") or {}).get("title"),
                    "privacy": (item.get("status") or {}).get("privacyStatus"),
                    "uploadStatus": (item.get("status") or {}).get("uploadStatus"),
                    "publishAt": (item.get("status") or {}).get("publishAt"),
                }
            time.sleep(delay)
        return None

    # ------------------------------------------------------- Data API stats
    def video_statistics(self, video_ids: list[str]) -> dict[str, dict]:
        """Cumulative statistics (views/likes/comments/shares) for given videos."""
        stats: dict[str, dict] = {}
        ids = [v for v in video_ids if v]
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            try:
                response = (
                    self.youtube()
                    .videos()
                    .list(part="statistics,snippet", id=",".join(chunk))
                    .execute()
                )
            except HttpError as exc:
                if _is_scope_error(exc):
                    raise AuthRequired(
                        "Missing read-only scope. Run: python scripts/youtube_auth.py --force"
                    ) from exc
                raise
            for item in response.get("items") or []:
                statistics = item.get("statistics") or {}
                snippet = item.get("snippet") or {}
                stats[item["id"]] = {
                    "views": int(statistics.get("viewCount") or 0),
                    "likes": int(statistics.get("likeCount") or 0),
                    "comments": int(statistics.get("commentCount") or 0),
                    "shares": int(statistics.get("sharing") or 0),
                    "title": snippet.get("title"),
                    "published_at": snippet.get("publishedAt"),
                    "tags": snippet.get("tags") or [],
                    "category_id": snippet.get("categoryId"),
                }
        return stats

    def channel_statistics(self) -> dict:
        try:
            response = (
                self.youtube()
                .channels()
                .list(part="statistics,snippet", mine=True)
                .execute()
            )
        except HttpError as exc:
            if _is_scope_error(exc):
                raise AuthRequired(
                    "Missing read-only scope. Run: python scripts/youtube_auth.py --force"
                ) from exc
            raise
        items = response.get("items") or []
        if not items:
            return {}
        item = items[0]
        statistics = item.get("statistics") or {}
        return {
            "id": item.get("id"),
            "title": (item.get("snippet") or {}).get("title"),
            "subscribers": int(statistics.get("subscriberCount") or 0),
            "views": int(statistics.get("viewCount") or 0),
            "videos": int(statistics.get("videoCount") or 0),
        }

    # ------------------------------------------------------ Analytics API
    def _query(self, *, metrics: str, dimensions: str | None, start: datetime,
               end: datetime, filters: str | None = None) -> list[dict]:
        kwargs = {
            "ids": "channel==MINE",
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "metrics": metrics,
        }
        if dimensions:
            kwargs["dimensions"] = dimensions
        if filters:
            kwargs["filters"] = filters
        response = self.youtube_analytics().reports().query(**kwargs).execute()
        rows = response.get("rows") or []
        column_header = response.get("columnHeaders") or []
        names = [h.get("name") for h in column_header]
        return [dict(zip(names, row)) for row in rows]

    def video_metrics(self, start: datetime, end: datetime) -> list[dict]:
        """Per-video performance for the window (retention, likes, subs gained)."""
        base = "views,likes,comments,shares,subscribersGained,averageViewDuration,averageViewPercentage"
        attempts = [base, base + ",swipedAway", "views,likes,averageViewDuration,averageViewPercentage", "views"]
        last_error: Exception | None = None
        for metrics in attempts:
            try:
                return self._query(metrics=metrics, dimensions="video", start=start, end=end)
            except HttpError as exc:
                last_error = exc
                if _is_scope_error(exc):
                    raise AuthRequired(
                        "Analytics scope missing. Run: python scripts/youtube_auth.py --force"
                    ) from exc
                continue
        log.warning("Analytics video_metrics unavailable: %s", last_error)
        return []

    def daily_metrics(self, start: datetime, end: datetime) -> list[dict]:
        try:
            return self._query(
                metrics="views,estimatedMinutesWatched,averageViewDuration",
                dimensions="day", start=start, end=end,
            )
        except HttpError as exc:
            if _is_scope_error(exc):
                raise AuthRequired(
                    "Analytics scope missing. Run: python scripts/youtube_auth.py --force"
                ) from exc
            log.warning("daily_metrics unavailable: %s", exc)
            return []

    def audience_geography(self, start: datetime, end: datetime) -> list[dict]:
        try:
            return self._query(
                metrics="views", dimensions="country", start=start, end=end,
            )
        except HttpError as exc:
            log.info("geography unavailable: %s", exc)
            return []

    def top_search(self, query: str, limit: int = 10, published_after_days: int = 30) -> list[dict]:
        """Public YouTube search used for content research (read-only scope)."""
        after = (now_ist() - timedelta(days=published_after_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            search = (
                self.youtube()
                .search()
                .list(
                    part="snippet",
                    q=query,
                    type="video",
                    order="viewCount",
                    maxResults=min(limit, 25),
                    publishedAfter=after,
                    relevanceLanguage="en",
                    regionCode="IN",
                )
                .execute()
            )
        except HttpError as exc:
            log.warning("YouTube search failed: %s", exc)
            return []
        items = search.get("items") or []
        video_ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
        statistics = self.video_statistics(video_ids) if video_ids else {}
        results = []
        for item in items:
            vid = (item.get("id") or {}).get("videoId")
            if not vid:
                continue
            snippet = item.get("snippet") or {}
            info = statistics.get(vid, {})
            results.append({
                "video_id": vid,
                "title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "published_at": snippet.get("publishedAt"),
                "views": info.get("views", 0),
                "url": f"https://youtu.be/{vid}",
                "description": (snippet.get("description") or "")[:300],
            })
        return results

    # ------------------------------------------------------- sync into DB
    def sync_to_db(self, db, videos: list[dict]) -> int:
        """Fetch fresh statistics for tracked videos and store them in `stats`."""
        ids = [v.get("youtube_id") for v in videos if v.get("youtube_id")]
        if not ids:
            return 0
        day = now_ist().strftime("%Y-%m-%d")
        statistics = self.video_statistics(ids)

        metric_rows: dict[str, dict] = {}
        try:
            start = now_ist() - timedelta(days=30)
            for row in self.video_metrics(start, now_ist()):
                metric_rows[row.get("videoId") or row.get("video_id") or ""] = row
        except AuthRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - Analytics is optional enrichment
            log.info("Analytics enrichment skipped: %s", exc)

        saved = 0
        for video_id, info in statistics.items():
            extra = metric_rows.get(video_id, {})
            views = int(extra.get("views") or info.get("views") or 0)
            avg_duration = extra.get("averageViewDuration")
            retention = extra.get("averageViewPercentage")
            swiped = extra.get("swipedAway")
            db.upsert_stats(
                video_id=video_id,
                day=day,
                views=views,
                likes=int(extra.get("likes") or info.get("likes") or 0),
                comments=int(extra.get("comments") or info.get("comments") or 0),
                shares=int(extra.get("shares") or info.get("shares") or 0),
                subs_gained=int(extra.get("subscribersGained") or 0),
                avg_view_duration=float(avg_duration) if avg_duration is not None else None,
                retention_pct=float(retention) if retention is not None else None,
                swiped_pct=float(swiped) if swiped is not None else None,
            )
            saved += 1
        if saved:
            log.info("Synced statistics for %d videos", saved)
        return saved
