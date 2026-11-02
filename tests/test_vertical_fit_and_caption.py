"""Tests for the 9:16 fit mode and the burned-in caption line.

Two owner complaints drove these:
  * the Short cut the sides off, so speakers/screens fell out of frame
  * no on-screen text, which costs engagement
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.autonomous_pipeline import _caption_text
from features.shorts_editor import (
    VERTICAL_CROP,
    VERTICAL_FIT,
    ShortsEditorError,
    build_vertical_filter,
    cut_vertical_clip,
)


class VerticalFilterTests(unittest.TestCase):
    def test_fit_keeps_the_whole_frame(self):
        chain = build_vertical_filter(1080, 1920, VERTICAL_FIT)
        # Foreground is shrunk to fit (decrease) and centred on a blurred copy.
        self.assertIn("force_original_aspect_ratio=decrease", chain)
        self.assertIn("boxblur", chain)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", chain)

    def test_fit_is_the_default(self):
        self.assertEqual(
            build_vertical_filter(1080, 1920),
            build_vertical_filter(1080, 1920, VERTICAL_FIT),
        )

    def test_crop_mode_still_available(self):
        chain = build_vertical_filter(1080, 1920, VERTICAL_CROP)
        self.assertIn("force_original_aspect_ratio=increase", chain)
        self.assertNotIn("overlay", chain)


class CutModeTests(unittest.TestCase):
    def _capture(self, mode):
        calls = []

        def fake_run(args, timeout=300):
            calls.append(args)
            Path(args[-1]).write_bytes(b"x" * 2048)
            return None

        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.mp4"
            source.write_bytes(b"src")
            with mock.patch(
                "features.shorts_editor._run", side_effect=fake_run
            ):
                cut_vertical_clip(
                    source, 0, 5, Path(tmp) / "out.mp4", mode=mode
                )
        return calls[0]

    def test_fit_mode_uses_filter_complex_and_labelled_map(self):
        args = self._capture(VERTICAL_FIT)
        self.assertIn("-filter_complex", args)
        graph = args[args.index("-filter_complex") + 1]
        self.assertIn("boxblur", graph)
        self.assertTrue(graph.endswith("[v]"))
        self.assertEqual(args[args.index("-map") + 1], "[v]")

    def test_crop_mode_graph_has_no_blur(self):
        args = self._capture(VERTICAL_CROP)
        graph = args[args.index("-filter_complex") + 1]
        self.assertNotIn("boxblur", graph)

    def test_blur_is_computed_at_quarter_size(self):
        # Blurring a full 1080x1920 frame is what got ffmpeg OOM-killed on the
        # 1 GB production instance; the backdrop is blurred at 270x480 instead.
        chain = build_vertical_filter(1080, 1920, VERTICAL_FIT)
        self.assertIn("crop=270:480", chain)

    def test_oom_kill_retries_with_the_low_memory_profile(self):
        calls = []

        def fake_run(args, timeout=300):
            calls.append(args)
            if len(calls) == 1:
                raise ShortsEditorError(
                    "ffmpeg failed: process killed by signal 9 (out of memory)"
                )
            Path(args[-1]).write_bytes(b"x" * 2048)
            return None

        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.mp4"
            source.write_bytes(b"src")
            with mock.patch("features.shorts_editor._run", side_effect=fake_run):
                cut_vertical_clip(source, 0, 5, Path(tmp) / "out.mp4")

        self.assertEqual(len(calls), 2)
        retry = calls[1]
        graph = retry[retry.index("-filter_complex") + 1]
        self.assertIn("scale=720:1280", graph)
        self.assertEqual(retry[retry.index("-threads") + 1], "1")
        self.assertEqual(retry[retry.index("-preset") + 1], "ultrafast")


class CaptionTextTests(unittest.TestCase):
    def test_hook_summary_wins(self):
        brief = {"hook_summary": "A doctor explains warm-up mistakes", "title": "X"}
        self.assertEqual(_caption_text(brief), "A doctor explains warm-up mistakes")

    def test_title_is_the_fallback(self):
        self.assertEqual(_caption_text({"title": "Gym mistakes"}), "Gym mistakes")

    def test_long_text_is_cut_at_a_word_boundary(self):
        text = _caption_text({"hook_summary": "word " * 40})
        self.assertLessEqual(len(text), 81)
        self.assertTrue(text.endswith("…"))
        self.assertFalse(text[:-1].endswith(" "))

    def test_empty_brief_returns_empty(self):
        self.assertEqual(_caption_text({}), "")
        self.assertEqual(_caption_text({"hook_summary": "  "}), "")


if __name__ == "__main__":
    unittest.main()
