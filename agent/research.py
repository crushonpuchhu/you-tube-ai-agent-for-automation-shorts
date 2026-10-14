"""AI-powered content research: YouTube + news + Reddit -> Gemini topic ideas.

Sources are queried politely (few requests, timeouts, descriptive user agent,
once per research run) and every claim in the output must carry its evidence.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from agent.analytics import YouTubeStats
from agent.clock import now_ist
from agent.db import Database
from agent.errors import ResearchError
from agent.llm import LLM

log = logging.getLogger(__name__)

USER_AGENT = "ShortsAgentResearch/1.0 (personal content research; contact: channel owner)"
TIMEOUT = 12

RESEARCH_SYSTEM = """\
You are a YouTube Shorts content strategist. You receive RAW research findings
(YouTube results, news headlines, Reddit discussions) plus this channel's own
published video titles. Produce ORIGINAL content opportunities — never instruct
the creator to copy someone else's video.

Return ONLY a JSON array of topic objects with exactly these keys:
{
  "topic": "the video idea",
  "why_now": "why it is worth making now",
  "audience": "who it is for",
  "evidence": "what the raw findings actually show (cite the source names)",
  "hook": "first 3-5 seconds hook line",
  "format": "recommended format (talking head / screen record / b-roll + voiceover / listicle / demo)",
  "title": "suggested YouTube title under 90 chars",
  "outline": "3-5 bullet script direction",
  "sources": ["url", "..."],
  "priority_score": 0-100,
  "priority_reason": "transparent scoring rationale",
  "confidence": "high | medium | low",
  "limitations": "what is estimated rather than verified",
  "category": "one of: education, entertainment, tech, comedy, lifestyle, news, other"
}

Rules:
- Separate verified facts (present in the raw findings) from estimates/predictions.
- Never promise virality. Priority score must reflect evidence strength,
  audience fit and competition, and you must explain the scoring.
- 5-7 topics, most promising first.
"""


def _fetch(url: str, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-IN,en;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _news_headlines(query: str, limit: int = 8) -> list[dict]:
    """Google News RSS (public feed). Returns [] on any failure."""
    try:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        root = ET.fromstring(_fetch(url))
        items = root.findall(".//item")[:limit]
        return [
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "source": "Google News",
                "published": (item.findtext("pubDate") or "").strip(),
            }
            for item in items
            if item.findtext("title")
        ]
    except Exception as exc:  # noqa: BLE001 - optional source
        log.info("News research skipped: %s", exc)
        return []


def _reddit_discussions(query: str, limit: int = 8) -> list[dict]:
    """Reddit public search JSON (1 request, polite UA). Returns [] on failure."""
    try:
        url = (
            "https://www.reddit.com/search.json?q="
            + urllib.parse.quote(query)
            + f"&sort=top&t=month&limit={limit}"
        )
        payload = json.loads(_fetch(url, accept="application/json").decode("utf-8"))
        posts = (((payload.get("data") or {}).get("children")) or [])[:limit]
        return [
            {
                "title": (post.get("data") or {}).get("title"),
                "url": "https://www.reddit.com" + ((post.get("data") or {}).get("permalink") or ""),
                "score": (post.get("data") or {}).get("score"),
                "source": "Reddit",
            }
            for post in posts
            if (post.get("data") or {}).get("title")
        ]
    except Exception as exc:  # noqa: BLE001 - optional source
        log.info("Reddit research skipped: %s", exc)
        return []


class ResearchEngine:
    def __init__(self, cfg, db: Database, stats: YouTubeStats, llm: LLM):
        self.cfg = cfg
        self.db = db
        self.stats = stats
        self.llm = llm

    # -------------------------------------------------------------- sources
    def _seed_queries(self) -> list[str]:
        niche = self.cfg.channel_niche or "technology"
        queries = [part.strip() for part in niche.split(",") if part.strip()][:3]
        recent = self.db.list_videos(status="published", limit=10)
        seeds: list[str] = []
        for video in recent:
            tags = (video.get("brief") or {}).get("tags") or []
            if tags:
                seeds.append(tags[0])
        return list(dict.fromkeys(queries + seeds[:2])) or ["youtube shorts tips"]

    def collect(self) -> dict:
        """Fetch raw evidence from the enabled sources (never raises)."""
        findings: dict = {"youtube": [], "news": [], "reddit": [], "queries": []}
        queries = self._seed_queries()
        findings["queries"] = queries

        if self.cfg.research_youtube:
            for query in queries[:2]:
                results = self.stats.top_search(query, limit=6)
                findings["youtube"].extend(results)

        if self.cfg.research_news:
            for query in queries[:2]:
                findings["news"].extend(_news_headlines(query, limit=6))

        if self.cfg.research_reddit:
            findings["reddit"].extend(_reddit_discussions(queries[0]))

        if not (findings["youtube"] or findings["news"] or findings["reddit"]):
            log.warning("No research source returned data")
        return findings

    # ----------------------------------------------------------------- run
    def run(self, niche: str | None = None, limit: int = 6) -> tuple[list[dict], str]:
        """Blocking research run. Returns (topics, evidence_summary)."""
        findings = self.collect()
        channel_titles = [
            (v.get("brief") or {}).get("title", "")
            for v in self.db.list_videos(status="published", limit=15)
        ]
        channel_stats = {}
        try:
            channel_stats = self.stats.channel_statistics()
        except Exception as exc:  # noqa: BLE001 - nice-to-have context
            log.info("Channel stats unavailable for research: %s", exc)

        evidence_summary = (
            f"YouTube results: {len(findings['youtube'])} | "
            f"News headlines: {len(findings['news'])} | "
            f"Reddit posts: {len(findings['reddit'])}"
        )

        prompt = {
            "channel_niche": niche or self.cfg.channel_niche,
            "channel": channel_stats,
            "recently_published_titles": channel_titles,
            "research_queries": findings["queries"],
            "raw_findings": {
                "youtube_top_results": findings["youtube"],
                "news_headlines": findings["news"],
                "reddit_discussions": findings["reddit"],
            },
            "instructions": (
                f"Produce {limit} ORIGINAL Shorts topic ideas. Use the raw findings as "
                "evidence of demand; do not copy titles. Label anything not directly "
                "supported by the raw findings as an estimate."
            ),
            "generated_at": now_ist().isoformat(),
        }

        payload = self.llm.json(
            system=RESEARCH_SYSTEM,
            prompt=json.dumps(prompt, ensure_ascii=False, indent=1),
        )
        topics = payload if isinstance(payload, list) else payload.get("topics", [])
        topics = [t for t in topics if isinstance(t, dict) and t.get("topic")][:limit]
        if not topics:
            raise ResearchError("Research model returned no usable topics.")

        self.db.save_topics(
            niche=niche or self.cfg.channel_niche,
            topics=topics,
            evidence=evidence_summary,
        )
        return topics, evidence_summary


def format_topics(topics: list[dict], evidence: str) -> str:
    """Render research results for Telegram (HTML)."""
    import html as _html

    esc = _html.escape
    lines = ["🔬 <b>Content research — topic ideas</b>", f"<i>Evidence: {esc(evidence)}</i>", ""]
    for index, topic in enumerate(topics, start=1):
        score = topic.get("priority_score", "?")
        confidence = topic.get("confidence", "unknown")
        lines.append(
            f"<b>{index}. {esc(str(topic.get('topic', 'Untitled')))}</b> "
            f"— priority {score}/100, confidence {confidence}"
        )
        lines.append(f"💡 {esc(str(topic.get('why_now', '')))}")
        lines.append(f"👥 {esc(str(topic.get('audience', '')))}")
        lines.append(f"📊 {esc(str(topic.get('evidence', '')))}")
        lines.append(f"🪝 Hook: <i>{esc(str(topic.get('hook', '')))}</i>")
        lines.append(f"🎞 Format: {esc(str(topic.get('format', '')))}")
        lines.append(f"🏷 Title: <i>{esc(str(topic.get('title', '')))}</i>")
        outline = topic.get("outline") or ""
        if isinstance(outline, list):
            outline = " · ".join(str(o) for o in outline)
        lines.append(f"🧾 {esc(str(outline))}")
        lines.append(f"✅ Why this score: {esc(str(topic.get('priority_reason', '')))}")
        limitations = topic.get("limitations") or ""
        if limitations:
            lines.append(f"⚠️ {esc(str(limitations))}")
        sources = topic.get("sources") or []
        if sources:
            lines.append("🔗 " + " ".join(esc(str(s)) for s in sources[:3]))
        lines.append("")
    lines.append("<i>Ideas are opportunities, not guarantees. Verify before filming.</i>")
    return "\n".join(lines)
