"""Tests for the additive evidence-backed topic research layer."""

from __future__ import annotations

import unittest

from features.content_research import (
    _normalise_topic,
    research_topics,
)


class TopicNormalisationTests(unittest.TestCase):
    def test_requires_topic_and_evidence(self):
        self.assertIsNone(_normalise_topic({"topic": "Only a topic"}, "fitness"))
        self.assertIsNone(_normalise_topic({"evidence": "Only evidence"}, "fitness"))

    def test_clamps_score_and_confidence(self):
        topic = _normalise_topic(
            {"topic": "A", "evidence": "B", "priority_score": 500, "confidence": "certain"},
            "fitness",
        )
        self.assertIsNotNone(topic)
        assert topic is not None
        self.assertEqual(topic["priority_score"], 100)
        self.assertEqual(topic["confidence"], "low")
        self.assertEqual(topic["niche"], "fitness")

    def test_accepts_comma_free_sources_string(self):
        topic = _normalise_topic(
            {"topic": "A", "evidence": "B", "sources": "https://example.com"},
            "fitness",
        )
        assert topic is not None
        self.assertEqual(topic["sources"], ["https://example.com"])


class ResearchTests(unittest.TestCase):
    class Engine:
        def __init__(self, topics, evidence="evidence"):
            self.topics = topics
            self.evidence = evidence
            self.calls = 0

        def run(self, niche=None, limit=6):
            self.calls += 1
            return self.topics, self.evidence

    class LLM:
        def __init__(self, payload):
            self.payload = payload
            self.calls = 0

        def json(self, **kwargs):
            self.calls += 1
            return self.payload

    def test_engine_topics_are_reused_and_deduped(self):
        engine = self.Engine(
            [
                {"topic": "Squat form", "evidence": "youtube result"},
                {"topic": "squat   form", "evidence": "duplicate"},
                {"topic": "Hip mobility", "evidence": "reddit thread"},
            ]
        )
        topics = research_topics(
            "fitness", "IN", "Hindi", 72, engine=engine, limit=5
        )
        self.assertEqual(engine.calls, 1)
        self.assertEqual([t["topic"] for t in topics], ["Squat form", "Hip mobility"])

    def test_topics_without_evidence_are_dropped(self):
        engine = self.Engine([{"topic": "Unverified claim"}], evidence="")
        topics = research_topics("fitness", "IN", "Hindi", 72, engine=engine, limit=5)
        self.assertEqual(topics, [])

    def test_falls_back_to_llm(self):
        engine = self.Engine([{"topic": "no evidence"}], evidence="")
        llm = self.LLM([{"topic": "Beginner push-ups", "evidence": "evergreen hypothesis"}])
        topics = research_topics(
            "fitness", "IN", "Hindi", 72, engine=engine, llm=llm, limit=5
        )
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic"], "Beginner push-ups")

    def test_llm_dict_payload_is_supported(self):
        llm = self.LLM({"topics": [{"topic": "Deadlift setup", "evidence": "coach consensus"}]})
        topics = research_topics("fitness", "IN", "Hindi", 72, llm=llm, limit=5)
        self.assertEqual(topics[0]["topic"], "Deadlift setup")

    def test_engine_failure_falls_through_to_llm(self):
        class BrokenEngine:
            def run(self, niche=None, limit=6):
                raise RuntimeError("network down")

        llm = self.LLM([{"topic": "Recovery sleep", "evidence": "study summary"}])
        topics = research_topics(
            "fitness", "IN", "Hindi", 72, engine=BrokenEngine(), llm=llm, limit=5
        )
        self.assertEqual(topics[0]["topic"], "Recovery sleep")

    def test_no_sources_returns_empty(self):
        self.assertEqual(research_topics("fitness", "IN", "Hindi", 72), [])

    def test_limit_is_applied(self):
        engine = self.Engine(
            [{"topic": f"Topic {i}", "evidence": "evidence"} for i in range(10)]
        )
        topics = research_topics("fitness", "IN", "Hindi", 72, engine=engine, limit=3)
        self.assertEqual(len(topics), 3)


if __name__ == "__main__":
    unittest.main()
