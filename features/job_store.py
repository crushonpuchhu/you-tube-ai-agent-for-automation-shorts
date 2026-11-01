"""Durable, feature-specific storage for the autonomous workflow.

The store lives in the **same SQLite file** the rest of the agent already uses
(``config.Config.db_path`` / ``AGENT_DB_PATH``), but only creates its own
``autonomous_*`` tables and its own settings keys.  No existing table,
column or record is altered, renamed or removed.

Why SQLite: the database is already mounted on a persistent host volume in
Docker, so the active niche and the candidate queue survive restarts and
container recreation.  All writes use parameterized SQL and a short
``BEGIN IMMEDIATE`` transaction where a read-modify-write is required.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from agent.clock import now_iso, now_ist, to_iso

log = logging.getLogger(__name__)

#: Maximum length of a niche label, per the specification.
NICHE_MAX_LENGTH = 80

#: Namespaced settings keys (the existing ``settings`` table is not touched).
NICHE_KEY = "autonomous.active_niche"
PAUSED_KEY = "autonomous.paused"
LAST_CYCLE_KEY = "autonomous.last_cycle"

SCHEMA = """
CREATE TABLE IF NOT EXISTS autonomous_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS autonomous_sources (
    video_id         TEXT PRIMARY KEY,
    title            TEXT,
    channel          TEXT,
    url              TEXT,
    duration_seconds INTEGER,
    published_at     TEXT,
    discovered_at    TEXT,
    license          TEXT,
    rights_basis     TEXT,
    rights_verified  INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'discovered',
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    payload_json     TEXT
);

CREATE TABLE IF NOT EXISTS autonomous_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    cycle_id       TEXT,
    video_row_id   INTEGER,
    source_video_id TEXT,
    topic          TEXT,
    niche          TEXT,
    clip_path      TEXT,
    moments_json   TEXT,
    source_json    TEXT,
    rights_json    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending_approval',
    message_chat_id INTEGER,
    message_id     INTEGER,
    published_url  TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_autonomous_candidates_status
    ON autonomous_candidates(status);

CREATE TABLE IF NOT EXISTS autonomous_cycles (
    cycle_id    TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    niche       TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS autonomous_daily (
    day    TEXT NOT NULL,
    metric TEXT NOT NULL,
    value  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, metric)
);

CREATE TABLE IF NOT EXISTS autonomous_locks (
    name        TEXT PRIMARY KEY,
    owner       TEXT,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""

CANDIDATE_STATUSES = (
    "pending_approval",
    "approved",
    "scheduled",
    "published",
    "discarded",
    "error",
)


class NicheValidationError(ValueError):
    """Raised when a requested niche is empty, too long or not plain text."""


class NicheStorageError(RuntimeError):
    """Raised when the niche could not be durably written/read back."""


@dataclass(frozen=True)
class NicheState:
    value: str
    source: str  # "persisted" | "default"
    updated_at: str = ""
    updated_by: str = ""

    @property
    def is_persisted(self) -> bool:
        return self.source == "persisted"


def validate_niche(value: str) -> str:
    """Normalise and validate a niche label.

    The value is treated strictly as data: whitespace is collapsed, control
    characters are rejected, length is bounded, and it is never interpreted as
    a command, SQL fragment or filesystem path by any caller.
    """
    if not isinstance(value, str):
        raise NicheValidationError("The niche must be plain text.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise NicheValidationError("The niche cannot be empty.")
    if len(cleaned) > NICHE_MAX_LENGTH:
        raise NicheValidationError(
            f"The niche is too long (max {NICHE_MAX_LENGTH} characters)."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise NicheValidationError("The niche cannot contain control characters.")
    return cleaned


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class JobStore:
    """Thread-safe feature store on the existing SQLite database file."""

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
            except sqlite3.Error:  # e.g. a filesystem without WAL support
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
            conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM autonomous_settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO autonomous_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now_iso()),
            )

    def delete_setting(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM autonomous_settings WHERE key=?", (key,))

    # ------------------------------------------------------------------- niche
    def get_active_niche(self, bootstrap: str) -> NicheState:
        """Return the persisted niche, or the bootstrap default if none exists."""
        raw = self.get_setting(NICHE_KEY)
        if raw:
            data = _loads(raw)
            if isinstance(data, dict):
                value = " ".join(str(data.get("niche") or "").split())
                if value:
                    return NicheState(
                        value=value,
                        source="persisted",
                        updated_at=str(data.get("updated_at") or ""),
                        updated_by=str(data.get("updated_by") or ""),
                    )
            elif raw.strip():
                # Tolerate a plain-text value written by an earlier build.
                return NicheState(value=raw.strip(), source="persisted")
        fallback = " ".join((bootstrap or "").split()) or "fitness"
        return NicheState(value=fallback, source="default")

    def set_active_niche(self, value: str, updated_by: str = "") -> NicheState:
        """Validate and atomically persist the active niche.

        Success is only reported after the value is read back from the
        database.  On any failure the previous value remains in place and an
        exception is raised (the caller must not acknowledge success).
        """
        cleaned = validate_niche(value)
        stamp = now_iso()
        payload = _dumps(
            {
                "niche": cleaned,
                "updated_at": stamp,
                "updated_by": str(updated_by or "")[:120],
                "schema": 1,
            }
        )
        # The write and its read-back verification happen in ONE transaction:
        # if the read-back does not match, the transaction rolls back and the
        # previous niche stays in effect.
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO autonomous_settings(key,value,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (NICHE_KEY, payload, stamp),
                )
                row = conn.execute(
                    "SELECT value FROM autonomous_settings WHERE key=?",
                    (NICHE_KEY,),
                ).fetchone()
                data = _loads(row["value"] if row else None)
                if not isinstance(data, dict) or data.get("niche") != cleaned:
                    raise NicheStorageError(
                        "The niche was written but could not be verified. "
                        "The previous niche is still in effect."
                    )
        except NicheStorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - storage must never lie
            raise NicheStorageError(
                "Could not save the niche to the durable database. "
                "The previous niche is unchanged."
            ) from exc

        log.info("Active niche persisted: %r (by %s)", cleaned, updated_by or "unknown")
        return NicheState(
            value=cleaned,
            source="persisted",
            updated_at=str(data.get("updated_at") or ""),
            updated_by=str(data.get("updated_by") or ""),
        )

    # ------------------------------------------------------------------ pause
    def is_paused(self) -> bool:
        raw = self.get_setting(PAUSED_KEY)
        data = _loads(raw, {})
        if isinstance(data, dict):
            return bool(data.get("paused"))
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def set_paused(self, paused: bool, updated_by: str = "") -> None:
        self.set_setting(
            PAUSED_KEY,
            _dumps(
                {
                    "paused": bool(paused),
                    "updated_at": now_iso(),
                    "updated_by": str(updated_by or "")[:120],
                }
            ),
        )

    # ------------------------------------------------------------------ quotas
    def claim_daily(self, day: str, metric: str, limit: int, amount: int = 1) -> bool:
        """Atomically reserve ``amount`` of a daily metric under ``limit``.

        A negative limit means "unlimited"; ``0`` means the metric is disabled.
        """
        amount = max(1, int(amount))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT value FROM autonomous_daily WHERE day=? AND metric=?",
                    (day, metric),
                ).fetchone()
                current = int(row["value"]) if row else 0
                if limit >= 0 and current + amount > limit:
                    conn.rollback()
                    return False
                conn.execute(
                    "INSERT INTO autonomous_daily(day,metric,value) VALUES(?,?,?) "
                    "ON CONFLICT(day,metric) DO UPDATE SET value=?",
                    (day, metric, amount, current + amount),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def daily_count(self, day: str, metric: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM autonomous_daily WHERE day=? AND metric=?",
                (day, metric),
            ).fetchone()
        return int(row["value"]) if row else 0

    def daily_usage(self, day: str | None = None) -> dict[str, int]:
        day = day or now_ist().strftime("%Y-%m-%d")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT metric, value FROM autonomous_daily WHERE day=?", (day,)
            ).fetchall()
        return {row["metric"]: int(row["value"]) for row in rows}

    # ------------------------------------------------------------------- locks
    def try_acquire_lock(self, name: str, owner: str, ttl_seconds: int = 3600) -> bool:
        """Acquire a named single-flight lock (stale locks expire)."""
        acquired = now_iso()
        expires = to_iso(now_ist() + timedelta(seconds=max(1, int(ttl_seconds))))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM autonomous_locks WHERE name=? AND expires_at<=?",
                    (name, acquired),
                )
                try:
                    conn.execute(
                        "INSERT INTO autonomous_locks(name,owner,acquired_at,expires_at) "
                        "VALUES(?,?,?,?)",
                        (name, owner, acquired, expires),
                    )
                except sqlite3.IntegrityError:
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def release_lock(self, name: str, owner: str | None = None) -> None:
        with self._conn() as conn:
            if owner is None:
                conn.execute("DELETE FROM autonomous_locks WHERE name=?", (name,))
            else:
                conn.execute(
                    "DELETE FROM autonomous_locks WHERE name=? AND owner=?",
                    (name, owner),
                )

    # ----------------------------------------------------------------- sources
    def source_seen(self, video_id: str) -> bool:
        if not video_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM autonomous_sources WHERE video_id=?", (video_id,)
            ).fetchone()
        return row is not None

    def remember_source(self, source: dict, status: str = "discovered") -> None:
        video_id = str(source.get("video_id") or "")
        if not video_id:
            return
        stamp = now_iso()
        rights = source.get("rights") or {}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO autonomous_sources(video_id,title,channel,url,"
                "duration_seconds,published_at,discovered_at,license,rights_basis,"
                "rights_verified,status,first_seen,last_seen,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET "
                "title=excluded.title, channel=excluded.channel, url=excluded.url, "
                "duration_seconds=excluded.duration_seconds, "
                "published_at=excluded.published_at, license=excluded.license, "
                "rights_basis=excluded.rights_basis, "
                "rights_verified=excluded.rights_verified, status=excluded.status, "
                "last_seen=excluded.last_seen, payload_json=excluded.payload_json",
                (
                    video_id,
                    source.get("title"),
                    source.get("channel"),
                    source.get("url"),
                    int(source.get("duration_seconds") or 0) or None,
                    source.get("published_at"),
                    source.get("discovered_at") or stamp,
                    source.get("license"),
                    rights.get("basis"),
                    1 if rights.get("verified") else 0,
                    status,
                    stamp,
                    stamp,
                    _dumps(source),
                ),
            )

    def mark_source_status(self, video_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE autonomous_sources SET status=?, last_seen=? WHERE video_id=?",
                (status, now_iso(), video_id),
            )

    # -------------------------------------------------------------- candidates
    def create_candidate(
        self,
        *,
        cycle_id: str,
        video_row_id: int | None,
        source: dict,
        rights: dict,
        topic: str,
        niche: str,
        clip_path: str,
        moments: list[dict],
        status: str = "pending_approval",
    ) -> int:
        stamp = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO autonomous_candidates(created_at,updated_at,cycle_id,"
                "video_row_id,source_video_id,topic,niche,clip_path,moments_json,"
                "source_json,rights_json,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    stamp,
                    stamp,
                    cycle_id,
                    video_row_id,
                    source.get("video_id"),
                    topic,
                    niche,
                    clip_path,
                    _dumps(moments),
                    _dumps(source),
                    _dumps(rights),
                    status,
                ),
            )
            return int(cur.lastrowid)

    def update_candidate(self, candidate_id: int, **fields: Any) -> None:
        allowed = {
            "video_row_id", "status", "message_chat_id", "message_id",
            "published_url", "error", "clip_path", "moments_json",
            "source_json", "rights_json", "topic", "niche",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        for key in ("moments_json", "source_json", "rights_json"):
            if key in updates and not isinstance(updates[key], str):
                updates[key] = _dumps(updates[key])
        updates["updated_at"] = now_iso()
        keys = ", ".join(f"{key}=?" for key in updates)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE autonomous_candidates SET {keys} WHERE id=?",
                (*updates.values(), candidate_id),
            )

    def get_candidate(self, candidate_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM autonomous_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        return self._row_to_candidate(row)

    def list_candidates(
        self, status: str | tuple[str, ...] | None = None, limit: int = 50
    ) -> list[dict]:
        sql = "SELECT * FROM autonomous_candidates"
        params: list[Any] = []
        if status:
            if isinstance(status, (tuple, list)):
                sql += f" WHERE status IN ({', '.join('?' for _ in status)})"
                params.extend(status)
            else:
                sql += " WHERE status=?"
                params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_candidate(row) for row in rows if row is not None]

    def count_candidates(self, status: str | tuple[str, ...]) -> int:
        if isinstance(status, (tuple, list)):
            clause = f"status IN ({', '.join('?' for _ in status)})"
            params: list[Any] = list(status)
        else:
            clause = "status=?"
            params = [status]
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM autonomous_candidates WHERE {clause}",
                tuple(params),
            ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["source"] = _loads(data.pop("source_json", None), {})
        data["rights"] = _loads(data.pop("rights_json", None), {})
        data["moments"] = _loads(data.pop("moments_json", None), [])
        return data

    # ------------------------------------------------------------------ cycles
    def start_cycle(self, niche: str) -> str:
        cycle_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO autonomous_cycles(cycle_id,started_at,niche,status) "
                "VALUES(?,?,?,?)",
                (cycle_id, now_iso(), niche, "running"),
            )
        self.set_setting(LAST_CYCLE_KEY, _dumps({"cycle_id": cycle_id, "started_at": now_iso()}))
        return cycle_id

    def finish_cycle(self, cycle_id: str, status: str, summary: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE autonomous_cycles SET finished_at=?, status=?, summary_json=? "
                "WHERE cycle_id=?",
                (now_iso(), status, _dumps(summary or {}), cycle_id),
            )

    def last_cycle(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM autonomous_cycles ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["summary"] = _loads(data.pop("summary_json", None), {})
        return data
