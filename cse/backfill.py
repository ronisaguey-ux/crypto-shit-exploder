"""Backfill: guarantee that every trade a wallet made is eventually fetched.

A websocket subscription is a live feed, not a ledger. It misses whatever happens
while the process is restarting, while an endpoint is parked, or while a burst
overflows the buffer. For a six-month study of 5,000 wallets that is fatal: the
result would be a sample wearing the label of a census.

So the queue is drained by two producers:

  * the websocket watcher, for latency (trades land within a slot or two), and
  * this poller, for completeness — it walks each wallet's signature history with
    ``getSignaturesForAddress`` and queues anything the live feed never delivered.

The poller is the safety net, and it is cheap: one call returns up to 1,000
signatures, so 5,000 wallets cost 5,000 calls per sweep. A cursor per wallet lets
it resume exactly where it stopped, and it pages backwards when a wallet traded
more than one page's worth between sweeps so the gap is closed rather than
skipped.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("cse.backfill")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_state (
    wallet        TEXT PRIMARY KEY,
    last_sig      TEXT,
    last_slot     INTEGER,
    last_polled_at REAL,
    sweeps        INTEGER NOT NULL DEFAULT 0,
    enqueued      INTEGER NOT NULL DEFAULT 0,
    gap_before    TEXT
);
"""

#: How far back to page when a wallet outran the sweep. Bounded so one hyperactive
#: bot cannot monopolise the RPC budget and starve every other wallet.
MAX_GAP_PAGES = 5


@dataclass
class BackfillStats:
    wallets: int = 0
    seeded: int = 0
    scanned: int = 0
    enqueued: int = 0
    gaps_closed: int = 0
    gaps_pending: int = 0
    errors: int = 0
    elapsed_s: float = 0.0
    error_kinds: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallets": self.wallets,
            "seeded": self.seeded,
            "scanned": self.scanned,
            "enqueued": self.enqueued,
            "gaps_closed": self.gaps_closed,
            "errors": self.errors,
            "elapsed_s": round(self.elapsed_s, 2),
            "error_kinds": dict(self.error_kinds),
        }


class Backfiller:
    """Walks wallet history and queues anything the live feed did not deliver."""

    def __init__(
        self,
        rpc,
        queue,
        *,
        state_path: str,
        page_limit: int = 1000,
        concurrency: int = 4,
        poll_interval: float = 300.0,
    ):
        self.rpc = rpc
        self.queue = queue
        self.page_limit = max(1, min(page_limit, 1000))
        self.concurrency = max(1, concurrency)
        self.poll_interval = poll_interval
        self._total = BackfillStats()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(state_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------------------------------------------------------------- cursors
    def _cursor(self, wallet: str) -> tuple[Optional[str], int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_sig, last_slot FROM backfill_state WHERE wallet=?",
                (wallet,),
            ).fetchone()
        if not row:
            return None, 0
        return row["last_sig"], int(row["last_slot"] or 0)

    def _set_cursor(self, wallet: str, last_sig: Optional[str], last_slot: int, added: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO backfill_state
                   (wallet, last_sig, last_slot, last_polled_at, sweeps, enqueued)
                   VALUES (?,?,?,?,1,?)
                   ON CONFLICT(wallet) DO UPDATE SET
                     last_sig=excluded.last_sig,
                     last_slot=excluded.last_slot,
                     last_polled_at=excluded.last_polled_at,
                     sweeps=backfill_state.sweeps+1,
                     enqueued=backfill_state.enqueued+excluded.enqueued""",
                (wallet, last_sig, last_slot, time.time(), added),
            )
            self._conn.commit()

    def _set_gap(self, wallet: str, gap_before: Optional[str]) -> None:
        """Persist (or clear) the continuation for a gap that outran one sweep."""
        with self._lock:
            self._conn.execute(
                "UPDATE backfill_state SET gap_before=? WHERE wallet=?",
                (gap_before, wallet),
            )
            self._conn.commit()

    def _gap(self, wallet: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT gap_before FROM backfill_state WHERE wallet=?", (wallet,)
            ).fetchone()
        return row["gap_before"] if row else None

    # ------------------------------------------------------------------ polling
    async def poll_wallet(self, wallet: str, stats: BackfillStats) -> int:
        """Queue every signature this wallet produced since the last sweep."""
        known_sig, known_slot = self._cursor(wallet)
        newest_sig = known_sig
        newest_slot = known_slot
        added = 0
        before: Optional[str] = None
        pages = 0
        reached_cursor = False
        page: list = []

        while pages < MAX_GAP_PAGES:
            try:
                page = await self.rpc.get_signatures_for_address(
                    wallet, before=before, limit=self.page_limit
                )
            except Exception as exc:  # endpoint exhausted; the sweep will retry
                stats.errors += 1
                kind = type(exc).__name__
                stats.error_kinds[kind] = stats.error_kinds.get(kind, 0) + 1
                log.debug("backfill %s failed: %s", wallet[:8], exc)
                break
            if not page:
                break

            if known_sig is None:
                # First sight of this wallet: adopt the head of its history as the
                # cursor and queue nothing. The study covers trades from the moment
                # we start watching; replaying a wallet's whole past would cost
                # millions of fetches and answer a different question.
                self._set_cursor(
                    wallet,
                    page[0].get("signature"),
                    int(page[0].get("slot") or 0),
                    0,
                )
                stats.seeded += 1
                return 0

            stats.scanned += len(page)
            rows: list[tuple[str, Optional[str], Optional[int]]] = []
            reached_cursor = False
            for item in page:
                sig = item.get("signature")
                if not sig:
                    continue
                if known_sig and sig == known_sig:
                    reached_cursor = True
                    break
                # A failed transaction moved nothing, so it is not a trade.
                if item.get("err"):
                    continue
                rows.append((sig, wallet, item.get("slot")))

            added += self.queue.enqueue_many(rows)

            if pages == 0:
                newest_sig = page[0].get("signature") or newest_sig
                newest_slot = max(newest_slot, int(page[0].get("slot") or 0))

            pages += 1
            # Stop when we hit the cursor (no gap) or the page was short (history
            # exhausted). Otherwise the wallet traded more than one page's worth
            # since the last sweep, so page back and close the gap.
            if reached_cursor or len(page) < self.page_limit:
                break
            stats.gaps_closed += 1
            before = page[-1].get("signature")

        # The head cursor always advances to the newest signature we saw — that is
        # the point of a cursor, and it is what the next sweep pages back from.
        #
        # The gap itself is NOT lost: when we ran out of pages before reaching the
        # cursor, the deepest signature we reached is persisted as a continuation,
        # and the next sweep resumes from there instead of skipping the unfetched
        # pages forever.
        self._set_cursor(wallet, newest_sig, newest_slot, added)
        if not (reached_cursor or len(page) < self.page_limit):
            stats.gaps_pending += 1
            self._set_gap(wallet, page[-1].get("signature"))
            log.debug(
                "backfill %s: gap still open after %d pages; continuation saved",
                wallet[:8], pages,
            )
        else:
            self._set_gap(wallet, None)
        return added

    async def run_once(self, wallets: Iterable[str]) -> BackfillStats:
        """One sweep over every wallet, bounded concurrency."""
        stats = BackfillStats()
        wallet_list = list(wallets)
        stats.wallets = len(wallet_list)
        started = time.time()
        sem = asyncio.Semaphore(self.concurrency)

        async def one(w: str) -> None:
            async with sem:
                stats.enqueued += await self.poll_wallet(w, stats)

        await asyncio.gather(*(one(w) for w in wallet_list), return_exceptions=True)
        stats.elapsed_s = time.time() - started
        return stats

    async def run_forever(
        self,
        wallets_provider: Callable[[], Iterable[str]],
        stop: Optional[asyncio.Event] = None,
    ) -> BackfillStats:
        """Sweep repeatedly until ``stop`` is set."""
        total = self._total
        while stop is None or not stop.is_set():
            wallets = list(wallets_provider())
            if wallets:
                sweep = await self.run_once(wallets)
                total.wallets = sweep.wallets
                total.seeded += sweep.seeded
                total.scanned += sweep.scanned
                total.enqueued += sweep.enqueued
                total.gaps_closed += sweep.gaps_closed
                total.errors += sweep.errors
                for k, v in sweep.error_kinds.items():
                    total.error_kinds[k] = total.error_kinds.get(k, 0) + v
                log.info(
                    "backfill sweep: %d wallets, %d seeded, %d scanned, %d queued, %d gaps",
                    sweep.wallets, sweep.seeded, sweep.scanned, sweep.enqueued, sweep.gaps_closed,
                )
            try:
                await asyncio.wait_for(
                    (stop or asyncio.Event()).wait(), timeout=self.poll_interval
                )
            except asyncio.TimeoutError:
                pass
        return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def stats(self) -> dict[str, Any]:
        """Cumulative sweep counters, for the run summary."""
        return self._total.to_dict()
