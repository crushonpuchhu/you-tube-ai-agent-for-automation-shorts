"""Tests for licence-scoped YouTube source discovery.

The Creative Commons search filter is what makes a third-party source pass the
hard rights gate, so it is covered explicitly.  No network calls are made: the
YouTube client is a fake that records the query arguments.
"""

from __future__ import annotations

import unittest
from unittest import mock

from features.autonomous_config import AutonomousConfig
from features.source_rights import evaluate_source
from features.youtube_discovery import search_youtube_long_videos


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeSearch:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self, **kwargs):
        self._recorder.append(kwargs)
        return _FakeRequest(
            {
                "items": [
                    {"id": {"videoId": "vid1"}},
                ]
            }
        )


class _FakeVideos:
    def list(self, **kwargs):
        return _FakeRequest(
            {
                "items": [
                    {
                        "id": "vid1",
                        "snippet": {
                            "title": "Python tutorial for beginners",
                            "channelTitle": "CC Channel",
                            "channelId": "chan1",
                            "publishedAt": "2024-01-01T00:00:00Z",
                            "description": "how to code",
                        },
                        "contentDetails": {"duration": "PT30M"},
                        "statistics": {"viewCount": "1000"},
                        "status": {"license": "creativeCommon"},
                    }
                ]
            }
        )


class _FakeYouTube:
    def __init__(self):
        self.search_calls: list[dict] = []

    def youtube(self):
        return type(
            "Service",
            (),
            {
                "search": lambda _self: _FakeSearch(self.search_calls),
                "videos": lambda _self: _FakeVideos(),
            },
        )()


class LicenseFilterTests(unittest.TestCase):
    def _search(self, license_arg):
        youtube = _FakeYouTube()
        results = search_youtube_long_videos(
            "python tutorial",
            "coding tips",
            min_duration_minutes=5,
            max_duration_minutes=120,
            result_limit=5,
            youtube=youtube,
            region="IN",
            language="en",
            lookback_hours=0,
            license=license_arg,
        )
        return youtube.search_calls[0], results

    def test_creative_common_is_sent_to_the_api(self):
        kwargs, _ = self._search("creativeCommon")
        self.assertEqual(kwargs.get("videoLicense"), "creativeCommon")

    def test_any_licence_sends_no_filter(self):
        kwargs, _ = self._search("any")
        self.assertNotIn("videoLicense", kwargs)

    def test_zero_lookback_hours_does_not_restrict_publish_date(self):
        kwargs, _ = self._search("creativeCommon")
        self.assertNotIn("publishedAfter", kwargs)

    def test_cc_result_passes_the_rights_gate(self):
        _, results = self._search("creativeCommon")
        self.assertEqual(len(results), 1)
        decision = evaluate_source(results[0])
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.basis, "verified_creative_commons")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_cc_only_and_evergreen(self):
        with mock.patch.dict(
            "os.environ",
            {"YOUTUBE_SEARCH_LICENSE": "", "YOUTUBE_SOURCE_LOOKBACK_HOURS": ""},
        ):
            aconf = AutonomousConfig.load()
        self.assertEqual(aconf.youtube_search_license, "creativeCommon")
        self.assertEqual(aconf.youtube_source_lookback_hours, 0)

    def test_env_overrides_are_honoured(self):
        with mock.patch.dict(
            "os.environ",
            {
                "YOUTUBE_SEARCH_LICENSE": "any",
                "YOUTUBE_SOURCE_LOOKBACK_HOURS": "48",
            },
        ):
            aconf = AutonomousConfig.load()
        self.assertEqual(aconf.youtube_search_license, "any")
        self.assertEqual(aconf.youtube_source_lookback_hours, 48)


class CookieFileTests(unittest.TestCase):
    def test_explicit_cookiefile_is_used(self):
        with mock.patch.dict("os.environ", {"YTDLP_COOKIEFILE": "/tmp/c.txt"}):
            self.assertEqual(AutonomousConfig.load().ytdlp_cookiefile, "/tmp/c.txt")

    def test_absent_cookiefile_is_empty(self):
        from pathlib import Path

        with mock.patch.dict("os.environ", {"YTDLP_COOKIEFILE": ""}):
            with mock.patch("features.autonomous_config.DATA_DIR", Path("/nonexistent")):
                self.assertEqual(AutonomousConfig.load().ytdlp_cookiefile, "")


class SkipSummaryTests(unittest.TestCase):
    def test_groups_reasons_by_prefix(self):
        from features.autonomous_pipeline import _skip_summary

        text = _skip_summary(
            [
                "download_failed:a",
                "download_failed:b",
                "rights:c",
            ]
        )
        self.assertIn("download blocked or failed", text)
        self.assertIn("(2)", text)
        self.assertIn("failed the rights gate", text)

    def test_empty_and_unknown_reasons(self):
        from features.autonomous_pipeline import _skip_summary

        self.assertEqual(_skip_summary([]), "")
        self.assertIn("mystery", _skip_summary(["mystery:1"]))


if __name__ == "__main__":
    unittest.main()
