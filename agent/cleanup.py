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
import time
from pathlib import Path

log = logging.getLogger(__name__)

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
