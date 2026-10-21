"""Caption/overlay text helpers for the Telegram workflow.

Three modes (per the feature spec):
* Mode B — the creator supplied the exact text in the video's caption
  (e.g. `Add this text to my video: "Nobody expected this to happen!"`).
  That exact text is used verbatim.
* Mode A/C — no text given: the agent analyzes the video and proposes a few
  one-line captions; the creator picks one, types their own, or skips.

These are pure text helpers (no Telegram, no FFmpeg, no Gemini) so they are
trivially unit-testable and reusable by both the immediate and scheduled
publishing paths.
"""

from __future__ import annotations

import re

# Phrases that introduce creator-supplied overlay text (Mode B). Each is
# matched at the start of a caption, optionally followed by a colon.
_CUSTOM_TRIGGERS = (
    "add this text to my video",
    "add this text",
    "add text to my video",
    "add text",
    "add caption",
    "add overlay",
    "overlay text",
    "overlay:",
    "text on video",
    "put this text",
    "burn this text",
    "show this text",
    "caption:",
    "text:",
)

_QUOTED = re.compile(r"[\"“']([^\"”']{2,120})[\"”']")
_CHOICE = re.compile(r"^\s*(\d{1,2})\s*[.).]?\s*$")

#: Max characters we allow on a burned-in overlay (keeps it legible).
MAX_OVERLAY_CHARS = 120


def extract_custom_text(caption: str | None) -> str | None:
    """Return the creator's exact overlay text (Mode B), else None.

    Recognises `...: "quoted text"` and `...: unquoted rest-of-caption`. The
    returned text is the creator's wording, cleaned of wrapping quotes and
    collapsed whitespace — NOT rewritten.
    """
    if not caption or not caption.strip():
        return None
    text = caption.strip()

    lowered = text.lower()
    trigger_at = -1
    trigger_len = 0
    for trigger in _CUSTOM_TRIGGERS:
        idx = lowered.find(trigger)
        if idx != -1 and (trigger_at == -1 or idx < trigger_at):
            trigger_at = idx
            trigger_len = len(trigger)
    if trigger_at == -1:
        # Fall back to a bare quoted string with no trigger word.
        match = _QUOTED.search(text)
        if match:
            return _clean(match.group(1))
        return None

    remainder = text[trigger_at + trigger_len:].lstrip()
    remainder = remainder.lstrip(":：-–— ").strip()
    if not remainder:
        return None

    match = _QUOTED.search(remainder)
    if match:
        return _clean(match.group(1))
    # Unquoted: use the rest of the caption as the overlay text.
    return _clean(remainder)


def _clean(value: str) -> str | None:
    cleaned = " ".join((value or "").split())
    cleaned = cleaned.strip().strip("\"“”'‘’")
    if not cleaned:
        return None
    if len(cleaned) > MAX_OVERLAY_CHARS:
        cleaned = cleaned[: MAX_OVERLAY_CHARS - 1].rstrip() + "…"
    return cleaned or None


def parse_choice(text: str | None) -> int | None:
    """Parse a bare '1' / '2' / '3' selection. Returns 0-based index or None."""
    if not text:
        return None
    match = _CHOICE.match(text)
    if not match:
        return None
    return int(match.group(1)) - 1


def format_suggestions(captions: list[str], timeout_minutes: int) -> str:
    """The 'AI Caption Suggestions' Telegram message (HTML-safe-ish plain text)."""
    lines = ["<b>AI Caption Suggestions</b>", ""]
    for i, cap in enumerate(captions, start=1):
        lines.append(f"{i}. {cap}")
    lines.append("")
    lines.append("Reply with <b>1</b>, <b>2</b> or <b>3</b> to pick one.")
    lines.append("Or send your own text, or tap <b>Skip</b> to continue without overlay.")
    lines.append(f"<i>(Continues without overlay after {timeout_minutes} min of no reply.)</i>")
    return "\n".join(lines)
