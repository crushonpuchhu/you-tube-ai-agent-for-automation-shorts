"""Central configuration, loaded from the .env file and filesystem layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
VIDEO_DIR = DATA_DIR / "videos"
TOKEN_DIR = DATA_DIR / "tokens"
LOG_DIR = DATA_DIR / "logs"
AUTH_DIR = ROOT / "auth"

for _dir in (DATA_DIR, VIDEO_DIR, TOKEN_DIR, LOG_DIR, AUTH_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = True) -> bool:
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


def _list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _default_db_path() -> Path:
    """SQLite lives on a local disk by default — NTFS/exFAT/FUSE mounts
    (e.g. anything under /media/...) are unreliable for SQLite locking.
    Override with AGENT_DB_PATH in .env if your project drive is a native Linux FS."""
    return Path.home() / ".shorts-agent" / "agent.db"


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_token: str
    telegram_local_api_url: str
    telegram_chat_id: str

    # Gemini
    gemini_api_key: str
    gemini_model: str

    # YouTube
    youtube_client_secret: Path
    youtube_token_file: Path
    youtube_default_category_id: str
    youtube_default_privacy: str

    # Limits
    max_video_bytes: int

    # Storage hygiene — downloaded Shorts are deleted, not kept forever.
    delete_after_upload: bool
    video_retention_days: int

    # Terminal UI — live dashboard instead of a scrolling log when on a TTY.
    terminal_ui: bool

    # --- agent (added, all optional with sensible defaults) ---
    db_path: Path
    daily_goal: int
    min_gap_hours: float
    min_lead_minutes: int
    reminder_times: list[str]
    weekly_report_enabled: bool
    weekly_weekday: int
    weekly_hour: int
    weekly_minute: int
    analytics_sync_hours: int
    research_enabled: bool
    research_hour: int
    research_youtube: bool
    research_news: bool
    research_reddit: bool
    channel_niche: str
    scheduler_tick_seconds: int

    @classmethod
    def load(cls) -> "Config":
        max_mb = float(_env("MAX_VIDEO_MB", "20") or 20)
        db_raw = _env("AGENT_DB_PATH")
        db_path = Path(db_raw).expanduser() if db_raw else _default_db_path()
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_local_api_url=_env("TELEGRAM_LOCAL_API_URL"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite",
            youtube_client_secret=ROOT / _env("YOUTUBE_CLIENT_SECRET", "auth/client_secret.json"),
            youtube_token_file=ROOT / _env("YOUTUBE_TOKEN_FILE", "data/tokens/youtube_token.json"),
            youtube_default_category_id=_env("YOUTUBE_DEFAULT_CATEGORY_ID", "24") or "24",
            youtube_default_privacy=(_env("YOUTUBE_DEFAULT_PRIVACY", "private") or "private").lower(),
            max_video_bytes=int(max_mb * 1024 * 1024),
            delete_after_upload=_bool("DELETE_AFTER_UPLOAD", True),
            video_retention_days=max(0, _int("VIDEO_RETENTION_DAYS", 3)),
            terminal_ui=_bool("TERMINAL_UI", False),
            db_path=db_path,
            daily_goal=_int("DAILY_SHORT_GOAL", 2),
            min_gap_hours=_float("MIN_GAP_BETWEEN_SHORTS_HOURS", 4.0),
            min_lead_minutes=_int("MIN_LEAD_MINUTES", 30),
            reminder_times=_list("GOAL_REMINDER_TIMES", "20:30,22:30"),
            weekly_report_enabled=_bool("WEEKLY_REPORT_ENABLED", True),
            weekly_weekday=_int("WEEKLY_REPORT_WEEKDAY", 0),
            weekly_hour=_int("WEEKLY_REPORT_HOUR", 20),
            weekly_minute=_int("WEEKLY_REPORT_MINUTE", 0),
            analytics_sync_hours=_int("ANALYTICS_SYNC_HOURS", 6),
            research_enabled=_bool("RESEARCH_ENABLED", True),
            research_hour=_int("RESEARCH_HOUR", 10),
            research_youtube=_bool("RESEARCH_YOUTUBE", True),
            research_news=_bool("RESEARCH_NEWS", True),
            research_reddit=_bool("RESEARCH_REDDIT", True),
            channel_niche=_env(
                "CHANNEL_NICHE", "coding tips, tech setup, AI tools, productivity"
            ),
            scheduler_tick_seconds=max(10, _int("SCHEDULER_TICK_SECONDS", 30)),
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable setup problems (empty == ready to run)."""
        problems: list[str] = []
        if not self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN is missing in .env (get one from @BotFather).")
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY is missing in .env (free key: aistudio.google.com/apikey).")
        if not self.youtube_client_secret.exists():
            problems.append(
                f"YouTube OAuth client secret not found at {self.youtube_client_secret} "
                "(download it from Google Cloud Console -> Credentials)."
            )
        if self.youtube_default_privacy not in {"private", "unlisted", "public"}:
            problems.append("YOUTUBE_DEFAULT_PRIVACY must be private, unlisted or public.")
        if self.daily_goal < 1:
            problems.append("DAILY_SHORT_GOAL must be at least 1.")
        if not 0 <= self.weekly_weekday <= 6:
            problems.append("WEEKLY_REPORT_WEEKDAY must be 0 (Monday) .. 6 (Sunday).")
        if not 0 <= self.weekly_hour <= 23:
            problems.append("WEEKLY_REPORT_HOUR must be 0..23.")
        return problems
