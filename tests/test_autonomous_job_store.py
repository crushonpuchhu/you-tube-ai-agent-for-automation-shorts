"""Tests for the durable autonomous store (additive SQLite tables only)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.job_store import (
    NicheStorageError,
    NicheValidationError,
    JobStore,
    validate_niche,
)


class NicheValidationTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(validate_niche("  fitness   for  busy people "), "fitness for busy people")

    def test_rejects_empty(self):
        with self.assertRaises(NicheValidationError):
            validate_niche("   ")

    def test_rejects_too_long(self):
        with self.assertRaises(NicheValidationError):
            validate_niche("x" * 81)

    def test_rejects_control_characters(self):
        with self.assertRaises(NicheValidationError):
            validate_niche("fitness\x00hack")

    def test_rejects_non_string(self):
        with self.assertRaises(NicheValidationError):
            validate_niche(123)  # type: ignore[arg-type]


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "agent.db"
        self.store = JobStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------ niche
    def test_bootstrap_niche_is_default_until_persisted(self):
        state = self.store.get_active_niche("coding tips")
        self.assertEqual(state.value, "coding tips")
        self.assertFalse(state.is_persisted)

    def test_niche_persists_across_instances(self):
        saved = self.store.set_active_niche("fitness", "telegram:42")
        self.assertTrue(saved.is_persisted)
        self.assertEqual(saved.updated_by, "telegram:42")

        reopened = JobStore(self.path)
        state = reopened.get_active_niche("coding tips")
        self.assertEqual(state.value, "fitness")
        self.assertEqual(state.source, "persisted")

    def test_invalid_niche_does_not_overwrite_previous_value(self):
        self.store.set_active_niche("fitness", "owner")
        with self.assertRaises(NicheValidationError):
            self.store.set_active_niche("   ", "owner")
        self.assertEqual(self.store.get_active_niche("x").value, "fitness")

    def test_storage_failure_is_reported(self):
        from unittest.mock import patch

        self.store.set_active_niche("fitness", "owner")
        with patch.object(self.store, "_conn", side_effect=OSError("disk full")):
            with self.assertRaises(NicheStorageError):
                self.store.set_active_niche("finance", "owner")
        # The previously stored value is untouched.
        state = self.store.get_active_niche("x")
        self.assertEqual(state.value, "fitness")
        self.assertTrue(state.is_persisted)

    # ----------------------------------------------------------------- pause
    def test_pause_toggle(self):
        self.assertFalse(self.store.is_paused())
        self.store.set_paused(True, "owner")
        self.assertTrue(self.store.is_paused())
        self.store.set_paused(False, "owner")
        self.assertFalse(self.store.is_paused())

    # ---------------------------------------------------------------- quotas
    def test_daily_quota_is_a_hard_limit(self):
        day = "2026-01-01"
        self.assertTrue(self.store.claim_daily(day, "shorts", 2))
        self.assertTrue(self.store.claim_daily(day, "shorts", 2))
        self.assertFalse(self.store.claim_daily(day, "shorts", 2))
        self.assertEqual(self.store.daily_count(day, "shorts"), 2)

    def test_zero_limit_disables_metric(self):
        self.assertFalse(self.store.claim_daily("2026-01-01", "shorts", 0))

    def test_negative_limit_is_unlimited(self):
        for _ in range(5):
            self.assertTrue(self.store.claim_daily("2026-01-01", "shorts", -1))
        self.assertEqual(self.store.daily_count("2026-01-01", "shorts"), 5)

    # ----------------------------------------------------------------- locks
    def test_single_flight_lock(self):
        self.assertTrue(self.store.try_acquire_lock("cycle", "a", ttl_seconds=60))
        self.assertFalse(self.store.try_acquire_lock("cycle", "b", ttl_seconds=60))
        self.store.release_lock("cycle", "a")
        self.assertTrue(self.store.try_acquire_lock("cycle", "b", ttl_seconds=60))

    # ------------------------------------------------------------ candidates
    def test_candidate_round_trip(self):
        candidate_id = self.store.create_candidate(
            cycle_id="c1",
            video_row_id=7,
            source={"video_id": "v1", "title": "Source"},
            rights={"allowed": True, "verified": True, "basis": "owner_media"},
            topic="Topic",
            niche="fitness",
            clip_path="/tmp/clip.mp4",
            moments=[{"start": 1, "end": 30}],
        )
        candidate = self.store.get_candidate(candidate_id)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["status"], "pending_approval")
        self.assertEqual(candidate["source"]["video_id"], "v1")
        self.assertEqual(candidate["rights"]["basis"], "owner_media")
        self.assertEqual(candidate["moments"][0]["start"], 1)

        self.store.update_candidate(candidate_id, status="scheduled")
        self.assertEqual(self.store.get_candidate(candidate_id)["status"], "scheduled")
        self.assertEqual(self.store.count_candidates("scheduled"), 1)
        self.assertEqual(len(self.store.list_candidates("pending_approval")), 0)

    def test_niche_change_does_not_rewrite_existing_candidates(self):
        candidate_id = self.store.create_candidate(
            cycle_id="c1",
            video_row_id=11,
            source={"video_id": "v1"},
            rights={"allowed": True, "verified": True, "basis": "owner_media"},
            topic="Old topic",
            niche="fitness",
            clip_path="/tmp/old.mp4",
            moments=[],
        )
        before = self.store.get_candidate(candidate_id)
        self.store.set_active_niche("finance", "owner")
        after = self.store.get_candidate(candidate_id)
        assert before is not None and after is not None
        self.assertEqual(after["topic"], "Old topic")
        self.assertEqual(after["niche"], "fitness")
        self.assertEqual(after["clip_path"], "/tmp/old.mp4")
        self.assertEqual(after["status"], before["status"])

    def test_cycle_history(self):
        cycle_id = self.store.start_cycle("fitness")
        self.store.finish_cycle(cycle_id, "ok", {"candidates": 2})
        last = self.store.last_cycle()
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last["status"], "ok")
        self.assertEqual(last["summary"]["candidates"], 2)


if __name__ == "__main__":
    unittest.main()
