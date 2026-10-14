"""Thread-safe Telegram notifications.

The background scheduler runs in its own thread; this bridges back onto the
python-telegram-bot event loop so messages are sent safely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

log = logging.getLogger(__name__)

TELEGRAM_MAX = 4000  # leave headroom under Telegram's 4096-char limit


def split_message(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split a long report into <=limit chunks on paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        piece = block + "\n\n"
        if len(current) + len(piece) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks or [text[:limit]]


class Notifier:
    def __init__(self, default_chat_id: int | None = None):
        self._app = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._default_chat_id = default_chat_id

    def bind(self, app, loop) -> None:
        self._app = app
        self._loop = loop

    def set_chat(self, chat_id: int) -> None:
        if chat_id:
            self._default_chat_id = int(chat_id)
            self._on_chat = int(chat_id)

    @property
    def chat_id(self) -> int | None:
        return self._default_chat_id

    # --------------------------------------------------------------- sync API
    def send(self, text: str, chat_id: int | None = None) -> bool:
        """Send from any thread. Fire-and-forget; returns False if not bound yet."""
        target = chat_id or self._default_chat_id
        if not self._app or not self._loop or not target:
            log.warning("Notifier not ready (chat_id=%s); message dropped: %s",
                        target, text[:80])
            return False
        chunks = split_message(text)
        for chunk in chunks:
            asyncio.run_coroutine_threadsafe(self._send_async(target, chunk), self._loop)
        return True

    async def _send_async(self, chat_id: int, text: str) -> None:
        from telegram.constants import ParseMode

        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except Exception as exc:  # noqa: BLE001 - notifications must never crash the agent
            log.warning("Telegram notify failed: %s", exc)

    async def send_async(self, text: str, chat_id: int | None = None) -> None:
        target = chat_id or self._default_chat_id
        if not target:
            return
        for chunk in split_message(text):
            await self._send_async(target, chunk)

    def send_many(self, messages: Iterable[str], chat_id: int | None = None) -> None:
        for message in messages:
            self.send(message, chat_id=chat_id)
