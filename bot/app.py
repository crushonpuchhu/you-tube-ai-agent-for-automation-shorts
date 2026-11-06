"""Telegram bot: receive a video -> analyze with Gemini -> publish now or schedule.

Existing flow is preserved (download -> analyze -> review card -> upload); the
upgrade adds natural-language instructions, scheduling, goal tracking, research,
weekly reports and queue management on top of it.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from agent.analytics import YouTubeStats
from agent.caption import extract_custom_text, parse_choice
from agent.clock import now_ist, to_iso, today_key
from agent.daily_goal import DailyGoalManager
from agent.db import (
    STAGE_ANALYZE,
    STAGE_DOWNLOAD,
    STATUS_ANALYZED,
    STATUS_DISCARDED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_RECEIVED,
    Database,
)
from agent.errors import AuthRequired, ResearchError
from agent.intent import (
    MODE_BEST,
    MODE_NOW,
    MODE_WHEN,
    describe_intent,
    parse_intent,
)
from agent.llm import LLM
from agent.notifier import Notifier
from agent.publish import Publisher
from agent.research import ResearchEngine, format_topics
from agent.scheduler import Scheduler
from agent.smart_schedule import describe as describe_slot
from agent.smart_schedule import recommend
from agent.weekly import build_decisions, build_report
from config import Config, VIDEO_DIR
from pipeline.analyzer import CATEGORIES, VideoAnalyzer
from pipeline.models import VideoBrief
from pipeline.overlay import TextOverlayEngine
from pipeline.youtube_uploader import YouTubeUploader

log = logging.getLogger(__name__)

PRIVACY_LABELS = {"private": "🔒 Private", "unlisted": "🔗 Unlisted", "public": "🌍 Public"}


def _short_slot(slot) -> str:
    """Compact label for a button, e.g. 'Tue 6:30 PM'."""
    if slot is None or not hasattr(slot, "run_at"):
        return ""
    return slot.run_at.strftime("%a %I:%M %p").replace(" 0", " ")


def lowered_cancel(text: str) -> bool:
    """True for the short 'no thanks' words people type to back out."""
    return text.strip().lower().rstrip("!.").rstrip() in {
        "cancel", "c", "no", "stop", "nevermind", "never mind", "abort", "exit",
    }


def _best_button_label(slot) -> str:
    return f"⏱ Best: {_short_slot(slot)}" if slot else "⏱ Best time"

WELCOME = """\
<b>🎬 Shorts Automation Bot</b>

Send me a video and I will watch it with Gemini, write the metadata, then
<b>publish immediately</b> or <b>schedule it intelligently</b>.

<b>Just say it in the caption</b>
• <i>"Upload this Short now."</i>
• <i>"Schedule this video for tomorrow at 6:30 PM IST."</i>
• <i>"Find a suitable publishing time and schedule it."</i>
• <i>"Upload this video immediately and track its performance."</i>
No caption? You get a review card with buttons.

<b>Commands</b>
/goal — today's Shorts target (IST)
/queue — scheduled videos · /cancel &lt;id&gt;
/besttime — recommended next slot
/research — AI content research
/decide — create more / improve / experiment / avoid
/report — weekly performance report
/status — system health
/reset — clear the current video

Daily target: <b>{goal} Shorts</b> · video limit {limit} MB
"""


# --------------------------------------------------------------------- helpers
def _fmt_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _keyboard(privacy: str, best_slot=None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📤 Publish now", callback_data="upload"),
                InlineKeyboardButton(_best_button_label(best_slot), callback_data="best"),
                InlineKeyboardButton("🔁 Regenerate", callback_data="regen"),
            ],
            [
                InlineKeyboardButton(
                    f"{PRIVACY_LABELS[p]}" + (" ✓" if p == privacy else ""),
                    callback_data=f"priv:{p}",
                )
                for p in ("private", "unlisted", "public")
            ],
            [
                InlineKeyboardButton("🕐 Custom time", callback_data="custom"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ],
        ]
    )


def _slot_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Use this slot", callback_data="slot:yes"),
            InlineKeyboardButton("🔀 Shift +2h", callback_data="slot:shift"),
            InlineKeyboardButton("❌ Cancel", callback_data="slot:no"),
        ]]
    )


def _preview_html(brief: VideoBrief, privacy: str, category_name: str,
                  intent_note: str | None = None, slot=None) -> str:
    tags = ", ".join(html.escape(t) for t in brief.tags[:20])
    hashtags = " ".join(html.escape(h) for h in brief.hashtags)
    header = "<b>✅ Analysis complete</b>\n"
    if intent_note:
        header += f"\n🧭 {intent_note}\n"
    slot_line = ""
    if slot is not None:
        pretty = slot.run_at.strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
        slot_line = (
            f"\n<b>💡 Suggested time</b>\n{pretty}\n"
            f"<i>{html.escape(slot.reason)}</i>\n"
        )
    return (
        header + "\n"
        f"<b>Title</b> ({len(brief.title)} chars)\n<i>{html.escape(brief.title)}</i>\n\n"
        f"<b>Category</b>\n{brief.category_id} — {html.escape(category_name)}\n\n"
        f"<b>Language</b>\n{html.escape(brief.language)}\n\n"
        f"<b>Hashtags</b> ({len(brief.hashtags)})\n{hashtags}\n\n"
        f"<b>Tags</b> ({len(brief.tags)})\n{tags}\n\n"
        f"<b>Description</b>\n{html.escape(brief.description)}\n"
        f"{slot_line}\n"
        f"<b>Visibility:</b> {PRIVACY_LABELS[privacy]}\n"
        "Publish now, pick a schedule, or refine with a message 👇"
    )


async def _safe_edit(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
    retries: int = 3,
    parse_mode=ParseMode.HTML,
):
    """Edit a status message, tolerating Telegram 'message is not modified' races."""
    for attempt in range(retries):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except Exception as exc:  # noqa: BLE001 - best-effort status updates
            if "not modified" in str(exc).lower():
                return
            if attempt == retries - 1:
                log.warning("edit_message_text failed: %s", exc)
                return
            await asyncio.sleep(1.5)


async def _capture_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remember where notifications should go (works from any handler).

    Must be async: python-telegram-bot v20+ awaits every handler callback, and
    a plain function makes it raise "object NoneType can't be used in 'await'".
    """
    chat = update.effective_chat
    if not chat:
        return
    db: Database = context.application.bot_data.get("db")
    notifier: Notifier = context.application.bot_data.get("notifier")
    if db:
        db.set_setting("owner_chat_id", str(chat.id))
    if notifier:
        notifier.set_chat(chat.id)


# --------------------------------------------------------------------- handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    await update.message.reply_text(
        WELCOME.format(goal=cfg.daily_goal, limit=cfg.max_video_bytes // (1024 * 1024)),
        parse_mode=ParseMode.HTML,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.pop("job", None)
    if job:
        _clear_caption_timeout(job)
    await update.message.reply_text("🗑️ Cleared. Send me a new video whenever you're ready.")


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show stored videos and delete the dead ones (failed/abandoned).

    Only 'failed' and 'discarded' rows are ever removable here: scheduled and
    publishing videos are protected, and published rows are kept for the weekly
    report's statistics.
    """
    db: Database = context.application.bot_data["db"]
    args = (context.args or [])
    action = args[0].lower() if args else "list"

    if action == "failed":
        removed = await asyncio.to_thread(db.delete_videos_by_status, (STATUS_FAILED,))
        freed = 0
        await update.message.reply_text(
            f"🗑️ Deleted {len(removed)} failed video(s)"
            + (f": #{', #'.join(map(str, removed))}" if removed else " (nothing to delete)")
        )
        return

    if action == "all":
        # Everything idle and abandoned. In-flight rows (downloading,
        # processing, scheduled, uploading) are protected inside delete_video,
        # and published rows are kept — the weekly report reads them.
        removable = (STATUS_FAILED, STATUS_ANALYZED, STATUS_DISCARDED)
        removed = await asyncio.to_thread(db.delete_videos_by_status, removable)
        await update.message.reply_text(
            f"🗑️ Deleted {len(removed)} stored video(s)"
            + (f": #{', #'.join(map(str, removed))}" if removed else " (nothing to delete)")
            + "\n\nScheduled and published videos were kept."
        )
        return

    # Default: show an inventory so the user can see what is being held.
    rows = await asyncio.to_thread(
        lambda: db.list_videos(status=None, limit=1000)
    )
    if not rows:
        await update.message.reply_text("✅ Nothing stored — the database is empty.")
        return

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))

    lines = [f"<b>🗄 Stored videos</b> ({len(rows)})", "", summary, ""]
    for r in rows[:20]:
        file_ok = "📁" if (r.get("file_path") and Path(r["file_path"]).exists()) else "🚫"
        err = (r.get("error") or "")[:60]
        mark = "  ⚠️ " + html.escape(err) if (r["status"] == "failed" and err) else ""
        lines.append(f"• #{r['id']} <b>{r['status']}</b> {file_ok}{mark}")
    if len(rows) > 20:
        lines.append(f"\n…and {len(rows) - 20} more")

    lines += [
        "",
        "<b>Delete:</b>",
        "<code>/cleanup failed</code> — remove failed videos only",
        "<code>/cleanup all</code> — remove failed + old analyzed ones",
        "",
        "<i>Scheduled and published videos are never deleted.</i>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    goal: DailyGoalManager = context.application.bot_data["goal"]
    await update.message.reply_text(goal.status_html(), parse_mode=ParseMode.HTML)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    db: Database = context.application.bot_data["db"]
    tasks = scheduler.queue_list()
    if not tasks:
        published = db.count_videos("published", daily_key=today_key())
        await update.message.reply_text(
            f"📭 Nothing scheduled.\n🎯 Today: {published} published.\n\n"
            "Send a video and say “schedule it” to queue one.",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = [f"📅 <b>Scheduled queue</b> ({len(tasks)})", ""]
    for task in tasks:
        video = db.get_video(int(task["payload"].get("video_id", 0)) or 0)
        title = ((video or {}).get("brief") or {}).get("title", "Untitled")
        when = task["run_at"]
        try:
            from agent.clock import ensure_ist
            from datetime import datetime as _dt
            pretty = ensure_ist(_dt.fromisoformat(when)).strftime("%a %d %b, %I:%M %p IST")
            pretty = pretty.replace(" 0", " ")
        except ValueError:
            pretty = when
        lines.append(
            f"• #{task['id']} — <i>{html.escape(title)}</i>\n  {pretty}\n"
            f"  status: {task['status']} · attempts {task['attempts']}/{task['max_attempts']}"
        )
    lines.append("\nCancel with /cancel &lt;id&gt; (or /cancel all)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    db: Database = context.application.bot_data["db"]
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /cancel <id>  or  /cancel all")
        return
    if args[0].lower() == "all":
        cancelled = 0
        for task in scheduler.queue_list():
            video_id = int(task["payload"].get("video_id", 0) or 0)
            if video_id and scheduler.cancel_video(video_id):
                cancelled += 1
        await update.message.reply_text(f"🗑️ Cancelled {cancelled} scheduled Short(s).")
        return
    try:
        task_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Give me a numeric task id from /queue.")
        return
    task = next((t for t in db.list_tasks("pending", limit=200) if t["id"] == task_id), None)
    if not task:
        await update.message.reply_text("That task is not pending (already ran or failed).")
        return
    video_id = int(task["payload"].get("video_id", 0) or 0)
    ok = scheduler.cancel_video(video_id) if video_id else db.cancel_task(task_id)
    await update.message.reply_text("🗑️ Cancelled." if ok else "Could not cancel that task.")


async def cmd_besttime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    slot = await asyncio.to_thread(
        recommend, db, None, cfg.daily_goal, cfg.min_gap_hours, cfg.min_lead_minutes
    )
    await update.message.reply_text(describe_slot(slot), parse_mode=ParseMode.HTML)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📊 Building your weekly report from YouTube Analytics…")
    text = await _build_report_text(context)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔬 Researching topics (YouTube, news, Reddit)…")
    engine: ResearchEngine = context.application.bot_data["research"]
    try:
        topics, evidence = await asyncio.to_thread(engine.run)
    except ResearchError as exc:
        await update.message.reply_text(f"🔬 Research returned nothing: {html.escape(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("research failed")
        await update.message.reply_text(f"❌ Research failed: {html.escape(str(exc))}")
        return
    await update.message.reply_text(format_topics(topics, evidence), parse_mode=ParseMode.HTML)


async def cmd_decide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🧭 Analysing 30 days of performance + research…")
    text = await _build_decisions_text(context)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def _build_report_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    stats: YouTubeStats = context.application.bot_data["stats"]
    llm: LLM = context.application.bot_data["llm"]
    return await build_report(db, stats, llm, default_target=cfg.daily_goal)


async def _build_decisions_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    stats: YouTubeStats = context.application.bot_data["stats"]
    llm: LLM = context.application.bot_data["llm"]
    return await build_decisions(db, stats, llm, default_target=cfg.daily_goal)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    goal: DailyGoalManager = context.application.bot_data["goal"]
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    uploader: YouTubeUploader = context.application.bot_data["uploader"]

    problems = cfg.validate()
    missing = [s.rsplit("/", 1)[-1] for s in uploader.missing_scopes()]
    progress = goal.progress()
    lines = ["<b>⚙️ Status</b>", ""]
    lines += [f"❌ {html.escape(p)}" for p in problems] or ["✅ Config looks good"]
    lines.append("✅ YouTube token ok" if not missing else
                 f"🔐 Missing YouTube scopes: {', '.join(missing)} → run scripts/youtube_auth.py --force")
    lines.append(f"🤖 Model: {html.escape(cfg.gemini_model)}")
    lines.append(f"🗄 DB: {html.escape(str(cfg.db_path))} "
                 f"({db.count_videos('published')} published on record)")
    running = bool(getattr(scheduler, "_thread", None) and scheduler._thread.is_alive())
    lines.append(f"⏱ Scheduler: {'running' if running else 'STOPPED'}")
    lines.append(f"🎯 Today: {progress['published']}/{progress['target']} published, "
                 f"{len(scheduler.queue_list())} scheduled")
    lines.append(f"🕘 {now_ist().strftime('%A %d %b %I:%M %p IST').replace(' 0', ' ')}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ video flow
async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    message = update.message

    tg_file = message.video or message.video_note or message.document
    if tg_file is None:
        return

    original_name = getattr(tg_file, "file_name", None) or "video.mp4"
    size = getattr(tg_file, "file_size", 0) or 0
    if size > cfg.max_video_bytes:
        await message.reply_text(
            f"⚠️ This video is {_fmt_size(size)} — over the {_fmt_size(cfg.max_video_bytes)} limit.\n\n"
            "Options:\n"
            "• Re-export the Short at 1080×1920, H.264, ≤ 30s\n"
            "• Or run a local Telegram Bot API server and set "
            "<code>TELEGRAM_LOCAL_API_URL</code> in .env to lift the cap."
        )
        return

    # Resolve the destination path up front. The DB row is created BEFORE the
    # bytes land, so a failure at any later stage still leaves a record that can
    # be retried instead of a video that silently vanished with no trace.
    file_id = tg_file.file_id
    unique = getattr(tg_file, "file_unique_id", file_id)
    suffix = Path(original_name).suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        suffix = ".mp4"
    dest = VIDEO_DIR / f"{message.from_user.id}_{unique}{suffix}"

    hint = (message.caption or "").strip()
    intent = parse_intent(hint)
    intent_desc = describe_intent(intent) if hint else "❓ No instruction — you choose below"

    # Mode B: the creator supplied the exact overlay text in the caption
    # (e.g. `Add this text: "…"`). Used verbatim, never rewritten.
    custom_overlay = extract_custom_text(hint) if hint else None

    status = await message.reply_text("🧾 Received — recording it…")

    video_id = db.insert_video(
        file_path=str(dest),
        chat_id=message.chat.id,
        tg_user_id=message.from_user.id,
        tg_file_id=file_id,
        hint=hint or None,
        privacy=cfg.youtube_default_privacy,
        status=STATUS_RECEIVED,
        intent={
            "mode": intent.mode, "at": to_iso(intent.at) if intent.at else None,
            "day": intent.day.isoformat() if intent.day else None,
            "track": intent.track, "raw": hint,
        },
    )

    context.user_data["job"] = {
        "video_id": video_id,
        "path": str(dest),
        "hint": hint,
        "intent": intent,
        "privacy": cfg.youtube_default_privacy,
        "brief": None,
        "pending_slot": None,
        "awaiting_time": False,
        "chat_id": message.chat.id,
        "custom_overlay_text": custom_overlay,
        "overlay_done": False,
        "awaiting_caption": False,
    }

    # ---- stage 1/3: download (Telegram -> disk) ------------------------------
    db.set_status(video_id, STATUS_DOWNLOADING)
    await status.edit_text("⬇️ Downloading video…")
    try:
        if not dest.exists():
            tg = await context.bot.get_file(file_id)
            await tg.download_to_drive(str(dest))
        if not dest.exists():
            # Telegram reported success but no bytes landed — treat it as a
            # download failure rather than letting the analysis die later on a
            # confusing "file not found".
            raise FileNotFoundError(f"Telegram returned no file at {dest}")
    except Exception as exc:  # noqa: BLE001
        log.exception("download failed")
        attempts = db.mark_failed(video_id, STAGE_DOWNLOAD, str(exc))
        await status.edit_text(
            f"❌ <b>Download failed</b> — video #{video_id}\n\n"
            f"<code>{html.escape(str(exc))}</code>\n\n"
            f"Stage: <b>download</b>. Attempt {attempts}.\n"
            "The record is kept; send the video again to retry.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ---- stage 2/3: AI processing (disk -> Gemini) --------------------------
    db.set_status(video_id, STATUS_PROCESSING)
    await status.edit_text(
        f"📤 Saved (#{video_id}).\n🧭 Instruction: {intent_desc}\n"
        "🧠 Analyzing with Gemini (watching + listening)… 30–60s."
    )
    await _run_analysis(update, context, status)


async def _run_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, status) -> None:
    job = context.user_data["job"]
    db: Database = context.application.bot_data["db"]
    analyzer: VideoAnalyzer = context.application.bot_data["analyzer"]

    try:
        brief = await analyzer.analyze(Path(job["path"]), hint=job.get("hint", ""))
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis failed")
        # The file is still on disk, so the failure is recorded rather than
        # thrown away: status becomes `failed`, the exact error is saved in
        # `error_message`, `retry_count` is bumped, and the job stays in memory
        # so the retry below re-runs only this stage with no re-upload.
        attempts = db.mark_failed(job["video_id"], STAGE_ANALYZE, str(exc))
        job["attempts"] = attempts
        job["last_error"] = str(exc)
        retry_button = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Retry analysis", callback_data="regen"),
            InlineKeyboardButton("❌ Discard", callback_data="cancel"),
        ]])
        await status.edit_text(
            f"❌ <b>Gemini analysis failed</b> — stage <b>analyze</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>\n\n"
            f"📹 Your video <b>is still saved</b> (#{job['video_id']}) — nothing was lost.\n"
            f"Tried {attempts} time{'s' if attempts != 1 else ''}.\n\n"
            "Tap <b>Retry analysis</b>, or discard it and send the video again.",
            parse_mode=ParseMode.HTML,
            reply_markup=retry_button,
        )
        return

    job["brief"] = brief
    db.update_video(
        job["video_id"],
        status=STATUS_ANALYZED,
        brief_json=brief.to_dict(),
        privacy=job["privacy"],
        # Analysis succeeded, so any failure recorded for an earlier attempt is
        # stale: clear it and hand the retry budget back.
        error=None,
        failed_stage=None,
        retry_count=0,
    )

    intent = job.get("intent")
    # Optional text overlay (FFmpeg drawtext) — resolve BEFORE the review card
    # or any publish/schedule action, so the burned-in video is what goes out.
    if _overlay_enabled(context) and not job.get("overlay_done"):
        if await _resolve_overlay(context, job, status):
            return  # paused: waiting for the creator to pick/enter/skip a caption

    await _finalize_job(context, job, status)


def _overlay_enabled(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when the text-overlay feature is on AND FFmpeg is usable."""
    cfg: Config = context.application.bot_data["cfg"]
    if not cfg.text_overlay_enabled:
        return False
    engine = context.application.bot_data.get("overlay")
    return engine is not None and engine.available(cfg)


async def _resolve_overlay(context: ContextTypes.DEFAULT_TYPE, job: dict, status) -> bool:
    """Resolve the text overlay for this video.

    Returns True when we PAUSED (awaiting a caption choice from the creator),
    False when the flow should continue straight to `_finalize_job`.
    * Mode B (custom text in the caption) is applied immediately, no prompt.
    * Mode A/C (no text): 3 AI captions are offered via inline buttons with a
      timeout that falls back to "no overlay" — the agent never hangs.
    """
    cfg: Config = context.application.bot_data["cfg"]
    analyzer: VideoAnalyzer = context.application.bot_data["analyzer"]

    # Mode B: creator already supplied the exact text.
    custom = job.get("custom_overlay_text")
    if custom:
        await _apply_overlay_and_continue(context, job, custom, status, prompt=False)
        return False

    # Mode A/C: generate suggestions (best-effort; never blocks the pipeline).
    await status.edit_text("🧠 Watching the video to suggest overlay captions…")
    captions = await analyzer.suggest_captions(Path(job["path"]), hint=job.get("hint", ""))
    if not captions:
        log.info("No overlay captions generated for #%s — continuing without overlay",
                 job["video_id"])
        job["overlay_done"] = True
        return False

    job["caption_suggestions"] = captions
    job["awaiting_caption"] = True
    buttons = [
        [InlineKeyboardButton(f"{i}. {cap[:40]}", callback_data=f"cap:{i}")]
        for i, cap in enumerate(captions)
    ]
    buttons.append([
        inline := InlineKeyboardButton("✏️ My own text", callback_data="cap:custom"),
        InlineKeyboardButton("⏭ Skip overlay", callback_data="cap:skip"),
    ])
    from agent.caption import format_suggestions
    await status.edit_text(
        format_suggestions(captions, cfg.caption_timeout_minutes),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _start_caption_timeout(context, job)
    return True


def _start_caption_timeout(context: ContextTypes.DEFAULT_TYPE, job: dict) -> None:
    """Schedule the 'continue without overlay' fallback after a timeout."""
    cfg: Config = context.application.bot_data["cfg"]
    loop = asyncio.get_running_loop()
    minutes = cfg.caption_timeout_minutes

    def _fire() -> None:
        # Only continue if the creator is still silent on this same job.
        if job.get("awaiting_caption"):
            job["awaiting_caption"] = False
            job["overlay_done"] = True
            context.application.create_task(
                _continue_without_overlay(context, job, minutes)
            )

    handle = loop.call_later(minutes * 60, _fire)
    job["caption_timeout"] = handle


def _clear_caption_timeout(job: dict) -> None:
    handle = job.pop("caption_timeout", None)
    if handle is not None:
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001 - already-fired timers are fine
            pass


async def _continue_without_overlay(context: ContextTypes.DEFAULT_TYPE, job: dict,
                                    minutes: int) -> None:
    """Timeout path: notify and proceed with the ORIGINAL video (no overlay)."""
    chat_id = job.get("chat_id")
    text = (
        f"⏭ No caption chosen within {minutes} min — continuing without a text "
        "overlay. The video stays exactly as you sent it.\n\n"
        "Send a video with your own text any time (e.g. "
        "<i>Add this text: \"…\"</i>)."
    )
    try:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001 - notification only
        log.warning("Could not send overlay-timeout notice")
    db: Database = context.application.bot_data["db"]
    await _finalize_job(context, job, None, db=db)


async def _apply_overlay_and_continue(context: ContextTypes.DEFAULT_TYPE, job: dict,
                                      text: str, status, prompt: bool = True) -> None:
    """Burn `text` onto the video with FFmpeg, then continue the flow.

    On overlay failure we CONTINUE with the original video (never lose the
    publish) and tell the creator what went wrong.
    """
    engine = context.application.bot_data["overlay"]
    db: Database = context.application.bot_data["db"]
    source = Path(job["path"])
    if status is not None:
        await status.edit_text(f"🎞 Applying text overlay:\n<i>{html.escape(text)}</i>")

    try:
        processed = await asyncio.to_thread(engine.apply, source, text)
    except Exception as exc:  # noqa: BLE001 - OverlayError or unexpected
        log.exception("text overlay failed for #%s", job["video_id"])
        job["overlay_done"] = True
        if status is not None:
            await status.edit_text(
                f"⚠️ <b>Text overlay could not be applied</b>\n\n"
                f"<code>{html.escape(str(exc))}</code>\n\n"
                "Continuing with the <b>original</b> video (no overlay).",
                parse_mode=ParseMode.HTML,
            )
        await _finalize_job(context, job, status, db=db)
        return

    # Point the job + DB at the processed file; drop the original to save disk.
    job["path"] = str(processed)
    job["overlay_done"] = True
    job["overlay_text"] = text
    db.update_video(job["video_id"], file_path=str(processed))
    if processed != source:
        try:
            source.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete original video %s (sweep will reclaim it)", source)

    if status is not None:
        await status.edit_text("✅ Text overlay applied — continuing…")
    await _finalize_job(context, job, status, db=db)


async def _finalize_job(context: ContextTypes.DEFAULT_TYPE, job: dict, status,
                        db: Database | None = None) -> None:
    """Dispatch to the intent path (publish/schedule/best) or the review card.

    This is the original tail of `_run_analysis`, extracted so the overlay
    step can pause and then resume the exact same flow.
    """
    if db is None:
        db = context.application.bot_data["db"]
    brief = job.get("brief")
    intent = job.get("intent")

    # The overlay timeout path resumes with no live status message — send a
    # fresh one so every branch below can safely edit it.
    if status is None:
        chat_id = job.get("chat_id")
        if not chat_id:
            log.error("finalize_job: no status message and no chat_id for #%s",
                      job.get("video_id"))
            return
        status = await context.bot.send_message(chat_id=chat_id, text="⏳ Continuing…")

    if intent and intent.mode == MODE_NOW:
        await status.edit_text("✅ Metadata ready — publishing immediately as instructed…")
        await _publish_now(context, job, status)
        return
    if intent and intent.mode == MODE_WHEN and intent.at:
        await _schedule_job(context, job, intent.at, status,
                            note="Scheduled from your caption instruction.")
        return
    if intent and intent.mode == MODE_BEST:
        await _offer_best_slot(context, job, status, preferred_day=intent.day)
        return

    category_name = CATEGORIES.get(brief.category_id, "Unknown")
    note = describe_intent(intent) if intent and intent.mode != "none" else None

    # Pre-compute the recommended slot so the card and the button can show the
    # exact time up front instead of hiding it behind a tap.
    slot = None
    try:
        cfg: Config = context.application.bot_data["cfg"]
        slot = await asyncio.to_thread(
            recommend, db, None, cfg.daily_goal, cfg.min_gap_hours, cfg.min_lead_minutes
        )
        job["pending_slot"] = to_iso(slot.run_at)
        job["slot_obj"] = slot
    except Exception:  # noqa: BLE001 - the card must still render without a slot
        log.exception("could not precompute a best slot")

    overlay_note = ""
    if job.get("overlay_text"):
        overlay_note = f"\n<b>🖼 Text overlay:</b> <i>{html.escape(job['overlay_text'])}</i>\n"

    await status.edit_text(
        _preview_html(brief, job["privacy"], category_name, note, slot=slot)
        + overlay_note,
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(job["privacy"], best_slot=slot),
    )


# ------------------------------------------------------------- publish actions
async def _publish_now(context: ContextTypes.DEFAULT_TYPE, job: dict, status) -> None:
    """Immediate publish with live progress (records the goal in the DB)."""
    db: Database = context.application.bot_data["db"]
    publisher: Publisher = context.application.bot_data["publisher"]
    cfg: Config = context.application.bot_data["cfg"]
    goal: DailyGoalManager = context.application.bot_data["goal"]

    db.update_video(job["video_id"], privacy=job["privacy"])
    loop = asyncio.get_running_loop()
    chat_id = status.chat_id
    message_id = status.message_id
    last_edit = {"t": 0.0}

    def progress(percent: int, message: str) -> None:
        now = time.monotonic()
        if percent < 100 and now - last_edit["t"] < 4:
            return
        last_edit["t"] = now
        asyncio.run_coroutine_threadsafe(
            _safe_edit(context.bot, chat_id, message_id, f"🚀 {message} ({percent}%)"), loop
        )

    try:
        record = await asyncio.to_thread(publisher.publish, job["video_id"], progress)
    except AuthRequired as exc:
        await _safe_edit(
            context.bot, chat_id, message_id,
            f"🔐 <b>YouTube authorization needed</b>\n\n{html.escape(str(exc))}\n\n"
            "The video is saved as <b>analyzed</b> — run the command above and tap "
            "<b>Publish now</b> again (or /queue for scheduling).",
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("upload failed")
        await _safe_edit(context.bot, chat_id, message_id,
                         f"❌ Upload failed:\n{html.escape(str(exc))}")
        return

    progress_state = goal.progress()
    goal_line = (
        f"🎯 Daily goal: {progress_state['published']}/{progress_state['target']}"
        + (" ✅ target achieved" if not progress_state["remaining"]
           else f" — {progress_state['remaining']} to go")
    )
    await _safe_edit(
        context.bot, chat_id, message_id,
        f"✅ <b>Published to YouTube!</b>\n\n"
        f"🎥 <i>{html.escape(record['brief'].get('title', ''))}</i>\n"
        f"👁️ Visibility: {PRIVACY_LABELS[record.get('privacy') or cfg.youtube_default_privacy]}\n"
        f"🔗 {record['url']}\n\n{goal_line}\n"
        "<i>Verified via the YouTube API.</i>",
    )
    context.user_data.pop("job", None)


async def _offer_best_slot(context: ContextTypes.DEFAULT_TYPE, job: dict, status,
                           preferred_day=None) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    db: Database = context.application.bot_data["db"]
    slot = await asyncio.to_thread(
        recommend, db, preferred_day, cfg.daily_goal, cfg.min_gap_hours, cfg.min_lead_minutes
    )
    job["pending_slot"] = to_iso(slot.run_at)
    brief = job.get("brief") or {}
    await status.edit_text(
        f"🧠 <b>Best available slot for this Short</b>\n\n"
        f"🎥 <i>{html.escape(brief.get('title', 'Untitled'))}</i>\n\n"
        f"{describe_slot(slot)}\n\n"
        "Accept, shift it, or send me a specific time (e.g. "
        "<i>tomorrow 6:30 PM</i>).",
        parse_mode=ParseMode.HTML,
        reply_markup=_slot_keyboard(),
    )


async def _schedule_job(context: ContextTypes.DEFAULT_TYPE, job: dict, run_at, status,
                        note: str = "") -> None:
    db: Database = context.application.bot_data["db"]
    scheduler: Scheduler = context.application.bot_data["scheduler"]
    goal: DailyGoalManager = context.application.bot_data["goal"]

    db.update_video(job["video_id"], privacy=job["privacy"])
    task_id = await asyncio.to_thread(scheduler.schedule_video, job["video_id"], to_iso(run_at))
    if task_id is None:
        await status.edit_text(
            "⚠️ This video is already scheduled. See /queue to manage it.",
            parse_mode=ParseMode.HTML,
        )
        context.user_data.pop("job", None)
        return

    pretty = run_at.strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
    progress = goal.progress()
    text = (
        f"🗓 <b>Short scheduled</b>\n\n"
        f"🎥 <i>{html.escape((job.get('brief') or {}).get('title', 'Untitled'))}</i>\n"
        f"⏰ {pretty}\n"
        f"🎯 Goal: {progress['published']}/{progress['target']} published today\n\n"
        "It publishes automatically (with retries) and I'll send you the link. "
        "Manage with /queue or /cancel.\n"
        "<i>Note: my computer must be on at that time — otherwise it runs on next start.</i>"
    )
    if note:
        text = f"{text}\n<i>{html.escape(note)}</i>"
    await status.edit_text(text, parse_mode=ParseMode.HTML)
    context.user_data.pop("job", None)


def _describe_action(intent) -> str:
    if intent.mode == MODE_NOW:
        return "🚀 Publishing immediately as instructed…"
    if intent.mode == MODE_WHEN and intent.at:
        pretty = intent.at.strftime("%A %d %B, %I:%M %p IST").replace(" 0", " ")
        return f"🗓 Scheduling for {pretty}…"
    return "🧠 Finding the best available slot…"


# ------------------------------------------------------------------ text router
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.get("job")
    text = (update.message.text or "").strip()
    intent = parse_intent(text)

    # 0) overlay caption selection (Mode A/C): pick 1/2/3 or type own text
    if job and job.get("awaiting_caption"):
        _clear_caption_timeout(job)
        suggestions = job.get("caption_suggestions") or []
        idx = parse_choice(text)
        if idx is not None and 0 <= idx < len(suggestions):
            job["awaiting_caption"] = False
            status = await update.message.reply_text(
                f"✅ Using caption {idx + 1}: <i>{html.escape(suggestions[idx])}</i>",
                parse_mode=ParseMode.HTML,
            )
            await _apply_overlay_and_continue(context, job, suggestions[idx], status)
            return
        if lowered_cancel(text):
            job["awaiting_caption"] = False
            job["overlay_done"] = True
            status = await update.message.reply_text(
                "⏭ Skipping the text overlay — continuing with your original video…"
            )
            await _finalize_job(context, job, status)
            return
        # Anything else is treated as the creator's own overlay text (Mode B).
        custom = extract_custom_text(text) or " ".join(text.split())
        custom = custom.strip()[:140]
        if custom:
            job["awaiting_caption"] = False
            status = await update.message.reply_text(
                f"✅ Using your text: <i>{html.escape(custom)}</i>", parse_mode=ParseMode.HTML
            )
            await _apply_overlay_and_continue(context, job, custom, status)
            return
        await update.message.reply_text(
            "Send a number (1/2/3), your own caption text, or /reset to cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    # 0b) creator is typing their own overlay text after tapping "My own text"
    if job and job.get("awaiting_caption_text"):
        job["awaiting_caption_text"] = False
        custom = extract_custom_text(text) or " ".join(text.split())
        custom = custom.strip()[:140]
        if lowered_cancel(text) or not custom:
            job["overlay_done"] = True
            status = await update.message.reply_text(
                "⏭ No text given — continuing WITHOUT a text overlay…"
            )
            await _finalize_job(context, job, status)
            return
        status = await update.message.reply_text(
            f"✅ Using your text: <i>{html.escape(custom)}</i>", parse_mode=ParseMode.HTML
        )
        await _apply_overlay_and_continue(context, job, custom, status)
        return

    # 1) user is answering the "when?" prompt
    if job and job.get("awaiting_time"):
        if lowered_cancel(text):
            job["awaiting_time"] = False
            context.user_data.pop("job", None)
            await update.message.reply_text("❌ Cancelled. Send another video anytime.")
            return
        if intent.mode == MODE_WHEN and intent.at:
            job["awaiting_time"] = False
            status = await update.message.reply_text("🗓 Scheduling…")
            await _schedule_job(context, job, intent.at, status)
            return
        if intent.mode == MODE_BEST:
            job["awaiting_time"] = False
            status = await update.message.reply_text("🧠 Finding the best slot…")
            await _offer_best_slot(context, job, status, preferred_day=intent.day)
            return
        await update.message.reply_text(
            "I couldn't read a time from that. Try:\n"
            "• <i>tomorrow 6:30 PM</i>\n• <i>today at 20:00</i>\n• <i>in 3 hours</i>\n• "
            "<i>best time</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # 2) an explicit instruction given while a video is in the review card
    if job and job.get("brief") is not None and intent.mode != "none":
        status = await update.message.reply_text(_describe_action(intent), parse_mode=ParseMode.HTML)
        if intent.mode == MODE_NOW:
            await _publish_now(context, job, status)
        elif intent.mode == MODE_WHEN and intent.at:
            await _schedule_job(context, job, intent.at, status)
        else:
            await _offer_best_slot(context, job, status, preferred_day=intent.day)
        return

    # 3) normal refinement of the generated metadata
    if job and job.get("brief") is not None and intent.mode == "none":
        # Tiny messages like "Hello" are chat, not creative direction —
        # regenerating on those burned a whole Gemini call for nothing.
        if len(text) < 10:
            await update.message.reply_text(
                "That looks like chat, not a change request 😊\n\n"
                "To refine the metadata, tell me what to change, e.g.\n"
                "<i>\"make the title funnier in Hinglish\"</i> or "
                "<i>\"use a Hindi description with Punjabi touch\"</i>.\n\n"
                "Or use the buttons: publish, schedule, regenerate, cancel.",
                parse_mode=ParseMode.HTML,
            )
            return
        job["hint"] = text
        status = await update.message.reply_text("🧠 Regenerating with your direction…")
        await _run_analysis(update, context, status)
        return

    if job and job.get("brief") is None:
        await update.message.reply_text("⏳ Still analyzing the current video — one moment.")
        return

    # 4) instruction without a video in hand
    if intent.is_explicit:
        await update.message.reply_text(
            "Got it — now <b>send me the video</b> with that instruction as the caption, "
            "or attach it first and I'll apply it.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        "Send me a Short 🎥 (with an optional caption like "
        "<i>“schedule this for tomorrow 6:30 PM”</i>), or use /goal, /queue, /research, "
        "/report, /decide, /besttime.",
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------------- callbacks
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    job = context.user_data.get("job")

    # ---- text-overlay caption selection (Mode A/C) ----
    if data.startswith("cap:") and job is not None:
        _clear_caption_timeout(job)
        choice = data.split(":", 1)[1]
        if choice == "skip":
            job["awaiting_caption"] = False
            job["overlay_done"] = True
            status = await query.edit_message_text(
                "⏭ Skipping the text overlay — continuing with your original video…"
            )
            await _finalize_job(context, job, status)
            return
        if choice == "custom":
            job["awaiting_caption_text"] = True
            await query.edit_message_text(
                "✏️ Send me the exact text you want overlaid on the video "
                "(one line works best)."
            )
            return
        # A numbered suggestion.
        suggestions = job.get("caption_suggestions") or []
        try:
            idx = int(choice)
        except ValueError:
            idx = -1
        if 0 <= idx < len(suggestions):
            job["awaiting_caption"] = False
            status = await query.edit_message_text(
                f"✅ Using caption {idx + 1}: <i>{html.escape(suggestions[idx])}</i>"
            )
            await _apply_overlay_and_continue(context, job, suggestions[idx], status)
            return
        await query.edit_message_text(
            "⚠️ That caption choice expired. Send the video again, or tap a button."
        )
        return

    # ---- slot confirmation (no job needed to cancel) ----
    if data.startswith("slot:"):
        action = data.split(":", 1)[1]
        if action == "no" or job is None:
            context.user_data.pop("job", None)
            await query.edit_message_text("❌ Not scheduled. Send another video anytime.")
            return
        if not job.get("pending_slot"):
            # The slot expired (bot restarted / job replaced) — never crash
            # on a missing key, just ask for a fresh instruction.
            await query.edit_message_text(
                "⚠️ That slot is no longer valid. Send the video again, or tap "
                "⏱ Best time on the review card.", parse_mode=ParseMode.HTML,
            )
            return
        if action == "yes":
            from agent.clock import from_iso
            status = await query.edit_message_text("🗓 Scheduling…")
            await _schedule_job(context, job, from_iso(job["pending_slot"]), status)
            return
        if action == "shift":
            from agent.clock import from_iso
            new_slot = from_iso(job["pending_slot"]) + timedelta(hours=2)
            job["pending_slot"] = to_iso(new_slot)
            await query.edit_message_text(
                f"🗓 Shifted to <b>{new_slot.strftime('%a %d %b, %I:%M %p IST').replace(' 0', ' ')}</b>\n\n"
                "Use it, shift again, or send a specific time.",
                parse_mode=ParseMode.HTML,
                reply_markup=_slot_keyboard(),
            )
            return

    if data == "cancel" or job is None:
        context.user_data.pop("job", None)
        await query.edit_message_text("❌ Cancelled. Send another video anytime.")
        return

    if data.startswith("priv:"):
        privacy = data.split(":", 1)[1]
        job["privacy"] = privacy
        db: Database = context.application.bot_data["db"]
        db.update_video(job["video_id"], privacy=privacy)
        brief: VideoBrief = job["brief"]
        if brief is None:
            return
        await query.edit_message_text(
            _preview_html(brief, privacy, CATEGORIES.get(brief.category_id, "Unknown"),
                          slot=job.get("slot_obj")),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(privacy, best_slot=job.get("slot_obj")),
        )
        return

    if data in ("regen", "upload", "best", "custom"):
        if job.get("brief") is None:
            await query.answer("⏳ Still analyzing — one moment.", show_alert=True)
            return

    if data == "regen":
        job["brief"] = None
        status = await query.edit_message_text("🔁 Regenerating title, description and hashtags…")
        await _run_analysis(update, context, status)
        return

    if data == "upload":
        status = await query.edit_message_text("🚀 Publishing now…")
        await _publish_now(context, job, status)
        return

    if data == "best":
        status = await query.edit_message_text("🧠 Finding the best available slot…")
        await _offer_best_slot(context, job, status)
        return

    if data == "custom":
        job["awaiting_time"] = True
        await query.edit_message_text(
            "🕐 <b>When should this Short go out?</b>\n\n"
            "Send a time in IST, for example:\n"
            "• <i>tomorrow 6:30 PM</i>\n• <i>today at 20:00</i>\n• <i>on friday 9 am</i>\n"
            "• <i>best time</i> — let the engine choose",
            parse_mode=ParseMode.HTML,
        )
        return


# ---------------------------------------------------------------------- errors
async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single place every uncaught handler exception lands.

    Logs the full traceback and, when we know where to reply, tells the user
    instead of letting the failure disappear silently.
    """
    err = context.error

    # Long-polling network blips (httpx.ReadError, timeouts) arrive with
    # update=None. PTB retries them automatically and there is no chat to
    # answer, so log one line instead of a full traceback + owner alert —
    # otherwise a flaky link floods the log with "handling NoneType" errors.
    if update is None and isinstance(err, (NetworkError, TimedOut)):
        log.warning("Telegram network error (polling will retry): %s", err)
        return

    log.exception("Unhandled error while handling %s", type(update).__name__,
                  exc_info=err)

    notifier: Notifier | None = context.application.bot_data.get("notifier")

    text = f"💥 <b>Something went wrong</b>\n\n<code>{html.escape(str(err) or type(err).__name__)}</code>"
    if isinstance(err, AuthRequired):
        text += "\n\n🔐 Run <code>python scripts/youtube_auth.py --force</code>."
    elif "timed out" in str(err).lower():
        text += "\n\n🌐 Looks like a network timeout — I'll retry, or try again shortly."
    text += "\n\nFull details are in <code>data/logs/app.log</code>."

    # Prefer replying in the chat that triggered it; fall back to the owner chat.
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)
            return
    except Exception:  # noqa: BLE001 - fall through to the notifier
        pass
    if notifier:
        notifier.send(text)


# ---------------------------------------------------------------------- app
def build_application(cfg: Config) -> Application:
    builder = ApplicationBuilder().token(cfg.telegram_token)
    if cfg.telegram_local_api_url:
        builder.base_url(cfg.telegram_local_api_url.rstrip("/") + "/bot")
    builder.post_init(_post_init).post_shutdown(_post_shutdown)

    app = builder.build()

    db = Database(cfg.db_path)
    uploader = YouTubeUploader(cfg)
    # Background paths (scheduler, analytics sync, research) use the
    # non-interactive provider so they can never stall on a browser prompt.
    stats = YouTubeStats(uploader.credentials_background, uploader.missing_scopes)
    notifier = Notifier(int(cfg.telegram_chat_id) if cfg.telegram_chat_id else None)
    # Wire the uploader's Telegram alerts + time-windowed dedupe so a revoked
    # refresh token is reported ONCE (not spammed every 45 min) and so the
    # automatic recovery is announced when it happens.
    uploader.bind_notifications(
        notifier=notifier,
        claim=lambda key, window: db.claim_notification(key, window_seconds=window),
    )
    goal = DailyGoalManager(db, notifier, target=cfg.daily_goal, reminder_times=cfg.reminder_times)
    publisher = Publisher(db, uploader, stats, goal, notifier,
                          delete_after_upload=cfg.delete_after_upload)
    llm = LLM(cfg)
    research = ResearchEngine(cfg, db, stats, llm)
    scheduler = Scheduler(
        cfg, db, notifier, publisher, goal, stats, research,
        tick_seconds=cfg.scheduler_tick_seconds,
    )

    stored_chat = db.get_setting("owner_chat_id")
    if stored_chat and not notifier.chat_id:
        notifier.set_chat(int(stored_chat))

    app.bot_data.update({
        "cfg": cfg,
        "db": db,
        "uploader": uploader,
        "stats": stats,
        "notifier": notifier,
        "goal": goal,
        "publisher": publisher,
        "llm": llm,
        "research": research,
        "scheduler": scheduler,
        "analyzer": VideoAnalyzer(cfg),
        "overlay": TextOverlayEngine(cfg),
    })

    # [AUTONOMOUS-FITNESS-V2] Additive autonomous feature wiring. All flags
    # default to OFF; if anything here fails the existing bot still starts.
    try:
        from features.autonomous_config import AutonomousConfig
        from features.job_store import JobStore

        _aconf = AutonomousConfig.load(cfg)
        app.bot_data["autonomous_cfg"] = _aconf
        app.bot_data["autonomous_store"] = JobStore(cfg.db_path)
        # Share the already-created analyzer / LLM / research engine with the
        # scheduler-driven autonomous worker (no second clients or scopes).
        scheduler.feature_analyzer = app.bot_data["analyzer"]
        scheduler.feature_llm = llm
        scheduler.feature_research = research
        # Share the same store/config instances so the scheduler bridge never
        # opens a second database handle or re-reads the environment.
        scheduler._autonomous_config = _aconf
        scheduler._autonomous_store = app.bot_data["autonomous_store"]
        log.info(
            "Autonomous features: content=%s scheduler=%s unattended=%s discovery=%s",
            _aconf.enabled,
            _aconf.scheduler_enabled,
            _aconf.unattended_publish,
            _aconf.youtube_discovery_enabled,
        )
        if _aconf.allow_download and not _aconf.ytdlp_cookiefile:
            log.warning(
                "AUTONOMOUS_ALLOW_DOWNLOAD is on but no YouTube cookies file is "
                "configured. Datacenter IPs get 'Sign in to confirm you're not a "
                "bot' and every remote download will fail. Send a cookies.txt to "
                "the bot with /autopilot_cookies (or set YTDLP_COOKIEFILE)."
            )
        elif _aconf.ytdlp_cookiefile and not Path(_aconf.ytdlp_cookiefile).is_file():
            log.warning(
                "YTDLP_COOKIEFILE is set but the file is missing: %s",
                _aconf.ytdlp_cookiefile,
            )
    except Exception:  # noqa: BLE001 - existing functionality must survive
        log.exception("Autonomous feature wiring failed — bot continues unchanged")

    app.add_handler(TypeHandler(Update, _capture_chat), group=-1)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("besttime", cmd_besttime))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("decide", cmd_decide))
    app.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.VIDEO_NOTE
            | (
                filters.Document.ALL
                & (
                    filters.Document.MimeType("video/mp4")
                    | filters.Document.MimeType("video/quicktime")
                    | filters.Document.MimeType("video/x-matroska")
                    | filters.Document.MimeType("video/webm")
                )
            ),
            receive_video,
        )
    )
    # [AUTONOMOUS-FITNESS-V2] Additive commands + the `af:` callback namespace.
    # Registered BEFORE the generic callback handler and pattern-restricted, so
    # no existing callback payload can ever reach the autonomous handler.
    try:
        from features.telegram_autonomous_hooks import register_autonomous_handlers

        register_autonomous_handlers(app)
    except Exception:  # noqa: BLE001 - existing bot must start unchanged
        log.exception("Autonomous Telegram handlers not registered — bot continues unchanged")

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(handle_error)
    return app


async def _post_init(app: Application) -> None:
    import asyncio as _asyncio

    notifier: Notifier = app.bot_data["notifier"]
    scheduler: Scheduler = app.bot_data["scheduler"]
    notifier.bind(app, _asyncio.get_running_loop())
    scheduler.start()
    log.info("Agent ready (daily goal=%s, IST)", app.bot_data["cfg"].daily_goal)


async def _post_shutdown(app: Application) -> None:
    scheduler: Scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.stop()
