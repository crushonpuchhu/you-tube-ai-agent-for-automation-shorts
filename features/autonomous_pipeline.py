"""Autonomous cycle orchestration.

    niche snapshot -> research -> YouTube/owner discovery -> rights gate
    -> permitted download/copy -> Gemini moments -> FFmpeg 9:16 clips
    -> existing metadata generator -> candidate queue
    -> existing Telegram approval / scheduler / publisher

Every step is isolated: one bad candidate, API failure or corrupt clip is
logged and skipped, and the existing bot keeps running.  All feature flags are
checked here as well as in the scheduler bridge, so the pipeline can never run
by accident on a default deployment.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.cleanup import delete_file
from agent.clock import to_iso, today_key
from agent.db import STATUS_ANALYZED, Database
from config import VIDEO_DIR, Config
from features.autonomous_config import AutonomousConfig
from features.content_research import research_topics
from features.gemini_content_analyzer import analyze_source_with_gemini
from features.job_store import JobStore
from features.shorts_editor import (
    estimate_source_bytes,
    has_disk_space,
    probe_duration,
    validate_and_cut,
)
from features.source_discovery import (
    SourceDownloadError,
    discover_owner_media,
    discover_sources,
    download_permitted_source,
)
from features.source_rights import (
    RightsDecision,
    SourcePolicy,
    can_publish,
    evaluate_source,
)
from features.youtube_discovery import search_youtube_long_videos

log = logging.getLogger(__name__)

LOCK_NAME = "autonomous_cycle"


# --------------------------------------------------------------------- context
@dataclass
class PipelineDeps:
    cfg: Config
    aconf: AutonomousConfig
    db: Database
    store: JobStore
    notifier: Any
    analyzer: Any
    youtube: Any
    publisher: Any
    scheduler: Any = None
    goal: Any = None
    research_engine: Any = None
    llm: Any = None

    @property
    def policy(self) -> SourcePolicy:
        return SourcePolicy.from_config(self.aconf)


@dataclass
class CycleResult:
    status: str
    niche: str = ""
    topics: int = 0
    candidates: int = 0
    clips: int = 0
    queued: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str = ""
    cycle_id: str = ""

    def summary(self) -> dict:
        return {
            "status": self.status,
            "niche": self.niche,
            "topics": self.topics,
            "candidates": self.candidates,
            "clips": self.clips,
            "queued": self.queued,
            "skipped": list(self.skipped),
            "error": self.error,
            "cycle_id": self.cycle_id,
        }


@dataclass
class CycleContext:
    cycle_id: str
    niche: str
    niche_source: str
    store: JobStore
    policy: SourcePolicy
    remaining: int
    generated: int = 0


def build_dependencies(
    *,
    cfg: Config,
    aconf: AutonomousConfig,
    db: Database,
    store: JobStore | None = None,
    notifier: Any,
    analyzer: Any,
    youtube: Any,
    publisher: Any,
    scheduler: Any = None,
    goal: Any = None,
    research_engine: Any = None,
    llm: Any = None,
) -> PipelineDeps:
    return PipelineDeps(
        cfg=cfg,
        aconf=aconf,
        db=db,
        store=store or JobStore(cfg.db_path),
        notifier=notifier,
        analyzer=analyzer,
        youtube=youtube,
        publisher=publisher,
        scheduler=scheduler,
        goal=goal,
        research_engine=research_engine,
        llm=llm,
    )


# ------------------------------------------------------------------- utilities
def _notify(deps: PipelineDeps, text: str, *, dedupe: str = "", manual: bool = False,
            window_seconds: float | None = 12 * 3600) -> None:
    try:
        if dedupe and not manual:
            if not deps.db.claim_notification(
                f"autonomous:{dedupe}", window_seconds=window_seconds
            ):
                return
        deps.notifier.send(text)
    except Exception:  # noqa: BLE001 - notifications must never break a cycle
        log.exception("Autonomous notification failed")


def _html(value: Any) -> str:
    return html.escape(str(value or ""))


_SKIP_LABELS = {
    "rights": "failed the rights gate (no verified reuse basis)",
    "download_disabled": "skipped: AUTONOMOUS_ALLOW_DOWNLOAD is off",
    "download_failed": "download blocked or failed (add data/cookies.txt)",
    "too_short": "shorter than YOUTUBE_MIN_SOURCE_VIDEO_MINUTES",
    "too_long": "longer than MAX_SOURCE_VIDEO_MINUTES",
    "disk_space": "not enough free disk space",
    "quota": "daily quota reached",
    "analysis_failed": "Gemini moment analysis failed",
    "no_moments": "Gemini found no clippable moment",
    "no_clips": "no clip passed the length/size validation",
    "duplicate": "already processed in an earlier cycle",
    "unreadable_media": "media file could not be read",
    "missing_owner_media": "owner media file disappeared",
    "error": "unexpected error while processing",
}


def _skip_summary(skipped: list[str], limit: int = 4) -> str:
    """Group cycle skip reasons into a short human-readable list."""
    counts: dict[str, int] = {}
    for reason in skipped or []:
        prefix = str(reason or "").split(":", 1)[0].strip() or "error"
        counts[prefix] = counts.get(prefix, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return "\n".join(
        f"· {_html(_SKIP_LABELS.get(prefix, prefix))} ({count})"
        for prefix, count in ordered
    )


def _owner_chat_id(deps: PipelineDeps) -> int | None:
    chat = getattr(deps.notifier, "chat_id", None)
    try:
        return int(chat) if chat else None
    except (TypeError, ValueError):
        return None


def next_publish_slot(deps: PipelineDeps) -> datetime:
    """Next slot from PUBLISH_TIMES_IST (the owner's configured schedule)."""
    from features.autonomous_scheduler import next_publish_datetime

    return next_publish_datetime(deps.aconf)


def _unattended_allowed(aconf: AutonomousConfig, rights: dict | None) -> bool:
    """Unattended publishing requires the explicit flag AND verified rights."""
    rights = rights or {}
    return bool(
        aconf.unattended_publish
        and rights.get("allowed")
        and rights.get("verified")
    )


# --------------------------------------------------------------- metadata step
def prepare_short(
    video_path: str | Path,
    topic: str,
    *,
    analyzer: Any,
    source: dict | None = None,
) -> dict:
    """Generate metadata for a cut clip with the *existing* analyzer."""
    if analyzer is None:
        raise RuntimeError("The existing Gemini video analyzer is not available.")

    hint = f"Autonomous Short about: {topic}. Describe only what is visible and audible in this clip."
    if source:
        hint += (
            f" The clip was cut from a longer source titled "
            f"\"{source.get('title', '')}\"."
        )

    result = analyzer.analyze(Path(video_path), hint=hint)
    if inspect.isawaitable(result):  # VideoAnalyzer.analyze is async
        result = asyncio.run(result)

    if hasattr(result, "to_dict"):
        brief = result.to_dict()
    elif isinstance(result, dict):
        brief = dict(result)
    else:  # pragma: no cover - defensive
        raise RuntimeError("The analyzer returned an unsupported metadata object.")

    if source:
        attribution = _attribution_line(source, source.get("rights") or {})
        description = str(brief.get("description") or "")
        if attribution and source.get("url") and source["url"] not in description:
            brief["description"] = f"{description}\n\n{attribution}".strip()
    return brief


def _attribution_line(source: dict, rights: dict) -> str:
    title = source.get("title") or "source video"
    channel = source.get("channel") or "original creator"
    url = source.get("url") or ""
    basis = rights.get("basis") or ""
    parts = [f"Source: {title} — {channel}"]
    if url:
        parts.append(url)
    if basis == "verified_creative_commons":
        parts.append("Reused under the source's Creative Commons licence; attribution retained.")
    elif basis in {"creator_permission", "creator_allowlist"}:
        parts.append("Reused with permission from the original creator.")
    elif basis == "owner_media":
        parts.append("Owner-provided source media.")
    return " · ".join(parts)


# ------------------------------------------------------------- approval/output
def format_approval_message(
    candidate: dict,
    *,
    brief: dict | None = None,
    clip: dict | None = None,
) -> str:
    """Owner-facing review card (source context, rights and timestamps)."""
    source = candidate.get("source") or {}
    rights = candidate.get("rights") or {}
    brief = brief or {}
    moments = candidate.get("moments") or []
    clip = clip or (moments[0] if moments else {})

    start = clip.get("start_label") or ""
    end = clip.get("end_label") or ""
    if not (start or end) and clip.get("start") is not None:
        from features.gemini_content_analyzer import format_timestamp

        start = format_timestamp(clip.get("start", 0))
        end = format_timestamp(clip.get("end", 0))
    duration = clip.get("duration") or 0

    lines = [
        "🤖 <b>Autonomous Short ready for review</b>",
        "",
        f"🎯 <b>Niche:</b> {_html(candidate.get('niche'))}",
        f"💡 <b>Topic:</b> {_html(candidate.get('topic'))}",
        "",
        "🎬 <b>Source</b>",
        f"• Title: <i>{_html(source.get('title') or 'Unknown')}</i>",
        f"• Creator/channel: {_html(source.get('channel') or 'Unknown')}",
        f"• Link: {_html(source.get('url') or 'n/a')}",
        f"• Source type: {_html(source.get('source_type') or 'unknown')}",
        f"• Rights basis: <b>{_html(rights.get('label') or rights.get('basis') or 'unknown')}</b>",
        f"• Rights verified: {'✅ yes' if rights.get('verified') else '❌ no'}",
        "",
        f"⏱ <b>Clip:</b> {_html(start)} → {_html(end)}"
        + (f" ({int(float(duration))}s)" if duration else ""),
        f"🧠 <b>Why:</b> {_html(clip.get('reason'))}",
        f"📊 <b>Editorial score:</b> {_html(clip.get('score', '?'))}/100 "
        f"({_html(clip.get('confidence') or 'unknown')} confidence)",
        "",
        f"📝 <b>Title:</b> <i>{_html(brief.get('title') or 'metadata pending')}</i>",
        f"👁 Suggested visibility: {_html(candidate.get('privacy') or 'configured default')}",
        f"🆔 Candidate #{candidate.get('id', '?')}",
    ]
    if not rights.get("verified"):
        lines += [
            "",
            "⛔ <b>Publishing is blocked:</b> the reuse basis is not verified.",
        ]
    else:
        lines += [
            "",
            "Approve to publish now, schedule it, retry the metadata, or discard.",
        ]
    return "\n".join(lines)


def send_approval_card(deps: PipelineDeps, candidate_id: int) -> None:
    candidate = deps.store.get_candidate(candidate_id)
    if not candidate:
        return
    video = deps.db.get_video(int(candidate.get("video_row_id") or 0))
    brief = (video or {}).get("brief") or {}
    candidate = dict(candidate)
    candidate["privacy"] = (video or {}).get("privacy") or ""
    text = format_approval_message(candidate, brief=brief)
    try:
        from features.telegram_autonomous_hooks import build_approval_keyboard

        keyboard = build_approval_keyboard(candidate_id)
    except Exception:  # noqa: BLE001 - Telegram import is always present in the bot
        keyboard = None

    sent = False
    if hasattr(deps.notifier, "send_markup"):
        sent = deps.notifier.send_markup(text, reply_markup=keyboard, chat_id=_owner_chat_id(deps))
    if not sent:
        deps.notifier.send(text)

    clip_path = candidate.get("clip_path")
    if clip_path and Path(clip_path).is_file() and hasattr(deps.notifier, "send_video_preview"):
        deps.notifier.send_video_preview(
            clip_path,
            caption=f"Preview — candidate #{candidate_id}",
            chat_id=_owner_chat_id(deps),
        )


# --------------------------------------------------------------- approvals
def publish_candidate(deps: PipelineDeps, candidate_id: int) -> dict:
    """Publish an approved candidate through the existing Publisher."""
    candidate = deps.store.get_candidate(candidate_id)
    if not candidate:
        raise RuntimeError(f"Candidate {candidate_id} not found.")
    rights = candidate.get("rights") or {}
    decision = RightsDecision(
        allowed=bool(rights.get("allowed")),
        verified=bool(rights.get("verified")),
        basis=str(rights.get("basis") or "unknown"),
        notes=str(rights.get("notes") or ""),
    )
    if not can_publish(decision):
        raise RuntimeError(
            "Refusing to publish: the source reuse rights are not verified. "
            "Owner approval does not replace source permission."
        )
    video_row_id = int(candidate.get("video_row_id") or 0)
    if not video_row_id:
        raise RuntimeError("Candidate has no linked video row.")
    record = deps.publisher.publish(video_row_id)
    deps.store.update_candidate(
        candidate_id,
        status="published",
        published_url=record.get("url") if isinstance(record, dict) else None,
        error=None,
    )
    return record


def schedule_candidate(deps: PipelineDeps, candidate_id: int) -> datetime:
    """Queue an approved candidate with the existing durable scheduler."""
    if deps.scheduler is None:
        raise RuntimeError("The existing scheduler is not available.")
    candidate = deps.store.get_candidate(candidate_id)
    if not candidate:
        raise RuntimeError(f"Candidate {candidate_id} not found.")
    rights = candidate.get("rights") or {}
    if not (rights.get("allowed") and rights.get("verified")):
        raise RuntimeError("Refusing to schedule: source rights are not verified.")
    video_row_id = int(candidate.get("video_row_id") or 0)
    if not video_row_id:
        raise RuntimeError("Candidate has no linked video row.")
    run_at = next_publish_slot(deps)
    task_id = deps.scheduler.schedule_video(video_row_id, to_iso(run_at))
    if task_id is None:
        # Already scheduled: keep the candidate state truthful.
        deps.store.update_candidate(candidate_id, status="scheduled")
        raise RuntimeError("This candidate is already scheduled — see /queue.")
    deps.store.update_candidate(candidate_id, status="scheduled", error=None)
    return run_at


def retry_candidate(deps: PipelineDeps, candidate_id: int) -> dict:
    """Regenerate metadata for an existing clip (existing analyzer reused)."""
    candidate = deps.store.get_candidate(candidate_id)
    if not candidate:
        raise RuntimeError(f"Candidate {candidate_id} not found.")
    clip_path = Path(str(candidate.get("clip_path") or ""))
    if not clip_path.is_file():
        raise RuntimeError(
            "The clip file is gone. Run /autopilot_run to generate a new candidate."
        )
    source = candidate.get("source") or {}
    topic = str(candidate.get("topic") or "")
    brief = prepare_short(clip_path, topic, analyzer=deps.analyzer, source=source)
    video_row_id = int(candidate.get("video_row_id") or 0)
    if video_row_id:
        deps.db.update_video(
            video_row_id,
            status=STATUS_ANALYZED,
            brief_json=brief,
            error=None,
            failed_stage=None,
            retry_count=0,
        )
    deps.store.update_candidate(candidate_id, status="pending_approval", error=None)
    send_approval_card(deps, candidate_id)
    return candidate


def discard_candidate(deps: PipelineDeps, candidate_id: int) -> None:
    candidate = deps.store.get_candidate(candidate_id)
    if not candidate:
        return
    video_row_id = int(candidate.get("video_row_id") or 0)
    if video_row_id:
        try:
            deps.db.delete_video(video_row_id, keep_record=True, force=True)
        except Exception:  # noqa: BLE001 - record cleanup is best-effort
            log.exception("Could not discard video row %s", video_row_id)
    clip_path = candidate.get("clip_path")
    if clip_path:
        delete_file(clip_path)
    deps.store.update_candidate(candidate_id, status="discarded")


# ------------------------------------------------------------------ core cycle
def run_autonomous_cycle(deps: PipelineDeps, *, manual: bool = False) -> CycleResult:
    """Run one complete autonomous cycle.  Never raises into the bot loop."""
    aconf = deps.aconf
    if not aconf.enabled:
        return CycleResult("disabled", niche=aconf.bootstrap_niche)

    store = deps.store
    if store.is_paused():
        return CycleResult("paused", niche=store.get_active_niche(aconf.bootstrap_niche).value)

    day = today_key()
    used_today = store.daily_count(day, "generated_shorts")
    if aconf.max_daily_generated_shorts >= 0 and used_today >= aconf.max_daily_generated_shorts:
        _notify(
            deps,
            "🤖 <b>Autonomous cycle skipped</b>\n\nDaily generated-Short quota "
            f"reached ({used_today}/{aconf.max_daily_generated_shorts}).",
            dedupe=f"quota:{day}",
            manual=manual,
        )
        return CycleResult("quota", niche=store.get_active_niche(aconf.bootstrap_niche).value)

    owner = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    if not store.try_acquire_lock(
        LOCK_NAME, owner, ttl_seconds=aconf.cycle_lock_ttl_minutes * 60
    ):
        log.info("Autonomous cycle already running — skipping this trigger")
        return CycleResult("locked", niche=store.get_active_niche(aconf.bootstrap_niche).value)

    result = CycleResult("ok")
    try:
        # The niche is snapshotted ONCE at cycle start. A later Telegram change
        # affects the next cycle and never mutates this one.
        niche_state = store.get_active_niche(aconf.bootstrap_niche)
        result.niche = niche_state.value
        cycle_id = store.start_cycle(niche_state.value)
        result.cycle_id = cycle_id
        policy = SourcePolicy.from_config(aconf)
        remaining = max(
            0,
            min(
                aconf.shorts_per_day,
                aconf.max_daily_generated_shorts - used_today,
            ),
        )
        if remaining <= 0:
            result.status = "quota"
            store.finish_cycle(cycle_id, "quota", result.summary())
            return result

        ctx = CycleContext(
            cycle_id=cycle_id,
            niche=niche_state.value,
            niche_source=niche_state.source,
            store=store,
            policy=policy,
            remaining=remaining,
        )

        topics = research_topics(
            niche=niche_state.value,
            region=aconf.channel_region,
            language=aconf.channel_language,
            lookback_hours=aconf.research_lookback_hours,
            engine=deps.research_engine,
            llm=deps.llm,
            limit=min(aconf.max_topics_per_cycle, max(1, remaining)),
            content_mix=aconf.content_mix,
            google_search=aconf.research_use_google,
        )
        result.topics = len(topics)
        if not topics:
            result.status = "no_topics"
            _notify(
                deps,
                "🤖 <b>Autonomous cycle</b>\n\nResearch returned no "
                "evidence-backed topics, so nothing was generated. "
                "The bot and the existing queue are unaffected.",
                dedupe=f"no_topics:{day}",
                manual=manual,
            )
            store.finish_cycle(cycle_id, "no_topics", result.summary())
            return result

        # Owner media is discovered once and can serve any topic. It is never
        # downloaded and never deleted.
        owner_media = discover_owner_media(aconf.source_dir)
        if owner_media:
            log.info("Discovered %d owner-provided source file(s)", len(owner_media))

        seen_source_ids: set[str] = set()
        rejected_count = 0

        for topic in topics:
            if ctx.generated >= ctx.remaining or ctx.generated >= remaining:
                break
            topic_text = str(topic.get("topic") or "")
            # A full news headline returns no videos from YouTube search; use
            # the short keyword query built at research time instead.
            search_query = str(topic.get("search_query") or topic_text).strip()
            language_code = (
                "en"
                if aconf.channel_language.lower().startswith("en")
                else aconf.channel_language.lower()[:2]
            )

            def _search(query: str) -> list[dict]:
                return search_youtube_long_videos(
                    query,
                    niche_state.value,
                    min_duration_minutes=aconf.youtube_min_source_video_minutes,
                    max_duration_minutes=aconf.max_source_video_minutes,
                    result_limit=aconf.youtube_search_results_per_topic,
                    youtube=deps.youtube,
                    region=aconf.channel_region,
                    language=language_code,
                    lookback_hours=aconf.youtube_source_lookback_hours,
                    license=aconf.youtube_search_license,
                )

            candidates: list[dict] = []
            if aconf.youtube_discovery_enabled:
                candidates = _search(search_query)
                if not candidates and search_query.lower() != niche_state.value.lower():
                    log.info(
                        "No source for query %r; retrying with the niche", search_query
                    )
                    candidates = _search(niche_state.value)
                log.info(
                    "Topic %r → query %r → %d candidate(s)",
                    topic_text[:60],
                    search_query,
                    len(candidates),
                )

            eligible, rejected = discover_sources(
                topic_text,
                policy,
                candidates=candidates,
                owner_media=owner_media,
            )
            rejected_count += len(rejected)

            for source in eligible:
                if ctx.generated >= ctx.remaining or ctx.generated >= remaining:
                    break
                source_id = str(source.get("video_id") or source.get("path") or "")
                if source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)

                dedupe_required = (
                    aconf.youtube_source_deduplication
                    and str(source.get("source_type") or "") != "owner_media"
                )
                if dedupe_required and source_id and store.source_seen(source_id):
                    log.info("Skipping already-processed source %s", source_id)
                    result.skipped.append(f"duplicate:{source_id}")
                    continue

                try:
                    generated, reason = _process_source(
                        deps, ctx, source, topic_text
                    )
                except Exception:  # noqa: BLE001 - one bad source must not stop the cycle
                    log.exception("Source %s failed; continuing", source_id)
                    result.skipped.append(f"error:{source_id}")
                    continue
                result.candidates += len(generated)
                result.clips += len(generated)
                ctx.generated += len(generated)
                if reason:
                    result.skipped.append(reason)
                if generated:
                    result.queued += len(generated)

        result.status = "ok"
        if ctx.generated == 0:
            result.status = "no_source"
            _notify(
                deps,
                "🤖 <b>Autonomous cycle finished</b>\n\nNo eligible source was "
                "processed. "
                + (
                    f"{rejected_count} candidate(s) failed the rights gate."
                    if rejected_count
                    else "YouTube search returned no usable Creative Commons "
                    "video for what it looked for."
                )
                + (f"\n\n<b>Why sources were skipped</b>\n{_skip_summary(result.skipped)}" if result.skipped else "")
                + "\n\n<b>How to fix</b>\n"
                "· Add long videos (5 min+) to <code>data/autonomous_sources/</code> "
                "with a matching <code>&lt;file&gt;.rights.json</code> "
                "containing <code>\"confirmed\": true</code>, or\n"
                "· Put a signed-in YouTube <code>cookies.txt</code> at "
                "<code>data/cookies.txt</code> so CC downloads are allowed, or\n"
                "· Add permitted channels to <code>SOURCE_ALLOWLIST</code>.",
                dedupe=f"no_source:{day}",
                manual=manual,
            )
        elif manual:
            _notify(
                deps,
                f"🤖 <b>Autonomous cycle finished</b>\n\n"
                f"🎬 {ctx.generated} candidate Short(s) created for niche "
                f"<b>{_html(niche_state.value)}</b>.\n"
                + (
                    "They are waiting in Telegram for your approval."
                    if not aconf.unattended_publish
                    else "They were queued through the existing scheduler."
                ),
                manual=True,
            )

        store.finish_cycle(cycle_id, result.status, result.summary())
        return result

    except Exception as exc:  # noqa: BLE001 - never crash the existing bot
        log.exception("Autonomous cycle failed")
        result.status = "error"
        result.error = str(exc)[:500]
        try:
            if result.cycle_id:
                store.finish_cycle(result.cycle_id, "error", result.summary())
        except Exception:  # noqa: BLE001
            pass
        _notify(
            deps,
            f"⚠️ <b>Autonomous cycle failed</b>\n\n<code>{_html(result.error)}</code>\n\n"
            "Existing scheduled uploads and the normal Telegram flow are unaffected.",
            dedupe="cycle_error",
            manual=manual,
            window_seconds=3600,
        )
        return result
    finally:
        try:
            store.release_lock(LOCK_NAME, owner)
        except Exception:  # noqa: BLE001
            log.exception("Could not release the autonomous cycle lock")


def _process_source(
    deps: PipelineDeps,
    ctx: CycleContext,
    source: dict,
    topic: str,
) -> tuple[list[dict], str]:
    """Process one rights-approved source. Returns (generated, skip_reason)."""
    aconf = deps.aconf
    day = today_key()
    rights = source.get("rights") or evaluate_source(source, ctx.policy).to_dict()
    decision = RightsDecision(
        allowed=bool(rights.get("allowed")),
        verified=bool(rights.get("verified")),
        basis=str(rights.get("basis") or "unknown"),
        notes=str(rights.get("notes") or ""),
    )
    if not decision.allowed or not decision.verified:
        return [], f"rights:{source.get('video_id')}"

    source_type = str(source.get("source_type") or "")
    source_path: Path | None = None
    downloaded = False

    try:
        if source_type == "owner_media":
            source_path = Path(str(source.get("path") or "")).expanduser()
            if not source_path.is_file():
                return [], f"missing_owner_media:{source.get('video_id')}"
        else:
            if not aconf.allow_download:
                log.info(
                    "Source %s skipped: AUTONOMOUS_ALLOW_DOWNLOAD is off",
                    source.get("video_id"),
                )
                return [], f"download_disabled:{source.get('video_id')}"
            if not deps.store.claim_daily(
                day, "source_downloads", aconf.max_daily_source_downloads
            ):
                return [], "quota:source_downloads"
            try:
                source_path = download_permitted_source(
                    source,
                    aconf.source_dir / "downloads",
                    ctx.policy,
                    max_minutes=aconf.max_source_video_minutes,
                    cookiefile=aconf.ytdlp_cookiefile,
                )
                downloaded = True
            except SourceDownloadError as exc:
                log.warning("Download skipped for %s: %s", source.get("video_id"), exc)
                return [], f"download_failed:{source.get('video_id')}"

        try:
            duration = probe_duration(source_path, deps.cfg.ffprobe_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read source duration: %s", exc)
            return [], f"unreadable_media:{source.get('video_id')}"

        minutes = duration / 60
        if minutes < aconf.youtube_min_source_video_minutes:
            return [], f"too_short:{source.get('video_id')}"
        if minutes > aconf.max_source_video_minutes:
            return [], f"too_long:{source.get('video_id')}"

        needed = max(
            estimate_source_bytes(duration),
            source_path.stat().st_size,
        ) * aconf.min_disk_space_multiplier
        if not has_disk_space(source_path.parent, needed):
            return [], f"disk_space:{source.get('video_id')}"

        if not deps.store.claim_daily(
            day, "gemini_analyses", aconf.max_daily_gemini_analyses
        ):
            return [], "quota:gemini_analyses"

        try:
            moments = analyze_source_with_gemini(
                source_path,
                topic,
                analyzer=deps.analyzer,
                cfg=deps.cfg,
                source_duration=duration,
                clip_min_seconds=aconf.clip_min_seconds,
                clip_max_seconds=aconf.clip_max_seconds,
                max_clips=aconf.max_clips_per_source,
            )
        except Exception:  # noqa: BLE001
            log.exception("Gemini moment analysis failed for %s", source.get("video_id"))
            deps.store.remember_source(source, status="analysis_failed")
            return [], f"analysis_failed:{source.get('video_id')}"

        if not moments:
            deps.store.remember_source(source, status="no_moments")
            return [], f"no_moments:{source.get('video_id')}"

        clip_dir = VIDEO_DIR / "autonomous" / ctx.cycle_id / str(
            abs(hash(str(source.get("video_id") or source_path.name)))
        )[:10]
        clips = validate_and_cut(
            source_path,
            moments,
            clip_dir,
            ffmpeg=deps.cfg.ffmpeg_path,
            ffprobe=deps.cfg.ffprobe_path,
            clip_min_seconds=aconf.clip_min_seconds,
            clip_max_seconds=aconf.clip_max_seconds,
            max_clips=aconf.max_clips_per_source,
            source_duration=duration,
        )
        if not clips:
            deps.store.remember_source(source, status="no_clips")
            return [], f"no_clips:{source.get('video_id')}"

        generated: list[dict] = []
        for clip in clips:
            if ctx.generated + len(generated) >= ctx.remaining:
                break
            if not deps.store.claim_daily(
                day, "generated_shorts", aconf.max_daily_generated_shorts
            ):
                break
            if not deps.store.claim_daily(
                day, "gemini_analyses", aconf.max_daily_gemini_analyses
            ):
                break

            try:
                brief = prepare_short(
                    clip["clip_path"],
                    topic,
                    analyzer=deps.analyzer,
                    source=source,
                )
            except Exception:  # noqa: BLE001
                log.exception("Metadata generation failed for %s", clip["clip_path"])
                delete_file(clip["clip_path"])
                continue

            record = _queue_candidate(deps, ctx, source, decision, topic, clip, brief)
            if record is None:
                delete_file(clip["clip_path"])
                continue
            generated.append(record)

        deps.store.remember_source(
            source, status="processed" if generated else "no_candidates"
        )
        return generated, ""

    finally:
        if downloaded and source_path is not None:
            # Temporary permitted download: reclaim it now (owner media is
            # never touched). The generated clips remain under data/videos.
            delete_file(source_path)


def _queue_candidate(
    deps: PipelineDeps,
    ctx: CycleContext,
    source: dict,
    decision: RightsDecision,
    topic: str,
    clip: dict,
    brief: dict,
) -> dict | None:
    """Create the video row + candidate record and notify/queue it."""
    aconf = deps.aconf
    privacy = deps.cfg.youtube_default_privacy
    try:
        video_row_id = deps.db.insert_video(
            file_path=clip["clip_path"],
            chat_id=_owner_chat_id(deps),
            hint=f"autonomous: {topic}",
            privacy=privacy,
            status=STATUS_ANALYZED,
        )
        deps.db.update_video(video_row_id, brief_json=brief)
    except Exception:  # noqa: BLE001
        log.exception("Could not create a video row for an autonomous clip")
        return None

    candidate_id = deps.store.create_candidate(
        cycle_id=ctx.cycle_id,
        video_row_id=video_row_id,
        source=source,
        rights=decision.to_dict(),
        topic=topic,
        niche=ctx.niche,
        clip_path=clip["clip_path"],
        moments=[clip],
    )

    if _unattended_allowed(aconf, decision.to_dict()):
        try:
            if deps.scheduler is None:
                raise RuntimeError("scheduler unavailable")
            run_at = next_publish_slot(deps)
            task_id = deps.scheduler.schedule_video(video_row_id, to_iso(run_at))
            if task_id is None:
                raise RuntimeError("video already scheduled")
            deps.store.update_candidate(candidate_id, status="scheduled")
            _notify(
                deps,
                "🤖 <b>Autonomous Short queued (unattended)</b>\n\n"
                f"🎬 <i>{_html(brief.get('title'))}</i>\n"
                f"📜 Rights: {_html(decision.label)}\n"
                f"⏰ {run_at.strftime('%A %d %B, %I:%M %p IST').replace(' 0', ' ')}\n"
                "Publishing still happens through the existing scheduler and "
                "YouTube verification path.",
            )
        except Exception as exc:  # noqa: BLE001 - fall back to owner review
            log.warning("Unattended scheduling failed, sending for review: %s", exc)
            deps.store.update_candidate(candidate_id, status="pending_approval")
            send_approval_card(deps, candidate_id)
    else:
        send_approval_card(deps, candidate_id)

    candidate = deps.store.get_candidate(candidate_id) or {}
    candidate["brief"] = brief
    return candidate
