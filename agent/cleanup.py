"""Disk hygiene for downloaded Shorts.

Telegram downloads land in `data/videos/`. Nothing else in the pipeline ever
removes them, so without this module the folder grows without bound.

Two complementary rules keep the disk small without ever breaking a publish:

1. Delete a video's file as soon as YouTube has confirmed the upload. The file
   has no further purpose — the record in SQLite keeps the title, URL and
   statistics, so re-publishing or reporting never needs the bytes again.
2. Sweep the folder for orphans: files older than `retention_days` whose video
   record is finished (published or permanently failed), plus any file that no
   longer belongs to any record at all (leftovers from a crash mid-download).

Files still waiting to be published are never touched — a scheduled Short must
survive a restart, so it is only removed once its status says the job is done.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

# NOTE: this module must not import agent.db. agent.db imports `delete_file`
# from here (Database.delete_video frees the bytes it points at), so importing
# it back would create an import cycle. Pass the database in as an argument.

# Statuses that mean "this record will never read its file again".
FINISHED = ("published", "failed")

# A file with no DB record can never be published. Give a just-finished
# download a short grace period, then reclaim the space.
ORPHAN_GRACE_SECONDS = 3600


def delete_file(path: str | Path) -> bool:
    """Remove a single file, never raising. Returns True when it is gone."""
    p = Path(path)
    try:
        if p.is_file():
            p.unlink()
            log.info("Deleted video file %s (%.1f MB)",
                     p.name, p.stat().st_size if p.exists() else 0)
        return True
    except OSError:
        # Windows/AV/FUSE can hold the file briefly — retried by the next sweep.
        log.warning("Could not delete %s (will retry on the next sweep)", p)
        return False


def discard_after_upload(db, video_row_id: int) -> None:
    """Drop the local file for a video YouTube has already confirmed."""
    row = db.get_video(video_row_id)
    if not row:
        return
    path = row.get("file_path")
    if path and delete_file(path):
        # Clear the pointer so nothing downstream assumes the bytes are there.
        db.update_video(video_row_id, file_path="")


def sweep(db, video_dir: str | Path, retention_days: int = 3) -> dict:
    """Remove finished/orphaned files. Safe to call on a timer. Returns counts."""
    video_dir = Path(video_dir)
    stats = {"deleted": 0, "bytes": 0, "kept": 0}
    if not video_dir.is_dir():
        return stats

    rows = db.list_videos(limit=5000)
    known: dict[str, dict] = {}
    for row in rows:
        fp = row.get("file_path")
        if fp:
            known[Path(fp).name] = row

    cutoff = time.time() - retention_days * 86400

    for item in video_dir.iterdir():
        if not item.is_file():
            continue
        row = known.get(item.name)
        try:
            age = item.stat().st_mtime
        except OSError:
            continue

        if row is None:
            # Orphan: no record references it (crash mid-download, or a DB
            # reset). It can never be published, so only a short grace period
            # is needed to avoid racing a download that just finished.
            reason = "orphan"
            removable = age < time.time() - ORPHAN_GRACE_SECONDS
        elif not row.get("file_path"):
            # Record already published and cleared its pointer.
            reason = "already-published"
            removable = True
        elif row.get("status") == "published":
            # Upload confirmed -> the local bytes have no future purpose.
            reason = "published"
            removable = True
        elif row.get("status") in FINISHED and age < cutoff:
            reason = row.get("status")
            removable = True
        else:
            # Pending/scheduled/retrying — must stay for the publish.
            stats["kept"] += 1
            continue

        if removable:
            try:
                size = item.stat().st_size
            except OSError:
                size = 0
            if delete_file(item):
                stats["deleted"] += 1
                stats["bytes"] += size
                log.info("Cleanup removed %s (%s, %.1f MB)",
                         item.name, reason, size / 1e6)

    if stats["deleted"]:
        log.info("Video cleanup: removed %d file(s), %.1f MB freed, %d kept",
                 stats["deleted"], stats["bytes"] / 1e6, stats["kept"])
    return stats


# --------------------------------------------------------------- downloads
# yt-dlp writes permitted source downloads into
# `data/autonomous_sources/downloads/` (`<id>.fNNN.<ext>`, `<id>.temp.mp4`, …).
# The pipeline deletes them in a `finally` block once a cycle finishes, but a
# hard kill (container restart, OOM, or the disk filling up mid-download) skips
# that block and leaves the bytes behind forever. Nothing else ever swept that
# folder, so leftovers accumulated until the volume hit 100% and SQLite started
# failing every write with "disk I/O error". This sweep closes that leak.

def autonomous_downloads_dir() -> Path:
    """Resolve the yt-dlp download folder without importing the feature module.

    Mirrors ``AutonomousConfig.load()``: ``AUTONOMOUS_SOURCE_DIR`` when set
    (relative paths resolve against the project root), otherwise
    ``<data>/autonomous_sources``; downloads always live in ``downloads/``.
    """
    from config import DATA_DIR, ROOT

    raw = os.getenv("AUTONOMOUS_SOURCE_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser()
        if not base.is_absolute():
            base = ROOT / base
    else:
        base = Path(DATA_DIR) / "autonomous_sources"
    return base / "downloads"


def sweep_downloads(download_dir: str | Path | None = None,
                    grace_seconds: int = ORPHAN_GRACE_SECONDS) -> dict:
    """Remove leftover/partial source downloads. Safe to call on a timer.

    Every file in this folder is temporary by design (the pipeline deletes its
    own download after processing), so anything older than ``grace_seconds`` is
    reclaimed. A short grace period avoids racing a download another thread
    just finished but has not yet consumed.
    """
    directory = Path(download_dir) if download_dir is not None else autonomous_downloads_dir()
    stats = {"deleted": 0, "bytes": 0, "kept": 0}
    if not directory.is_dir():
        return stats

    cutoff = time.time() - max(0, int(grace_seconds))
    for item in directory.iterdir():
        if not item.is_file():
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        if stat.st_mtime > cutoff:
            stats["kept"] += 1
            continue
        if delete_file(item):
            stats["deleted"] += 1
            stats["bytes"] += stat.st_size

    if stats["deleted"]:
        log.info("Download cleanup: removed %d leftover source file(s), %.1f MB freed",
                 stats["deleted"], stats["bytes"] / 1e6)
    return stats


def free_bytes(path: str | Path) -> int:
    """Free bytes on the filesystem holding ``path``; -1 when unreadable."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return -1
