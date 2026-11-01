"""Bridge between the existing durable scheduler and the autonomous workflow.

The existing ``agent.scheduler.Scheduler`` remains the only background worker.
This module only:

* adds ``autonomous_cycle`` tasks for the configured publish slots
  (``ensure_autonomous_tasks`` — called from ``ensure_recurring``), and
* runs a cycle when such a task becomes due (``handle_autonomous_task`` —
  called from ``_dispatch``).

Everything is wrapped by the caller in ``try/except`` so a feature failure can
never stop publishing, analytics, research or cleanup.  When the feature flags
are OFF both functions return immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from agent.clock import IST, now_ist, to_iso, today_key
from features.autonomous_config import AutonomousConfig
from features.job_store import JobStore

log = logging.getLogger(__name__)

TASK_KIND = "autonomous_cycle"


def _aconf(scheduler: Any, aconf: AutonomousConfig | None = None) -> AutonomousConfig:
    if aconf is not None:
        return aconf
    cached = getattr(scheduler, "_autonomous_config", None)
    if cached is None:
        cached = AutonomousConfig.load(getattr(scheduler, "cfg", None))
        try:
            scheduler._autonomous_config = cached
        except Exception:  # noqa: BLE001 - a slotted/mock object is fine
            pass
    return cached


def _store(scheduler: Any, store: JobStore | None = None) -> JobStore:
    if store is not None:
        return store
    cached = getattr(scheduler, "_autonomous_store", None)
    if cached is None:
        db = getattr(scheduler, "db", None)
        path = getattr(db, "path", None)
        if path is None:
            cfg = getattr(scheduler, "cfg", None)
            path = getattr(cfg, "db_path", None)
        if path is None:
            raise RuntimeError("No database path available for the autonomous store")
        cached = JobStore(path)
        try:
            scheduler._autonomous_store = cached
        except Exception:  # noqa: BLE001
            pass
    return cached


def next_publish_datetime(
    aconf: AutonomousConfig, now: datetime | None = None
) -> datetime:
    """Next configured publish slot converted to IST (for fallbacks/tests)."""
    current = (now or now_ist()).astimezone(aconf.tzinfo)
    for offset in (0, 1, 2):
        day = (current + timedelta(days=offset)).date()
        for hhmm in aconf.publish_times_ist:
            candidate = datetime.combine(day, aconf.slot_time(hhmm), tzinfo=aconf.tzinfo)
            if candidate > current:
                return candidate.astimezone(IST)
    return (current + timedelta(days=1)).astimezone(IST)


def ensure_autonomous_tasks(
    scheduler: Any,
    *,
    aconf: AutonomousConfig | None = None,
    store: JobStore | None = None,
    now: datetime | None = None,
) -> int:
    """Queue today's autonomous cycles (duplicate-safe).  Returns tasks added."""
    conf = _aconf(scheduler, aconf)
    if not (conf.enabled and conf.scheduler_enabled):
        return 0
    job_store = _store(scheduler, store)
    if job_store.is_paused():
        return 0

    current = (now or now_ist()).astimezone(conf.tzinfo)
    today = current.date()
    used = job_store.daily_count(today_key(current), "generated_shorts")
    if conf.max_daily_generated_shorts >= 0 and used >= conf.max_daily_generated_shorts:
        return 0

    db = getattr(scheduler, "db", None)
    if db is None:
        return 0

    added = 0
    for hhmm in conf.publish_times_ist[: conf.shorts_per_day]:
        slot = datetime.combine(today, conf.slot_time(hhmm), tzinfo=conf.tzinfo)
        if slot <= current:
            continue  # today's slot already started; tomorrow gets a new task
        trigger = slot - timedelta(minutes=conf.approval_lead_minutes)
        run_at = max(trigger, current)
        dedupe = f"autonomous:{today.isoformat()}:{hhmm}"
        task_id = db.add_task(
            TASK_KIND,
            to_iso(run_at),
            payload={"slot": hhmm, "reason": "publish-slot"},
            dedupe_key=dedupe,
            max_attempts=2,
        )
        if task_id is not None:
            added += 1
    if added:
        log.info("Queued %d autonomous cycle task(s) for %s", added, today)
    return added


def build_deps_from_scheduler(scheduler: Any):
    """Build pipeline dependencies from the existing scheduler's collaborators."""
    from features.autonomous_pipeline import build_dependencies

    cached = getattr(scheduler, "_autonomous_deps", None)
    if cached is not None:
        return cached

    cfg = scheduler.cfg
    db = scheduler.db
    notifier = scheduler.notifier
    aconf = _aconf(scheduler)
    store = _store(scheduler)

    analyzer = getattr(scheduler, "feature_analyzer", None)
    if analyzer is None:
        from pipeline.analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer(cfg)

    llm = getattr(scheduler, "feature_llm", None)
    if llm is None:
        research = getattr(scheduler, "research_engine", None)
        llm = getattr(research, "llm", None)
    if llm is None:
        from agent.llm import LLM

        llm = LLM(cfg)

    deps = build_dependencies(
        cfg=cfg,
        aconf=aconf,
        db=db,
        store=store,
        notifier=notifier,
        analyzer=analyzer,
        youtube=getattr(scheduler, "stats", None),
        publisher=getattr(scheduler, "publisher", None),
        scheduler=scheduler,
        goal=getattr(scheduler, "goal", None),
        research_engine=getattr(scheduler, "research_engine", None),
        llm=llm,
    )
    try:
        scheduler._autonomous_deps = deps
    except Exception:  # noqa: BLE001
        pass
    return deps


def handle_autonomous_task(
    scheduler: Any, task: dict, *, deps: Any = None
) -> None:
    """Execute one due autonomous task.  Exceptions are contained by the caller."""
    if str(task.get("kind") or "") != TASK_KIND:
        return
    conf = _aconf(scheduler)
    if not conf.enabled:
        log.info("Autonomous cycle task ignored: ENABLE_AUTONOMOUS_CONTENT is off")
        return
    job_store = _store(scheduler)
    if job_store.is_paused():
        log.info("Autonomous cycle task ignored: autopilot is paused")
        return

    from features.autonomous_pipeline import run_autonomous_cycle

    result = run_autonomous_cycle(deps or build_deps_from_scheduler(scheduler))
    log.info(
        "Autonomous cycle (%s) finished: status=%s topics=%s candidates=%s",
        task.get("id"),
        result.status,
        result.topics,
        result.candidates,
    )


def reconcile_candidate_statuses(store: JobStore, db: Any) -> int:
    """Mark candidates published after the existing scheduler confirms upload.

    Scheduled autonomous candidates are published by the ordinary
    ``publish_video`` task, which does not know about ``autonomous_candidates``.
    Reading the linked video row keeps the additive status view truthful without
    changing any existing scheduler or publisher code.
    """
    if db is None:
        return 0
    changed = 0
    for candidate in store.list_candidates("scheduled", limit=200):
        video = db.get_video(int(candidate.get("video_row_id") or 0))
        if not video or video.get("status") != "published":
            continue
        store.update_candidate(
            int(candidate["id"]),
            status="published",
            published_url=video.get("url") or "",
            error=None,
        )
        changed += 1
    if changed:
        log.info("Reconciled %d published autonomous candidate(s)", changed)
    return changed


def queue_snapshot(scheduler: Any) -> dict:
    """Small status helper used by /autopilot_status."""
    if scheduler is None:
        return {}
    conf = _aconf(scheduler)
    store = _store(scheduler)
    try:
        reconcile_candidate_statuses(store, getattr(scheduler, "db", None))
    except Exception:  # noqa: BLE001 - status must never fail
        log.exception("Could not reconcile autonomous candidate statuses")
    day = today_key()
    return {
        "paused": store.is_paused(),
        "pending_approval": store.count_candidates("pending_approval"),
        "scheduled": store.count_candidates("scheduled"),
        "published": store.count_candidates("published"),
        "discarded": store.count_candidates("discarded"),
        "errors": store.count_candidates("error"),
        "today_usage": store.daily_usage(day),
        "next_run": next_publish_datetime(conf).isoformat(),
        "last_cycle": store.last_cycle(),
    }
