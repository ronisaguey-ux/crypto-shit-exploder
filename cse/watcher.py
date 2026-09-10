"""Live watcher: WebSocket notifications -> transactions -> paper trades.

This is the ingest path that replaces the Helius webhook. It consumes
`logsSubscribe` notifications from the sharded pool, fetches each transaction
over the free RPC pool, decodes the swap with no DEX-specific knowledge, prices
it, and feeds it through the same paper engine the webhook used — so behaviour
is identical, just without the paid webhook tier.

Cost control is the whole design: signatures are deduplicated, non-swap
notifications are filtered before any fetch, and in-flight `getTransaction`
calls are capped so a burst cannot drain a free tier's quota.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .aggregation import Aggregator
from .config import Config
from .db import Database
from .models import Trade
from .paper import PaperTradingEngine
from .prices import PriceOracle
from .rpc import RpcPool
from .swapdecode import decode_trades
from .ws import Notification, SubscriptionPool

log = logging.getLogger("cse.watcher")


@dataclass
class WatchStats:
    notifications: int = 0
    fetched: int = 0
    not_found: int = 0
    fetch_errors: int = 0
    decoded: int = 0
    trades: int = 0
    closed: int = 0
    signals: int = 0
    duplicates: int = 0

    def as_dict(self) -> dict:
        return {
            "notifications": self.notifications,
            "fetched": self.fetched,
            "not_found": self.not_found,
            "fetch_errors": self.fetch_errors,
            "decoded": self.decoded,
            "trades": self.trades,
            "closed": self.closed,
            "signals": self.signals,
            "duplicates": self.duplicates,
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
        concurrency: int = 16,
        refresh_seconds: float = 300.0,
    ):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.ws = ws
        self.prices = prices or PriceOracle()
        self.engine = engine or PaperTradingEngine(cfg.paper, db)
        self.agg = aggregator or Aggregator(cfg.aggregation)
        self.concurrency = max(1, concurrency)
        self.refresh_seconds = refresh_seconds

        self.stats = WatchStats()
        self._tracked: dict[str, object] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._running = False
        self._last_refresh = 0.0
        self._pending: set[asyncio.Task] = set()

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

    # -------------------------------------------------------------- pipeline
    async def _handle(self, note: Notification) -> None:
        assert self.rpc is not None
        self.stats.notifications += 1
        if self.rpc.already_seen(note.signature):
            self.stats.duplicates += 1
            return
        self.rpc.mark_seen(note.signature)

        assert self._sem is not None
        async with self._sem:
            try:
                tx = await self.rpc.get_transaction(note.signature)
            except Exception as e:  # noqa: BLE001 - one bad fetch must not stop the feed
                self.stats.fetch_errors += 1
                log.debug("getTransaction %s failed: %s", note.signature[:12], e)
                return
        if tx is None:
            # Too old, or not yet available on the node we asked.
            self.stats.not_found += 1
            return
        self.stats.fetched += 1

        trades = decode_trades(
            tx,
            wallets=[note.wallet],
            sol_price_usd=self.cfg.paper.sol_price_usd,
        )
        if not trades:
            return
        self.stats.decoded += len(trades)

        await self._price(trades)
        self._record(trades)

    async def _price(self, trades: list[Trade]) -> None:
        """Fill missing price / liquidity from the keyless oracle."""
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

    def _record(self, trades: list[Trade]) -> None:
        """Persist observed trades, run the paper engine, emit signals."""
        self._refresh_tracked()
        for t in trades:
            if t.price <= 0 or t.amount <= 0:
                continue  # cannot simulate a fill without a price
            self.db.insert_trade(t)
            self.stats.trades += 1
            try:
                ct = self.engine.on_trade(t)
            except Exception as e:  # noqa: BLE001 - engine must never kill ingest
                log.warning("engine failed on %s: %s", t.id, e)
                continue
            if ct is not None:
                self.stats.closed += 1

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
        self._running = True
        self._sem = asyncio.Semaphore(self.concurrency)
        watched = await self.ws.start(wallets if wallets is not None else self.wallets())
        self.db.set_meta("watch_started_at", str(time.time()))
        self.db.set_meta("watch_wallets", str(watched))
        return watched

    async def run(self) -> None:
        """Consume notifications until stopped."""
        assert self.ws is not None
        async for note in self.ws.notifications():
            if not self._running:
                break
            task = asyncio.create_task(self._handle(note))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            # Bound the task backlog so memory cannot run away under a burst.
            if len(self._pending) > self.concurrency * 4:
                await asyncio.wait(self._pending, return_when=asyncio.FIRST_COMPLETED)

    async def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            await self.ws.stop()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
        self.db.set_meta("watch_stopped_at", str(time.time()))

    def summary(self) -> dict:
        out: dict = {"stats": self.stats.as_dict()}
        if self.rpc is not None:
            out["rpc"] = self.rpc.summary()
        if self.ws is not None:
            out["ws"] = self.ws.summary()
        return out
