"""SQLite job queue. Two processes share it; WAL + busy_timeout make that safe."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    shortcode       TEXT PRIMARY KEY,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',   -- queued|running|retry|done|failed|skipped
    stage           TEXT,                              -- downloading|extracting|writing
    attempts        INTEGER NOT NULL DEFAULT 0,
    error_class     TEXT,
    last_error      TEXT,
    next_attempt_at TEXT,
    note_path       TEXT,
    media_path      TEXT,
    media_pruned    INTEGER NOT NULL DEFAULT 0,
    chat_id         INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_at ON downloads(at);
"""

TERMINAL = ("done", "failed", "skipped")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Queue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # --- jobs -----------------------------------------------------------
    def enqueue(self, shortcode: str, url: str, chat_id: Optional[int]) -> tuple[bool, sqlite3.Row]:
        """Insert a job. Returns (created, row). Re-sharing an existing reel is a no-op."""
        now = iso(utcnow())
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs (shortcode, source_url, chat_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (shortcode, url, chat_id, now, now),
            )
            created = cur.rowcount == 1
        return created, self.get(shortcode)

    def get(self, shortcode: str) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM jobs WHERE shortcode=?", (shortcode,)).fetchone()

    def claim_next(self) -> Optional[sqlite3.Row]:
        """Atomically take the oldest runnable job and mark it running."""
        now = iso(utcnow())
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued'"
                "    OR (status='retry' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))"
                " ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE jobs SET status='running', stage='downloading', attempts=attempts+1,"
                " updated_at=? WHERE shortcode=?",
                (now, row["shortcode"]),
            )
        return self.get(row["shortcode"])

    def set_stage(self, shortcode: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET stage=?, updated_at=? WHERE shortcode=?",
            (stage, iso(utcnow()), shortcode),
        )

    def mark_done(self, shortcode: str, note_path: str, media_path: Optional[str]) -> None:
        now = iso(utcnow())
        self._conn.execute(
            "UPDATE jobs SET status='done', stage=NULL, note_path=?, media_path=?,"
            " error_class=NULL, last_error=NULL, updated_at=?, done_at=? WHERE shortcode=?",
            (note_path, media_path, now, now, shortcode),
        )

    def mark_retry(self, shortcode: str, error_class: str, error: str, delay_s: int) -> None:
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET status='retry', stage=NULL, error_class=?, last_error=?,"
            " next_attempt_at=?, updated_at=? WHERE shortcode=?",
            (error_class, error[:2000], iso(now + timedelta(seconds=delay_s)), iso(now), shortcode),
        )

    def mark_terminal(self, shortcode: str, status: str, error_class: str, error: str) -> None:
        now = iso(utcnow())
        self._conn.execute(
            "UPDATE jobs SET status=?, stage=NULL, error_class=?, last_error=?,"
            " updated_at=?, done_at=? WHERE shortcode=?",
            (status, error_class, error[:2000], now, now, shortcode),
        )

    def requeue(self, shortcode: str) -> bool:
        now = iso(utcnow())
        cur = self._conn.execute(
            "UPDATE jobs SET status='queued', stage=NULL, attempts=0, error_class=NULL,"
            " last_error=NULL, next_attempt_at=NULL, updated_at=? WHERE shortcode=?",
            (now, shortcode),
        )
        return cur.rowcount == 1

    def release_running(self) -> int:
        """Reclaim jobs left 'running' by a crash or restart."""
        cur = self._conn.execute(
            "UPDATE jobs SET status='queued', stage=NULL, updated_at=? WHERE status='running'",
            (iso(utcnow()),),
        )
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def list_by_status(self, statuses: tuple[str, ...], limit: int = 20) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()

    def prunable_media(self, older_than_days: int) -> list[sqlite3.Row]:
        cutoff = iso(utcnow() - timedelta(days=older_than_days))
        return self._conn.execute(
            "SELECT * FROM jobs WHERE media_path IS NOT NULL AND media_pruned=0"
            " AND done_at IS NOT NULL AND done_at < ?",
            (cutoff,),
        ).fetchall()

    def mark_media_pruned(self, shortcode: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET media_pruned=1, updated_at=? WHERE shortcode=?",
            (iso(utcnow()), shortcode),
        )

    # --- state ------------------------------------------------------------
    def get_state(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def is_paused(self) -> bool:
        return self.get_state("paused", "0") == "1"

    def pause(self, reason: str) -> None:
        self.set_state("paused", "1")
        self.set_state("pause_reason", reason[:500])
        self.set_state("paused_at", iso(utcnow()))

    def resume(self) -> None:
        self.set_state("paused", "0")
        self.set_state("pause_reason", "")

    # --- download rate limiting -------------------------------------------
    def record_download(self) -> None:
        self._conn.execute("INSERT INTO downloads (at) VALUES (?)", (iso(utcnow()),))

    def downloads_since(self, hours: int = 24) -> int:
        cutoff = iso(utcnow() - timedelta(hours=hours))
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM downloads WHERE at >= ?", (cutoff,)
        ).fetchone()
        return int(row["n"])

    def seconds_since_last_download(self) -> Optional[float]:
        row = self._conn.execute("SELECT at FROM downloads ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return (utcnow() - datetime.fromisoformat(row["at"])).total_seconds()

    def purge_old_downloads(self, days: int = 7) -> None:
        self._conn.execute(
            "DELETE FROM downloads WHERE at < ?", (iso(utcnow() - timedelta(days=days)),)
        )
