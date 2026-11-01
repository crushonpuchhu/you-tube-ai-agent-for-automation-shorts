"""Explicit YouTube long-form discovery.

Searching YouTube is **not** permission to download or reuse a video.  This
module only searches and records grounded metadata (video id, title, channel,
canonical URL, duration, publish time, discovery time and public engagement
signals).  The source-rights gate in :mod:`features.source_rights` decides
whether media may be downloaded or reused.

The search reuses the application's existing YouTube Data API client (the
``agent.analytics.YouTubeStats`` instance), so no second OAuth credential or
scope is introduced.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import timedelta
from typing import Any

from agent.clock import now_ist

log = logging.getLogger(__name__)

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
 )

_EDUCATIONAL_HINTS = (
    "how to", "tutorial", "guide", "explained", "explain", "tips", "step by step",
    "beginner", "beginners", "workout", "form", "technique", "routine", "science",
    "mistake", "mistakes", "learn", "lesson", "masterclass", "training",
)


def parse_iso8601_duration(value: str) -> int:
    """Convert a YouTube ``contentDetails.duration`` value to seconds.

    Returns 0 when the value cannot be parsed so callers can safely filter it
    out rather than trusting an unknown duration.
    """
    if not value or not isinstance(value, str):
        return 0
    match = _ISO_DURATION.match(value.strip())
    if not match:
        return 0
    parts = match.groupdict()
    seconds = (
        int(parts.get("days") or 0) * 86400
        + int(parts.get("hours") or 0) * 3600
        + int(parts.get("minutes") or 0) * 60
        + float(parts.get("seconds") or 0)
    )
    return int(seconds)


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", (value or "").lower()) if token}


def score_candidate(candidate: dict, topic: str, niche: str) -> tuple[int, str]:
    """Transparent 0-100 suitability score (never a virality prediction)."""
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "description", "channel")
    ).lower()
    topic_tokens = _tokens(topic) | _tokens(niche)
    matched = sum(1 for token in topic_tokens if token and token in haystack)
    relevance = min(30, round(30 * matched / max(1, len(topic_tokens))))

    edu_hits = sum(1 for hint in _EDUCATIONAL_HINTS if hint in haystack)
    education = min(15, edu_hits * 3)

    recency = 0
    published = candidate.get("published_at")
    if published:
        try:
            from datetime import datetime

            published_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            age_days = max(0, (now_ist() - published_dt).days)
            if age_days <= 7:
                recency = 20
            elif age_days <= 30:
                recency = 12
            elif age_days <= 90:
                recency = 6
        except (TypeError, ValueError):
            recency = 0

    views = max(0, int(candidate.get("views") or 0))
    audience = min(20, round(math.log10(views + 1) / 7 * 20))

    minutes = float(candidate.get("duration_seconds") or 0) / 60
    if 5 <= minutes <= 60:
        clarity = 10
    elif minutes > 60:
        clarity = 5
    else:
        clarity = 2

    channel = 5 if candidate.get("channel") else 0
    score = max(0, min(100, relevance + education + recency + audience + clarity + channel))
    reason = (
        f"relevance {relevance}/30 · educational value {education}/15 · "
        f"recency {recency}/20 · public engagement signal {audience}/20 · "
        f"clip suitability {clarity}/10 · source clarity {channel}/5"
    )
    return score, reason


def _search_items(service, *, query: str, result_limit: int, region: str,
                  language: str, lookback_hours: int,
                  license: str = "any") -> list[dict]:
    kwargs: dict[str, Any] = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "relevance",
        "maxResults": max(1, min(int(result_limit), 50)),
        "regionCode": region or "IN",
        "relevanceLanguage": (language or "en")[:5].lower(),
    }
    # Ask YouTube itself for a licence class. "creativeCommon" returns only
    # videos YouTube reports as CC-licensed, which is the one third-party
    # basis the hard rights gate can verify without owner input.
    wanted_license = " ".join(str(license or "").split())
    normalised = wanted_license.lower().replace("_", "").replace(" ", "")
    if normalised in {"creativecommons", "creativecommon", "cc", "ccby", "ccbysa"}:
        kwargs["videoLicense"] = "creativeCommon"
    elif normalised and normalised not in {"any", "all", "*"}:
        kwargs["videoLicense"] = wanted_license
    if lookback_hours and int(lookback_hours) > 0:
        after = now_ist() - timedelta(hours=int(lookback_hours))
        kwargs["publishedAfter"] = after.strftime("%Y-%m-%dT%H:%M:%SZ")
    response = service.search().list(**kwargs).execute()
    return response.get("items") or []


def _details(service, video_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]
        response = (
            service.videos()
            .list(
                part="snippet,contentDetails,statistics,status",
                id=",".join(chunk),
                maxResults=50,
            )
            .execute()
        )
        for item in response.get("items") or []:
            if item.get("id"):
                out[item["id"]] = item
    return out


def search_youtube_long_videos(
    topic: str,
    niche: str,
    min_duration_minutes: int = 5,
    max_duration_minutes: int = 120,
    result_limit: int = 10,
    *,
    youtube: Any = None,
    region: str = "IN",
    language: str = "en",
    lookback_hours: int = 72,
    license: str = "any",
    known_video_ids: set[str] | None = None,
) -> list[dict]:
    """Search YouTube for relevant long-form candidates.

    Returns a de-duplicated list of grounded candidate dictionaries sorted by
    the transparent suitability score.  Returns ``[]`` on any API failure —
    the autonomous cycle continues with whatever other sources exist.
    """
    topic = " ".join((topic or "").split())
    niche = " ".join((niche or "").split())
    if not topic:
        return []
    if youtube is None:
        log.info("YouTube discovery skipped: no YouTube client supplied")
        return []

    min_seconds = max(0, int(min_duration_minutes)) * 60
    max_seconds = max(1, int(max_duration_minutes)) * 60
    if max_seconds <= min_seconds:
        max_seconds = min_seconds + 60

    query = topic if niche.lower() in topic.lower() else f"{topic} {niche}".strip()
    try:
        items = _search_items(
            youtube.youtube(),
            query=query,
            result_limit=max(int(result_limit) * 2, int(result_limit)),
            region=region,
            language=language,
            lookback_hours=lookback_hours,
            license=license,
        )
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        log.warning("YouTube search failed for %r: %s", query, str(exc)[:200])
        return []

    video_ids = [
        (item.get("id") or {}).get("videoId")
        for item in items
        if (item.get("id") or {}).get("videoId")
    ]
    if not video_ids:
        return []

    try:
        details = _details(youtube.youtube(), video_ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("YouTube video details failed: %s", str(exc)[:200])
        return []

    known = set(known_video_ids or set())
    seen: set[str] = set()
    candidates: list[dict] = []

    for video_id in video_ids:
        if video_id in seen:
            continue
        seen.add(video_id)
        if video_id in known:
            continue
        item = details.get(video_id)
        if not item:
            continue
        snippet = item.get("snippet") or {}
        content = item.get("contentDetails") or {}
        statistics = item.get("statistics") or {}
        status = item.get("status") or {}

        duration_seconds = parse_iso8601_duration(content.get("duration") or "")
        if duration_seconds <= 0:
            continue
        if not (min_seconds <= duration_seconds <= max_seconds):
            continue

        candidate = {
            "video_id": video_id,
            "title": " ".join(str(snippet.get("title") or "").split())[:300],
            "channel": " ".join(str(snippet.get("channelTitle") or "").split())[:200],
            "channel_id": snippet.get("channelId") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "duration_seconds": duration_seconds,
            "duration_minutes": round(duration_seconds / 60, 2),
            "published_at": snippet.get("publishedAt") or "",
            "discovered_at": now_ist().isoformat(),
            "views": int(statistics.get("viewCount") or 0),
            "likes": int(statistics.get("likeCount") or 0),
            "comments": int(statistics.get("commentCount") or 0),
            "license": str(status.get("license") or ""),
            "privacy_status": str(status.get("privacyStatus") or ""),
            "description": " ".join(str(snippet.get("description") or "").split())[:600],
            "source_type": "youtube",
            "topic": topic,
            "niche": niche,
        }
        score, reason = score_candidate(candidate, topic, niche)
        candidate["score"] = score
        candidate["score_reason"] = reason
        candidates.append(candidate)

    candidates.sort(key=lambda c: (c.get("score", 0), c.get("views", 0)), reverse=True)
    log.info(
        "YouTube discovery: %d candidate(s) for topic %r (%.0f–%.0f min)",
        len(candidates[: max(1, int(result_limit))]),
        topic,
        min_seconds / 60,
        max_seconds / 60,
    )
    return candidates[: max(1, int(result_limit))]
