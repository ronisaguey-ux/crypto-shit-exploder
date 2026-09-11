"""Live watcher: WebSocket notifications -> durable queue -> paper trades.

Ingest is deliberately split in two, because the two halves fail differently.

**Produce** (fast, must never block): a `logsSubscribe` notification is filtered
for "does this even look like a swap", then its signature is written to the
SQLite queue. No network call happens on this path, so a burst, a slow endpoint or
a reconnect cannot make the feed drop work — the worst case is a longer backlog.

**Consume** (bounded, retrying): workers claim signatures from the queue, fetch
each transaction, decode the swap with no DEX-specific knowledge, read the real
pool reserves and real fees out of it, price it, log it per-trader, and run it
through the paper engine. A failure re-queues the signature with backoff instead
of losing the trade.

The swap pre-filter is what makes this affordable on free RPC: most of what a
wallet emits is not a swap, and every notification rejected here is a
`getTransaction` never spent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .aggregation import Aggregator
from .config import Config
from .db import Database
from .guards import CircuitBreaker, KillSwitch, StalenessGuard
from .models import Trade
from .paper import PaperTradingEngine
from .prices import PriceOracle
from .queue import FetchQueue
from .reserves import enrich_trade
from .rpc import RpcPool
from .swapdecode import decode_trades, is_swap_candidate
from .tradelog import TraderLogger
from .ws import Notification, SubscriptionPool

log = logging.getLogger("cse.watcher")


def _rss_bytes() -> int:
    """Resident set size, or 0 where the platform will not say.

    A six-month run has to be able to prove it is not leaking, and the only
    credible evidence is the number the kernel reports about this process.
    """
    try:
        with open("/proc/self/statm", "r") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _trim_allocator() -> bool:
    """Hand freed arenas back to the OS. Returns whether it ran.

    Python returns objects to the allocator, and glibc keeps that memory for reuse
    instead of releasing it, so the resident set of a long-lived process only ever
    climbs. Measured here: tracemalloc attributed ~1 MB of a 66 MB RSS rise to
    Python objects, so nearly all of it was native buffers and allocator arenas.
    This is the call that gives it back.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
        return True
    except (OSError, AttributeError):
        return False


@dataclass
class WatchStats:
    notifications: int = 0
    filtered: int = 0
    queued: int = 0
    fetched: int = 0
    not_found: int = 0
    fetch_errors: int = 0
    decoded: int = 0
    decode_errors: int = 0
    trades: int = 0
    duplicates: int = 0
    closed: int = 0
    signals: int = 0
    exact_fills: int = 0
    estimated_fills: int = 0
    shed: int = 0

    def as_dict(self) -> dict:
        return {
            "notifications": self.notifications,
            "filtered": self.filtered,
            "queued": self.queued,
            "fetched": self.fetched,
            "not_found": self.not_found,
            "fetch_errors": self.fetch_errors,
            "decoded": self.decoded,
            "decode_errors": self.decode_errors,
            "trades": self.trades,
            "duplicates": self.duplicates,
            "closed": self.closed,
            "signals": self.signals,
            "exact_fills": self.exact_fills,
            "estimated_fills": self.estimated_fills,
            "shed": self.shed,
        }


class Watcher:
    """Consumes the subscription pool and drives the paper engine."""

    def __init__(
        self,
        cfg: Config,
        db: Database,
        *,
        rpc: Optional[RpcPool] = None,
        ws: Optional[SubscriptionPool] = None,
        prices: Optional[PriceOracle] = None,
        engine: Optional[PaperTradingEngine] = None,
        aggregator: Optional[Aggregator] = None,
        queue: Optional[FetchQueue] = None,
        trader_log: Optional[TraderLogger] = None,
        kill_switch: Optional[KillSwitch] = None,
        concurrency: int = 16,
        refresh_seconds: float = 300.0,
        workers: int = 4,
        enrich: bool = True,
    ):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.ws = ws
        self.prices = prices or PriceOracle()
        self.engine = engine or PaperTradingEngine(cfg.paper, db)
        self.agg = aggregator or Aggregator(cfg.aggregation)
        self.queue = queue
        self.trader_log = trader_log or TraderLogger()
        self.kill = kill_switch or KillSwitch()
        self.breaker = CircuitBreaker()
        self.concurrency = max(1, concurrency)
        self.workers = max(1, workers)
        self.refresh_seconds = refresh_seconds
        self.enrich = enrich
        self.maintenance_seconds = float(
            getattr(cfg.watch, "maintenance_seconds", 300.0)
        )
        self.queue_retention_s = float(
            getattr(cfg.watch, "queue_retention_hours", 72.0)
        ) * 3600.0
        self.queue_max_pending = int(
            getattr(cfg.watch, "queue_max_pending", 500_000)
        )

        self.stats = WatchStats()
        self.feed = StalenessGuard(
            threshold_s=max(120.0, float(getattr(cfg.watch, "stale_after_seconds", 300.0)))
        )
        self._tracked: dict[str, object] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._running = False
        self._last_refresh = 0.0
        self._last_maintain = time.time()
        self._pending: set[asyncio.Task] = set()
        self._drain_tasks: list[asyncio.Task] = []
        self._maintain_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- bookkeeping
    def _refresh_tracked(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_refresh) < self.refresh_seconds:
            return
        self._tracked = {t.address: t for t in self.db.traders(active_only=True)}
        self._last_refresh = now
        log.info("tracked %d wallets", len(self._tracked))

    def wallets(self) -> list[str]:
        self._refresh_tracked(force=True)
        return list(self._tracked)

    # ---------------------------------------------------------------- producer
    async def _handle(self, note: Notification) -> None:
        """Queue a notification. Never performs a network call."""
        self.stats.notifications += 1
        self.feed.note_event(note.received_at)

        if self.queue is None:
            return
        # Cheapest possible rejection: a plain transfer costs us nothing at all.
        if not is_swap_candidate(note.logs):
            self.stats.filtered += 1
            return
        if self.queue.enqueue(note.signature, wallet=note.wallet, slot=note.slot):
            self.stats.queued += 1
        else:
            # Already queued, in flight, or long since fetched. The websocket and
            # the backfill poller both surface the same trade; count it once.
            self.stats.duplicates += 1

    # ---------------------------------------------------------------- consumer
    async def _process_one(self, signature: str, wallet: Optional[str]) -> None:
        """Fetch, decode, enrich, price and record a single signature."""
        assert self.rpc is not None
        try:
            tx = await self.rpc.get_transaction(signature)
        except Exception as e:  # noqa: BLE001 - retry, never lose the trade
            self.stats.fetch_errors += 1
            self.breaker.record_error()
            if self.queue is not None:
                self.queue.mark_failed(signature, f"{type(e).__name__}: {e}")
            return

        if tx is None:
            # Pruned, or not visible yet on this node. Keep it for a retry: a node
            # that has not caught up is not the same as a trade that never existed.
            self.stats.not_found += 1
            if self.queue is not None:
                self.queue.mark_failed(signature, "not found")
            return

        self.stats.fetched += 1
        wanted = [wallet] if wallet else None
        try:
            trades = decode_trades(
                tx, wallets=wanted, sol_price_usd=self.cfg.paper.sol_price_usd
            )
            if not trades:
                if self.queue is not None:
                    self.queue.mark_done(signature)
                return
            self.stats.decoded += len(trades)

            if self.enrich:
                for t in trades:
                    try:
                        enrich_trade(
                            tx,
                            t,
                            wallets=wanted,
                            sol_price_usd=self.cfg.paper.sol_price_usd,
                        )
                    except Exception as e:  # noqa: BLE001 - enrichment is additive
                        log.debug("enrich failed for %s: %s", signature[:12], e)

            await self._price(trades)
            self._record(trades)
        except Exception as e:  # noqa: BLE001
            # A malformed transaction (bad fields, unexpected shape) used to
            # propagate out of here with the queue row never settled, so the
            # watcher re-fetched the same signature on every drain forever.
            # mark_failed puts it into backoff: a transient bad node reply is
            # retried, a permanently broken row eventually stops being claimed.
            self.stats.decode_errors += 1
            log.warning("decode failed for %s: %s", signature[:12], e)
            if self.queue is not None:
                self.queue.mark_failed(signature, f"decode error: {type(e).__name__}")
            return
        if self.queue is not None:
            self.queue.mark_done(signature)

    async def drain_once(self, limit: Optional[int] = None) -> int:
        """Claim and process one batch. Returns how many were handled."""
        if self.queue is None:
            return 0
        batch = self.queue.claim(limit or max(8, self.concurrency))
        if not batch:
            return 0
        assert self._sem is not None
        async def one(item) -> None:
            async with self._sem:
                await self._process_one(item.signature, item.wallet)
        await asyncio.gather(*(one(i) for i in batch), return_exceptions=True)
        return len(batch)

    async def _drain_loop(self) -> None:
        """Keep the queue moving until stopped, backing off when it is empty."""
        idle = 0.0
        while self._running:
            try:
                n = await self.drain_once()
            except Exception as e:  # noqa: BLE001 - a drain crash must not kill the run
                log.warning("drain failed: %s", e)
                n = 0
            if n:
                idle = 0.0
            else:
                idle = min(idle + 0.25, 5.0)
                await asyncio.sleep(idle)

    # -------------------------------------------------------------- maintenance
    def maintain(self) -> dict:
        """Periodic upkeep: autosave logs, prune settled work, checkpoint WALs.

        This is what makes the run crash-safe rather than merely restart-safe. The
        rolling summaries are held in memory and would otherwise only reach disk on
        a clean stop, so a kill -9 would lose every trader's summary back to the
        start of the run. Everything else here bounds disk: settled queue rows are
        deleted once the backfill cursor has moved past them, and the WAL is folded
        back into the database instead of growing for six months.
        """
        out: dict = {"at": time.time()}
        try:
            out["summaries_flushed"] = self.trader_log.flush_all_summaries()
        except Exception as e:  # noqa: BLE001 - upkeep must never kill ingest
            log.warning("summary flush failed: %s", e)
            out["summaries_flushed"] = 0
        try:
            if self.queue is not None and self.queue_retention_s > 0:
                out["queue_pruned"] = self.queue.prune(self.queue_retention_s)
            if self.queue is not None and self.queue_max_pending > 0:
                shed = self.queue.enforce_cap(self.queue_max_pending)
                if shed:
                    self.stats.shed += shed
                    log.warning(
                        "queue over cap (%d pending); shed %d oldest signatures",
                        self.queue_max_pending,
                        shed,
                    )
                out["queue_shed"] = shed
            if self.queue is not None:
                self.queue.checkpoint()
        except Exception as e:  # noqa: BLE001
            log.warning("queue upkeep failed: %s", e)
        try:
            self.db.checkpoint()
        except Exception as e:  # noqa: BLE001
            log.warning("db checkpoint failed: %s", e)
        out["rss_before_trim"] = _rss_bytes()
        out["allocator_trimmed"] = _trim_allocator()
        out["rss_bytes"] = _rss_bytes()
        self._last_maintain = time.time()
        return out

    async def _maintain_loop(self) -> None:
        """Run ``maintain`` on a timer for as long as the watcher is up."""
        interval = max(30.0, float(self.maintenance_seconds))
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                stats = self.maintain()
            except Exception as e:  # noqa: BLE001
                log.warning("maintenance failed: %s", e)
                continue
            log.info(
                "maintenance: summaries=%s pruned=%s rss=%.1fMB",
                stats.get("summaries_flushed", 0),
                stats.get("queue_pruned", 0),
                stats.get("rss_bytes", 0) / 1e6,
            )

    # ------------------------------------------------------------------ pricing
    async def _price(self, trades: list[Trade]) -> None:
        """Fill price / liquidity from the keyless oracle where the chain did not."""
        missing = [
            t.mint for t in trades if t.price <= 0 or t.pool_liquidity_usd is None
        ]
        if not missing:
            return
        try:
            pmap = await self.prices.get(missing)
        except Exception as e:  # noqa: BLE001 - pricing is best-effort
            log.debug("price lookup failed: %s", e)
            return
        for t in trades:
            p = pmap.get(t.mint)
            if p is None:
                continue
            if t.price <= 0:
                t.price = p.usd
            if t.pool_liquidity_usd is None:
                t.pool_liquidity_usd = p.liquidity_usd

    # ------------------------------------------------------------------ recording
    def _record(self, trades: list[Trade]) -> None:
        """Persist observed trades, run the paper engine, emit signals, log."""
        self._refresh_tracked()
        for t in trades:
            if t.price <= 0 or t.amount <= 0:
                continue  # cannot simulate a fill without a price
            inserted = self.db.insert_trade(t)
            if not inserted:
                # The websocket and the backfill both surfaced it; count once.
                self.stats.duplicates += 1
                continue
            self.stats.trades += 1
            if t.slippage_basis == "exact":
                self.stats.exact_fills += 1
            elif t.slippage_basis in ("estimate", "observed"):
                self.stats.estimated_fills += 1

            try:
                self.trader_log.log_trade(t)
            except Exception as e:  # noqa: BLE001 - logging must not stop ingest
                log.debug("trader log failed: %s", e)

            try:
                ct = self.engine.on_trade(t)
            except Exception as e:  # noqa: BLE001 - engine must never kill ingest
                log.warning("engine failed on %s: %s", t.id, e)
                continue
            if ct is not None:
                self.stats.closed += 1
                try:
                    self.trader_log.log_close(ct)
                except Exception as e:  # noqa: BLE001
                    log.debug("close log failed: %s", e)

        try:
            signals = self.agg.signals_from_trades(trades, self._tracked)
        except Exception as e:  # noqa: BLE001
            log.debug("aggregation failed: %s", e)
            signals = []
        for s in signals:
            self.db.insert_signal(s)
            self.stats.signals += 1

    # ------------------------------------------------------------------- run
    async def start(self, wallets: Optional[list[str]] = None) -> int:
        if self.ws is None or self.rpc is None:
            raise RuntimeError("Watcher needs a SubscriptionPool and an RpcPool")
        if self.kill.engaged:
            raise RuntimeError(f"kill switch engaged: {self.kill.reason}")
        self._running = True
        self._sem = asyncio.Semaphore(self.concurrency)
        watched = await self.ws.start(wallets if wallets is not None else self.wallets())
        self.db.set_meta("watch_started_at", str(time.time()))
        self.db.set_meta("watch_wallets", str(watched))
        return watched

    async def run(self) -> None:
        """Consume notifications and drain the queue until stopped."""
        assert self.ws is not None
        self._drain_tasks = [
            asyncio.create_task(self._drain_loop()) for _ in range(self.workers)
        ]
        self._maintain_task = asyncio.create_task(self._maintain_loop())
        try:
            async for note in self.ws.notifications():
                if not self._running:
                    break
                task = asyncio.create_task(self._handle(note))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
                # Bound the enqueue backlog. Unlike the old in-memory fetch path
                # this only delays *queuing*, and the backfill poller still covers
                # anything the feed dropped, so nothing is lost by waiting here.
                if len(self._pending) > self.concurrency * 4:
                    await asyncio.wait(self._pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in self._drain_tasks:
                t.cancel()
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
            self._drain_tasks.clear()
            if self._maintain_task is not None:
                self._maintain_task.cancel()
                await asyncio.gather(self._maintain_task, return_exceptions=True)
                self._maintain_task = None

    async def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            await self.ws.stop()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
        # Final autosave + checkpoint, so a clean shutdown leaves nothing in RAM.
        try:
            self.maintain()
        except Exception as e:  # noqa: BLE001
            log.debug("final maintenance failed: %s", e)
        self.db.set_meta("watch_stopped_at", str(time.time()))

    def health(self) -> dict:
        """Current health, so silence is never the only signal."""
        out: dict = {
            "healthy": True,
            "kill": self.kill.to_dict(),
            "feed": self.feed.to_dict(),
            "stats": self.stats.as_dict(),
            "rss_bytes": _rss_bytes(),
        }
        if self.queue is not None:
            out["queue"] = self.queue.stats()
        if self.rpc is not None:
            out["rpc"] = self.rpc.summary()
        if self.ws is not None:
            out["ws"] = self.ws.summary()
        out["healthy"] = not self.kill.engaged and not self.feed.is_stale()
        return out

    def footprint(self) -> dict:
        """Memory and disk held right now — the two ways a long run dies."""
        out: dict = {
            "rss_bytes": _rss_bytes(),
            "db_bytes": self.db.size_bytes(),
            "rows": self.db.row_counts(),
            "summaries_held": len(getattr(self.trader_log, "_summaries", {})),
            "open_log_files": len(getattr(self.trader_log, "_open", {})),
            "price_cache": len(getattr(self.prices, "_cache", {})),
            "seconds_since_maintenance": round(time.time() - self._last_maintain, 1),
        }
        if self.queue is not None:
            out["queue_bytes"] = self.queue.size_bytes()
        if self.rpc is not None:
            out["seen_signatures"] = len(getattr(self.rpc, "_seen", ()))
        return out

    def summary(self) -> dict:
        return self.health()
