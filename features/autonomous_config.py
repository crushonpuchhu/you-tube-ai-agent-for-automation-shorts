"""Feature-flag and tuning configuration for the autonomous workflow.

This module deliberately does **not** change ``config.Config`` or any existing
environment variable.  It reads the additive ``AUTONOMOUS_*`` / ``ENABLE_*``
keys directly and falls back to the existing ``Config`` values (Gemini model,
FFmpeg paths, database path, channel niche, daily goal) wherever possible.

All flags default to OFF.  Loading this module on a default deployment returns
an object whose :attr:`AutonomousConfig.enabled` is ``False``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.clock import IST
from config import DATA_DIR, ROOT, Config

log = logging.getLogger(__name__)

DEFAULT_PUBLISH_TIMES = "06:30,22:00"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    value = _env(name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _valid_times(raw: list[str]) -> list[str]:
    """Keep only valid HH:MM values; fall back to the documented defaults."""
    out: list[str] = []
    for part in raw:
        try:
            parsed = datetime.strptime(part, "%H:%M")
        except ValueError:
            log.warning("Ignoring invalid PUBLISH_TIMES_IST entry: %r", part)
            continue
        value = parsed.strftime("%H:%M")
        if value not in out:
            out.append(value)
    if not out:
        return DEFAULT_PUBLISH_TIMES.split(",")
    # Chronological order keeps "next slot" deterministic for unsorted input.
    return sorted(out, key=lambda value: (int(value[:2]), int(value[3:])))


def _owner_ids(raw: list[str]) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw:
        try:
            ids.append(int(part))
        except ValueError:
            log.warning("Ignoring invalid AUTOPILOT_OWNER_IDS entry: %r", part)
    return tuple(dict.fromkeys(ids))


@dataclass(frozen=True)
class AutonomousConfig:
    """Immutable snapshot of the autonomous feature configuration."""

    # ---- master switches (all default OFF) -----------------------------
    enabled: bool
    scheduler_enabled: bool
    unattended_publish: bool
    trends_enabled: bool
    highlights_enabled: bool
    nl_agent_enabled: bool
    youtube_discovery_enabled: bool

    # ---- channel / content ---------------------------------------------
    bootstrap_niche: str
    channel_language: str
    channel_region: str
    content_mix: str
    shorts_per_day: int
    publish_times_ist: tuple[str, ...]
    bot_timezone: str

    # ---- research / discovery ------------------------------------------
    research_lookback_hours: int
    youtube_source_lookback_hours: int
    youtube_min_source_video_minutes: int
    max_source_video_minutes: int
    youtube_search_results_per_topic: int
    youtube_search_license: str
    ytdlp_cookiefile: str
    youtube_source_deduplication: bool
    max_topics_per_cycle: int

    # ---- clip generation ------------------------------------------------
    max_clips_per_source: int
    clip_min_seconds: int
    clip_max_seconds: int

    # ---- quotas / safety ------------------------------------------------
    max_daily_source_downloads: int
    max_daily_gemini_analyses: int
    max_daily_generated_shorts: int
    max_concurrent_source_jobs: int
    min_disk_space_multiplier: float
    source_allowlist: tuple[str, ...]
    source_dir: Path

    # ---- approval / publishing -----------------------------------------
    owner_review_mode: bool
    auto_publish_requires_license_check: bool
    allow_download: bool
    approval_lead_minutes: int
    cycle_lock_ttl_minutes: int
    owner_ids: tuple[int, ...]

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, cfg: Config | None = None) -> "AutonomousConfig":
        """Build the configuration from the environment.

        ``cfg`` is only used for values that already exist in the application
        (bootstrap niche, Gemini model is read through the injected analyzer).
        The function never mutates the environment or the existing config.
        """
        bootstrap = (getattr(cfg, "channel_niche", "") or "").strip()
        if not bootstrap:
            bootstrap = _env("CHANNEL_NICHE", "fitness") or "fitness"

        source_dir_raw = _env("AUTONOMOUS_SOURCE_DIR")
        if source_dir_raw:
            source_dir = Path(source_dir_raw).expanduser()
            if not source_dir.is_absolute():
                # Relative paths resolve against the project folder, exactly
                # like the existing config module resolves other paths.
                source_dir = ROOT / source_dir
        else:
            source_dir = DATA_DIR / "autonomous_sources"

        times = tuple(_valid_times(_list("PUBLISH_TIMES_IST", DEFAULT_PUBLISH_TIMES)))

        # YouTube blocks anonymous downloads from datacenter IPs ("Sign in to
        # confirm you're not a bot"). A Netscape cookies.txt exported from a
        # signed-in browser fixes it. Explicit env wins; otherwise a file at
        # <data>/cookies.txt is picked up automatically.
        cookiefile = _env("YTDLP_COOKIEFILE")
        if not cookiefile:
            default_cookie = Path(DATA_DIR) / "cookies.txt"
            cookiefile = str(default_cookie) if default_cookie.is_file() else ""

        return cls(
            enabled=_bool("ENABLE_AUTONOMOUS_CONTENT", False),
            scheduler_enabled=_bool("ENABLE_AUTONOMOUS_SCHEDULER", False),
            unattended_publish=_bool("ENABLE_UNATTENDED_PUBLISH", False),
            trends_enabled=_bool("ENABLE_TRENDS", False),
            highlights_enabled=_bool("ENABLE_HIGHLIGHTS", False),
            nl_agent_enabled=_bool("ENABLE_NL_AGENT", False),
            youtube_discovery_enabled=_bool("YOUTUBE_DISCOVERY_ENABLED", False),
            bootstrap_niche=bootstrap,
            channel_language=_env("CHANNEL_LANGUAGE", "Hindi") or "Hindi",
            channel_region=_env("CHANNEL_REGION", "IN") or "IN",
            content_mix=_env("CONTENT_MIX", "70% trending, 30% evergreen educational")
            or "70% trending, 30% evergreen educational",
            shorts_per_day=max(1, _int("SHORTS_PER_DAY", 2)),
            publish_times_ist=times,
            bot_timezone=_env("BOT_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata",
            research_lookback_hours=max(1, _int("RESEARCH_LOOKBACK_HOURS", 72)),
            # Source discovery deliberately does NOT reuse the 72h research
            # window: usable long-form sources are evergreen, and a 3-day
            # filter would reject almost every Creative Commons candidate.
            # 0 means "no publishedAfter filter".
            youtube_source_lookback_hours=max(
                0, _int("YOUTUBE_SOURCE_LOOKBACK_HOURS", 0)
            ),
            youtube_min_source_video_minutes=max(
                1, _int("YOUTUBE_MIN_SOURCE_VIDEO_MINUTES", 5)
            ),
            max_source_video_minutes=max(
                1, _int("MAX_SOURCE_VIDEO_MINUTES", 120)
            ),
            youtube_search_results_per_topic=max(
                1, _int("YOUTUBE_SEARCH_RESULTS_PER_TOPIC", 10)
            ),
            # Only Creative Commons results are searched by default. This makes
            # discovery produce sources the hard rights gate can actually
            # verify, instead of wasting API quota on unusable videos.
            # Set YOUTUBE_SEARCH_LICENSE=any to search every licence.
            youtube_search_license=_env("YOUTUBE_SEARCH_LICENSE", "creativeCommon")
            or "creativeCommon",
            ytdlp_cookiefile=cookiefile,
            youtube_source_deduplication=_bool("YOUTUBE_SOURCE_DEDUPLICATION", True),
            max_topics_per_cycle=max(1, _int("AUTONOMOUS_MAX_TOPICS_PER_CYCLE", 3)),
            max_clips_per_source=max(1, _int("MAX_CLIPS_PER_SOURCE", 5)),
            clip_min_seconds=max(1, _int("CLIP_MIN_SECONDS", 20)),
            clip_max_seconds=max(2, _int("CLIP_MAX_SECONDS", 55)),
            max_daily_source_downloads=max(
                0, _int("MAX_DAILY_SOURCE_DOWNLOADS", 5)
            ),
            max_daily_gemini_analyses=max(
                0, _int("MAX_DAILY_GEMINI_ANALYSES", 10)
            ),
            max_daily_generated_shorts=max(
                0, _int("MAX_DAILY_GENERATED_SHORTS", 4)
            ),
            max_concurrent_source_jobs=max(
                1, _int("MAX_CONCURRENT_SOURCE_JOBS", 1)
            ),
            min_disk_space_multiplier=max(
                1.0, _float("MIN_DISK_SPACE_MULTIPLIER", 2.0)
            ),
            source_allowlist=tuple(_list("SOURCE_ALLOWLIST", "")),
            source_dir=source_dir,
            owner_review_mode=_bool("OWNER_REVIEW_MODE", True),
            auto_publish_requires_license_check=_bool(
                "AUTO_PUBLISH_REQUIRES_LICENSE_CHECK", True
            ),
            allow_download=_bool("AUTONOMOUS_ALLOW_DOWNLOAD", False),
            approval_lead_minutes=max(0, _int("AUTONOMOUS_PUBLISH_LEAD_MINUTES", 90)),
            cycle_lock_ttl_minutes=max(
                5, _int("AUTONOMOUS_CYCLE_LOCK_TTL_MINUTES", 180)
            ),
            owner_ids=_owner_ids(_list("AUTOPILOT_OWNER_IDS", "")),
        )

    # ------------------------------------------------------------- helpers
    @property
    def any_enabled(self) -> bool:
        return any(
            (
                self.enabled,
                self.scheduler_enabled,
                self.unattended_publish,
                self.youtube_discovery_enabled,
            )
        )

    @property
    def cycle_enabled(self) -> bool:
        """Content generation requires ENABLE_AUTONOMOUS_CONTENT."""
        return self.enabled

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.bot_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(
                "Unknown BOT_TIMEZONE %r — falling back to Asia/Kolkata",
                self.bot_timezone,
            )
            return IST

    @property
    def approval_mode(self) -> str:
        if self.unattended_publish:
            return "unattended (owner opted in)"
        return "owner review (Approve / Schedule / Retry / Discard)"

    def slot_time(self, hhmm: str) -> time:
        hour, minute = (int(part) for part in hhmm.split(":", 1))
        return time(hour=hour, minute=minute)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            "AutonomousConfig(content=%s, scheduler=%s, unattended=%s, "
            "discovery=%s, niche=%r)"
            % (
                self.enabled,
                self.scheduler_enabled,
                self.unattended_publish,
                self.youtube_discovery_enabled,
                self.bootstrap_niche,
            )
        )
