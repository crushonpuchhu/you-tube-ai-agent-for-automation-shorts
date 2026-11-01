"""Research niche topics for the autonomous workflow.

This is an *additive* research layer.  When the existing
``agent.research.ResearchEngine`` is supplied it is reused (same YouTube /
news / Reddit sources and same Gemini client).  Otherwise the function falls
back to the shared ``agent.llm.LLM`` helper.  On any failure it returns an
empty list instead of raising into the scheduler.

Every returned topic carries an ``evidence`` string.  Topics without evidence
are dropped — the workflow never promises virality and never presents an
unverified guess as a trend.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from agent.clock import now_ist

log = logging.getLogger(__name__)

# Keyless Google endpoints. Gemini's `google_search` grounding tool is not
# available on every API tier (it answers 429 for some plans), but these RSS
# feeds are public, stable and reachable from a cloud IP.
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss"
_UA = "Mozilla/5.0 (compatible; ShortsAgent/1.0; +https://github.com)"

RESEARCH_PROMPT = """\
You are the research planner for a YouTube Shorts channel.

Channel niche: {niche}
Audience region: {region}
Language: {language}
Research window: the last {lookback_hours} hours (recency matters).
Content mix requested by the owner: {content_mix}

Return ONLY a JSON array (no markdown fences) of 3-6 topic objects:
{{
  "topic": "specific Shorts idea",
  "why_now": "why this is worth making now",
  "audience": "who it is for",
  "evidence": "what evidence supports this (name the source or say it is an evergreen hypothesis)",
  "hook": "first 3-5 seconds hook line",
  "format": "talking head / demo / listicle / b-roll + voiceover",
  "sources": ["url"],
  "priority_score": 0-100,
  "confidence": "high | medium | low",
  "limitations": "what is estimated rather than verified",
  "kind": "trending | evergreen"
}}

Rules:
- Use the supplied research findings as evidence. Do not invent view counts,
  trends or news that are not present in the findings.
- Never promise virality. Explain the score in "why_now".
- Prefer topics that can be explained or demonstrated clearly on camera.
- Output 3-6 topics, most promising first.
"""


@dataclass
class ResearchTopic:
    topic: str
    why_now: str = ""
    audience: str = ""
    evidence: str = ""
    hook: str = ""
    format: str = ""
    sources: list[str] = field(default_factory=list)
    priority_score: int = 0
    confidence: str = "low"
    limitations: str = ""
    kind: str = "evergreen"
    niche: str = ""
    discovered_at: str = ""

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "why_now": self.why_now,
            "audience": self.audience,
            "evidence": self.evidence,
            "hook": self.hook,
            "format": self.format,
            "sources": list(self.sources),
            "priority_score": self.priority_score,
            "confidence": self.confidence,
            "limitations": self.limitations,
            "kind": self.kind,
            "niche": self.niche,
            "discovered_at": self.discovered_at or now_ist().isoformat(),
        }


def _normalise_topic(raw: dict, niche: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    topic = " ".join(str(raw.get("topic") or "").split())
    evidence = " ".join(str(raw.get("evidence") or "").split())
    if not topic or not evidence:
        return None
    try:
        score = int(float(raw.get("priority_score") or 0))
    except (TypeError, ValueError):
        score = 0
    confidence = str(raw.get("confidence") or "low").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    sources = raw.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    return ResearchTopic(
        topic=topic[:240],
        why_now=str(raw.get("why_now") or "")[:600],
        audience=str(raw.get("audience") or "")[:300],
        evidence=evidence[:600],
        hook=str(raw.get("hook") or "")[:300],
        format=str(raw.get("format") or "")[:120],
        sources=[str(s)[:500] for s in sources if str(s).strip()][:5],
        priority_score=max(0, min(100, score)),
        confidence=confidence,
        limitations=str(raw.get("limitations") or "")[:400],
        kind=str(raw.get("kind") or "evergreen").lower(),
        niche=niche,
        discovered_at=now_ist().isoformat(),
    ).to_dict()


def _dedupe(topics: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for topic in topics:
        key = " ".join(str(topic.get("topic") or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(topic)
    return out


# ------------------------------------------------------- Google web search
def _fetch_rss(url: str, timeout: int = 15) -> list[dict]:
    """Fetch and parse an RSS feed into simple dicts (never raises)."""
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read()
    root = ET.fromstring(payload)
    items: list[dict] = []
    for item in root.findall(".//item"):
        items.append(
            {
                "title": " ".join((item.findtext("title") or "").split()),
                "link": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
                "description": " ".join(
                    (item.findtext("description") or "").split()
                )[:400],
            }
        )
    return items


def _niche_tokens(niche: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", (niche or "").lower())
        if len(token) > 2
    }


def _google_locale(region: str, language: str) -> tuple[str, str]:
    geo = (region or "IN").upper()[:2]
    lang = (language or "Hindi").lower()
    code = "hi" if lang.startswith("hi") else "en"
    return f"{code}-{geo}", geo


def google_topic_candidates(
    niche: str,
    region: str = "IN",
    language: str = "Hindi",
    limit: int = 3,
    *,
    fetcher: Any = None,
    include_trends: bool = True,
) -> list[dict]:
    """Google-sourced topic candidates (Google News RSS + Google Trends RSS).

    Only items that share a keyword with the niche are kept, so a trending
    cricket score never becomes a coding Short. Every returned topic carries
    the article URL as ``evidence``, which the pipeline requires.
    """
    fetch = fetcher or _fetch_rss
    niche = " ".join((niche or "").split()) or "fitness"
    tokens = _niche_tokens(niche)
    hl, geo = _google_locale(region, language)

    raw_items: list[dict] = []
    queries: list[str] = [niche]
    for segment in re.split(r"[,;|]", niche):
        segment = " ".join(segment.split())
        if segment and segment.lower() not in {q.lower() for q in queries}:
            queries.append(segment)
    for query in queries[:3]:
        news_url = (
            f"{GOOGLE_NEWS_RSS}?"
            + urllib.parse.urlencode(
                {
                    "q": query,
                    "hl": f"{hl}",
                    "gl": geo,
                    "ceid": f"{geo}:{hl.split('-')[0]}",
                }
            )
        )
        try:
            raw_items.extend(fetch(news_url))
        except Exception as exc:  # noqa: BLE001 - research must never crash the bot
            log.warning("Google News fetch failed for %r: %s", query, str(exc)[:160])

    if include_trends:
        try:
            raw_items.extend(
                fetch(f"{GOOGLE_TRENDS_RSS}?{urllib.parse.urlencode({'geo': geo})}")
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Trends fetch failed: %s", str(exc)[:200])

    scored: list[tuple[int, dict]] = []
    seen_titles: set[str] = set()
    for item in raw_items:
        title = " ".join(str(item.get("title") or "").split())
        link = str(item.get("link") or "").strip()
        if not title or not link:
            continue
        haystack = f"{title} {item.get('description') or ''}".lower()
        hits = sum(1 for token in tokens if token in haystack)
        if tokens and hits == 0:
            continue  # off-niche trend (sports, politics, celebrity)
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)

        score = min(85, 45 + hits * 10)
        topic_text = title if len(title) <= 120 else title[:117].rstrip() + "…"
        scored.append(
            (
                score,
                {
                    "topic": topic_text,
                    "why_now": f"Surfaced by Google Search / News for {niche!r}.",
                    "audience": f"{language} speakers in {geo}",
                    "evidence": f"{title} — {link}",
                    "hook": "",
                    "format": "talking head / demo",
                    "sources": [link],
                    "priority_score": score,
                    "confidence": "medium",
                    "limitations": "Headline relevance only; verify before recording.",
                    "kind": "trending",
                },
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    log.info(
        "Google search: %d niche-matched topic(s) from %d result(s)",
        len(scored),
        len(raw_items),
    )
    return [item for _, item in scored[: max(1, int(limit))]]


def research_topics(
    niche: str,
    region: str,
    language: str,
    lookback_hours: int,
    *,
    engine: Any = None,
    llm: Any = None,
    limit: int = 5,
    content_mix: str = "70% trending, 30% evergreen educational",
    google_search: bool = False,
    google_fetcher: Any = None,
) -> list[dict]:
    """Return evidence-backed topic candidates (never raises).

    ``engine`` is the existing ``agent.research.ResearchEngine`` instance and
    ``llm`` the existing ``agent.llm.LLM`` instance.  Both are optional so the
    function is easy to test and to call from a fresh process.

    ``google_search`` runs the keyless Google News/Trends search first; the
    existing research engine and the shared Gemini helper stay as fallbacks.
    """
    limit = max(1, int(limit))
    niche = " ".join((niche or "").split()) or "fitness"

    # 0) Google Search (News + Trends RSS) — no API key, no Gemini grounding
    # quota. Only niche-matched headlines survive.
    if google_search:
        try:
            candidates = google_topic_candidates(
                niche,
                region=region or "IN",
                language=language or "Hindi",
                limit=limit,
                fetcher=google_fetcher,
            )
            normalised = [
                cleaned
                for raw in candidates
                if (cleaned := _normalise_topic(raw, niche))
            ]
            if normalised:
                return _dedupe(normalised)[:limit]
            log.info("Google search returned no niche-matched topics")
        except Exception as exc:  # noqa: BLE001 - research must never crash the bot
            log.warning("Google topic search failed: %s", str(exc)[:200])

    # 1) Preferred path: reuse the existing research engine (real sources).
    if engine is not None:
        try:
            topics, evidence_summary = engine.run(niche=niche, limit=limit)
            normalised: list[dict] = []
            for raw in topics or []:
                item = dict(raw)
                if not item.get("evidence"):
                    item["evidence"] = evidence_summary or ""
                if not item.get("evidence"):
                    continue
                cleaned = _normalise_topic(item, niche)
                if cleaned:
                    normalised.append(cleaned)
            if normalised:
                return _dedupe(normalised)[:limit]
            log.info("Research engine returned no evidence-backed topics")
        except Exception as exc:  # noqa: BLE001 - research must never crash the bot
            log.warning("Research engine failed: %s", str(exc)[:200])

    # 2) Fallback: shared Gemini helper, still requiring an evidence field.
    if llm is not None:
        prompt = RESEARCH_PROMPT.format(
            niche=niche,
            region=region or "IN",
            language=language or "Hindi",
            lookback_hours=max(1, int(lookback_hours)),
            content_mix=content_mix,
        )
        try:
            payload = llm.json(prompt=prompt, temperature=0.5, max_tokens=4096)
            items = payload if isinstance(payload, list) else (payload or {}).get("topics", [])
            normalised = [
                cleaned
                for raw in items or []
                if (cleaned := _normalise_topic(raw, niche))
            ]
            if normalised:
                return _dedupe(normalised)[:limit]
            log.warning("Research model returned no usable evidence-backed topics")
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM research failed: %s", str(exc)[:200])

    return []


def format_topics(topics: list[dict]) -> str:
    """Small helper for logs / manual diagnostics."""
    if not topics:
        return "No evidence-backed topics found."
    return json.dumps(topics, ensure_ascii=False, indent=2)
