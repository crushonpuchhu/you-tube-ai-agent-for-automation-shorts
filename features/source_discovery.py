"""Source discovery helpers for the autonomous workflow.

Two sources are supported:

1. **Owner-provided media** — files placed in ``data/autonomous_sources`` with
   a small ``*.rights.json`` sidecar documenting the rights.  This path never
   downloads anything, so it is safe to enable without extra tooling.
2. **YouTube candidates** — only downloadable when the rights gate passes *and*
   the owner explicitly opted in with ``AUTONOMOUS_ALLOW_DOWNLOAD=true``.
   ``yt-dlp`` is imported lazily, so the agent runs normally when it is absent.

No module here removes watermarks, bypasses DRM/access controls or downloads a
source whose rights are unknown.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.clock import now_ist
from features.source_rights import (
    RightsDecision,
    SourcePolicy,
    attach_rights,
    can_download,
    evaluate_source,
)

log = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SIDECAR_SUFFIXES = (".rights.json", ".json")


class SourceDownloadError(RuntimeError):
    """Raised when a source cannot be downloaded safely."""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sidecar_for(path: Path) -> Path | None:
    for suffix in SIDECAR_SUFFIXES:
        candidate = path.with_name(path.name + suffix)
        if candidate.is_file():
            return candidate
    for suffix in SIDECAR_SUFFIXES:
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "confirmed"}


def discover_owner_media(directory: str | Path | None) -> list[dict]:
    """Scan a directory for owner media with a confirmed rights sidecar."""
    if not directory:
        return []
    root = Path(directory).expanduser()
    if not root.is_dir():
        return []

    found: list[dict] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        sidecar_path = _sidecar_for(path)
        if sidecar_path is None:
            log.info(
                "Owner media %s skipped: no rights sidecar (rights unknown)",
                path.name,
            )
            continue
        sidecar = _read_json(sidecar_path)
        if not _truthy(sidecar.get("confirmed") or sidecar.get("rights_confirmed")):
            log.info(
                "Owner media %s skipped: sidecar does not confirm rights",
                path.name,
            )
            continue

        basis = str(sidecar.get("rights_basis") or "owner_media").strip().lower()
        licence = str(sidecar.get("license") or sidecar.get("licence") or "").strip()
        found.append(
            {
                "source_type": "owner_media",
                "video_id": f"owner:{path.stem}",
                "title": str(sidecar.get("title") or path.stem)[:300],
                "channel": str(
                    sidecar.get("creator") or sidecar.get("channel") or "owner"
                )[:200],
                "channel_id": str(sidecar.get("channel_id") or ""),
                "url": str(sidecar.get("source_url") or sidecar.get("url") or ""),
                "path": str(path),
                "license": licence,
                "rights_confirmed": True,
                "rights_basis": basis,
                "explicit_permission": _truthy(sidecar.get("explicit_permission")),
                "description": str(sidecar.get("description") or "")[:600],
                "topic": str(sidecar.get("topic") or "")[:240],
                "duration_seconds": int(sidecar.get("duration_seconds") or 0),
                "published_at": str(sidecar.get("published_at") or ""),
                "discovered_at": now_ist().isoformat(),
                "score": int(sidecar.get("score") or 100),
                "score_reason": "owner-provided media with confirmed rights",
            }
        )
    return found


def discover_sources(
    topic: str,
    source_policy: SourcePolicy | None = None,
    *,
    candidates: list[dict] | None = None,
    owner_media: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Combine sources and apply the hard rights gate.

    Returns ``(eligible, rejected)``.  ``rejected`` is useful for audit logs and
    for explaining to the owner why a candidate was skipped.
    """
    policy = source_policy or SourcePolicy()
    combined: list[dict] = list(candidates or [])
    combined.extend(owner_media or [])

    # A topic on the sidecar only affects ordering; it never bypasses rights.
    topic_tokens = {t for t in re.split(r"[^a-z0-9]+", (topic or "").lower()) if t}

    def priority(source: dict) -> tuple[int, int]:
        text = f"{source.get('title', '')} {source.get('topic', '')}".lower()
        matches = sum(1 for token in topic_tokens if token and token in text)
        return (int(source.get("score") or 0), matches)

    eligible, rejected = attach_rights(combined, policy)
    eligible.sort(key=priority, reverse=True)
    if rejected:
        log.info(
            "Source-rights gate skipped %d candidate(s) without a verified basis",
            len(rejected),
        )
    return eligible, rejected


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
    return cleaned[:120] or "source"


def download_permitted_source(
    source: dict,
    destination: str | Path,
    policy: SourcePolicy,
    *,
    max_minutes: int = 0,
    cookiefile: str = "",
) -> Path:
    """Return a local path for an eligible source.

    Owner media is used in place (never copied or modified).  A remote source is
    downloaded only when the rights gate passes **and** downloading was
    explicitly enabled by the owner.
    """
    decision: RightsDecision = evaluate_source(source, policy)
    if not decision.allowed or not decision.verified:
        raise SourceDownloadError(
            "Refusing to download: " + decision.notes
        )

    if str(source.get("source_type") or "").lower() == "owner_media":
        path = Path(str(source.get("path") or "")).expanduser()
        if not path.is_file():
            raise SourceDownloadError(f"Owner media file not found: {path}")
        return path

    if not can_download(decision, source, policy):
        raise SourceDownloadError(
            "Refusing to download: AUTONOMOUS_ALLOW_DOWNLOAD is not enabled or "
            "the reuse basis is not verified."
        )

    url = str(source.get("url") or "").strip()
    if not url:
        raise SourceDownloadError("Candidate has no canonical source URL.")

    dest_dir = Path(destination).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        import yt_dlp  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise SourceDownloadError(
            "yt-dlp is not installed. Rebuild the image after adding it to "
            "requirements.txt, or use owner-provided media."
        ) from exc

    max_seconds = max(0, int(max_minutes)) * 60

    def _match_filter(info: dict, *, incomplete: bool = False) -> str | None:
        if max_seconds and int(info.get("duration") or 0) > max_seconds:
            return f"duration exceeds {max_minutes} minutes"
        return None

    options = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "overwrites": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "match_filter": _match_filter,
    }

    # A signed-in cookie jar is the only reliable way to download from a
    # datacenter IP; YouTube answers anonymous requests with
    # "Sign in to confirm you're not a bot".
    if cookiefile:
        cookie_path = Path(str(cookiefile)).expanduser()
        if cookie_path.is_file():
            options["cookiefile"] = str(cookie_path)
        else:
            log.warning("YTDLP_COOKIEFILE configured but missing: %s", cookie_path)

    video_id = safe_filename(str(source.get("video_id") or "source"))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            info = ydl.sanitize_info(info)
    except Exception as exc:  # noqa: BLE001 - upstream/network/format errors
        raise SourceDownloadError(
            f"yt-dlp could not download the permitted source: {str(exc)[:300]}"
        ) from exc

    candidates: list[str] = []
    for item in (info or {}).get("requested_downloads") or []:
        if item.get("filepath"):
            candidates.append(str(item["filepath"]))
    if info and info.get("_filename"):
        candidates.append(str(info["_filename"]))
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    for path in sorted(dest_dir.glob(f"{video_id}.*")):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            return path

    raise SourceDownloadError("Download finished but no media file was produced.")


def copy_owner_media_if_needed(source: dict, destination: Path) -> Path:
    """Copy owner media into a work directory without touching the original.

    Kept as a small helper for callers that prefer a private working copy.
    """
    original = Path(str(source.get("path") or "")).expanduser()
    if not original.is_file():
        raise SourceDownloadError(f"Owner media file not found: {original}")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"owner_{safe_filename(original.stem)}{original.suffix}"
    if not target.exists():
        shutil.copy2(original, target)
    return target


def rights_audit_line(source: dict) -> str:
    decision = evaluate_source(source, SourcePolicy())
    stamp = datetime.now().isoformat(timespec="seconds")
    return (
        f"{stamp} | {source.get('video_id')} | {source.get('channel')} | "
        f"{decision.basis} | verified={decision.verified}"
    )
