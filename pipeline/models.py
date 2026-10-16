"""Data models shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class VideoBrief:
    """Everything Gemini figured out about the clip, ready for YouTube."""

    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category_id: str = "24"
    language: str = "en"
    hook_summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def get(self, key: str, default=None):
        """Dict-style access.

        The bot stores either a VideoBrief (fresh analysis) or a plain dict
        (loaded from the DB) in the same slots, so both shapes must support
        `.get(...)` — this used to crash with
        "'VideoBrief' object has no attribute 'get'".
        """
        return getattr(self, key, default)
