"""Live terminal dashboard for the Shorts agent.

Instead of an endless scrolling log, `python main.py` renders one screen with
everything that matters: the daily goal, the publish queue and a rolling feed
of recent events. It is strictly a view — every action still happens in
Telegram — and it degrades to plain logs whenever the terminal is not a TTY.

Driven from the asyncio loop (refresh is manual, not Rich's own thread) so
there is exactly one thread touching the terminal.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.clock import now_ist

log = logging.getLogger(__name__)

# Matches CSI sequences (colour, cursor movement, hide/show cursor) plus OSC.
# Third-party libs (google-genai, httpx, rich itself) log with colours baked in;
# if those bytes reach the dashboard panel the terminal EXECUTES them as cursor
# commands and leaves garbage like "[[b" and "[[A" in the scrollback.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"   # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"              # other single-char escapes
)


def _strip_ansi(text: str) -> str:
    """Remove terminal control sequences so they can never be rendered."""
    return _ANSI_RE.sub("", text)


class _EventFeed(logging.Handler):
    """Keeps the newest log lines in memory for the dashboard's event feed."""

    _STYLES = {
        "ERROR": "bold red",
        "CRITICAL": "bold red",
        "WARNING": "yellow",
        "INFO": "white",
        "DEBUG": "dim",
    }

    def __init__(self, sink: deque, width: int = 160):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.width = width
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                            "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Strip terminal control sequences first: a coloured log line from a
            # third-party library would otherwise move the cursor around inside
            # the panel and leave "[[b" / "[[A" garbage in the scrollback.
            line = _strip_ansi(self.format(record))
            # Collapse control chars (bells, stray newlines) that would break the box.
            line = "".join(ch for ch in line if ch == " " or ch >= " ")
            style = self._STYLES.get(record.levelname, "white")
            self.sink.append((line[: self.width], style))
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass


class Dashboard:
    """Renders the agent's live state as a single Rich screen."""

    def __init__(self, bot_data: dict, max_events: int = 16,
                 console: Console | None = None):
        self.data = bot_data
        self.events: deque[tuple[str, str]] = deque(maxlen=max_events)
        self._started = time.monotonic()
        self._handler = _EventFeed(self.events)
        # Needed to clip lines to the real terminal width at render time.
        self.console = console or Console()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        logging.getLogger().addHandler(self._handler)

    def stop(self) -> None:
        logging.getLogger().removeHandler(self._handler)

    # ---------------------------------------------------------------- helpers
    def _safe(self, obj, name: str, default=None):
        """Call obj.name() defensively. Accepts None for obj (returns default)."""
        if obj is None:
            return default
        try:
            return getattr(obj, name)()
        except Exception:  # noqa: BLE001 - a busy DB must not blank the screen
            return default

    def _uptime_str(self) -> str:
        secs = int(time.monotonic() - self._started)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ---------------------------------------------------------------- render
    def render(self) -> Layout:
        try:
            return self._build()
        except Exception:  # noqa: BLE001 - never let the dashboard kill the bot
            log.exception("dashboard render failed")
            return Layout(Panel(Text("Dashboard unavailable — see data/logs/app.log",
                                     style="red"), title="Shorts Agent"))

    def _build(self) -> Layout:
        """Deliberately minimal: a slim status line + the live activity feed.

        The daily goal and the publish queue are Telegram's job (the bot already
        reminds you). The terminal only answers "what is it doing right now?".
        """
        root = Layout()
        root.split_column(
            Layout(self._header(), name="header", size=3),
            Layout(self._events_panel(), name="events"),
        )
        return root

    def _header(self) -> Panel:
        cfg = self.data.get("cfg")
        scheduler = self.data.get("scheduler")
        running = bool(getattr(scheduler, "_thread", None) and scheduler._thread.is_alive())
        model = str(getattr(cfg, "gemini_model", "?")).replace("gemini-", "")

        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")

        grid.add_row(
            Text("🎬 Shorts Automation Agent", style="bold cyan"),
            Text(f"{now_ist().strftime('%a %d %b %I:%M %p')}".replace(" 0", " ")
                 + f"  ·  ⏱ {self._uptime_str()}"
                 + f"  ·  {'🟢' if running else '🔴'}"
                 + f"  ·  🧠 {model}",
                 style="green" if running else "red"),
        )
        return Panel(grid, style="on grey11")

    def _events_panel(self) -> Panel:
        body = Text()
        if not self.events:
            body.append("Waiting for activity…\n", style="dim")
            body.append("Send a Short to your Telegram bot to begin.", style="dim")
        else:
            # Clip to the real terminal width at RENDER time. A line longer than
            # the terminal wraps, which desyncs Rich's cursor math on redraw and
            # leaves "[A" / "[[b" fragments in the scrollback.
            inner = max(20, self.console.width - 6)  # panel borders + padding
            for line, style in self.events:
                body.append(line[:inner] + "\n", style=style)
        return Panel(body, title="📡 Live events", border_style="yellow")


def run_with_dashboard(app, bot_data: dict, refresh_hz: float = 4.0) -> None:
    """Run polling + the dashboard together. Replaces app.run_polling().

    Ctrl+C must leave the terminal exactly as it found it: no "Shutting down…"
    line, no trailing scheduler logs, no traceback. Logging is muted for the
    duration of the teardown and every step is individually guarded, so a
    second Ctrl+C mid-shutdown still exits cleanly.
    """
    dashboard = Dashboard(bot_data)
    console = Console()
    dashboard.console = console  # clip event lines to this terminal's width
    interval = 1.0 / max(0.5, refresh_hz)

    async def _teardown() -> None:
        """Silently stop everything. Never raises, never prints."""
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.CRITICAL + 1)  # mute every log line from here on
        try:
            dashboard.stop()
            if getattr(app, "updater", None) and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass  # a second Ctrl+C during teardown is fine — just stop
        except Exception:  # noqa: BLE001 - teardown must never surface an error
            pass
        finally:
            root.setLevel(previous_level)

    async def _run() -> None:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=None, drop_pending_updates=True)
        dashboard.start()
        try:
            with Live(dashboard.render(), console=console, screen=True,
                      auto_refresh=False) as live:
                while True:
                    live.update(dashboard.render())
                    await asyncio.sleep(interval)
        finally:
            await _teardown()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Restore the cursor Rich hid and leave silently — nothing printed.
        console.show_cursor(True)
    except Exception:  # noqa: BLE001 - a real startup failure, not a Ctrl+C
        raise
