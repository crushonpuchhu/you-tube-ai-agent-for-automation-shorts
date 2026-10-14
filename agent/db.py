"""Persistent local database (SQLite file). No DB server required.

The file lives outside the project folder by default because SQLite locking is
unreliable on NTFS/exFAT/FUSE mounts such as /media/... — put AGENT_DB_PATH in
.env if you want it somewhere else.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agent.clock import now_iso
from agent.cleanup import delete_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    chat_id      INTEGER,
    tg_user_id   INTEGER,
    file_path    TEXT NOT NULL,
    tg_file_id   TEXT,
    hint         TEXT,
    status       TEXT NOT NULL DEFAULT 'received',
    brief_json   TEXT,
    intent_json  TEXT,
    privacy      TEXT,
    youtube_id   TEXT,
    url          TEXT,
    published_at TEXT,
    verified_at  TEXT,
    daily_key    TEXT,
    scheduled_for TEXT,
    error        TEXT,
    failed_stage TEXT,
    retry_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_daily  ON videos(daily_key);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    payload_json TEXT NOT NULL DEFAULT '{}',
    last_error   TEXT,
    dedupe_key   TEXT UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    locked_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, run_at);

CREATE TABLE IF NOT EXISTS daily_goals (
    daily_key         TEXT PRIMARY KEY,
    target            INTEGER NOT NULL DEFAULT 2,
    achieved_notified INTEGER NOT NULL DEFAULT 0,
    reminder_sent     TEXT NOT NULL DEFAULT '',
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    video_id        TEXT NOT NULL,
    day             TEXT NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0,
    likes           INTEGER NOT NULL DEFAULT 0,
    comments        INTEGER NOT NULL DEFAULT 0,
    shares          INTEGER NOT NULL DEFAULT 0,
    subs_gained     INTEGER NOT NULL DEFAULT 0,
    avg_view_duration REAL,
    retention_pct   REAL,
    swiped_pct      REAL,
    fetched_at      TEXT,
    PRIMARY KEY (video_id, day)
);

CREATE TABLE IF NOT EXISTS topic_research (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    niche      TEXT,
    payload_json TEXT NOT NULL,
    evidence   TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    dedupe_key TEXT PRIMARY KEY,
    sent_at    TEXT NOT NULL
);
"""

# ---------------------------------------------------------------- statuses
# Video lifecycle. Every stage transition goes through Database.set_status, so a
# row always says exactly how far a video got.
#
#   received -> downloading -> processing -> analyzed --+--> uploading -> published
#                    |            |            |        |
#                    |            |            |        +-> scheduled (queued for later)
#                    +------------+------------+---------> failed (failed_stage says where)
#                                 \------------+---------> discarded (you tapped Discard)
#
# `published` is the terminal success state — this is the "uploaded" state from
# the spec, deliberately kept named `published` so the daily goal, weekly report
# and analytics that already read it keep working unchanged.
STATUS_RECEIVED = "received"        # row created; bytes not on disk yet
STATUS_DOWNLOADING = "downloading"  # pulling the file from Telegram
STATUS_PROCESSING = "processing"    # Gemini writing title/description/tags
STATUS_ANALYZED = "analyzed"        # metadata ready, waiting for your review
STATUS_SCHEDULED = "scheduled"      # queued for a future publish time
STATUS_UPLOADING = "uploading"      # talking to the YouTube API
STATUS_PUBLISHED = "published"      # YouTube confirmed a video id
STATUS_FAILED = "failed"            # see failed_stage + error
STATUS_DISCARDED = "discarded"      # you tapped Discard

#: Every value the `status` column may hold (used by /status counts).
ALL_STATUSES = (
    STATUS_RECEIVED, STATUS_DOWNLOADING, STATUS_PROCESSING, STATUS_ANALYZED,
    STATUS_SCHEDULED, STATUS_UPLOADING, STATUS_PUBLISHED, STATUS_FAILED,
    STATUS_DISCARDED,
)

#: In-flight / queued states: never deleted, because the pipeline still wants
#: the file (or is actively using it right now).
IN_FLIGHT = (
    STATUS_RECEIVED, STATUS_DOWNLOADING, STATUS_PROCESSING,
    STATUS_SCHEDULED, STATUS_UPLOADING,
)

#: Terminal state whose row must be preserved: the weekly report, analytics and
#: smart-scheduling all read `published` rows for statistics.
PROTECTED = IN_FLIGHT + (STATUS_PUBLISHED,)

# The three stages the pipeline is split into. Stored in `failed_stage` so a
# retry knows which one to re-run instead of starting from scratch.
STAGE_DOWNLOAD = "download"   # Telegram -> disk
STAGE_ANALYZE = "analyze"     # disk -> Gemini metadata
STAGE_UPLOAD = "upload"       # disk -> YouTube

STAGES = (STAGE_DOWNLOAD, STAGE_ANALYZE, STAGE_UPLOAD)

#: Attempts allowed per stage before auto-retry is withdrawn (spec: 3).
MAX_RETRIES = 3


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


class Database:
    """Thread-safe wrapper: one short-lived connection per operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    # ---------------------------------------------------------------- plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=30000",
            "PRAGMA synchronous=NORMAL",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.Error:  # pragma unsupported on this filesystem
                pass
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring a database created by an older build up to the current schema.

        SQLite cannot add a column with a constraint inline, and the project has
        no migration framework, so each addition is checked against
        PRAGMA table_info — running this twice is a no-op.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
        for name, ddl in (
            ("failed_stage", "TEXT"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {ddl}")

        # Older builds called the intake state 'submitted'.
        conn.execute(
            "UPDATE videos SET status=? WHERE status=?",
            (STATUS_RECEIVED, "submitted"),
        )
        # Backfill the stage for rows that already failed, so /pending and the
        # retry path know where they stopped: a row with no brief never got past
        # the analysis stage; anything else died at the upload.
        conn.execute(
            "UPDATE videos SET failed_stage=? WHERE status=? AND failed_stage IS NULL "
            "AND (brief_json IS NULL OR brief_json='' OR brief_json='{}')",
            (STAGE_ANALYZE, STATUS_FAILED),
        )
        conn.execute(
            "UPDATE videos SET failed_stage=? WHERE status=? AND failed_stage IS NULL",
            (STAGE_UPLOAD, STATUS_FAILED),
        )

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ----------------------------------------------------------- notifications
    def claim_notification(self, dedupe_key: str) -> bool:
        """True the first time a given notification key is used, False afterwards."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notifications(dedupe_key, sent_at) VALUES(?,?)",
                (dedupe_key, now_iso()),
            )
        return cur.rowcount == 1

    # ----------------------------------------------------------------- videos
    def insert_video(
        self,
        file_path: str,
        chat_id: int | None = None,
        tg_user_id: int | None = None,
        tg_file_id: str | None = None,
        hint: str | None = None,
        privacy: str | None = None,
        status: str = STATUS_RECEIVED,
        intent: dict | None = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO videos(created_at,chat_id,tg_user_id,file_path,tg_file_id,hint,"
                "status,intent_json,privacy) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    now_iso(), chat_id, tg_user_id, file_path, tg_file_id, hint,
                    status, _dumps(intent) if intent else None, privacy,
                ),
            )
            return int(cur.lastrowid)

    def update_video(self, video_id: int, **fields: Any) -> None:
        if not fields:
            return
        keys = []
        values = []
        for key, value in fields.items():
            if key.endswith("_json") and not isinstance(value, str):
                value = _dumps(value)
            keys.append(f"{key}=?")
            values.append(value)
        values.append(video_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE videos SET {', '.join(keys)} WHERE id=?", tuple(values))

    # ------------------------------------------------------- stage transitions
    def set_status(self, video_id: int, status: str, **fields: Any) -> None:
        """Move a video to the next pipeline stage in one atomic write.

        This is the only sanctioned way to change `status`, so the DB never ends
        up with a status set by one place and error/retry fields set by another.
        """
        self.update_video(video_id, status=status, **fields)

    def mark_failed(self, video_id: int, stage: str, error: str,
                    **extra: Any) -> int:
        """Record a stage failure: status=failed, error_message, retry_count++.

        The local file is deliberately left alone — a retry must be able to use
        it without re-fetching from Telegram. Returns the new retry_count so the
        caller can withdraw auto-retry once it passes MAX_RETRIES.
        """
        row = self.get_video(video_id)
        if not row:
            return 0
        attempts = int(row.get("retry_count") or 0) + 1
        self.update_video(
            video_id,
            status=STATUS_FAILED,
            failed_stage=stage,
            error=str(error),
            retry_count=attempts,
            **extra,
        )
        return attempts

    def clear_failure(self, video_id: int) -> None:
        """Forget a stage's failure bookkeeping once that stage succeeds.

        retry_count is per-stage: it counts attempts since the last success, so a
        video that got past analysis starts from 0 again when it later hits an
        upload error and still gets the full budget of attempts.
        """
        self.update_video(video_id, error=None, failed_stage=None, retry_count=0)

    def retries_left(self, video_id: int) -> int:
        """Attempts remaining before auto-retry is withdrawn (never negative)."""
        row = self.get_video(video_id)
        if not row:
            return 0
        return max(0, MAX_RETRIES - int(row.get("retry_count") or 0))

    def get_video(self, video_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return self._row_to_video(row)

    def delete_video(self, video_id: int, keep_record: bool = False,
                     force: bool = False) -> bool:
        """Free a video's disk space, and optionally forget the record too.

        The local file is always removed — reclaiming the storage is the whole
        point. `keep_record` only decides whether the SQLite row survives as
        history (`keep_record=True` keeps the row, clears `file_path` and marks
        it discarded).

        Protected rows are refused (returns False) unless `force=True`:
        a video that is downloading, processing, scheduled or uploading must
        survive a `/cleanup` typed while it works and a restart, and a
        `published` row is the source the weekly report and analytics read.

        Returns True when something was actually removed.
        """
        row = self.get_video(video_id)
        if not row:
            return False
        if not force and row.get("status") in PROTECTED:
            return False

        path = row.get("file_path")
        if path:
            delete_file(path)

        if keep_record:
            self.update_video(video_id, file_path="", status=STATUS_DISCARDED)
        else:
            with self._conn() as conn:
                conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        return True

    def delete_videos_by_status(self, statuses: tuple[str, ...],
                                force: bool = False) -> list[int]:
        """Delete every video in the given statuses. Returns the removed ids."""
        removed: list[int] = []
        for row in self.list_videos(status=statuses, limit=5000):
            if self.delete_video(row["id"], force=force):
                removed.append(row["id"])
        return removed

    def find_video(self, **filters: Any) -> dict | None:
        where = " AND ".join(f"{k}=?" for k in filters)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT * FROM videos WHERE {where} ORDER BY id DESC LIMIT 1",
                tuple(filters.values()),
            ).fetchone()
        return self._row_to_video(row)

    def list_videos(
        self,
        status: str | tuple[str, ...] | None = None,
        daily_key: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        sql = "SELECT * FROM videos WHERE 1=1"
        params: list[Any] = []
        if status:
            if isinstance(status, (tuple, list)):
                sql += f" AND status IN ({', '.join('?' for _ in status)})"
                params.extend(status)
            else:
                sql += " AND status=?"
                params.append(status)
        if daily_key:
            sql += " AND daily_key=?"
            params.append(daily_key)
        if since:
            sql += " AND published_at>=?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_video(r) for r in rows]

    def count_videos(self, status: str | tuple[str, ...], daily_key: str | None = None) -> int:
        if isinstance(status, (tuple, list)):
            clause = f"status IN ({', '.join('?' for _ in status)})"
            params: list[Any] = list(status)
        else:
            clause = "status=?"
            params = [status]
        sql = f"SELECT COUNT(*) AS n FROM videos WHERE {clause}"
        if daily_key:
            sql += " AND daily_key=?"
            params.append(daily_key)
        with self._conn() as conn:
            return int(conn.execute(sql, tuple(params)).fetchone()["n"])

    @staticmethod
    def _row_to_video(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["brief"] = _loads(data.pop("brief_json", None), {})
        data["intent"] = _loads(data.pop("intent_json", None), {})
        return data

    # ------------------------------------------------------------------ tasks
    def add_task(
        self,
        kind: str,
        run_at_iso: str,
        payload: dict | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> int | None:
        """Insert a task; returns None when the dedupe_key already exists."""
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO tasks(kind,run_at,payload_json,dedupe_key,max_attempts,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (kind, run_at_iso, _dumps(payload or {}), dedupe_key,
                     max_attempts, now_iso(), now_iso()),
                )
            except sqlite3.IntegrityError:
                return None
            return int(cur.lastrowid)

    def claim_due_tasks(self, now_iso_val: str, limit: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='pending' AND run_at<=? "
                "ORDER BY run_at LIMIT ?",
                (now_iso_val, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                cur = conn.execute(
                    "UPDATE tasks SET status='running', locked_at=?, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (now_iso_val, now_iso_val, row["id"]),
                )
                if cur.rowcount:
                    task = dict(row)
                    task["payload"] = _loads(task.pop("payload_json"), {})
                    claimed.append(task)
        return claimed

    def complete_task(self, task_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='done', updated_at=?, locked_at=NULL WHERE id=?",
                (now_iso(), task_id),
            )

    def retry_task(self, task_id: int, error: str, next_run_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='pending', attempts=attempts+1, last_error=?, "
                "run_at=?, updated_at=?, locked_at=NULL WHERE id=?",
                (error, next_run_at, now_iso(), task_id),
            )

    def fail_task(self, task_id: int, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', attempts=attempts+1, last_error=?, "
                "updated_at=?, locked_at=NULL WHERE id=?",
                (error, now_iso(), task_id),
            )

    def cancel_task(self, task_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now_iso(), task_id),
            )
        return cur.rowcount > 0

    def recover_stale_tasks(self, cutoff_iso: str) -> int:
        """Re-queue tasks left 'running' by a crash/restart (crash recovery)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending', locked_at=NULL, updated_at=? "
                "WHERE status='running' AND (locked_at IS NULL OR locked_at<?)",
                (now_iso(), cutoff_iso),
            )
        return cur.rowcount

    def release_running_tasks(self) -> int:
        """Graceful shutdown: put in-flight tasks back on the queue."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending', locked_at=NULL, updated_at=? "
                "WHERE status='running'",
                (now_iso(),),
            )
        return cur.rowcount

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM tasks"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY run_at LIMIT ?"
        params += (limit,)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            task = dict(row)
            task["payload"] = _loads(task.pop("payload_json"), {})
            out.append(task)
        return out

    def claim_publish(self, video_id: int) -> bool:
        """Atomic guard so the same video can never be uploaded twice."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE videos SET status='uploading', error=NULL WHERE id=? "
                "AND status IN ('analyzed','scheduled','failed')",
                (video_id,),
            )
        return cur.rowcount > 0

    def task_is_active(self, dedupe_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE dedupe_key=? AND status IN ('pending','running')",
                (dedupe_key,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------ daily goals
    def get_goal(self, daily_key: str, target: int) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_goals WHERE daily_key=?", (daily_key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO daily_goals(daily_key,target,updated_at) VALUES(?,?,?)",
                    (daily_key, target, now_iso()),
                )
                row = conn.execute(
                    "SELECT * FROM daily_goals WHERE daily_key=?", (daily_key,)
                ).fetchone()
        return dict(row)

    def update_goal(self, daily_key: str, **fields: Any) -> None:
        if not fields:
            return
        keys = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE daily_goals SET {keys}, updated_at=? WHERE daily_key=?",
                (*fields.values(), now_iso(), daily_key),
            )

    def goal_history(self, limit: int = 30) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_goals ORDER BY daily_key DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ stats
    def upsert_stats(self, video_id: str, day: str, **metrics: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO stats(video_id,day,views,likes,comments,shares,subs_gained,"
                "avg_view_duration,retention_pct,swiped_pct,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id,day) DO UPDATE SET "
                "views=excluded.views, likes=excluded.likes, comments=excluded.comments, "
                "shares=excluded.shares, subs_gained=excluded.subs_gained, "
                "avg_view_duration=excluded.avg_view_duration, "
                "retention_pct=excluded.retention_pct, swiped_pct=excluded.swiped_pct, "
                "fetched_at=excluded.fetched_at",
                (
                    video_id, day,
                    int(metrics.get("views") or 0),
                    int(metrics.get("likes") or 0),
                    int(metrics.get("comments") or 0),
                    int(metrics.get("shares") or 0),
                    int(metrics.get("subs_gained") or 0),
                    metrics.get("avg_view_duration"),
                    metrics.get("retention_pct"),
                    metrics.get("swiped_pct"),
                    now_iso(),
                ),
            )

    def latest_stats(self) -> dict[str, dict]:
        """Most recent snapshot per video (YouTube stats are cumulative)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.* FROM stats s "
                "JOIN (SELECT video_id, MAX(day) AS day FROM stats GROUP BY video_id) m "
                "ON m.video_id=s.video_id AND m.day=s.day"
            ).fetchall()
        return {r["video_id"]: dict(r) for r in rows}

    # --------------------------------------------------------------- research
    def save_topics(self, niche: str, topics: list[dict], evidence: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO topic_research(created_at,niche,payload_json,evidence) "
                "VALUES(?,?,?,?)",
                (now_iso(), niche, _dumps(topics), evidence),
            )
            return int(cur.lastrowid)

    def recent_topics(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM topic_research ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        topics: list[dict] = []
        for row in rows:
            for topic in _loads(row["payload_json"], []):
                topic["_researched_at"] = row["created_at"]
                topics.append(topic)
        return topics[:limit]
