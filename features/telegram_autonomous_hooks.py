"""Additive Telegram controls for the autonomous workflow.

New commands only — none of the existing commands, callbacks or conversation
states are modified.  The callback handler is registered with the ``^af:``
pattern *before* the existing generic callback handler, so it can only ever see
its own callback payloads.

Owner authorisation reuses the bot's existing mechanism: ``TELEGRAM_CHAT_ID``
or the ``owner_chat_id`` value already persisted by the existing
``_capture_chat`` handler.  ``AUTOPILOT_OWNER_IDS`` is an optional additive
allowlist of user ids.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from agent.clock import from_iso, now_ist
from features.autonomous_config import AutonomousConfig
from features.job_store import (
    JobStore,
    NicheStorageError,
    NicheValidationError,
    validate_niche,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers
def _aconf(context: ContextTypes.DEFAULT_TYPE) -> AutonomousConfig:
    cached = context.application.bot_data.get("autonomous_cfg")
    if isinstance(cached, AutonomousConfig):
        return cached
    cfg = context.application.bot_data.get("cfg")
    aconf = AutonomousConfig.load(cfg)
    context.application.bot_data["autonomous_cfg"] = aconf
    return aconf


def _store(context: ContextTypes.DEFAULT_TYPE) -> JobStore:
    cached = context.application.bot_data.get("autonomous_store")
    if isinstance(cached, JobStore):
        return cached
    cfg = context.application.bot_data["cfg"]
    store = JobStore(cfg.db_path)
    context.application.bot_data["autonomous_store"] = store
    return store


def _cfg(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["cfg"]


def _db(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["db"]


def build_deps(context: ContextTypes.DEFAULT_TYPE):
    """Build pipeline dependencies from the already-created bot objects."""
    from features.autonomous_pipeline import build_dependencies

    data = context.application.bot_data
    return build_dependencies(
        cfg=data["cfg"],
        aconf=_aconf(context),
        db=data["db"],
        store=_store(context),
        notifier=data["notifier"],
        analyzer=data["analyzer"],
        youtube=data["stats"],
        publisher=data["publisher"],
        scheduler=data["scheduler"],
        goal=data.get("goal"),
        research_engine=data.get("research"),
        llm=data.get("llm"),
    )


def is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Owner/admin check using the existing single-owner configuration."""
    aconf = _aconf(context)
    user = update.effective_user
    chat = update.effective_chat
    user_id = str(getattr(user, "id", "") or "")
    chat_id = str(getattr(chat, "id", "") or "")

    if aconf.owner_ids:
        allowed = {str(value) for value in aconf.owner_ids}
        if user_id in allowed or chat_id in allowed:
            return True

    configured = str(getattr(_cfg(context), "telegram_chat_id", "") or "").strip()
    if configured:
        return configured in {user_id, chat_id}

    stored = _db(context).get_setting("owner_chat_id")
    if stored:
        return str(stored).strip() == chat_id
    # No owner recorded yet. _capture_chat runs in an earlier handler group and
    # persists the first chat id, so this is only reached if that failed.
    return False


async def _deny(
    update: Update, text: str = "⛔ Only the channel owner can use this."
) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


def _pretty(iso_value: str) -> str:
    try:
        return from_iso(iso_value).strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
    except (TypeError, ValueError):
        return iso_value or "n/a"


def build_approval_keyboard(candidate_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Publish now", callback_data=f"af:pub:{candidate_id}"
                ),
                InlineKeyboardButton(
                    "🗓 Schedule", callback_data=f"af:sch:{candidate_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔁 Retry metadata", callback_data=f"af:retry:{candidate_id}"
                ),
                InlineKeyboardButton(
                    "❌ Discard", callback_data=f"af:dis:{candidate_id}"
                ),
            ],
        ]
    )


# -------------------------------------------------------------------- commands
def _cookie_status_line(aconf) -> str:
    """One-line health check for the yt-dlp cookie jar shown in /autopilot_status."""
    cookie_path = str(getattr(aconf, "ytdlp_cookiefile", "") or "")
    if not cookie_path:
        return "🍪 <b>Download cookies:</b> ⭕ not configured (use /autopilot_cookies)"
    path = Path(cookie_path).expanduser()
    if not path.is_file():
        return "🍪 <b>Download cookies:</b> ❌ configured file is missing"
    try:
        ok, detail = cookie_jar_status(
            path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError as exc:
        return f"🍪 <b>Download cookies:</b> ❌ unreadable ({html.escape(str(exc)[:80])})"
    icon = "✅" if ok else "❌"
    return f"🍪 <b>Download cookies:</b> {icon} {html.escape(detail)}"


async def cmd_autopilot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await _deny(update)
        return
    aconf = _aconf(context)
    store = _store(context)

    try:
        from features.autonomous_scheduler import queue_snapshot

        snapshot = await asyncio.to_thread(queue_snapshot, context.application.bot_data["scheduler"])
    except Exception as exc:  # noqa: BLE001 - status must never crash
        log.exception("Autopilot status snapshot failed")
        snapshot = {}
        log.info("Status snapshot unavailable: %s", exc)

    state = store.get_active_niche(aconf.bootstrap_niche)
    usage = snapshot.get("today_usage") or store.daily_usage()
    pending = snapshot.get("pending_approval", store.count_candidates("pending_approval"))
    scheduled = snapshot.get("scheduled", store.count_candidates("scheduled"))

    source_label = (
        "persisted in SQLite (autonomous_settings)"
        if state.is_persisted
        else "CHANNEL_NICHE default (no persisted value yet)"
    )
    flags = [
        f"content: {'✅ ON' if aconf.enabled else '⭕ OFF'}",
        f"scheduler: {'✅ ON' if aconf.scheduler_enabled else '⭕ OFF'}",
        f"unattended publish: {'✅ ON' if aconf.unattended_publish else '⭕ OFF'}",
        f"YouTube discovery: {'✅ ON' if aconf.youtube_discovery_enabled else '⭕ OFF'}",
    ]

    lines = [
        "🤖 <b>Autopilot status</b>",
        "",
        f"🎯 <b>Active niche:</b> {html.escape(state.value)}",
        f"💾 <b>Source:</b> {html.escape(source_label)}",
        f"⏸ <b>Paused:</b> {'yes' if store.is_paused() else 'no'}",
        "",
        "<b>Flags</b>",
        " · ".join(flags),
        _cookie_status_line(aconf),
        "",
        f"🕒 <b>Next configured slot:</b> {_pretty(str(snapshot.get('next_run') or ''))}",
        f"📥 <b>Pending approval:</b> {pending}",
        f"🗓 <b>Scheduled candidates:</b> {scheduled}",
        f"✅ <b>Published candidates:</b> {snapshot.get('published', store.count_candidates('published'))}",
        "",
        "<b>Today's quotas</b>",
        f"· source downloads: {usage.get('source_downloads', 0)}/{aconf.max_daily_source_downloads}",
        f"· Gemini analyses: {usage.get('gemini_analyses', 0)}/{aconf.max_daily_gemini_analyses}",
        f"· generated Shorts: {usage.get('generated_shorts', 0)}/{aconf.max_daily_generated_shorts}",
        "",
        f"👤 <b>Approval mode:</b> {html.escape(aconf.approval_mode)}",
        f"🗄 <b>Durable store:</b> {html.escape(str(_cfg(context).db_path))}",
    ]
    if not aconf.enabled:
        lines += [
            "",
            "ℹ️ Autonomous content is OFF. Set "
            "<code>ENABLE_AUTONOMOUS_CONTENT=true</code> in .env and restart.",
        ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_autopilot_niche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or durably change the active niche (owner only)."""
    if not is_owner(update, context):
        await _deny(update)
        return
    aconf = _aconf(context)
    store = _store(context)
    args = context.args or []

    if not args:
        state = store.get_active_niche(aconf.bootstrap_niche)
        source = (
            "persisted selection (survives restarts and container recreation)"
            if state.is_persisted
            else "CHANNEL_NICHE bootstrap default"
        )
        await update.effective_message.reply_text(
            "🎯 <b>Active niche</b>\n\n"
            f"<b>{html.escape(state.value)}</b>\n"
            f"Source: {html.escape(source)}\n\n"
            "Change it with <code>/autopilot_niche fitness</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    requested = " ".join(args)
    try:
        validate_niche(requested)
    except NicheValidationError as exc:
        await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
        return

    user_id = getattr(update.effective_user, "id", "unknown")
    try:
        state = await asyncio.to_thread(
            store.set_active_niche, requested, f"telegram:{user_id}"
        )
    except NicheValidationError as exc:
        await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
        return
    except NicheStorageError as exc:
        await update.effective_message.reply_text(
            "❌ <b>Niche was NOT changed</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "The previous niche is still active.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not persist niche")
        await update.effective_message.reply_text(
            "❌ <b>Niche was NOT changed</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_message.reply_text(
        "✅ <b>Active niche updated</b>\n\n"
        f"<b>{html.escape(state.value)}</b> (persisted)\n\n"
        "It applies to the next autonomous cycle. Existing queued, "
        "pending-approval and scheduled Shorts were left untouched.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_autopilot_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await _deny(update)
        return
    await asyncio.to_thread(
        _store(context).set_paused,
        True,
        f"telegram:{getattr(update.effective_user, 'id', 'unknown')}",
    )
    await update.effective_message.reply_text(
        "⏸ <b>Autopilot paused</b>\n\n"
        "No new autonomous cycles will start. Existing scheduled YouTube "
        "uploads are NOT cancelled and keep running normally.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_autopilot_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await _deny(update)
        return
    await asyncio.to_thread(
        _store(context).set_paused,
        False,
        f"telegram:{getattr(update.effective_user, 'id', 'unknown')}",
    )
    await update.effective_message.reply_text(
        "▶️ <b>Autopilot resumed</b>\n\nNew cycles can start at the next trigger.",
        parse_mode=ParseMode.HTML,
    )


async def _run_manual_cycle(context: ContextTypes.DEFAULT_TYPE, deps: Any) -> None:
    from features.autonomous_pipeline import run_autonomous_cycle

    try:
        # run_autonomous_cycle takes `manual` as keyword-only; passing it
        # positionally raised TypeError and killed every /autopilot_run.
        result = await asyncio.to_thread(
            lambda: run_autonomous_cycle(deps, manual=True)
        )
        log.info("Manual autonomous cycle result: %s", result.summary())
    except Exception:  # noqa: BLE001 - the pipeline already notifies failures
        log.exception("Manual autonomous cycle failed")


async def cmd_autopilot_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await _deny(update)
        return
    aconf = _aconf(context)
    if not aconf.enabled:
        await update.effective_message.reply_text(
            "⭕ Autonomous content is disabled. Set "
            "<code>ENABLE_AUTONOMOUS_CONTENT=true</code> in .env and restart.",
            parse_mode=ParseMode.HTML,
        )
        return
    if _store(context).is_paused():
        await update.effective_message.reply_text(
            "⏸ Autopilot is paused. Use /autopilot_resume first."
        )
        return

    await update.effective_message.reply_text(
        "🤖 Running one autonomous cycle now…\n"
        "Research, rights gate, Gemini analysis and clip rendering can take "
        "several minutes. I will send the result or a review card when done."
    )
    try:
        context.application.create_task(_run_manual_cycle(context, build_deps(context)))
    except Exception:  # noqa: BLE001
        log.exception("Could not start manual autonomous cycle")
        await update.effective_message.reply_text("❌ Could not start the cycle.")


async def cmd_autopilot_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await _deny(update)
        return
    store = _store(context)
    pending = await asyncio.to_thread(store.list_candidates, "pending_approval", 10)
    if not pending:
        await update.effective_message.reply_text(
            "📭 No autonomous Shorts are waiting for approval."
        )
        return

    lines = [f"📥 <b>Pending autonomous Shorts</b> ({len(pending)})", ""]
    for candidate in pending:
        source = candidate.get("source") or {}
        moments = candidate.get("moments") or []
        clip = moments[0] if moments else {}
        lines.append(
            f"• <b>#{candidate.get('id')}</b> — {html.escape(str(candidate.get('topic') or ''))}\n"
            f"  🎬 {html.escape(str(source.get('title') or 'Unknown source'))}\n"
            f"  👤 {html.escape(str(source.get('channel') or 'Unknown creator'))}\n"
            f"  🔗 {html.escape(str(source.get('url') or 'n/a'))}\n"
            f"  📜 {html.escape(str((candidate.get('rights') or {}).get('basis') or 'unknown'))}\n"
            f"  ⏱ {html.escape(str(clip.get('start_label') or '?'))} → "
            f"{html.escape(str(clip.get('end_label') or '?'))}"
        )
    lines += [
        "",
        "Use the <b>Approve / Schedule / Retry / Discard</b> buttons on each "
        "review card. Rights-uncertain candidates cannot be published.",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------- callbacks
async def handle_autonomous_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or not str(query.data or "").startswith("af:"):
        return

    # Answer exactly once: Telegram rejects a second answer for the same query,
    # and an un-answered query leaves a spinner on the button.
    if not is_owner(update, context):
        await query.answer("Only the channel owner can do this.", show_alert=True)
        return

    parts = str(query.data).split(":")
    if len(parts) != 3:
        await query.answer("Unsupported action.", show_alert=True)
        return
    action, raw_id = parts[1], parts[2]
    try:
        candidate_id = int(raw_id)
    except ValueError:
        await query.answer("Invalid candidate id.", show_alert=True)
        return

    await query.answer()

    store = _store(context)
    candidate = await asyncio.to_thread(store.get_candidate, candidate_id)
    if not candidate:
        await query.edit_message_text(
            "⚠️ This candidate no longer exists (already handled or cleaned up)."
        )
        return

    deps = build_deps(context)
    try:
        if action == "pub":
            from features.autonomous_pipeline import publish_candidate

            await query.edit_message_text("🚀 Publishing through the existing YouTube path…")
            record = await asyncio.to_thread(publish_candidate, deps, candidate_id)
            url = record.get("url") if isinstance(record, dict) else ""
            await query.edit_message_text(
                "✅ <b>Published to YouTube</b>\n\n"
                f"🔗 {html.escape(str(url or 'see YouTube Studio'))}\n"
                "Confirmed by the existing uploader/verification path.",
                parse_mode=ParseMode.HTML,
            )
            return

        if action == "sch":
            from features.autonomous_pipeline import schedule_candidate

            run_at = await asyncio.to_thread(schedule_candidate, deps, candidate_id)
            pretty = run_at.strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
            await query.edit_message_text(
                "🗓 <b>Autonomous Short scheduled</b>\n\n"
                f"⏰ {html.escape(pretty)}\n"
                "It now uses the existing durable scheduler and YouTube upload "
                "verification. Manage it with /queue or /cancel.",
                parse_mode=ParseMode.HTML,
            )
            return

        if action == "retry":
            from features.autonomous_pipeline import retry_candidate

            await query.edit_message_text("🔁 Regenerating metadata with Gemini…")
            await asyncio.to_thread(retry_candidate, deps, candidate_id)
            await query.edit_message_text(
                "🔁 Metadata regenerated — a fresh review card was sent above."
            )
            return

        if action == "dis":
            from features.autonomous_pipeline import discard_candidate

            await asyncio.to_thread(discard_candidate, deps, candidate_id)
            await query.edit_message_text(
                "❌ Candidate discarded. The clip file was removed. "
                "Existing scheduled uploads are unaffected."
            )
            return

        if action == "info":
            from features.autonomous_pipeline import format_approval_message

            video = await asyncio.to_thread(_db(context).get_video, int(candidate.get("video_row_id") or 0))
            await query.edit_message_text(
                format_approval_message(candidate, brief=(video or {}).get("brief") or {}),
                parse_mode=ParseMode.HTML,
                reply_markup=build_approval_keyboard(candidate_id),
            )
            return
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the bot
        log.exception("Autonomous callback %s failed", action)
        try:
            await asyncio.to_thread(
                store.update_candidate, candidate_id, status="error", error=str(exc)[:500]
            )
        except Exception:  # noqa: BLE001
            pass
        await query.edit_message_text(
            f"❌ <b>Action failed</b>\n\n<code>{html.escape(str(exc))}</code>\n\n"
            "The candidate was kept so you can retry or discard it.",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text("Unsupported action.")


# ------------------------------------------------------- owner media ingest
def _slug(value: str, fallback: str = "source") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return cleaned[:80] or fallback


def ingest_owner_media(
    dest_dir: str | Path,
    uploaded_path: str | Path,
    *,
    title: str = "",
    creator: str = "owner",
    topic: str = "",
    note: str = "",
) -> tuple[Path, Path]:
    """Move an uploaded video into the owner-media folder with a rights sidecar.

    Issuing ``/autopilot_add_source`` *is* the owner's rights confirmation, so
    the sidecar records ``confirmed: true`` with the ``owner_media`` basis. The
    file is renamed (never in the source folder before this point) and is never
    deleted by the pipeline.
    """
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    source = Path(uploaded_path)
    if not source.is_file():
        raise FileNotFoundError(f"Upload not found: {source}")

    stamp = now_ist().strftime("%Y%m%d-%H%M%S")
    stem = _slug(source.stem or title, "owner_video")
    target = dest / f"{stem}_{stamp}{source.suffix.lower() or '.mp4'}"
    source.replace(target)

    sidecar = target.with_name(target.name + ".rights.json")
    payload = {
        "confirmed": True,
        "rights_basis": "owner_media",
        "title": title or stem,
        "creator": creator or "owner",
        "topic": topic,
        "note": note,
        "ingested_at": now_ist().isoformat(),
        "ingested_via": "telegram:/autopilot_add_source",
    }
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target, sidecar


def _media_from_message(message: Any) -> Any:
    """Return the video/document object carried by a Telegram message."""
    if message is None:
        return None
    video = getattr(message, "video", None)
    if video is not None:
        return video
    document = getattr(message, "document", None)
    if document is not None and str(
        getattr(document, "mime_type", "") or ""
    ).startswith("video/"):
        return document
    return None


async def cmd_autopilot_add_source(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ingest the owner's own long video as an autonomous source."""
    if not is_owner(update, context):
        await _deny(update)
        return

    message = update.effective_message
    media = _media_from_message(message) or _media_from_message(
        getattr(message, "reply_to_message", None)
    )
    if media is None:
        await message.reply_text(
            "📥 <b>Add an autonomous source</b>\n\n"
            "Send (or forward) a long video here, then reply to it with "
            "<code>/autopilot_add_source</code>.\n"
            "Captioning the video with the command also works.\n\n"
            "The file is stored in the owner-media folder with a rights "
            "sidecar, so the pipeline may clip it. It is never deleted.",
            parse_mode=ParseMode.HTML,
        )
        return

    aconf = _aconf(context)
    cfg = _cfg(context)
    try:
        limit_mb = max(1, int(os.getenv("AUTONOMOUS_SOURCE_MAX_MB", "700")))
    except ValueError:
        limit_mb = 700

    size = int(getattr(media, "file_size", 0) or 0)
    if size and size > limit_mb * 1024 * 1024:
        await message.reply_text(
            f"❌ File is {size / (1024 * 1024):.0f} MB; the limit is "
            f"{limit_mb} MB (AUTONOMOUS_SOURCE_MAX_MB)."
        )
        return

    dest_dir = Path(aconf.source_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(str(dest_dir)).free
    except OSError as exc:
        await message.reply_text(f"❌ Source folder unavailable: {exc}")
        return
    if size and free < int(size * 1.5):
        await message.reply_text(
            f"❌ Not enough disk space: need ~{size * 1.5 / (1024 ** 3):.1f} GB free, "
            f"have {free / (1024 ** 3):.1f} GB. Nothing was stored."
        )
        return

    tmp = dest_dir / f"upload_{uuid.uuid4().hex}.mp4"
    status = await message.reply_text("⬇️ Downloading the video from Telegram…")
    try:
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(str(tmp))

        from features.shorts_editor import probe_duration

        duration = await asyncio.to_thread(probe_duration, tmp, cfg.ffprobe_path)
        minimum = aconf.youtube_min_source_video_minutes * 60
        if duration < minimum:
            tmp.unlink(missing_ok=True)
            await status.edit_text(
                f"❌ Source is {duration / 60:.1f} min; the minimum is "
                f"{aconf.youtube_min_source_video_minutes} min "
                "(YOUTUBE_MIN_SOURCE_VIDEO_MINUTES). Nothing was stored."
            )
            return

        caption = str(getattr(message, "caption", "") or "").strip()
        if caption.startswith("/"):
            parts = caption.split(maxsplit=1)
            caption = parts[1] if len(parts) > 1 else ""
        title = caption or str(getattr(media, "file_name", "") or "") or "Owner video"

        path, sidecar = await asyncio.to_thread(
            ingest_owner_media,
            dest_dir,
            tmp,
            title=title,
            creator="owner",
            topic="",
            note="Uploaded through Telegram and confirmed by the owner.",
        )
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the bot
        log.exception("Owner media ingest failed")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        await status.edit_text(
            f"❌ Could not store the source: <code>{html.escape(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await status.edit_text(
        "✅ <b>Source stored</b>\n\n"
        f"🎬 <i>{html.escape(title[:120])}</i>\n"
        f"⏱ {duration / 60:.1f} min\n"
        f"📜 Rights: owner media (confirmed)\n"
        f"📁 <code>{html.escape(path.name)}</code>\n"
        f"🗂 Sidecar: <code>{html.escape(sidecar.name)}</code>\n\n"
        "The next autonomous cycle can clip it. Run "
        "<code>/autopilot_run</code> to start one now, or let the scheduled "
        "cycle pick it up.",
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------- cookie jar
def cookie_jar_status(text: str) -> tuple[bool, str]:
    """Check that an uploaded file really is a signed-in YouTube cookies.txt."""
    if not (text or "").strip():
        return False, "File is empty."
    rows = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not rows:
        return False, "No cookie lines found."
    youtube_rows = [row for row in rows if "youtube.com" in row]
    if not youtube_rows:
        return False, "No youtube.com cookies in this file."
    if any(len(row.split("\t")) < 6 for row in youtube_rows):
        return (
            False,
            "Cookie lines are not in Netscape cookies.txt format. "
            "Export with the 'Get cookies.txt LOCALLY' extension.",
        )

    names = {row.split("\t")[5].strip() for row in youtube_rows}
    # A logged-in export always carries the primary session cookie. Without it
    # YouTube keeps answering "Sign in to confirm you're not a bot" even though
    # the file looks like a valid Netscape jar (this happened in production).
    if not names & {"SID", "__Secure-1PSID"}:
        return (
            False,
            "This jar is not signed in: it has no primary YouTube cookie "
            "(SID or __Secure-1PSID). Open youtube.com while signed in, enable "
            "'HttpOnly cookies' in the extension, and export again.",
        )
    if not names & {"HSID", "SSID", "APISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}:
        return (
            False,
            "This jar is missing the companion login cookies "
            "(HSID/SSID/APISID). Re-export with HttpOnly cookies enabled.",
        )
    return True, f"{len(youtube_rows)} youtube.com cookie line(s), signed-in set present"


async def cmd_autopilot_cookies(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Store an uploaded cookies.txt so yt-dlp can download on the server."""
    if not is_owner(update, context):
        await _deny(update)
        return

    message = update.effective_message
    document = getattr(message, "document", None) or getattr(
        getattr(message, "reply_to_message", None), "document", None
    )
    if document is None:
        await message.reply_text(
            "🍪 <b>Upload YouTube cookies</b>\n\n"
            "1. Install the browser extension <b>Get cookies.txt LOCALLY</b>.\n"
            "2. Sign in to youtube.com, then click the extension → <b>Export</b> "
            "(Netscape format). Enable <b>HttpOnly cookies</b> in the extension "
            "options — without them the export cannot download anything.\n"
            "3. Send the <code>cookies.txt</code> file here as a <b>file</b>, "
            "then reply to it with <code>/autopilot_cookies</code>.\n\n"
            "This only fixes downloads; nothing is published without your approval.",
            parse_mode=ParseMode.HTML,
        )
        return

    size = int(getattr(document, "file_size", 0) or 0)
    if size > 2 * 1024 * 1024:
        await message.reply_text("❌ File is larger than 2 MB; that is not a cookie jar.")
        return

    status = await message.reply_text("🍪 Checking the cookie file…")
    try:
        tg_file = await context.bot.get_file(document.file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        ok, detail = cookie_jar_status(raw.decode("utf-8", errors="replace"))
        if not ok:
            await status.edit_text(f"❌ {html.escape(detail)}")
            return

        from config import DATA_DIR

        target = Path(DATA_DIR) / "cookies.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".txt.tmp")
        temporary.write_bytes(raw)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(target)
    except Exception as exc:  # noqa: BLE001 - report instead of crashing
        log.exception("Cookie jar ingest failed")
        await status.edit_text(
            f"❌ Could not store cookies: <code>{html.escape(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # The config and the pipeline dependencies are cached per process; drop
    # them so the next cycle sees the new cookie jar.
    try:
        context.application.bot_data.pop("autonomous_cfg", None)
        scheduler = context.application.bot_data.get("scheduler")
        if scheduler is not None:
            for attr in ("_autonomous_config", "_autonomous_deps"):
                if hasattr(scheduler, attr):
                    setattr(scheduler, attr, None)
    except Exception:  # noqa: BLE001 - cache invalidation is best effort
        log.exception("Could not refresh autonomous config after cookie upload")

    await status.edit_text(
        "✅ <b>Cookies stored</b>\n\n"
        f"🍪 {html.escape(detail)}\n"
        f"📁 <code>{html.escape(str(target))}</code>\n\n"
        "YouTube downloads should work from the next cycle. Run "
        "<code>/autopilot_run</code> to test now.\n"
        "Treat this file like a password — delete it and re-export if you "
        "change your Google password.",
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------------ registration
def register_autonomous_handlers(app: Application) -> None:
    """Register the additive commands and the ``af:`` callback namespace."""
    app.add_handler(CommandHandler("autopilot_status", cmd_autopilot_status))
    app.add_handler(CommandHandler("autopilot_niche", cmd_autopilot_niche))
    app.add_handler(CommandHandler("autopilot_pause", cmd_autopilot_pause))
    app.add_handler(CommandHandler("autopilot_resume", cmd_autopilot_resume))
    app.add_handler(CommandHandler("autopilot_run", cmd_autopilot_run))
    app.add_handler(CommandHandler("autopilot_review", cmd_autopilot_review))
    app.add_handler(
        CommandHandler("autopilot_add_source", cmd_autopilot_add_source)
    )
    app.add_handler(CommandHandler("autopilot_cookies", cmd_autopilot_cookies))
    # Pattern-restricted, registered BEFORE the existing generic callback
    # handler by the caller: no existing callback payload can reach it.
    app.add_handler(CallbackQueryHandler(handle_autonomous_callback, pattern=r"^af:"))
