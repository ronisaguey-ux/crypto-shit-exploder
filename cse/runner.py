"""Wire the free stack together and supervise a long paper-trading run.

Two entry points:

  * `run_watch`      - live ingest only (watch the pool, paper-trade every trade).
  * `run_supervisor` - the six-month shape: discover the wallet pool, watch it,
                       and periodically re-score and report so progress is
                       visible and a restart resumes cleanly.

Everything here is built from free/keyless components. A Helius free key adds
5 connections x 1,000 subscriptions, which is exactly the 5,000-wallet target;
without it the keyless public endpoint carries a much smaller pool.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .db import Database
from .backfill import Backfiller
from .guards import KillSwitch, StalenessGuard
from .prices import PriceOracle
from .queue import FetchQueue
from .rpc import RpcPool, RpcEndpoint, default_endpoints
from .scoring import apply_scores, score_traders
from .tradelog import TraderLogger
from .watcher import Watcher
from .ws import SubscriptionPool, WsEndpoint

log = logging.getLogger("cse.runner")

#: Helius free tier: 5 concurrent connections, 1,000 subscriptions each.
HELIUS_FREE_CONNS = 5
HELIUS_FREE_SUBS = 1000
#: Alchemy free tier allows ~10 concurrent websocket requests.
ALCHEMY_FREE_CONNS = 2


def build_rpc_pool(cfg: Config) -> RpcPool:
    """Keyless endpoints first, plus any free keyed tiers we were given."""
    endpoints = default_endpoints(
        helius_key=cfg.helius_api_key,
        alchemy_key=cfg.alchemy_api_key,
    )
    # Apply the configured budget to the built-in keyless endpoints.
    for ep in endpoints:
        if ep.cost == 0.0:
            ep.rps = cfg.rpc.public_rps
            ep.heavy_rps = cfg.rpc.public_heavy_rps
            ep._cheap = type(ep._cheap)(ep.rps)  # type: ignore[attr-defined]
            ep._heavy = type(ep._heavy)(ep.heavy_rps)  # type: ignore[attr-defined]
    # Then any extra URLs from config.
    for url in cfg.rpc.endpoints:
        endpoints.append(
            RpcEndpoint(url, rps=cfg.rpc.public_rps, heavy_rps=cfg.rpc.public_heavy_rps, cost=0.0)
        )
    return RpcPool(endpoints, timeout=cfg.rpc.timeout)


def build_ws_pool(cfg: Config) -> SubscriptionPool:
    """Helius first (largest free capacity), then configured endpoints."""
    endpoints: list[WsEndpoint] = []
    if cfg.helius_api_key:
        endpoints.append(
            WsEndpoint(
                f"wss://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}",
                name="helius",
                subs_per_connection=HELIUS_FREE_SUBS,
                max_connections=HELIUS_FREE_CONNS,
            )
        )
    if cfg.alchemy_api_key:
        endpoints.append(
            WsEndpoint(
                f"wss://solana-mainnet.g.alchemy.com/v2/{cfg.alchemy_api_key}",
                name="alchemy",
                subs_per_connection=HELIUS_FREE_SUBS,
                max_connections=ALCHEMY_FREE_CONNS,
            )
        )
    for raw in cfg.watch.ws_endpoints:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        endpoints.append(
            WsEndpoint(
                str(raw["url"]),
                name=str(raw.get("name") or ""),
                subs_per_connection=int(raw.get("subscriptions_per_connection", 100)),
                max_connections=int(raw.get("max_connections", 1)),
            )
        )
    pool = SubscriptionPool(
        endpoints,
        queue_size=cfg.watch.queue_size,
        swap_filter=cfg.watch.swap_filter,
        commitment=cfg.watch.commitment,
    )
    cap = pool.capacity()
    if cap < 5000:
        log.warning(
            "wallet capacity is %d (<5000). Add a free Helius key (5x1000) to reach 5000.",
            cap,
        )
    return pool


def build_oracle(cfg: Config) -> PriceOracle:
    return PriceOracle(ttl_seconds=cfg.watch.price_ttl_seconds)


def runtime_paths(db: Database) -> dict[str, str]:
    """Sidecar files next to the database, so a run is one self-contained dir."""
    base = Path(db.path).parent
    base.mkdir(parents=True, exist_ok=True)
    return {
        "queue": str(base / "queue.db"),
        "backfill": str(base / "backfill.db"),
        "kill": str(base / "kill_switch.json"),
    }


def build_watcher(cfg: Config, db: Database) -> Watcher:
    paths = runtime_paths(db)
    return Watcher(
        cfg,
        db,
        rpc=build_rpc_pool(cfg),
        ws=build_ws_pool(cfg),
        prices=build_oracle(cfg),
        queue=FetchQueue(paths["queue"]),
        trader_log=TraderLogger(cfg.watch.log_dir),
        kill_switch=KillSwitch(paths["kill"]),
        concurrency=cfg.rpc.concurrency,
        workers=cfg.watch.workers,
        refresh_seconds=cfg.watch.refresh_seconds,
        enrich=cfg.watch.enrich,
    )


def build_backfiller(cfg: Config, db: Database, rpc: RpcPool, queue: FetchQueue):
    """The completeness safety net for the live feed."""
    paths = runtime_paths(db)
    return Backfiller(
        rpc,
        queue,
        state_path=paths["backfill"],
        poll_interval=cfg.watch.refresh_seconds,
    )


def rescore(cfg: Config, db: Database) -> int:
    """Recompute fitness for every trader with closed history."""
    closed = db.closed_trades()
    by_trader: dict[str, list] = {}
    for ct in closed:
        by_trader.setdefault(ct.trader, []).append(ct)
    reports = score_traders(by_trader, cfg.scoring, cfg.paper.starting_equity_usd)
    apply_scores(db.traders(active_only=False), reports, db=db)
    return len(reports)


def _install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """Turn SIGTERM/SIGINT into a clean stop instead of an abrupt death.

    The trade log is append-only, so it survives a kill either way. What does not
    is the in-memory rolling summary per trader, which only reaches disk on a clean
    stop: without this, a `systemctl stop` after five months would discard every
    trader's summary. Returns a restore function for tests and repeated runs.
    """
    import signal

    loop = asyncio.get_running_loop()
    installed: list[int] = []

    def handler() -> None:
        log.info("stop signal received; finishing the current batch and saving")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handler)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            continue

    def restore() -> None:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    return restore


async def run_watch(
    cfg: Config,
    db: Database,
    *,
    wallets: Optional[list[str]] = None,
    duration: Optional[float] = None,
    heartbeat: float = 60.0,
) -> dict:
    """Watch the pool and paper-trade every observed trade.

    `duration` seconds bounds the run (used for verification); None runs until
    cancelled or signalled.
    """
    watcher = build_watcher(cfg, db)
    watched = await watcher.start(wallets)
    log.info("watching %d wallets", watched)
    backfiller = build_backfiller(cfg, db, watcher.rpc, watcher.queue)
    stop = asyncio.Event()
    restore_signals = _install_signal_handlers(stop)
    bf_task = asyncio.create_task(backfiller.run_forever(watcher.wallets, stop))
    run_task = asyncio.create_task(watcher.run())
    started = time.time()
    deadline = None if duration is None else started + duration
    try:
        while not stop.is_set() and not run_task.done():
            if deadline is not None and time.time() >= deadline:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(heartbeat, 5.0))
            except asyncio.TimeoutError:
                pass
            db.set_meta("watch_heartbeat", str(time.time()))
            log.info("watch stats: %s", watcher.stats.as_dict())
    finally:
        restore_signals()
        stop.set()
        bf_task.cancel()
        await asyncio.gather(bf_task, return_exceptions=True)
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await watcher.stop()
        try:
            await watcher.prices.aclose()
            await watcher.rpc.aclose() if watcher.rpc else None
        except Exception:  # noqa: BLE001
            pass
    summary = watcher.summary()
    summary["watched"] = watched
    summary["backfill"] = backfiller.stats()
    summary["footprint"] = watcher.footprint()
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    return summary


async def run_supervisor(
    cfg: Config,
    db: Database,
    *,
    discover: bool = True,
    target: Optional[int] = None,
    maintenance_hours: float = 24.0,
    duration: Optional[float] = None,
) -> dict:
    """Discover the pool, then watch + re-score on a schedule for the long haul."""
    if discover:
        from .discovery import TraderDiscovery

        want = target or cfg.discovery.target_traders
        log.info("discovering up to %d traders", want)
        result = await TraderDiscovery(cfg, db).discover(limit=want)
        log.info("discovered %d (%s)", result.total, result.per_provider)
        db.set_meta("discovered_total", str(db.count_traders()))

    if db.count_traders() == 0:
        log.warning("no traders in the pool; watch will have nothing to do")

    interval = max(maintenance_hours, 0.01) * 3600.0
    next_maintenance = time.time() + interval
    watched = 0
    started = time.time()

    watcher = build_watcher(cfg, db)
    watched = await watcher.start()
    log.info("watching %d wallets", watched)
    run_task = asyncio.create_task(watcher.run())
    backfiller = build_backfiller(cfg, db, watcher.rpc, watcher.queue)
    stop = asyncio.Event()
    restore_signals = _install_signal_handlers(stop)
    bf_task = asyncio.create_task(backfiller.run_forever(watcher.wallets, stop))
    # The staleness guard was instantiated nowhere, so a silent feed looked
    # identical to a healthy one. Now it is polled every tick and, on a stall,
    # engages the kill switch and stops the run.
    staleness = StalenessGuard(cfg.watch.stale_seconds)
    try:
        while not stop.is_set() and not run_task.done():
            if duration is not None and (time.time() - started) >= duration:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass
            db.set_meta("watch_heartbeat", str(time.time()))
            last_signal = float(db.get_meta("last_signal_at") or 0.0)
            if staleness.check(last_signal, now=time.time()):
                log.error("feed stale for %.0fs — engaging kill switch", staleness.stale_for())
                watcher.kill_switch.engage("feed stale")
                db.set_meta("killed_at", str(time.time()))
                break
            if time.time() >= next_maintenance:
                try:
                    n = rescore(cfg, db)
                    db.set_meta("last_rescore_at", str(time.time()))
                    db.set_meta("last_rescore_traders", str(n))
                    log.info("rescored %d traders", n)
                except Exception as e:  # noqa: BLE001
                    log.warning("rescore failed: %s", e)
                next_maintenance = time.time() + interval
    finally:
        restore_signals()
        stop.set()
        bf_task.cancel()
        await asyncio.gather(bf_task, return_exceptions=True)
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await watcher.stop()
    summary = watcher.summary()
    summary["watched"] = watched
    summary["traders"] = db.count_traders()
    summary["backfill"] = backfiller.stats()
    summary["footprint"] = watcher.footprint()
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    return summary
