"""Tests for keyless Google topic discovery (no network)."""

from __future__ import annotations

import unittest

from features.content_research import google_topic_candidates, research_topics


class _FakeGoogle:
    """Stands in for _fetch_rss: returns canned RSS items per URL kind."""

    def __init__(self, news=None, trends=None):
        self.news = news or []
        self.trends = trends or []
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int = 15):
        self.calls.append(url)
        return self.trends if "trends.google" in url else self.news


NEWS = [
    {
        "title": "Vibe coding tools are changing how developers ship",
        "link": "https://news.example/vibe-coding",
        "description": "AI coding assistants and developer productivity",
    },
    {
        "title": "India vs Namibia cricket result",
        "link": "https://news.example/cricket",
        "description": "sports",
    },
]

TRENDS = [
    {
        "title": "ai tools",
        "link": "https://trends.example/ai-tools",
        "description": "trending in IN",
    },
    {
        "title": "cbse date sheet",
        "link": "https://trends.example/cbse",
        "description": "education",
    },
]

NICHE = "coding tips, tech setup, AI tools, productivity"


class GoogleTopicTests(unittest.TestCase):
    def test_only_niche_matched_items_survive(self):
        google = _FakeGoogle(NEWS, TRENDS)
        topics = google_topic_candidates(NICHE, "IN", "English", limit=5, fetcher=google)

        titles = " | ".join(t["topic"] for t in topics)
        self.assertIn("Vibe coding", titles)
        self.assertIn("ai tools", titles)
        self.assertNotIn("cricket", titles)
        self.assertNotIn("cbse", titles)

    def test_every_topic_carries_evidence_and_source(self):
        google = _FakeGoogle(NEWS, TRENDS)
        for topic in google_topic_candidates(NICHE, "IN", "English", fetcher=google):
            self.assertTrue(topic["evidence"])
            self.assertTrue(topic["sources"])
            self.assertTrue(topic["sources"][0].startswith("http"))

    def test_limit_is_respected(self):
        google = _FakeGoogle(NEWS, TRENDS)
        self.assertLessEqual(
            len(google_topic_candidates(NICHE, "IN", "English", limit=1, fetcher=google)),
            1,
        )

    def test_fetch_failure_returns_empty_without_raising(self):
        def boom(url, timeout=15):
            raise OSError("network down")

        self.assertEqual(
            google_topic_candidates(NICHE, "IN", "English", fetcher=boom), []
        )


class ResearchFallbackTests(unittest.TestCase):
    def test_google_path_is_used_when_it_returns_topics(self):
        google = _FakeGoogle(NEWS, TRENDS)
        topics = research_topics(
            NICHE, "IN", "English", 72, limit=3, google_search=True, google_fetcher=google
        )
        self.assertTrue(topics)
        self.assertTrue(all(t["evidence"] for t in topics))

    def test_empty_google_falls_back_to_existing_engine(self):
        class Engine:
            def run(self, *, niche, limit):
                return (
                    [{"topic": "Engine topic", "evidence": "engine evidence"}],
                    "engine summary",
                )

        google = _FakeGoogle([], [])
        topics = research_topics(
            NICHE,
            "IN",
            "English",
            72,
            engine=Engine(),
            limit=3,
            google_search=True,
            google_fetcher=google,
        )
        self.assertEqual(topics[0]["topic"], "Engine topic")

    def test_google_disabled_skips_google(self):
        google = _FakeGoogle(NEWS, TRENDS)
        topics = research_topics(
            NICHE, "IN", "English", 72, limit=3, google_search=False, google_fetcher=google
        )
        self.assertEqual(topics, [])
        self.assertEqual(google.calls, [])


if __name__ == "__main__":
    unittest.main()
