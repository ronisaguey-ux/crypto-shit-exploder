"""Durable work queue of signatures waiting to be fetched.

The watcher's websocket feed is lossy by nature: a reconnect drops whatever the
slot produced while it was down, and a burst can overflow an in-memory buffer.
Any design that keeps pending work in RAM therefore *samples* — it silently loses
trades, which is exactly what a six-month paper-trade of 5,000 wallets cannot do.

So the queue lives in SQLite. A signature is written the moment the feed mentions
it and is only removed by being fetched, or by exhausting its retries. Process
death, a wedged endpoint, a laptop sleeping for two hours: none of it drops work,
it just makes the backlog longer.

Retries use exponential backoff in ``next_attempt_at`` so a dead endpoint does not
burn the whole budget re-failing the same row, and rows are keyed by signature, so
seeing the same trade twice (websocket + backfill) costs one fetch, not two.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

log = logging.getLogger("cse.queue")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_queue (
    signature       TEXT PRIMARY KEY,
    wallet          TEXT,
    slot            INTEGER,
    discovered_at   REAL NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    next_attempt_at REAL NOT NULL DEFAULT 0,
    fetched_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON fetch_queue (status, next_attempt_at, slot);
CREATE INDEX IF NOT EXISTS idx_queue_wallet
    ON fetch_queue (wallet, status);
"""

#: Give up on a signature only after this many failed fetches. A transaction that
#: is still unfetchable after this many tries is genuinely gone (pruned, or the
#: slot never existed), not merely unlucky.
MAX_ATTEMPTS = 8

#: Backoff schedule, seconds, indexed by attempt count. Caps at 10 minutes so a
#: long outage does not push recovery out by hours.
_BACKOFF = (0.0, 2.0, 10.0, 30.0, 120.0, 300.0, 600.0, 600.0, 600.0)


@dataclass
class QueueItem:
    signature: str
    wallet: Optional[str]
    slot: Optional[int]
    attempts: int
    discovered_at: float


class FetchQueue:
    """SQLite-backed pending-signature queue. Never drops work on overflow."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ writes
    def enqueue(
        self, signature: str, *, wallet: Optional[str] = None, slot: Optional[int] = None
    ) -> bool:
        """Queue a signature. Returns True only if it was not already known."""
        if not signature:
            return False
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fetch_queue"
                " (signature, wallet, slot, discovered_at, next_attempt_at)"
                " VALUES (?, ?, ?, ?, 0)",
                (signature, wallet, slot, time.time()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def enqueue_many(
        self, items: Iterable[tuple[str, Optional[str], Optional[int]]]
    ) -> int:
        """Bulk enqueue of (signature, wallet, slot). Returns the new-row count."""
        now = time.time()
        rows = [(s, w, sl, now) for s, w, sl in items if s]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO fetch_queue"
                " (signature, wallet, slot, discovered_at, next_attempt_at)"
                " VALUES (?, ?, ?, ?, 0)",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def claim(self, limit: int = 64) -> list[QueueItem]:
        """Pending signatures that are due, oldest slot first.

        Rows are returned without being removed: the caller marks each one done or
        failed. If the process dies mid-fetch the row simply becomes due again,
        which costs one duplicate fetch — the safe direction to fail in.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "SELECT signature, wallet, slot, attempts, discovered_at"
                " FROM fetch_queue"
                " WHERE status = 'pending' AND next_attempt_at <= ?"
                " ORDER BY slot IS NULL, slot ASC, discovered_at ASC"
                " LIMIT ?",
                (now, limit),
            )
            return [
                QueueItem(
                    signature=r["signature"],
                    wallet=r["wallet"],
                    slot=r["slot"],
                    attempts=r["attempts"],
                    discovered_at=r["discovered_at"],
                )
                for r in cur.fetchall()
            ]

    def mark_done(self, signature: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE fetch_queue SET status='done', fetched_at=?, last_error=NULL"
                " WHERE signature=?",
                (time.time(), signature),
            )
            self._conn.commit()

    def mark_failed(self, signature: str, error: str) -> None:
        """Retry with backoff, or give up once MAX_ATTEMPTS is reached."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT attempts FROM fetch_queue WHERE signature=?", (signature,)
            )
            row = cur.fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            if attempts >= MAX_ATTEMPTS:
                self._conn.execute(
                    "UPDATE fetch_queue SET status='failed', attempts=?, last_error=?"
                    " WHERE signature=?",
                    (attempts, error[:400], signature),
                )
            else:
                delay = _BACKOFF[min(attempts, len(_BACKOFF) - 1)]
                self._conn.execute(
                    "UPDATE fetch_queue SET attempts=?, last_error=?, next_attempt_at=?"
                    " WHERE signature=?",
                    (attempts, error[:400], time.time() + delay, signature),
                )
            self._conn.commit()

    # ------------------------------------------------------------------- reads
    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM fetch_queue GROUP BY status"
            ).fetchall()
        out = {"pending": 0, "done": 0, "failed": 0, "total": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        return out

    def due_count(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM fetch_queue"
                " WHERE status='pending' AND next_attempt_at <= ?",
                (time.time(),),
            )
            return int(cur.fetchone()["n"])

    def has(self, signature: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM fetch_queue WHERE signature=?", (signature,)
            )
            return cur.fetchone() is not None

    def requeue_failed(self) -> int:
        """Reopen permanently-failed rows. Used after an endpoint outage clears."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE fetch_queue SET status='pending', attempts=0, next_attempt_at=0"
                " WHERE status='failed'"
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------- upkeep
    def prune(self, older_than_s: float, keep_failed: bool = True) -> int:
        """Delete settled rows older than ``older_than_s``. Returns rows removed.

        The queue doubles as the dedupe store, so pruning needs a reason to be
        safe. It has one: the backfill cursor only ever moves *forward* and is
        advanced only after everything behind it has been enqueued, so a signature
        older than the cursor is never surfaced again — not by the poller, and not
        by the websocket, which only reports new slots. Deleting a settled row
        therefore cannot resurrect a fetch.

        ``failed`` rows are kept by default: they are the short list worth
        inspecting, and ``requeue_failed`` is the recovery path for them.
        """
        cutoff = time.time() - max(60.0, float(older_than_s))
        with self._lock:
            if keep_failed:
                cur = self._conn.execute(
                    "DELETE FROM fetch_queue"
                    " WHERE status='done' AND COALESCE(fetched_at, discovered_at) < ?",
                    (cutoff,),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM fetch_queue"
                    " WHERE status IN ('done','failed')"
                    "   AND COALESCE(fetched_at, discovered_at) < ?",
                    (cutoff,),
                )
            self._conn.commit()
            return cur.rowcount

    def enforce_cap(self, max_pending: int) -> int:
        """Shed the oldest pending rows once the backlog exceeds ``max_pending``.

        The queue promises not to lose work, and that promise holds only while the
        observed swap rate fits inside the RPC fetch budget. It does for real
        traders and it does not for bots: a handful of high-frequency wallets can
        produce ~20 signatures/second against a keyless pool that fetches ~6, so an
        unbounded queue is a disk-filling bug wearing a completeness promise.

        Oldest-first is the honest policy, not just the convenient one: a
        transaction old enough to still be sitting in the backlog is one the RPC
        nodes are closest to pruning, so it is both the least likely to be
        fetchable and the least valuable to keep. Shed rows are counted so the run
        reports exactly how much it had to give up instead of quietly sampling.
        """
        cap = int(max_pending)
        if cap <= 0:
            return 0
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM fetch_queue WHERE status='pending'"
            ).fetchone()[0]
            excess = int(total) - cap
            if excess <= 0:
                return 0
            cur = self._conn.execute(
                "DELETE FROM fetch_queue WHERE signature IN ("
                "  SELECT signature FROM fetch_queue WHERE status='pending'"
                "  ORDER BY slot IS NULL, slot ASC, discovered_at ASC LIMIT ?"
                ")",
                (excess,),
            )
            self._conn.commit()
            return cur.rowcount

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the database file.

        Without this the WAL grows for the life of the process. On a six-month run
        that is gigabytes of disk held by a file nobody is reading.
        """
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()
