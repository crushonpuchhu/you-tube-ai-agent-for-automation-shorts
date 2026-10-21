"""FFmpeg text-overlay engine: burn a one-line caption onto a Short.

Uses FFmpeg's ``drawtext`` filter (libfreetype) so the text is part of the
pixels — it survives any player, stays visible after YouTube re-encoding, and
needs no sidecar file. The filter chain is built to keep the result readable
on a phone while staying inside YouTube Shorts' safe areas (clear of the top
status bar and the bottom title/hashtag band that the Shorts player paints
over the video).

Design notes:
* Resolution is detected with ``ffprobe`` — font size and wrap width scale to
  the actual frame, so a 1080x1920 clip and a 720x1280 clip both come out
  legible and correctly placed.
* Text is written to a temporary file and referenced via ``textfile=``. This
  sidesteps FFmpeg's filter-graph escaping rules entirely (quotes, colons,
  commas and non-ASCII in a caption would otherwise break the command).
* Audio is passed through untouched (``-c:a copy``) so there is no quality
  loss and no re-sync risk. Video is re-encoded only because a burn-in
  requires it; CRF keeps the file close to the original size.
* Every temporary file is removed on success AND on failure.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import Config

log = logging.getLogger(__name__)


class OverlayError(RuntimeError):
    """Text overlay could not be applied (FFmpeg missing, unreadable video, …)."""


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float | None
    has_audio: bool
    vcodec: str = ""
    acodec: str = ""


#: Candidate TrueType fonts, most preferred first. The Docker image ships
#: DejaVu; a host machine usually has DejaVu or Liberation. Checked in order —
#: the first that exists on disk is used, and its absence is reported clearly.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)


def resolve_font(explicit: str | None = None) -> str:
    """Return a usable TrueType font path, or raise a clear OverlayError."""
    if explicit and Path(explicit).exists():
        return explicit
    env_font = os.getenv("OVERLAY_FONT", "").strip()
    if env_font and Path(env_font).exists():
        return env_font
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise OverlayError(
        "No TrueType font found for the text overlay. Install one "
        "(DejaVu: `sudo apt-get install -y fonts-dejavu-core`) or set "
        "OVERLAY_FONT=/path/to/Font.ttf in .env."
    )


class TextOverlayEngine:
    """Applies a burned-in caption to a vertical Short via FFmpeg drawtext."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ffmpeg = self._which(cfg.ffmpeg_path, "ffmpeg")
        self._ffprobe = self._which(cfg.ffprobe_path, "ffprobe")

    # ------------------------------------------------------------- availability
    @staticmethod
    def _which(configured: str, binary: str) -> str:
        """Resolve an executable: exact path if it exists, else search PATH."""
        if configured and configured != binary and Path(configured).exists():
            return configured
        found = shutil.which(configured) or shutil.which(binary)
        if found:
            return found
        return ""  # checked at call time via available()

    @classmethod
    def available(cls, cfg: Config) -> bool:
        """True when both ffmpeg and ffprobe are runnable."""
        ffmpeg = cls._which(cfg.ffmpeg_path, "ffmpeg")
        ffprobe = cls._which(cfg.ffprobe_path, "ffprobe")
        if not (ffmpeg and ffprobe):
            return False
        try:
            subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=15, check=True)
            subprocess.run([ffprobe, "-version"], capture_output=True, timeout=15, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _require_tools(self) -> None:
        if not self._ffmpeg or not self._ffprobe:
            raise OverlayError(
                "FFmpeg is not installed (or not on PATH). Install it "
                "(`sudo apt-get install -y ffmpeg`) or set FFMPEG_PATH/FFPROBE_PATH "
                "in .env. In Docker it is baked into the image."
            )

    # ------------------------------------------------------------------ probing
    def probe(self, path: Path) -> VideoInfo:
        """Read resolution / duration / audio presence with ffprobe."""
        self._require_tools()
        if not path.exists():
            raise OverlayError(f"Video file not found: {path}")
        cmd = [
            self._ffprobe, "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
            data = json.loads(out.stdout or "{}")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise OverlayError(
                f"FFmpeg could not read this video ({path.name}): {stderr[-300:] or 'unknown error'}"
            ) from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise OverlayError(f"FFmpeg probe failed: {exc}") from exc

        width = height = 0
        duration: float | None = None
        has_audio = False
        vcodec = acodec = ""
        for stream in data.get("streams") or []:
            ctype = stream.get("codec_type")
            if ctype == "video" and not width:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                vcodec = stream.get("codec_name") or ""
            elif ctype == "audio":
                has_audio = True
                acodec = stream.get("codec_name") or ""
        fmt = data.get("format") or {}
        if fmt.get("duration"):
            try:
                duration = float(fmt["duration"])
            except (TypeError, ValueError):
                duration = None
        if not width or not height:
            raise OverlayError(f"No playable video stream found in {path.name}.")
        return VideoInfo(width=width, height=height, duration=duration,
                         has_audio=has_audio, vcodec=vcodec, acodec=acodec)

    # ----------------------------------------------------------------- geometry
    @staticmethod
    def _font_size(width: int, height: int) -> int:
        """Font size that stays legible on a phone without dominating the frame.

        Scales with the SHORTER edge (the width of a 9:16 clip) and is clamped
        so tiny and huge sources both look right.
        """
        base = min(width, height)
        size = max(28, int(base * 0.055))  # ~59px on 1080-wide, ~40px on 720-wide
        return min(size, 96)

    @staticmethod
    def _wrap(text: str, max_chars: int) -> str:
        """Greedy word wrap. Keeps the caption to at most 3 readable lines."""
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_chars or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        # If it still overflows to >3 lines, widen the wrap so we never get a
        # stack of tiny fragments covering the whole frame.
        if len(lines) > 3:
            return TextOverlayEngine._wrap(text, max_chars * 2)
        return "\n".join(lines)

    def build_filter(self, info: VideoInfo, text: str, textfile: str,
                     fontfile: str) -> str:
        """Construct the drawtext filter string for this frame + text.

        Writes the wrapped text into `textfile` (referenced by drawtext's
        ``textfile=`` so no filter-graph escaping is ever needed).
        """
        fontsize = self._font_size(info.width, info.height)
        # Wrap to roughly one line per ~20 characters — tuned for one-line
        # captions that may need 2 short lines on a narrow vertical frame.
        max_chars = max(12, int(info.width / (fontsize * 0.62)))
        wrapped = self._wrap(text, max_chars)
        Path(textfile).write_text(wrapped, encoding="utf-8")

        # Vertical placement: center the block, then lift it slightly so it
        # clears the bottom UI band (title/hashtags/controls ~ bottom 15%).
        y_expr = "(h-text_h)/2 - h*0.12"
        # Contrast: white fill + soft black outline + drop shadow. This stays
        # readable over both bright and dark backgrounds without a heavy box.
        border = max(2, fontsize // 24)
        shadow = max(1, fontsize // 40)
        parts = [
            f"fontfile='{fontfile}'",
            f"textfile='{textfile}'",
            f"fontsize={fontsize}",
            "fontcolor=white",
            f"borderw={border}",
            "bordercolor=black@0.85",
            f"shadowx={shadow}",
            f"shadowy={shadow}",
            "shadowcolor=black@0.6",
            f"line_spacing={max(4, fontsize // 8)}",
            "x=(w-text_w)/2",
            f"y={y_expr}",
            "expansion=none",   # treat % and friends literally
        ]
        return "drawtext=" + ":".join(parts)

    # ----------------------------------------------------------------- apply
    def apply(self, source: Path, text: str,
              position: str = "center") -> Path:
        """Burn `text` onto `source`; return the new file path.

        `position` ("top"|"center"|"bottom") is honoured in the y-expression.
        The original file is left untouched; the caller decides whether to keep
        or delete it. All temporary files are cleaned up either way.
        """
        self._require_tools()
        text = (text or "").strip()
        if not text:
            raise OverlayError("No overlay text given.")
        if len(text) > 140:
            text = text[:137].rstrip() + "…"

        info = self.probe(source)
        font = resolve_font()

        out_path = source.with_name(f"{source.stem}_text{source.suffix}")
        tmp_dir = Path(tempfile.mkdtemp(prefix="overlay_"))
        textfile = tmp_dir / "caption.txt"

        try:
            filt = self.build_filter(info, text, str(textfile), font)
            if position == "top":
                filt = filt.replace("(h-text_h)/2 - h*0.12", "h*0.10")
            elif position == "bottom":
                filt = filt.replace("(h-text_h)/2 - h*0.12", "h*0.70")

            cmd = [
                self._ffmpeg, "-y",
                "-i", str(source),
                "-vf", filt,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
            ]
            if info.has_audio:
                # Copy audio bit-for-bit: no re-encode, no drift.
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-an"]
            cmd += ["-movflags", "+faststart", str(out_path)]

            log.info("Applying text overlay to %s -> %s (%sx%s, %s)",
                     source.name, out_path.name, info.width, info.height, position)
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired as exc:
                raise OverlayError("FFmpeg timed out while rendering the text overlay.") from exc

            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                hint = ""
                low = stderr.lower()
                if "no such filter" in low or "drawtext" in low and "error" in low:
                    hint = (" This FFmpeg build lacks the drawtext/freetype filter — "
                            "install the full FFmpeg (the Docker image already has it).")
                elif "no such file or directory" in low and "font" in low:
                    hint = " The selected font file could not be read."
                raise OverlayError(
                    f"FFmpeg failed (exit {proc.returncode}): {stderr[-400:] or 'no output'}.{hint}"
                )

            if not out_path.exists() or out_path.stat().st_size == 0:
                raise OverlayError("FFmpeg reported success but produced no video.")
            return out_path
        finally:
            # Always clean the temp dir (the caption text file).
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def cleanup_temp(self, path: Path) -> None:
        """Best-effort removal of an intermediate file."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
