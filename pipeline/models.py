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
