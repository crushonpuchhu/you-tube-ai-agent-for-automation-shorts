"""Tests for the YouTube search query built from a Google headline.

A full Hindi news headline returns ZERO videos from YouTube search (verified
against the live API: "Tech Tips: फोन चार्जर पर लिखा R कोड…" -> 0 results,
"tech tips" -> 1, "charger code" -> 5). Cycles therefore searched with the raw
headline and always ended as `no_source`.
"""

from __future__ import annotations

import unittest

from features.content_research import build_youtube_query, google_topic_candidates

NICHE = "coding tips, tech setup, AI tools, productivity"

HINDI_HEADLINE = (
    "Tech Tips: फोन चार्जर पर लिखा R कोड क्या बताता है? 99% लोग करते हैं "
    "नजरअंदाज, सच जानकर चौंक जाएंगे - Amar Ujala"
)


class BuildQueryTests(unittest.TestCase):
    def test_hindi_headline_becomes_short_latin_query(self):
        query = build_youtube_query(HINDI_HEADLINE, NICHE)
        self.assertEqual(query, "Tech Tips")

    def test_site_suffix_is_removed(self):
        query = build_youtube_query("Best AI Tools for Editing - Some Site", NICHE)
        self.assertNotIn("Some Site", query)

    def test_qr_code_headline_keeps_latin_words(self):
        query = build_youtube_query(
            "Tech Tips: ऐसे घर बैठे खुद ही बनाएं अपना QR कोड - Amar Ujala", NICHE
        )
        self.assertIn("QR", query)

    def test_pure_devanagari_falls_back_to_first_niche_segment(self):
        query = build_youtube_query("फोन चार्जर पर लिखा कोड", NICHE)
        self.assertEqual(query, "coding tips")

    def test_query_is_short(self):
        for title in (HINDI_HEADLINE, "a b c d e f g h i j k l m n o p"):
            self.assertLessEqual(len(build_youtube_query(title, NICHE)), 120)


class TopicCarriesQueryTests(unittest.TestCase):
    def test_google_topics_expose_a_search_query(self):
        fetcher = lambda url, timeout=15: [  # noqa: E731
            {
                "title": HINDI_HEADLINE,
                "link": "https://news.example/a",
                "description": "tech tips",
            }
        ]
        topics = google_topic_candidates(
            NICHE, "IN", "Hindi", limit=2, fetcher=fetcher, include_trends=False
        )
        self.assertTrue(topics)
        self.assertEqual(topics[0]["search_query"], "Tech Tips")


if __name__ == "__main__":
    unittest.main()
