"""Entry point: python main.py"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import Config, LOG_DIR


def _setup_logging(console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # The dashboard owns the screen when active, so the console handler would
    # corrupt it — events reach the dashboard through its own log handler instead.
    if console:
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        root.addHandler(handler)

    # Quieter third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)


def main() -> None:
    cfg = Config.load()

    # Decide the UI mode first: the console log handler must not be installed
    # when a live dashboard is going to redraw the same terminal.
    use_dashboard = cfg.terminal_ui and sys.stdout.isatty()
    _setup_logging(console=not use_dashboard)

    problems = cfg.validate()
    if problems:
        for p in problems:
            logging.error("Setup problem: %s", p)
        raise SystemExit("Fix the problems above (see .env.example and README.md), then run again.")

    # Import after config validation so missing keys fail with a clear message first.
    from bot.app import build_application
    from pipeline.youtube_uploader import YouTubeUploader

    # Fail loudly at startup if the YouTube token cannot work unattended —
    # much better than discovering it when a scheduled Short is due.
    report = YouTubeUploader(cfg).preflight()
    if report["ok"]:
        logging.info("YouTube auth OK — token refreshes silently in the background")
    else:
        logging.warning("YouTube auth NOT ready: %s", report["hint"])
        logging.warning(
            "The bot still starts (Telegram + Gemini work), but publishing will "
            "be blocked until you fix the auth."
        )

    logging.info("Starting Shorts Automation Bot…")
    app = build_application(cfg)

    if use_dashboard:
        from bot.dashboard import run_with_dashboard
        run_with_dashboard(app, app.bot_data)
    else:
        app.run_polling(allowed_updates=None, drop_pending_updates=True)


if __name__ == "__main__":
    main()
