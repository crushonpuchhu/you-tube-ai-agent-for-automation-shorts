"""Tests for the autonomous pipeline's pure/validation helpers.

No network, Gemini, Telegram or FFmpeg calls are made here.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from features.autonomous_config import AutonomousConfig
from features.autonomous_pipeline import (
    _attribution_line,
    _unattended_allowed,
    format_approval_message,
    prepare_short,
    publish_candidate,
    run_autonomous_cycle,
)
from features.gemini_content_analyzer import (
    format_timestamp,
    parse_moments,
    parse_time,
)


class TimeParsingTests(unittest.TestCase):
    def test_parse_time_formats(self):
        self.assertEqual(parse_time("01:02:03"), 3723)
        self.assertEqual(parse_time("02:03"), 123)
        self.assertEqual(parse_time("1h2m3s"), 3723)
        self.assertEqual(parse_time(42), 42)
        self.assertIsNone(parse_time("nonsense"))
        self.assertIsNone(parse_time(True))

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(65), "01:05")
        self.assertEqual(format_timestamp(3723), "01:02:03")


class MomentValidationTests(unittest.TestCase):
    def test_valid_moment_is_normalised(self):
        moments = parse_moments(
            {"moments": [{"start": "00:10", "end": "00:40", "score": 80,
                          "confidence": "high", "reason": "clear demo"}]},
            source_duration=120,
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["start"], 10)
        self.assertEqual(moments[0]["end"], 40)
        self.assertEqual(moments[0]["start_label"], "00:10")
        self.assertEqual(moments[0]["confidence"], "high")

    def test_out_of_range_and_inverted_moments_are_dropped(self):
        moments = parse_moments(
            {
                "moments": [
                    {"start": "05:00", "end": "05:30"},  # beyond a 60s source
                    {"start": "00:30", "end": "00:10"},  # inverted
                    {"start": "bad", "end": "00:40"},
                ]
            },
            source_duration=60,
        )
        self.assertEqual(moments, [])

    def test_overlapping_moments_keep_the_better_score(self):
        moments = parse_moments(
            {
                "moments": [
                    {"start": 0, "end": 40, "score": 40},
                    {"start": 10, "end": 50, "score": 90},
                ]
            },
            source_duration=120,
            clip_min_seconds=20,
            clip_max_seconds=55,
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["score"], 90)

    def test_too_short_moment_is_expanded_or_dropped(self):
        expanded = parse_moments(
            {"moments": [{"start": 30, "end": 35, "score": 50}]},
            source_duration=120,
            clip_min_seconds=20,
            clip_max_seconds=55,
        )
        self.assertEqual(len(expanded), 1)
        self.assertGreaterEqual(expanded[0]["duration"], 20)

        # A source shorter than the minimum cannot produce a valid clip.
        dropped = parse_moments(
            {"moments": [{"start": 0, "end": 5, "score": 50}]},
            source_duration=10,
            clip_min_seconds=20,
            clip_max_seconds=55,
        )
        self.assertEqual(dropped, [])

    def test_overlong_moment_is_trimmed(self):
        moments = parse_moments(
            {"moments": [{"start": 0, "end": 100, "score": 70}]},
            source_duration=120,
            clip_min_seconds=20,
            clip_max_seconds=55,
        )
        self.assertEqual(moments[0]["duration"], 55)

    def test_markdown_fenced_payload_is_rejected_safely(self):
        self.assertEqual(parse_moments("```json\n[]\n```"), [])


class ApprovalMessageTests(unittest.TestCase):
    def _candidate(self, verified: bool = True) -> dict:
        return {
            "id": 3,
            "niche": "fitness",
            "topic": "<b>squats</b>",
            "source": {
                "title": "Full leg day",
                "channel": "Creator",
                "url": "https://youtu.be/abc",
                "source_type": "youtube",
            },
            "rights": {
                "allowed": verified,
                "verified": verified,
                "basis": "owner_media" if verified else "unknown",
                "label": "Owner-provided media" if verified else "No verified basis",
            },
            "moments": [{"start_label": "00:10", "end_label": "00:40", "duration": 30}],
        }

    def test_escapes_untrusted_text_and_shows_rights(self):
        text = format_approval_message(self._candidate(), brief={"title": "Great form"})
        self.assertIn("&lt;b&gt;squats&lt;/b&gt;", text)
        self.assertIn("Rights verified: ✅ yes", text)
        self.assertIn("00:10", text)

    def test_unverified_rights_block_publishing(self):
        text = format_approval_message(self._candidate(verified=False))
        self.assertIn("Publishing is blocked", text)


class PipelineGateTests(unittest.TestCase):
    def test_disabled_feature_returns_immediately(self):
        aconf = replace(AutonomousConfig.load(), enabled=False)

        class ExplodingStore:
            def __getattr__(self, name):  # noqa: D401 - must never be touched
                raise AssertionError(f"store.{name} accessed while disabled")

        result = run_autonomous_cycle(
            SimpleNamespace(aconf=aconf, store=ExplodingStore())
        )
        self.assertEqual(result.status, "disabled")

    def test_unattended_requires_flag_and_verified_rights(self):
        base = AutonomousConfig.load()
        self.assertFalse(_unattended_allowed(base, {"allowed": True, "verified": True}))
        opted_in = replace(base, unattended_publish=True)
        self.assertTrue(_unattended_allowed(opted_in, {"allowed": True, "verified": True}))
        self.assertFalse(_unattended_allowed(opted_in, {"allowed": False, "verified": False}))

    def test_publish_refuses_unverified_rights(self):
        candidate = {
            "id": 1,
            "video_row_id": 9,
            "rights": {"allowed": True, "verified": False, "basis": "unknown"},
        }

        class Store:
            def get_candidate(self, candidate_id):
                return candidate

            def update_candidate(self, *args, **kwargs):
                raise AssertionError("must not be updated")

        class Publisher:
            def publish(self, video_row_id):
                raise AssertionError("publisher must not be called")

        deps = SimpleNamespace(store=Store(), publisher=Publisher())
        with self.assertRaises(RuntimeError):
            publish_candidate(deps, 1)

    def test_attribution_line_mentions_cc_requirements(self):
        line = _attribution_line(
            {"title": "Source", "channel": "Creator", "url": "https://youtu.be/x"},
            {"basis": "verified_creative_commons"},
        )
        self.assertIn("Creative Commons", line)


class OwnerAuthTests(unittest.TestCase):
    @staticmethod
    def _context(aconf, configured_chat: str = "", stored: str | None = None):
        from types import SimpleNamespace

        return SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "cfg": SimpleNamespace(telegram_chat_id=configured_chat),
                    "autonomous_cfg": aconf,
                    "db": SimpleNamespace(get_setting=lambda key: stored),
                }
            )
        )

    @staticmethod
    def _update(user_id: int, chat_id: int):
        from types import SimpleNamespace

        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
        )

    def test_configured_owner_chat_is_authorised(self):
        from features.telegram_autonomous_hooks import is_owner

        aconf = AutonomousConfig.load()
        context = self._context(aconf, configured_chat="99")
        self.assertTrue(is_owner(self._update(1, 99), context))
        self.assertFalse(is_owner(self._update(1, 100), context))

    def test_optional_owner_ids_allowlist(self):
        from features.telegram_autonomous_hooks import is_owner

        aconf = replace(AutonomousConfig.load(), owner_ids=(7,))
        context = self._context(aconf)
        self.assertTrue(is_owner(self._update(7, 500), context))
        self.assertFalse(is_owner(self._update(8, 500), context))

    def test_persisted_owner_chat_id_is_the_fallback(self):
        from features.telegram_autonomous_hooks import is_owner

        aconf = AutonomousConfig.load()
        context = self._context(aconf, stored="321")
        self.assertTrue(is_owner(self._update(5, 321), context))
        self.assertFalse(is_owner(self._update(5, 999), context))

    def test_no_owner_recorded_means_denied(self):
        from features.telegram_autonomous_hooks import is_owner

        aconf = AutonomousConfig.load()
        self.assertFalse(is_owner(self._update(5, 10), self._context(aconf)))


class FullCycleTests(unittest.TestCase):
    """One complete cycle with every external dependency faked."""

    class DB:
        def __init__(self):
            self.videos = {}
            self.notifications = set()

        def insert_video(self, **kwargs):
            video_id = len(self.videos) + 1
            self.videos[video_id] = {
                "id": video_id,
                "status": kwargs.get("status"),
                "privacy": kwargs.get("privacy"),
                "file_path": kwargs.get("file_path"),
                "brief": {},
            }
            return video_id

        def update_video(self, video_id, **fields):
            if "brief_json" in fields:
                self.videos[video_id]["brief"] = fields["brief_json"]
            else:
                self.videos[video_id].update(fields)

        def get_video(self, video_id):
            return self.videos.get(int(video_id or 0))

        def claim_notification(self, key, window_seconds=0):
            if key in self.notifications:
                return False
            self.notifications.add(key)
            return True

        def delete_video(self, video_id, keep_record=False, force=False):
            self.videos.pop(int(video_id), None)
            return True

    class Notifier:
        chat_id = 42

        def __init__(self):
            self.sent = []

        def send(self, text, chat_id=None):
            self.sent.append(text)
            return True

        def send_markup(self, text, reply_markup=None, chat_id=None):
            self.sent.append(text)
            return True

        def send_video_preview(self, path, caption="", chat_id=None):
            return True

    def test_owner_media_cycle_creates_a_review_candidate(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from features.autonomous_pipeline import build_dependencies
        from features.job_store import JobStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "owner.mp4"
            source.write_bytes(b"fake media")
            (root / "owner.mp4.rights.json").write_text(
                json.dumps({"confirmed": True, "title": "Owner full session"}),
                encoding="utf-8",
            )
            clip = root / "clip.mp4"
            clip.write_bytes(b"fake clip")

            aconf = replace(
                AutonomousConfig.load(),
                enabled=True,
                youtube_discovery_enabled=False,
                source_dir=root,
                shorts_per_day=1,
                max_daily_generated_shorts=4,
                max_daily_gemini_analyses=10,
                owner_review_mode=True,
                unattended_publish=False,
            )
            cfg = SimpleNamespace(
                ffmpeg_path="ffmpeg",
                ffprobe_path="ffprobe",
                youtube_default_privacy="private",
                daily_goal=2,
                min_gap_hours=4,
                min_lead_minutes=30,
            )
            store = JobStore(root / "agent.db")
            db = self.DB()
            notifier = self.Notifier()
            deps = build_dependencies(
                cfg=cfg,
                aconf=aconf,
                db=db,
                store=store,
                notifier=notifier,
                analyzer=object(),
                youtube=None,
                publisher=None,
            )

            moments = [
                {
                    "start": 10,
                    "end": 40,
                    "duration": 30,
                    "start_label": "00:10",
                    "end_label": "00:40",
                    "reason": "clear demonstration",
                    "score": 80,
                    "confidence": "high",
                }
            ]
            cut = [dict(moments[0], clip_path=str(clip))]

            with patch(
                "features.autonomous_pipeline.research_topics",
                return_value=[{"topic": "squat form", "evidence": "youtube trend"}],
            ), patch("features.autonomous_pipeline.probe_duration", return_value=600), patch(
                "features.autonomous_pipeline.has_disk_space", return_value=True
            ), patch(
                "features.autonomous_pipeline.analyze_source_with_gemini",
                return_value=moments,
            ), patch(
                "features.autonomous_pipeline.validate_and_cut", return_value=cut
            ), patch(
                "features.autonomous_pipeline.prepare_short",
                return_value={"title": "Squat form", "description": "D"},
            ):
                result = run_autonomous_cycle(deps)

            self.assertEqual(result.status, "ok", result.summary())
            self.assertEqual(result.candidates, 1)
            candidates = store.list_candidates("pending_approval")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["rights"]["basis"], "owner_media")
            self.assertEqual(candidates[0]["source"]["title"], "Owner full session")
            self.assertTrue(any("Autonomous Short ready for review" in m for m in notifier.sent))


class MetadataTests(unittest.TestCase):
    class AsyncAnalyzer:
        def __init__(self):
            self.hint = ""

        async def analyze(self, video_path, hint=""):
            self.hint = hint
            return SimpleNamespace(to_dict=lambda: {"title": "Cut clip", "description": "D"})

    def test_prepare_short_reuses_analyzer_and_attribution(self):
        analyzer = self.AsyncAnalyzer()
        brief = prepare_short(
            "/tmp/clip.mp4",
            "squat form",
            analyzer=analyzer,
            source={
                "title": "Full leg day",
                "channel": "Creator",
                "url": "https://youtu.be/abc",
                "rights": {"basis": "creator_permission"},
            },
        )
        self.assertEqual(brief["title"], "Cut clip")
        self.assertIn("squat form", analyzer.hint)
        self.assertIn("Source: Full leg day", brief["description"])
        self.assertIn("permission", brief["description"])

    def test_async_analyzer_runs_without_a_running_loop(self):
        # prepare_short is called from worker threads; asyncio.run is safe there.
        analyzer = self.AsyncAnalyzer()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                asyncio.to_thread(
                    prepare_short, "/tmp/clip.mp4", "topic", analyzer=analyzer
                )
            )
        finally:
            loop.close()
        self.assertEqual(result["title"], "Cut clip")


if __name__ == "__main__":
    unittest.main()
