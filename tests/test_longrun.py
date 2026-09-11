"""Tests for the six-month properties: bounded memory, bounded disk, crash safety.

A six-month run of 5,000 wallets fails in three ways that never show up in a
one-minute smoke test, and this file pins all three:

  * memory grows without bound (a cache that never evicts, a summary that keeps
    one unbounded list per trader),
  * disk grows without bound (settled queue rows and a WAL that is never folded
    back into the database),
  * data is lost on an abrupt stop because it only lived in RAM.

The last one is the important one. Restart-resume is tested by actually closing
the databases and reopening them, not by inspecting a flag.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from cse.backfill import Backfiller
from cse.db import Database
from cse.models import Side, Trade
from cse.prices import PriceOracle, TokenPrice
from cse.queue import MAX_ATTEMPTS, FetchQueue
from cse.tradelog import _MAX_MINTS, TraderLogger

WALLET = "Wallet111111111111111111111111111111111111111"
MINT = "Mint11111111111111111111111111111111111111111"


def _trade(wallet: str = WALLET, *, sig: str = "sig-1", mint: str = MINT) -> Trade:
    return Trade(
        trader=wallet,
        mint=mint,
        side=Side.BUY,
        price=1e-4,
        amount=1_000.0,
        signature=sig,
        slot=100,
        observed_at=1_700_000_000.0,
        dex="raydium_amm",
        slippage_basis="exact",
        slippage_bps=42.0,
        fees_usd=0.003,
    )


# ------------------------------------------------------- queue disk bounding
def test_prune_deletes_settled_rows_past_the_retention_window(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("old", slot=1)
    q.enqueue("new", slot=2)
    q.mark_done("old")
    q.mark_done("new")
    # Backdate the old row past the window; the new one stays.
    with q._lock:
        q._conn.execute(
            "UPDATE fetch_queue SET fetched_at=? WHERE signature='old'",
            (time.time() - 10 * 3600,),
        )
        q._conn.commit()
    removed = q.prune(older_than_s=3600)
    assert removed == 1
    assert q.has("old") is False
    assert q.has("new") is True
    q.close()


def test_prune_never_deletes_pending_or_failed_work(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("pending", slot=1)
    q.enqueue("failed", slot=2)
    for _ in range(MAX_ATTEMPTS):
        q.mark_failed("failed", "boom")
    assert q.stats()["failed"] == 1
    with q._lock:
        q._conn.execute("UPDATE fetch_queue SET discovered_at=?", (time.time() - 10 * 3600,))
        q._conn.commit()
    assert q.prune(older_than_s=60) == 0
    assert q.has("pending") is True
    assert q.has("failed") is True
    q.close()


def test_prune_can_reap_failed_rows_when_asked(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("failed")
    for _ in range(MAX_ATTEMPTS):
        q.mark_failed("failed", "boom")
    assert q.stats()["failed"] == 1
    with q._lock:
        q._conn.execute("UPDATE fetch_queue SET discovered_at=?", (time.time() - 10 * 3600,))
        q._conn.commit()
    assert q.prune(older_than_s=60, keep_failed=False) == 1
    assert q.has("failed") is False
    q.close()


def test_queue_checkpoint_and_size_are_callable(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    q.mark_done("sig-1")
    q.checkpoint()
    assert q.size_bytes() > 0
    q.close()


def test_enforce_cap_sheds_the_oldest_pending_rows(tmp_path):
    """A capped queue is what keeps a six-month run from filling the disk."""
    q = FetchQueue(str(tmp_path / "q.db"))
    for i in range(20):
        q.enqueue(f"sig-{i:02d}", slot=i)
    shed = q.enforce_cap(max_pending=5)
    assert shed == 15
    assert q.stats()["pending"] == 5
    # The survivors are the newest five: old slots were dropped first.
    remaining = {i.signature for i in q.claim(limit=10)}
    assert remaining == {f"sig-{i:02d}" for i in range(15, 20)}
    q.close()


def test_enforce_cap_never_touches_settled_rows(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    for i in range(10):
        q.enqueue(f"sig-{i:02d}", slot=i)
    q.mark_done("sig-00")
    # 9 still pending, cap 3 -> shed 6, and the settled row is untouched.
    assert q.enforce_cap(max_pending=3) == 6
    assert q.has("sig-00") is True
    assert q.stats()["done"] == 1
    assert q.stats()["pending"] == 3
    q.close()


def test_enforce_cap_is_a_noop_under_the_limit(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    assert q.enforce_cap(max_pending=100) == 0
    assert q.enqueue("sig-2") is True
    q.close()


def test_enforce_cap_zero_means_unlimited(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    for i in range(5):
        q.enqueue(f"sig-{i}")
    assert q.enforce_cap(max_pending=0) == 0
    assert q.stats()["pending"] == 5
    q.close()


def test_watcher_maintain_sheds_and_counts(tmp_path):
    """The cap must be wired into maintenance, and the loss must be visible."""
    from cse.config import load_config
    from cse.runner import build_watcher

    cfg = load_config()
    cfg.watch.queue_max_pending = 3
    db = Database(tmp_path / "cse.db")
    w = build_watcher(cfg, db)
    for i in range(10):
        w.queue.enqueue(f"sig-{i:02d}", slot=i)
    stats = w.maintain()
    assert stats["queue_shed"] == 7
    assert w.stats.shed == 7
    assert w.queue.stats()["pending"] == 3
    assert w.stats.as_dict()["shed"] == 7
    w.queue.close()
    db.close()


# --------------------------------------------------------- db disk bounding
def test_db_checkpoint_truncates_the_wal(tmp_path):
    db = Database(tmp_path / "cse.db")
    db.set_meta("k", "v")
    db.checkpoint()
    wal = tmp_path / "cse.db-wal"
    # A truncated WAL is empty; without the checkpoint it holds the writes.
    assert not wal.exists() or wal.stat().st_size == 0
    db.close()


def test_db_row_counts_reports_every_table(tmp_path):
    db = Database(tmp_path / "cse.db")
    counts = db.row_counts()
    assert set(counts) == {"traders", "trades", "positions", "closed_trades", "signals"}
    assert all(v == 0 for v in counts.values())
    db.close()


def test_db_close_checkpoints_so_a_restart_sees_committed_rows(tmp_path):
    path = tmp_path / "cse.db"
    db = Database(path)
    db.set_meta("watch_heartbeat", "123")
    db.close()
    assert Database(path).get_meta("watch_heartbeat") == "123"


# ------------------------------------------------------- price cache bounding
def test_price_cache_evicts_expired_entries(tmp_path):
    oracle = PriceOracle(ttl_seconds=0.0, max_entries=4)
    for i in range(3):
        oracle._cache[f"m{i}"] = TokenPrice(mint=f"m{i}", usd=1.0, at=time.time() - 10)
    oracle._evict()
    assert oracle._cache == {}
    assert oracle.stats["evicted"] == 3


def test_price_cache_stays_under_its_cap(tmp_path):
    oracle = PriceOracle(ttl_seconds=10_000.0, max_entries=100)
    for i in range(1_000):
        oracle._cache[f"m{i}"] = TokenPrice(mint=f"m{i}", usd=1.0, at=float(i))
    oracle._evict()
    assert len(oracle._cache) <= 100
    assert oracle.stats["evicted"] > 0


def test_price_cache_keeps_recent_entries_over_old_ones():
    oracle = PriceOracle(ttl_seconds=10_000.0, max_entries=10)
    for i in range(10):
        oracle._cache[f"old{i}"] = TokenPrice(mint=f"old{i}", usd=1.0, at=float(i))
    oracle._cache["fresh"] = TokenPrice(mint="fresh", usd=1.0, at=time.time())
    oracle._evict()
    assert "fresh" in oracle._cache
    assert len(oracle._cache) <= 10


# ----------------------------------------------------- summary memory bounding
def test_summary_mint_list_is_bounded_and_flags_truncation(tmp_path):
    logger = TraderLogger(root=str(tmp_path / "traders"))
    for i in range(_MAX_MINTS + 25):
        logger.log_trade(_trade(sig=f"sig-{i}", mint=f"Mint{i}"))
    summary = logger.summary_for(WALLET)
    assert len(summary["mints"]) == _MAX_MINTS
    assert summary["mints_truncated"] is True
    assert summary["trades"] == _MAX_MINTS + 25
    logger.close()


def test_summary_is_not_truncated_below_the_cap(tmp_path):
    logger = TraderLogger(root=str(tmp_path / "traders"))
    logger.log_trade(_trade(sig="s1", mint="M1"))
    logger.log_trade(_trade(sig="s2", mint="M2"))
    summary = logger.summary_for(WALLET)
    assert sorted(summary["mints"]) == ["M1", "M2"]
    assert "mints_truncated" not in summary
    logger.close()


# ------------------------------------------------------------ crash safety
def test_queue_survives_a_reopen_with_its_backlog_intact(tmp_path):
    path = str(tmp_path / "q.db")
    q = FetchQueue(path)
    for i in range(5):
        q.enqueue(f"sig-{i}", slot=i)
    q.mark_done("sig-0")
    q.close()  # simulate process death: no drain, just close

    reopened = FetchQueue(path)
    assert reopened.stats()["pending"] == 4
    assert reopened.stats()["done"] == 1
    assert reopened.has("sig-0") is True
    reopened.close()


def test_backfill_cursor_survives_a_reopen(tmp_path):
    path = str(tmp_path / "bf.db")
    bf = Backfiller(None, None, state_path=path)
    bf._set_cursor(WALLET, "sig-abc", 555, 3)
    bf._conn.close()

    reopened = Backfiller(None, None, state_path=path)
    last_sig, last_slot = reopened._cursor(WALLET)
    assert last_sig == "sig-abc"
    assert last_slot == 555
    reopened._conn.close()


def test_trade_log_is_durable_without_a_clean_close(tmp_path):
    """The append-only log is the last line of defence: it needs no shutdown."""
    root = tmp_path / "traders"
    logger = TraderLogger(root=str(root))
    logger.log_trade(_trade(sig="s1"))
    logger.log_trade(_trade(sig="s2"))
    # No flush, no close, no summary write.
    path = root / WALLET / "trades.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["signature"] == "s1"


def test_maintenance_autosaves_summaries_without_a_stop(tmp_path):
    """maintain() is what puts summaries on disk mid-run, not only at shutdown."""
    logger = TraderLogger(root=str(tmp_path / "traders"))
    logger.log_trade(_trade(sig="s1"))
    summary_path = tmp_path / "traders" / WALLET / "summary.json"
    assert not summary_path.exists()
    n = logger.flush_all_summaries()
    assert n == 1
    assert summary_path.exists()
    assert json.loads(summary_path.read_text())["trades"] == 1
    logger.close()


# --------------------------------------------------------- signal handling
def test_signal_handlers_set_the_stop_event(tmp_path):
    """SIGTERM must stop the loop, not kill it: summaries depend on a clean stop."""
    import signal

    from cse.runner import _install_signal_handlers

    async def scenario() -> None:
        stop = asyncio.Event()
        restore = _install_signal_handlers(stop)
        try:
            assert not stop.is_set()
            import os

            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(50):
                if stop.is_set():
                    break
                await asyncio.sleep(0.01)
            assert stop.is_set() is True
        finally:
            restore()

    asyncio.run(scenario())


def test_signal_restore_is_safe_to_call_twice():
    from cse.runner import _install_signal_handlers

    async def scenario() -> None:
        stop = asyncio.Event()
        restore = _install_signal_handlers(stop)
        restore()
        restore()  # must not raise

    asyncio.run(scenario())


# ── migration, malformed tx, restart equity (F-004) ─────────────────────────

def test_database_opens_a_pre_kind_schema(tmp_path):
    """B-04: SCHEMA used to create idx_trades_unique_kind before _migrate()
    added the column, so opening an older collector's DB raised
    `no such column: kind` and the process could not start."""
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE trades (id TEXT PRIMARY KEY, trader TEXT, mint TEXT, side TEXT,
            price REAL, amount REAL, signature TEXT, slot INTEGER,
            pool_liquidity_usd REAL, observed_at REAL, effective_price REAL,
            fees_usd REAL, slippage_bps REAL, mev_tax_usd REAL);
        CREATE UNIQUE INDEX idx_trades_unique ON trades(signature, trader, mint);
    """)
    c.commit(); c.close()

    db = Database(p)                      # must not raise
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(trades)")}
    assert "kind" in cols
    idx = {r["name"] for r in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_trades_unique_kind" in idx
    assert "idx_trades_unique" not in idx   # superseded index dropped
    db.close()


def test_malformed_transaction_is_settled_not_left_pending(tmp_path):
    """F-010: decode_trades raising used to leave the queue row unclaimed
    forever, so the watcher re-fetched the same signature on every drain."""
    from cse.queue import FetchQueue
    q = FetchQueue(tmp_path / "q.db")
    q.enqueue_many([("bad-sig", WALLET, 1)])
    assert len(q.claim(10)) == 1
    q.mark_failed("bad-sig", "decode error: ValueError")
    # It must not be immediately claimable again: backoff holds it.
    assert len(q.claim(10)) == 0
    q.close()


def test_portfolio_equity_survives_a_restart(tmp_path):
    """F-008: equity reset to starting_equity while open positions persisted, so
    a flat close after a restart booked a phantom gain."""
    from cse.config import PaperConfig
    from cse.paper import PaperTradingEngine

    path = tmp_path / "eq.db"
    cfg = PaperConfig(starting_equity_usd=10_000, position_pct=0.10,
                      slippage_bps=0, dynamic_slippage=False, slippage_jitter=[1.0, 1.0],
                      mev_tax_bps=0, latency_seconds=0.0,
                      base_fee_lamports=0, priority_fee_lamports=0)

    db1 = Database(path)
    eng1 = PaperTradingEngine(cfg, db1)
    eng1.on_trade(_trade(sig="b1"))                       # open a position
    equity_before = eng1.portfolio(WALLET).equity_usd
    assert equity_before < 10_000                          # cash went into the position
    db1.close()

    # Reopen: the engine is new but the position and the equity must both persist.
    db2 = Database(path)
    eng2 = PaperTradingEngine(cfg, db2)
    assert eng2.portfolio(WALLET).equity_usd == pytest.approx(equity_before)
    closed = eng2.on_trade(Trade(trader=WALLET, mint=MINT, side=Side.SELL,
                                 price=1e-4, amount=1_000.0, signature="s1", slot=101,
                                 observed_at=200.0))
    assert closed is not None
    # A flat round trip (same price, no fees) must not manufacture a gain.
    assert closed.pnl_usd == pytest.approx(0.0, abs=1e-9)
    db2.close()
