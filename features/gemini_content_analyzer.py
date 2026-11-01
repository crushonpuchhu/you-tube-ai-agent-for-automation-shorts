"""Gemini highlight selection for long-form source media.

This module reuses the application's existing ``pipeline.analyzer.VideoAnalyzer``
(SDK client, model name, retry/backoff helpers and JSON parsing helpers).  It
does not create a second Gemini SDK or a second model configuration.

The model returns timestamps only.  Everything that can be validated locally is
validated locally: fenced JSON, malformed timestamps, non-numeric values,
out-of-range bounds, overlaps, minimum/maximum clip length and the source
duration.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from agent.clock import now_ist
from pipeline.analyzer import _json_loads_best_effort

log = logging.getLogger(__name__)

MOMENT_PROMPT = """\
You are selecting moments from a long-form video for a vertical YouTube Short.

Topic / niche: {topic}
Source duration: {duration_label} ({duration_seconds:.0f} seconds)
Target clip length: {clip_min}-{clip_max} seconds
Maximum moments to return: {max_clips}

Watch and listen to the whole video. Choose the most engaging OR genuinely
educational moments that stand alone without prior context. Prefer:
- a clear explanation, demonstration, transformation, or useful tip;
- a strong opening line in the first 2 seconds;
- moments that are visually suitable for a 9:16 centre crop.

Return ONLY a raw JSON object (no markdown fences, no commentary):
{{
  "moments": [
    {{
      "start": "HH:MM:SS",
      "end": "HH:MM:SS",
      "title": "short internal label",
      "reason": "why this moment works",
      "score": 0-100,
      "confidence": "high | medium | low"
    }}
  ]
}}

Rules:
- Timestamps must be inside 00:00:00 and {duration_label}.
- Each moment must be between {clip_min} and {clip_max} seconds long.
- Do not invent events that are not visible or audible.
- Do not promise virality; score is editorial suitability only.
"""

_TIME_RE = re.compile(
    r"^(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)$"
)
_DURATION_RE = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?$"
)


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_time(value: Any) -> float | None:
    """Parse ``HH:MM:SS``, ``MM:SS``, ``1h2m3s`` or a number of seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if seconds >= 0 else None

    text = str(value).strip().lower()
    if not text:
        return None

    match = _TIME_RE.match(text)
    if match:
        hours = float(match.group("h") or 0)
        minutes = float(match.group("m") or 0)
        seconds = float(match.group("s") or 0)
        return max(0.0, hours * 3600 + minutes * 60 + seconds)

    if "h" in text or "m" in text or "s" in text:
        match = _DURATION_RE.match(text)
        if match and any(match.groupdict().values()):
            hours = float(match.group("h") or 0)
            minutes = float(match.group("m") or 0)
            seconds = float(match.group("s") or 0)
            return max(0.0, hours * 3600 + minutes * 60 + seconds)

    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _coerce_score(raw: Any) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 50.0
    if 0 < value <= 1:  # models sometimes return a 0-1 confidence
        value *= 100
    return int(max(0, min(100, round(value))))


def _coerce_confidence(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    try:
        value = float(text)
    except ValueError:
        return "medium"
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _moment_items(payload: Any) -> list[dict]:
    if isinstance(payload, str):
        try:
            payload = _json_loads_best_effort(payload)
        except ValueError:
            return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("moments", "highlights", "clips", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def parse_moments(
    payload: Any,
    source_duration: float = 0,
    *,
    clip_min_seconds: int = 20,
    clip_max_seconds: int = 55,
    max_clips: int = 5,
) -> list[dict]:
    """Validate and normalise model moments.  Returns ``[]`` for bad output."""
    clip_min = max(1, int(clip_min_seconds))
    clip_max = max(clip_min + 1, int(clip_max_seconds))
    max_clips = max(1, int(max_clips))
    duration = max(0.0, float(source_duration or 0))

    normalised: list[dict] = []
    for raw in _moment_items(payload):
        start = parse_time(
            raw.get("start", raw.get("start_seconds", raw.get("start_time", raw.get("startTime"))))
        )
        end = parse_time(
            raw.get("end", raw.get("end_seconds", raw.get("end_time", raw.get("endTime"))))
        )
        if start is None or end is None:
            log.info("Dropping moment with unreadable timestamps: %r", raw)
            continue
        if duration:
            if start >= duration:
                continue
            end = min(end, duration)
        if end <= start:
            continue

        # Expand a too-short moment when the source allows it, otherwise drop it.
        if end - start < clip_min:
            needed = clip_min - (end - start)
            start = max(0.0, start - needed / 2)
            end = start + clip_min
            if duration:
                end = min(end, duration)
                if end - start < clip_min:
                    start = max(0.0, end - clip_min)
            if end - start < clip_min - 0.5:
                log.info("Dropping moment shorter than %ss: %r", clip_min, raw)
                continue

        # Trim an over-long moment instead of rejecting a useful segment.
        if end - start > clip_max:
            end = start + clip_max
        if duration:
            end = min(end, duration)
        if end <= start:
            continue

        normalised.append(
            {
                "start": round(float(start), 2),
                "end": round(float(end), 2),
                "duration": round(float(end - start), 2),
                "start_label": format_timestamp(start),
                "end_label": format_timestamp(end),
                "title": " ".join(
                    str(raw.get("title") or raw.get("label") or raw.get("hook") or "")
                    .split()
                )[:160],
                "reason": " ".join(
                    str(
                        raw.get("reason")
                        or raw.get("why")
                        or raw.get("rationale")
                        or raw.get("description")
                        or ""
                    ).split()
                )[:600],
                "score": _coerce_score(raw.get("score") or raw.get("confidence_score")),
                "confidence": _coerce_confidence(raw.get("confidence")),
                "selected_at": now_ist().isoformat(),
            }
        )

    # Remove overlapping moments, keeping the higher-scoring one first.
    accepted: list[dict] = []
    for moment in sorted(normalised, key=lambda m: m["score"], reverse=True):
        overlaps = any(
            moment["start"] < other["end"] - 0.5
            and other["start"] < moment["end"] - 0.5
            for other in accepted
        )
        if not overlaps:
            accepted.append(moment)
        if len(accepted) >= max_clips:
            break

    accepted.sort(key=lambda m: m["start"])
    return accepted


def _duration_label(seconds: float) -> str:
    return format_timestamp(seconds) if seconds else "unknown"


def analyze_source_with_gemini(
    source_path: str | Path,
    topic: str,
    *,
    analyzer: Any = None,
    cfg: Any = None,
    source_duration: float = 0,
    clip_min_seconds: int = 20,
    clip_max_seconds: int = 55,
    max_clips: int = 5,
) -> list[dict]:
    """Ask the existing Gemini video analyzer for highlight timestamps.

    Blocking; run it in a worker thread.  Raises on a genuine API failure so
    the caller can record it, but returns ``[]`` for an unusable model answer.
    """
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(f"Source video not found: {path}")

    if analyzer is None:
        from config import Config
        from pipeline.analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer(cfg or Config.load())

    duration = float(source_duration or 0)
    if duration <= 0:
        try:
            from features.shorts_editor import probe_duration

            duration = probe_duration(
                path, getattr(getattr(analyzer, "cfg", None), "ffprobe_path", "ffprobe")
            )
        except Exception as exc:  # noqa: BLE001 - ffprobe may be unavailable
            log.warning("Could not probe source duration: %s", str(exc)[:200])
            duration = 0

    prompt = MOMENT_PROMPT.format(
        topic=topic or "general",
        duration_label=_duration_label(duration),
        duration_seconds=duration,
        clip_min=max(1, int(clip_min_seconds)),
        clip_max=max(2, int(clip_max_seconds)),
        max_clips=max(1, int(max_clips)),
    )

    # Reuse the exact upload/retry plumbing of the existing analyzer.
    upload = getattr(analyzer, "_upload", None)
    wait = getattr(analyzer, "_wait_until_active", None)
    generate = getattr(analyzer, "_generate_with_retry", None)
    if upload and wait and generate:
        file_ref = upload(path)
        file_ref = wait(file_ref)
        text = generate(file_ref, prompt)
    else:  # pragma: no cover - alternate/mock analyzer
        file_ref = analyzer.client.files.upload(file=str(path))
        text = analyzer.client.models.generate_content(
            model=analyzer.model,
            contents=[file_ref, prompt],
            config=None,
        ).text or ""

    try:
        payload = _json_loads_best_effort(text)
    except ValueError:
        log.warning("Gemini returned non-JSON moments: %s", str(text)[:300])
        return []

    moments = parse_moments(
        payload,
        duration,
        clip_min_seconds=clip_min_seconds,
        clip_max_seconds=clip_max_seconds,
        max_clips=max_clips,
    )
    log.info(
        "Gemini selected %d valid moment(s) from %s (%.0fs source)",
        len(moments),
        path.name,
        duration,
    )
    return moments


async def analyze_source_with_gemini_async(source_path: str | Path, topic: str, **kwargs) -> list[dict]:
    return await asyncio.to_thread(analyze_source_with_gemini, source_path, topic, **kwargs)
