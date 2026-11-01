"""FFmpeg clip rendering for the autonomous workflow.

The existing Docker image already installs FFmpeg (the text-overlay feature
uses it), so no base-image change is required.  Every invocation uses an
argument list with ``shell=False`` — no source metadata, topic or timestamp is
ever interpolated into a shell command.

Captions are intentionally not burned in here: the specification requires any
caption to come from real transcription, and this additive workflow does not
add a speech-to-text dependency.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920


class ShortsEditorError(RuntimeError):
    """Raised when a clip cannot be rendered."""


def _run(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            check=True,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ShortsEditorError(
            f"Executable not found: {args[0]}. Install FFmpeg or set FFMPEG_PATH/FFPROBE_PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[:400]
        raise ShortsEditorError(f"{Path(args[0]).name} failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ShortsEditorError(f"{Path(args[0]).name} timed out") from exc


def probe_duration(video_path: str | Path, ffprobe: str = "ffprobe") -> float:
    """Return media duration in seconds."""
    path = Path(video_path)
    if not path.is_file():
        raise ShortsEditorError(f"Video not found: {path}")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    try:
        return max(0.0, float((result.stdout or "").strip()))
    except ValueError as exc:
        raise ShortsEditorError(f"ffprobe returned an unreadable duration for {path}") from exc


def estimate_source_bytes(duration_seconds: float, bits_per_second: int = 4_500_000) -> int:
    """Conservative size estimate (defaults to ~4.5 Mbps video)."""
    return int(max(1.0, float(duration_seconds or 0)) * max(1, bits_per_second) / 8)


def has_disk_space(path: str | Path, needed_bytes: int) -> bool:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return True  # never block on an environment quirk
    return usage.free >= max(0, int(needed_bytes))


def build_vertical_filter(width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> str:
    width = max(2, int(width))
    height = max(2, int(height))
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )


def cut_vertical_clip(
    source_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    output_path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> Path:
    """Cut one 9:16 clip, preserving the source file."""
    source = Path(source_path)
    output = Path(output_path)
    start = max(0.0, float(start_seconds))
    duration = float(end_seconds) - start
    if not source.is_file():
        raise ShortsEditorError(f"Source video not found: {source}")
    if duration <= 0:
        raise ShortsEditorError("Clip end must be after its start.")

    output.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-vf",
        build_vertical_filter(width, height),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]
    _run(args, timeout=max(300, int(duration * 20)))
    if not output.is_file() or output.stat().st_size < 1024:
        raise ShortsEditorError(f"FFmpeg produced no usable clip at {output}")
    return output


def validate_and_cut(
    source_path: str | Path,
    moments: list[dict],
    output_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    clip_min_seconds: int = 20,
    clip_max_seconds: int = 55,
    max_clips: int = 5,
    source_duration: float = 0,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> list[dict]:
    """Validate moments against the real media and render each clip.

    Returns a list of clip dictionaries.  Individual clip failures are logged
    and skipped so one bad moment cannot abort the whole cycle.
    """
    source = Path(source_path)
    if not source.is_file():
        raise ShortsEditorError(f"Source video not found: {source}")

    duration = float(source_duration or 0)
    if duration <= 0:
        duration = probe_duration(source, ffprobe)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clips: list[dict] = []
    for index, moment in enumerate(moments[: max(1, int(max_clips))], start=1):
        try:
            start = max(0.0, float(moment.get("start", 0)))
            end = min(duration, float(moment.get("end", 0)))
        except (TypeError, ValueError):
            log.warning("Skipping moment with invalid bounds: %r", moment)
            continue
        if end <= start:
            continue
        length = end - start
        if length < max(1, int(clip_min_seconds)) - 0.5:
            log.info("Skipping moment shorter than the configured minimum: %r", moment)
            continue
        if length > int(clip_max_seconds) + 0.5:
            end = start + int(clip_max_seconds)
            length = end - start

        output = out_dir / f"{source.stem}_autoshort_{index:02d}.mp4"
        try:
            cut_vertical_clip(
                source,
                start,
                end,
                output,
                ffmpeg=ffmpeg,
                width=width,
                height=height,
            )
        except ShortsEditorError as exc:
            log.warning("Clip %d could not be rendered: %s", index, exc)
            continue

        clips.append(
            {
                "clip_path": str(output),
                "source_path": str(source),
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(length, 2),
                "start_label": moment.get("start_label") or f"{int(start) // 60:02d}:{int(start) % 60:02d}",
                "end_label": moment.get("end_label") or f"{int(end) // 60:02d}:{int(end) % 60:02d}",
                "title": moment.get("title", ""),
                "reason": moment.get("reason", ""),
                "score": moment.get("score", 0),
                "confidence": moment.get("confidence", "medium"),
            }
        )

    if clips:
        log.info("Rendered %d vertical clip(s) from %s", len(clips), source.name)
    else:
        log.warning("No clips could be rendered from %s", source)
    return clips


def cleanup_clip(path: str | Path) -> None:
    """Best-effort removal used by the Discard action."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.warning("Could not delete clip %s", path)
