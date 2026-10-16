"""Weekly performance review + content decisions (create more / improve / experiment / avoid)."""

from __future__ import annotations

import html
import logging
import statistics
from datetime import timedelta

from agent.analytics import YouTubeStats
from agent.clock import now_ist
from agent.db import Database
from agent.llm import LLM
from pipeline.analyzer import CATEGORIES

log = logging.getLogger(__name__)

REPORT_SYSTEM = """\
You are a YouTube Shorts analytics consultant. You receive REAL metrics collected
from the channel's own YouTube Analytics API (some fields may be null when the API
did not provide them — never invent those numbers).

Return a JSON object:
{
  "summary": "2-3 sentence executive summary of the week",
  "topics_to_explore": ["..."],
  "formats_to_double_down": ["..."],
  "videos_needing_improvement": ["..."],
  "publishing_strategy": ["..."],
  "experiments_next_week": ["..."]
}
Rules: every recommendation must trace back to a number in the data or be explicitly
labelled as an experiment/hypothesis. No guarantees of virality. Be concrete.
"""

DECISIONS_SYSTEM = """\
You are a Shorts content planner. Given this channel's performance and fresh research
ideas, classify recommendations into exactly four groups.

Return a JSON object:
{
  "create_more": [{"action": "...", "why": "...", "next_step": "..."}],
  "improve": [{"action": "...", "why": "...", "next_step": "..."}],
  "experiment": [{"action": "...", "why": "...", "next_step": "..."}],
  "avoid": [{"action": "...", "why": "...", "next_step": "..."}]
}
Base everything on repeatable patterns across multiple data points — never on a single
video. If a group has nothing defensible, return an empty array for it.
"""


def _window(days: int = 7, offset_days: int = 0) -> tuple[str, str]:
    today = now_ist().date()
    end = today - timedelta(days=offset_days)
    start = end - timedelta(days=days - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _published_in(db: Database, start: str, end: str) -> list[dict]:
    rows = []
    for video in db.list_videos(status="published", limit=400):
        key = video.get("daily_key") or ""
        if key and start <= key <= end:
            rows.append(video)
    return rows


def _goal_days(db: Database, start: str, end: str, default_target: int) -> tuple[int, int]:
    """(days_target_achieved, days_counted) for the window."""
    achieved = counted = 0
    cursor = now_ist().date()
    from datetime import date as _date
    start_d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    day = start_d
    while day <= end_d:
        key = day.strftime("%Y-%m-%d")
        goal = db.get_goal(key, default_target)
        target = int(goal.get("target") or default_target)
        published = db.count_videos("published", daily_key=key)
        counted += 1
        if published >= target:
            achieved += 1
        day += timedelta(days=1)
    if end_d >= cursor:  # don't count days that haven't happened yet
        pass
    return achieved, counted


def collect_metrics(db: Database, stats: YouTubeStats, days: int = 7,
                    offset_days: int = 0, default_target: int = 2) -> dict:
    start, end = _window(days, offset_days)
    published = _published_in(db, start, end)
    latest = db.latest_stats()

    video_rows = []
    for video in published:
        info = latest.get(video.get("youtube_id") or "", {})
        video_rows.append({
            "id": video.get("youtube_id"),
            "title": (video.get("brief") or {}).get("title", "Untitled"),
            "url": video.get("url"),
            "published_at": video.get("published_at"),
            "hour": _hour_of(video.get("published_at")),
            "category_id": (video.get("brief") or {}).get("category_id"),
            "tags": (video.get("brief") or {}).get("tags") or [],
            "views": int(info.get("views") or 0),
            "likes": int(info.get("likes") or 0),
            "comments": int(info.get("comments") or 0),
            "shares": int(info.get("shares") or 0),
            "subs_gained": int(info.get("subs_gained") or 0),
            "retention_pct": info.get("retention_pct"),
            "avg_view_duration": info.get("avg_view_duration"),
            "swiped_pct": info.get("swiped_pct"),
        })

    views = [v["views"] for v in video_rows]
    total_views = sum(views)
    engagement = sum(v["likes"] + v["comments"] + v["shares"] for v in video_rows)

    by_hour: dict[int, list[int]] = {}
    for row in video_rows:
        if row["hour"] is not None and row["views"]:
            by_hour.setdefault(row["hour"], []).append(row["views"])
    hour_averages = {h: sum(v) / len(v) for h, v in by_hour.items() if v}

    by_category: dict[str, list[int]] = {}
    for row in video_rows:
        if row["views"]:
            by_category.setdefault(row["category_id"] or "24", []).append(row["views"])
    category_averages = {c: sum(v) / len(v) for c, v in by_category.items() if v}

    tag_views: dict[str, list[int]] = {}
    for row in video_rows:
        for tag in row["tags"][:5]:
            if row["views"]:
                tag_views.setdefault(tag.lower(), []).append(row["views"])

    retentions = [r["retention_pct"] for r in video_rows if r["retention_pct"] is not None]
    durations = [r["avg_view_duration"] for r in video_rows if r["avg_view_duration"] is not None]

    goals_achieved, goals_counted = _goal_days(db, start, end, default_target)
    previous_start, previous_end = _window(days, offset_days + days)
    previous_rows = _published_in(db, previous_start, previous_end)
    previous_views = sum(
        int((latest.get(v.get("youtube_id") or "") or {}).get("views") or 0)
        for v in previous_rows
    )

    return {
        "window": {"start": start, "end": end, "days": days},
        "published": len(video_rows),
        "goal_days_achieved": goals_achieved,
        "goal_days_counted": goals_counted,
        "total_views": total_views,
        "previous_views": previous_views,
        "views_delta_pct": (
            round((total_views - previous_views) / previous_views * 100, 1)
            if previous_views else None
        ),
        "avg_views": int(statistics.mean(views)) if views else 0,
        "median_views": int(statistics.median(views)) if views else 0,
        "total_likes": sum(v["likes"] for v in video_rows),
        "total_comments": sum(v["comments"] for v in video_rows),
        "total_shares": sum(v["shares"] for v in video_rows),
        "subs_gained": sum(v["subs_gained"] for v in video_rows),
        "engagement_rate_pct": round(engagement / total_views * 100, 2) if total_views else 0,
        "avg_retention_pct": round(sum(retentions) / len(retentions), 1) if retentions else None,
        "avg_view_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
        "swiped_pct": (next((r["swiped_pct"] for r in video_rows if r["swiped_pct"] is not None),
                            None)),
        "best_hour": max(hour_averages, key=hour_averages.get) if hour_averages else None,
        "hour_averages": {str(h): int(v) for h, v in sorted(hour_averages.items())},
        "best_category": max(category_averages, key=category_averages.get) if category_averages else None,
        "category_names": {
            c: CATEGORIES.get(c, "Unknown") for c in category_averages
        },
        "category_averages": {c: int(v) for c, v in category_averages.items()},
        "top_tags": _top_tags(tag_views),
        "best_video": max(video_rows, key=lambda r: r["views"]) if video_rows else None,
        "worst_video": min(video_rows, key=lambda r: r["views"]) if video_rows else None,
        "videos": video_rows,
    }


def _hour_of(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        from agent.clock import ensure_ist
        from datetime import datetime
        return ensure_ist(datetime.fromisoformat(stamp)).hour
    except (ValueError, TypeError):
        return None


def _top_tags(tag_views: dict[str, list[int]], limit: int = 5) -> list[dict]:
    ranked = sorted(tag_views.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
    return [
        {"tag": tag, "avg_views": int(sum(views) / len(views)), "n": len(views)}
        for tag, views in ranked[:limit]
    ]


# --------------------------------------------------------------------- report
def format_report(metrics: dict, recommendations: dict | None) -> str:
    esc = html.escape
    window = metrics["window"]
    lines = [
        "📊 <b>Weekly YouTube Shorts Report</b>",
        f"<i>{esc(window['start'])} → {esc(window['end'])} (IST)</i>",
        "",
        f"🎬 Shorts published: <b>{metrics['published']}</b>",
        f"🎯 Daily targets achieved: <b>{metrics['goal_days_achieved']} out of "
        f"{metrics['goal_days_counted']} days</b>",
        f"👀 Total views: <b>{metrics['total_views']:,}</b>",
    ]
    if metrics["views_delta_pct"] is not None:
        arrow = "📈" if metrics["views_delta_pct"] >= 0 else "📉"
        lines.append(
            f"{arrow} vs previous week: <b>{metrics['views_delta_pct']:+}%</b> "
            f"({metrics['previous_views']:,} views before)"
        )
    lines += [
        f"➡️ Average views / Short: <b>{metrics['avg_views']:,}</b>",
        f"➡️ Median views / Short: <b>{metrics['median_views']:,}</b>",
        f"👍 Likes: {metrics['total_likes']:,} · 💬 Comments: {metrics['total_comments']:,} · "
        f"↗️ Shares: {metrics['total_shares']:,} · 🧲 Subs gained: {metrics['subs_gained']:,}",
        f"❤️ Engagement rate: {metrics['engagement_rate_pct']}%",
    ]
    if metrics["avg_retention_pct"] is not None:
        lines.append(f"📉 Avg viewed %: {metrics['avg_retention_pct']}%")
    if metrics["avg_view_duration_s"] is not None:
        seconds = metrics["avg_view_duration_s"]
        lines.append(f"⏱ Avg view duration: {int(seconds // 60)}m {int(seconds % 60)}s")
    if metrics["swiped_pct"] is not None:
        lines.append(f"👆 Viewed vs swiped away: {100 - metrics['swiped_pct']}% viewed")

    if metrics["best_hour"] is not None:
        lines.append(f"🕘 Best publishing hour this week: <b>{metrics['best_hour']:02d}:00 IST</b>")
    if metrics["best_category"]:
        name = metrics["category_names"].get(metrics["best_category"], "Unknown")
        lines.append(f"🏷 Best-performing category: {esc(name)}")
    if metrics["top_tags"]:
        best = metrics["top_tags"][0]
        lines.append(
            f"🏆 Best-performing topic: #{esc(best['tag'])} "
            f"(avg {best['avg_views']:,} views across {best['n']})"
        )

    if metrics["best_video"]:
        video = metrics["best_video"]
        lines += [
            "",
            f"🥇 Best Short: <i>{esc(video['title'])}</i> — {video['views']:,} views",
            f"🔗 {video['url']}",
        ]
    if metrics["worst_video"]:
        video = metrics["worst_video"]
        lines += [
            f"🕳 Weakest Short: <i>{esc(video['title'])}</i> — {video['views']:,} views",
            f"🔗 {video['url']}",
        ]

    if recommendations:
        lines += ["", "<b>🤖 AI recommendations</b>"]
        for key, label in (
            ("summary", "Summary"),
            ("topics_to_explore", "Topics worth exploring"),
            ("formats_to_double_down", "Formats to double down on"),
            ("videos_needing_improvement", "Needs improvement"),
            ("publishing_strategy", "Publishing strategy"),
            ("experiments_next_week", "Experiments to run next week"),
        ):
            value = recommendations.get(key)
            if not value:
                continue
            if isinstance(value, list):
                body = "\n".join(f"• {esc(str(v))}" for v in value)
            else:
                body = esc(str(value))
            lines.append(f"<b>{label}</b>\n{body}")
    else:
        lines += ["", "<i>Not enough data yet for AI recommendations — publish more Shorts.</i>"]

    lines += ["", "<i>Metrics come from YouTube Analytics/Data API where available. "
                  "Missing fields mean the API did not return them.</i>"]
    return "\n".join(lines)


async def build_report(db: Database, stats: YouTubeStats, llm: LLM, default_target: int = 2) -> str:
    try:
        published = _published_in(db, *_window(7))
        if published:
            stats.sync_to_db(db, published)
    except Exception as exc:  # noqa: BLE001 - report still goes out with cached data
        log.warning("Fresh stats sync failed: %s", exc)

    metrics = collect_metrics(db, stats, days=7, default_target=default_target)
    recommendations = None
    if metrics["published"]:
        try:
            recommendations = await llm.json_async(
                system=REPORT_SYSTEM,
                prompt=_metrics_json(metrics),
                temperature=0.5,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Report recommendations failed: %s", exc)
        # A misbehaving model may answer with a list — never let that crash
        # the whole report, it just drops the AI section.
        if not isinstance(recommendations, dict):
            recommendations = None
    return format_report(metrics, recommendations)


def _metrics_json(metrics: dict) -> str:
    import json

    payload = {k: v for k, v in metrics.items() if k != "videos"}
    payload["videos"] = [
        {k: v for k, v in row.items() if k in (
            "title", "url", "published_at", "hour", "views", "likes", "comments",
            "shares", "subs_gained", "retention_pct", "avg_view_duration", "tags"
        )}
        for row in metrics["videos"]
    ]
    return json.dumps(payload, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ decisions
def format_decisions(decisions: dict, basis: str) -> str:
    esc = html.escape
    groups = (
        ("create_more", "✅ A. Create more"),
        ("improve", "🔧 B. Improve"),
        ("experiment", "🧪 C. Experiment with"),
        ("avoid", "⛔ D. Avoid or reduce"),
    )
    lines = ["🧭 <b>Content decisions</b>", f"<i>Basis: {esc(basis)}</i>", ""]
    for key, label in groups:
        lines.append(f"<b>{label}</b>")
        items = decisions.get(key) or []
        if not items:
            lines.append("• Nothing supported by the data yet.")
        for item in items:
            if not isinstance(item, dict):
                lines.append(f"• {esc(str(item))}")
                continue
            lines.append(f"• <b>{esc(str(item.get('action', '')))}</b>")
            if item.get("why"):
                lines.append(f"   why: {esc(str(item['why']))}")
            if item.get("next_step"):
                lines.append(f"   next: {esc(str(item['next_step']))}")
        lines.append("")
    return "\n".join(lines).strip()


async def build_decisions(db: Database, stats: YouTubeStats, llm: LLM,
                          default_target: int = 2) -> str:
    metrics = collect_metrics(db, stats, days=30, default_target=default_target)
    topics = db.recent_topics(limit=10)
    payload = {
        "published_videos": len(metrics["videos"]),
        "metrics": {k: v for k, v in metrics.items() if k != "videos"},
        "recent_research_topics": [
            {k: t.get(k) for k in ("topic", "priority_score", "confidence", "evidence")}
            for t in topics
        ],
        "note": "Use repeatable patterns only. Do not judge a format from one video.",
    }
    import json

    decisions = await llm.json_async(
        system=DECISIONS_SYSTEM,
        prompt=json.dumps(payload, ensure_ascii=False, default=str),
        temperature=0.4,
    )
    basis = (
        f"{metrics['published']} Shorts over {metrics['window']['start']}→{metrics['window']['end']}"
        if metrics["published"] else "research only (no published videos in the window)"
    )
    return format_decisions(decisions if isinstance(decisions, dict) else {}, basis)
