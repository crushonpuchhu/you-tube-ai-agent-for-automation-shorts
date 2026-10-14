"""Persistent background scheduler.

A DB-backed worker thread (not an in-memory queue, not a sleep loop inside the
chat) executes due tasks: scheduled publishing, analytics sync, daily goal
monitoring, research and the weekly report.

Reliability properties:
* every task lives in SQLite -> survives restarts and crashes
* running tasks are re-queued automatically after a crash/restart
* temporary errors are retried with exponential backoff
* duplicate publishes are impossible (unique dedupe keys + atomic status claim)
* graceful shutdown returns in-flight tasks to the queue
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta

from agent.analytics import YouTubeStats
from agent.clock import from_iso, next_weekday, now_ist, now_iso, today_key, to_iso
from agent.db import Database
from agent.daily_goal import DailyGoalManager
from agent.errors import AuthRequired, ResearchError
from agent.notifier import Notifier

log = logging.getLogger(__name__)

RETRY_BASE_MINUTES = 5


class Scheduler:
    def __init__(
        self,
        cfg,
        db: Database,
        notifier: Notifier,
        publisher,
        goal: DailyGoalManager,
        stats: YouTubeStats,
        research_engine=None,
        tick_seconds: int = 30,
    ):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.publisher = publisher
        self.goal = goal
        self.stats = stats
        self.research_engine = research_engine
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        recovered = self.db.recover_stale_tasks(
            (now_ist() - timedelta(hours=1)).isoformat()
        )
        if recovered:
            log.info("Recovered %d in-flight task(s) after restart", recovered)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="shorts-scheduler", daemon=True)
        self._thread.start()
        log.info("Scheduler started (tick=%ss, IST)", self.tick_seconds)

        # Reclaim disk from previous runs right away, then again daily.
        if self.cfg.video_retention_days >= 0:
            threading.Thread(target=self._startup_sweep, name="startup-sweep",
                             daemon=True).start()

    def _startup_sweep(self) -> None:
        try:
            from agent import cleanup
            from config import VIDEO_DIR
            cleanup.sweep(self.db, VIDEO_DIR, self.cfg.video_retention_days)
        except Exception:  # noqa: BLE001 - hygiene must never break startup
            log.exception("Startup video sweep failed")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        released = self.db.release_running_tasks()
        if released:
            log.info("Released %d in-flight task(s) for the next run", released)

    # ------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the scheduler must never die
                log.exception("Scheduler tick failed")
            self._stop.wait(self.tick_seconds)

    def _tick(self) -> None:
        try:
            self.goal.check()
        except Exception:  # noqa: BLE001
            log.exception("Daily goal check failed")

        try:
            self.ensure_recurring()
        except Exception:  # noqa: BLE001
            log.exception("Recurring task setup failed")

        for task in self.db.claim_due_tasks(now_iso()):
            worker = threading.Thread(
                target=self._run_task, args=(task,), name=f"task-{task['id']}", daemon=True
            )
            worker.start()
            self._workers.add(worker)

    # ------------------------------------------------------- recurring tasks
    def ensure_recurring(self) -> None:
        now = now_ist()
        cfg = self.cfg

        # Analytics sync (default every 6 hours) — feeds scheduling + weekly report.
        step = max(1, int(cfg.analytics_sync_hours))
        slot = f"{now.strftime('%Y-%m-%d')}T{now.hour // step}"
        self.db.add_task("analytics_sync", now_iso(), dedupe_key=f"sync:{slot}")

        # Daily content research, once the configured hour has passed.
        if cfg.research_enabled and now.hour >= cfg.research_hour:
            self.db.add_task("research", now_iso(),
                             dedupe_key=f"research:{today_key(now)}", max_attempts=2)

        # Disk hygiene — sweep stale/orphaned downloads once a day.
        if cfg.video_retention_days >= 0 and now.hour >= 2:
            self.db.add_task("cleanup_videos", now_iso(),
                             dedupe_key=f"cleanup:{today_key(now)}", max_attempts=2)

        # Weekly report — catch up if we missed one while the PC was off.
        if cfg.weekly_report_enabled:
            last = self.db.get_setting("last_weekly_report")
            due_now = last is None or (now - from_iso(last)).days >= 7
            if due_now and (now.hour > cfg.weekly_hour or (
                    now.hour == cfg.weekly_hour and now.minute >= cfg.weekly_minute)):
                self.db.add_task("weekly_report", now_iso(),
                                 dedupe_key=f"weekly:{today_key(now)}", max_attempts=2)
            else:
                upcoming = next_weekday(now, cfg.weekly_weekday, cfg.weekly_hour,
                                        cfg.weekly_minute)
                self.db.add_task("weekly_report", to_iso(upcoming),
                                 dedupe_key=f"weekly:{upcoming.strftime('%Y-%m-%d')}",
                                 max_attempts=2)

    # ------------------------------------------------------------ scheduling
    def schedule_video(self, video_row_id: int, run_at_iso: str) -> int | None:
        """Persist a scheduled publish. Survives restarts by design."""
        task_id = self.db.add_task(
            "publish_video",
            run_at_iso,
            payload={"video_id": video_row_id},
            dedupe_key=f"publish:{video_row_id}",
            max_attempts=3,
        )
        if task_id is None:
            log.info("Video %d already scheduled", video_row_id)
            return None
        self.db.update_video(video_row_id, status="scheduled", scheduled_for=run_at_iso)
        log.info("Scheduled video %d for %s", video_row_id, run_at_iso)
        return task_id

    def cancel_video(self, video_row_id: int) -> bool:
        cancelled = False
        for task in self.db.list_tasks(status="pending", limit=200):
            if task.get("dedupe_key") == f"publish:{video_row_id}":
                cancelled = self.db.cancel_task(task["id"]) or cancelled
        if cancelled:
            self.db.update_video(video_row_id, status="analyzed", scheduled_for=None)
        return cancelled

    def queue_list(self) -> list[dict]:
        return [t for t in self.db.list_tasks("pending", limit=50)
                if t["kind"] == "publish_video"]

    # -------------------------------------------------------------- execution
    def _run_task(self, task: dict) -> None:
        task_id = task["id"]
        try:
            self._dispatch(task)
        except AuthRequired as exc:
            self._handle_failure(task, f"auth: {exc}", auth=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Task %s (%s) failed", task_id, task["kind"])
            self._handle_failure(task, str(exc))
        else:
            self.db.complete_task(task_id)
            if task["kind"] == "weekly_report":
                self.db.set_setting("last_weekly_report", now_iso())
        finally:
            self._workers.discard(threading.current_thread())

    def _handle_failure(self, task: dict, error: str, auth: bool = False) -> None:
        task_id = task["id"]
        attempts = int(task.get("attempts") or 0)
        max_attempts = int(task.get("max_attempts") or 3)

        if auth and self.db.claim_notification(f"auth:{task['kind']}"):
            self.notifier.send(
                "🔐 <b>YouTube authorization is required</b>\n\n"
                f"Task: {task['kind']}\n{error}\n\n"
                "Run <code>python scripts/youtube_auth.py --force</code> and the "
                "agent will retry automatically."
            )

        if attempts + 1 >= max_attempts:
            self.db.fail_task(task_id, error)
            if task["kind"] == "publish_video":
                # publish_and_notify() already told the user why it failed.
                log.error("Publish task %s permanently failed: %s", task_id, error)
            elif not auth and self.db.claim_notification(f"task_failed:{task_id}"):
                self.notifier.send(
                    f"⚠️ <b>Background task failed</b>\n\n"
                    f"Task: {task['kind']}\nReason: {error}"
                )
            return

        delay = min(60, RETRY_BASE_MINUTES * (2 ** attempts))
        next_run = (now_ist() + timedelta(minutes=delay)).isoformat()
        self.db.retry_task(task_id, error, next_run)
        log.info("Task %s retried in %s min (%s)", task_id, delay, error[:120])

    def _dispatch(self, task: dict) -> None:
        kind = task["kind"]
        payload = task.get("payload") or {}

        if kind == "publish_video":
            self.publisher.publish_and_notify(int(payload["video_id"]))
            return

        if kind == "analytics_sync":
            videos = self.db.list_videos(status="published", limit=60)
            if videos:
                self.stats.sync_to_db(self.db, videos)
            return

        if kind == "weekly_report":
            self._send_weekly_report()
            return

        if kind == "research":
            self._run_research()
            return

        if kind == "cleanup_videos":
            from agent import cleanup
            from config import VIDEO_DIR
            cleanup.sweep(self.db, VIDEO_DIR, self.cfg.video_retention_days)
            return

        log.warning("Unknown task kind: %s", kind)

    def _send_weekly_report(self) -> None:
        from agent.weekly import build_report

        text = asyncio.run(
            build_report(self.db, self.stats, self._llm(), default_target=self.cfg.daily_goal)
        )
        self.notifier.send(text)
        log.info("Weekly report sent")

    def _llm(self):
        if not hasattr(self, "_llm_client"):
            from agent.llm import LLM
            self._llm_client = LLM(self.cfg)
        return self._llm_client

    def _run_research(self) -> None:
        engine = self.research_engine
        if engine is None:
            from agent.research import ResearchEngine
            engine = ResearchEngine(self.cfg, self.db, self.stats, self._llm())
            self.research_engine = engine
        from agent.research import format_topics

        try:
            topics, evidence = engine.run()
        except ResearchError as exc:
            log.warning("Research produced nothing: %s", exc)
            return
        self.notifier.send(format_topics(topics, evidence))
