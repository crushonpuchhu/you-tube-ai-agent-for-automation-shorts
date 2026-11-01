"""Tests for the scheduler bridge and its OFF-by-default guarantees."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agent.clock import IST
from features.autonomous_config import AutonomousConfig
from features.autonomous_scheduler import (
    TASK_KIND,
    ensure_autonomous_tasks,
    handle_autonomous_task,
    next_publish_datetime,
)
from features.job_store import JobStore


class SimpleScheduler:
    """Minimal scheduler double: touching it unexpectedly is a test failure."""

    def __init__(self):
        self.cfg = None
        self.db = None


class DefaultFlagsTests(unittest.TestCase):
    def test_all_flags_default_off(self):
        import os
        from unittest.mock import patch

        flags = {
            "ENABLE_AUTONOMOUS_CONTENT": "false",
            "ENABLE_AUTONOMOUS_SCHEDULER": "false",
            "ENABLE_UNATTENDED_PUBLISH": "false",
            "ENABLE_TRENDS": "false",
            "ENABLE_HIGHLIGHTS": "false",
            "ENABLE_NL_AGENT": "false",
            "YOUTUBE_DISCOVERY_ENABLED": "false",
        }
        with patch.dict(os.environ, flags):
            aconf = AutonomousConfig.load()
        self.assertFalse(aconf.enabled)
        self.assertFalse(aconf.scheduler_enabled)
        self.assertFalse(aconf.unattended_publish)
        self.assertFalse(aconf.youtube_discovery_enabled)
        self.assertFalse(aconf.any_enabled)


class NextSlotTests(unittest.TestCase):
    def setUp(self):
        self.aconf = replace(
            AutonomousConfig.load(), publish_times_ist=("06:30", "22:00")
        )

    def test_returns_the_next_slot_today(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=IST)
        slot = next_publish_datetime(self.aconf, now)
        self.assertEqual((slot.hour, slot.minute), (22, 0))
        self.assertEqual(slot.date(), now.date())

    def test_rolls_over_to_tomorrow(self):
        now = datetime(2026, 1, 1, 23, 0, tzinfo=IST)
        slot = next_publish_datetime(self.aconf, now)
        self.assertEqual((slot.hour, slot.minute), (6, 30))
        self.assertEqual(slot.day, 2)

    def test_slot_is_always_in_the_future(self):
        now = datetime(2026, 1, 1, 6, 30, tzinfo=IST)
        slot = next_publish_datetime(self.aconf, now)
        self.assertGreater(slot, now)


class SchedulerGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self._tmp.name) / "agent.db")

    def tearDown(self):
        self._tmp.cleanup()

    class DB:
        def __init__(self):
            self.tasks = []

        def add_task(self, kind, run_at, payload=None, dedupe_key=None, max_attempts=3):
            if any(task["dedupe_key"] == dedupe_key for task in self.tasks):
                return None
            self.tasks.append(
                {"kind": kind, "run_at": run_at, "payload": payload,
                 "dedupe_key": dedupe_key}
            )
            return len(self.tasks)

    class Scheduler:
        def __init__(self, db):
            self.db = db

    def test_disabled_feature_queues_nothing(self):
        aconf = replace(
            AutonomousConfig.load(), enabled=False, scheduler_enabled=True
        )
        scheduler = SimpleScheduler()
        self.assertEqual(ensure_autonomous_tasks(scheduler, aconf=aconf, store=self.store), 0)
        self.assertFalse(hasattr(scheduler, "_touched"))

    def test_scheduler_flag_off_queues_nothing(self):
        aconf = replace(
            AutonomousConfig.load(), enabled=True, scheduler_enabled=False
        )
        scheduler = SimpleScheduler()
        self.assertEqual(ensure_autonomous_tasks(scheduler, aconf=aconf, store=self.store), 0)

    def test_enabled_queues_slots_once(self):
        aconf = replace(
            AutonomousConfig.load(),
            enabled=True,
            scheduler_enabled=True,
            publish_times_ist=("06:30", "22:00"),
            shorts_per_day=2,
            approval_lead_minutes=90,
        )
        db = self.DB()
        scheduler = self.Scheduler(db)
        now = datetime(2026, 1, 1, 0, 0, tzinfo=IST)
        added = ensure_autonomous_tasks(
            scheduler, aconf=aconf, store=self.store, now=now
        )
        self.assertEqual(added, 2)
        self.assertEqual({task["kind"] for task in db.tasks}, {TASK_KIND})
        # A second tick cannot duplicate the same day's tasks.
        self.assertEqual(
            ensure_autonomous_tasks(scheduler, aconf=aconf, store=self.store, now=now), 0
        )

    def test_paused_autopilot_queues_nothing(self):
        aconf = replace(
            AutonomousConfig.load(),
            enabled=True,
            scheduler_enabled=True,
            publish_times_ist=("22:00",),
            shorts_per_day=1,
        )
        self.store.set_paused(True, "test")
        db = self.DB()
        scheduler = self.Scheduler(db)
        added = ensure_autonomous_tasks(
            scheduler,
            aconf=aconf,
            store=self.store,
            now=datetime(2026, 1, 1, 0, 0, tzinfo=IST),
        )
        self.assertEqual(added, 0)
        self.assertEqual(db.tasks, [])

    def test_published_video_reconciles_scheduled_candidate(self):
        from features.autonomous_scheduler import reconcile_candidate_statuses

        candidate_id = self.store.create_candidate(
            cycle_id="c1",
            video_row_id=3,
            source={"video_id": "v1"},
            rights={"allowed": True, "verified": True, "basis": "owner_media"},
            topic="Topic",
            niche="fitness",
            clip_path="/tmp/clip.mp4",
            moments=[],
            status="scheduled",
        )

        class DB:
            def get_video(self, video_id):
                return {"status": "published", "url": "https://youtu.be/x"}

        changed = reconcile_candidate_statuses(self.store, DB())
        self.assertEqual(changed, 1)
        candidate = self.store.get_candidate(candidate_id)
        assert candidate is not None
        self.assertEqual(candidate["status"], "published")
        self.assertEqual(candidate["published_url"], "https://youtu.be/x")

    def test_wrong_task_kind_is_ignored(self):
        class Scheduler:
            cfg = None

        # Must not import/construct the pipeline or touch the store.
        handle_autonomous_task(Scheduler(), {"kind": "publish_video", "id": 1})


if __name__ == "__main__":
    unittest.main()
