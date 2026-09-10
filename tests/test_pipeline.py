"""Tests for the completeness + observability stack: queue, backfill, logs, guards.

The property under test is the one that decides whether a six-month study is a
census or a sample: a signature that has been seen is never silently lost, and a
gap in the live feed is closed rather than skipped.
"""
from __future__ import annotations

import json

from cse.backfill import MAX_GAP_PAGES, BackfillStats, Backfiller
from cse.guards import DrawdownGuard, HealthReport, KillSwitch, StalenessGuard
from cse.models import ClosedTrade, Side, Trade
from cse.queue import MAX_ATTEMPTS, FetchQueue
from cse.tradelog import _MAX_OPEN, TraderLogger

WALLET = "Wallet111111111111111111111111111111111111111"
MINT = "Mint11111111111111111111111111111111111111111"


def _trade(wallet: str = WALLET, *, price: float = 1e-4, amount: float = 1_000.0,
           side: Side = Side.BUY, sig: str = "sig-1") -> Trade:
    return Trade(trader=wallet, mint=MINT, side=side, price=price, amount=amount,
                 signature=sig, slot=100, observed_at=1_700_000_000.0,
                 dex="raydium_amm", slippage_basis="exact", slippage_bps=42.0,
                 fees_usd=0.003)


# ------------------------------------------------------------------ queue
def test_queue_dedupes_by_signature(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    assert q.enqueue("sig-1", wallet=WALLET, slot=10) is True
    assert q.enqueue("sig-1", wallet=WALLET, slot=10) is False
    assert q.has("sig-1") is True
    assert q.stats()["total"] == 1
    q.close()


def test_queue_ignores_an_empty_signature(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    assert q.enqueue("") is False
    assert q.stats()["total"] == 0
    q.close()


def test_queue_claims_oldest_slot_first(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("b", slot=20)
    q.enqueue("a", slot=10)
    q.enqueue("c", slot=None)  # unknown slot sorts last
    assert [i.signature for i in q.claim(limit=10)] == ["a", "b", "c"]
    q.close()


def test_queue_mark_done_removes_the_row_from_claim(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    assert len(q.claim()) == 1
    q.mark_done("sig-1")
    assert q.claim() == []
    assert q.stats() == {"pending": 0, "done": 1, "failed": 0, "total": 1}
    q.close()


def test_queue_failure_backs_off_instead_of_hot_looping(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    q.mark_failed("sig-1", "endpoint parked")
    # Still pending, but not due yet: the backoff pushes it into the future.
    assert q.stats()["pending"] == 1
    assert q.claim() == []
    assert q.due_count() == 0
    q.close()


def test_queue_gives_up_only_after_max_attempts(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    for _ in range(MAX_ATTEMPTS - 1):
        q.mark_failed("sig-1", "boom")
        assert q.stats()["pending"] == 1  # still retrying
    q.mark_failed("sig-1", "boom")
    assert q.stats()["failed"] == 1
    assert q.stats()["pending"] == 0
    assert q.claim() == []  # dead-lettered, never claimed again
    q.close()


def test_queue_requeue_failed_reopens_dead_letters(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    q.enqueue("sig-1")
    for _ in range(MAX_ATTEMPTS):
        q.mark_failed("sig-1", "boom")
    assert q.requeue_failed() == 1
    assert q.stats()["pending"] == 1
    assert q.due_count() == 1  # immediately due, no leftover backoff
    q.close()


def test_queue_enqueue_many_counts_only_new_rows(tmp_path):
    q = FetchQueue(str(tmp_path / "q.db"))
    assert q.enqueue_many([("a", WALLET, 1), ("b", WALLET, 2)]) == 2
    assert q.enqueue_many([("b", WALLET, 2), ("c", WALLET, 3)]) == 1
    assert q.stats()["total"] == 3
    q.close()


def test_queue_survives_reopening_the_file(tmp_path):
    path = str(tmp_path / "q.db")
    q = FetchQueue(path)
    q.enqueue("sig-1", wallet=WALLET)
    q.close()
    # The whole point: pending work outlives the process.
    q2 = FetchQueue(path)
    assert q2.has("sig-1") is True
    assert [i.signature for i in q2.claim()] == ["sig-1"]
    q2.close()


# --------------------------------------------------------------- backfill
class _StubSigRpc:
    """Serves a fixed newest-first signature history, with real paging."""

    def __init__(self, history: dict[str, list[dict]]):
        self.history = history
        self.calls: list[tuple] = []

    async def get_signatures_for_address(self, wallet, before=None, limit=1000):
        self.calls.append((wallet, before, limit))
        items = list(self.history.get(wallet, []))
        if before is not None:
            for i, it in enumerate(items):
                if it["signature"] == before:
                    items = items[i + 1:]
                    break
            else:
                items = []
        return items[:limit]


def _sig(sig: str, slot: int, err=None) -> dict:
    return {"signature": sig, "slot": slot, "err": err}


def _backfiller(tmp_path, rpc, *, page_limit=1000) -> Backfiller:
    return Backfiller(
        rpc,
        FetchQueue(str(tmp_path / "q.db")),
        state_path=str(tmp_path / "bf.db"),
        page_limit=page_limit,
    )


async def test_backfill_seeds_the_cursor_without_replaying_history(tmp_path):
    rpc = _StubSigRpc({WALLET: [_sig("newest", 900), _sig("older", 800)]})
    bf = _backfiller(tmp_path, rpc)
    stats = BackfillStats()
    added = await bf.poll_wallet(WALLET, stats)

    # A first sight must not enqueue: the study starts now, and replaying a
    # wallet's whole past would be millions of fetches for a different question.
    assert added == 0
    assert stats.seeded == 1
    assert bf.queue.stats()["total"] == 0
    assert bf._cursor(WALLET) == ("newest", 900)
    bf.close()


async def test_backfill_queues_everything_newer_than_the_cursor(tmp_path):
    rpc = _StubSigRpc({WALLET: [_sig("c1", 100), _sig("c2", 90)]})
    bf = _backfiller(tmp_path, rpc)
    await bf.poll_wallet(WALLET, BackfillStats())
    assert bf._cursor(WALLET)[0] == "c1"

    # The wallet traded twice between sweeps.
    rpc.history[WALLET] = [_sig("n1", 300), _sig("n2", 200), _sig("c1", 100), _sig("c2", 90)]
    stats = BackfillStats()
    added = await bf.poll_wallet(WALLET, stats)

    assert added == 2
    assert bf.queue.has("n1") and bf.queue.has("n2")
    assert not bf.queue.has("c1")  # the cursor itself is already accounted for
    assert bf._cursor(WALLET) == ("n1", 300)
    bf.close()


async def test_backfill_skips_failed_transactions(tmp_path):
    rpc = _StubSigRpc({WALLET: [_sig("c1", 100)]})
    bf = _backfiller(tmp_path, rpc)
    await bf.poll_wallet(WALLET, BackfillStats())

    # A failed tx moved nothing, so it is not a trade and costs no fetch.
    rpc.history[WALLET] = [
        _sig("ok", 300),
        _sig("failed", 200, err={"InstructionError": [0, "x"]}),
        _sig("c1", 100),
    ]
    added = await bf.poll_wallet(WALLET, BackfillStats())
    assert added == 1
    assert bf.queue.has("ok") is True
    assert bf.queue.has("failed") is False
    bf.close()


async def test_backfill_pages_back_to_close_a_gap(tmp_path):
    rpc = _StubSigRpc({WALLET: [_sig("c1", 100)]})
    bf = _backfiller(tmp_path, rpc, page_limit=2)
    await bf.poll_wallet(WALLET, BackfillStats())

    # Four new signatures: more than one page's worth, so one sweep must page back.
    rpc.history[WALLET] = [
        _sig("n1", 400), _sig("n2", 300), _sig("n3", 200), _sig("n4", 100), _sig("c1", 50)
    ]
    stats = BackfillStats()
    added = await bf.poll_wallet(WALLET, stats)

    assert added == 4
    # Two pages were full, so the poller had to walk back twice to reach the cursor.
    assert stats.gaps_closed == 2
    assert bf._cursor(WALLET) == ("n1", 400)
    bf.close()


async def test_backfill_gap_paging_is_bounded(tmp_path):
    # A wallet that outran the sweep by more than MAX_GAP_PAGES pages is capped, so
    # one hyperactive bot cannot starve every other wallet of RPC budget.
    rpc = _StubSigRpc({WALLET: [_sig("c1", 1)]})
    bf = _backfiller(tmp_path, rpc, page_limit=2)
    await bf.poll_wallet(WALLET, BackfillStats())

    rpc.history[WALLET] = [_sig(f"n{i}", 1000 - i) for i in range(20)] + [_sig("c1", 1)]
    stats = BackfillStats()
    added = await bf.poll_wallet(WALLET, stats)

    assert added == MAX_GAP_PAGES * 2
    assert stats.gaps_closed == MAX_GAP_PAGES
    # And the cursor still advanced, so the next sweep resumes from the head.
    assert bf._cursor(WALLET)[0] == "n0"
    bf.close()


async def test_backfill_records_an_endpoint_error_without_losing_the_sweep(tmp_path):
    class _Broken:
        async def get_signatures_for_address(self, wallet, before=None, limit=1000):
            raise RuntimeError("all endpoints parked")

    bf = _backfiller(tmp_path, _Broken())
    stats = BackfillStats()
    added = await bf.poll_wallet(WALLET, stats)
    assert added == 0
    assert stats.errors == 1
    assert stats.error_kinds["RuntimeError"] == 1
    # Nothing is written, so the next sweep retries this wallet from scratch.
    assert bf._cursor(WALLET) == (None, 0)
    bf.close()


async def test_backfill_run_once_covers_every_wallet(tmp_path):
    wallets = [f"Wallet{i:040d}" for i in range(5)]
    rpc = _StubSigRpc({w: [_sig(f"{w}-head", 100)] for w in wallets})
    bf = _backfiller(tmp_path, rpc)
    stats = await bf.run_once(wallets)
    assert stats.wallets == 5
    assert stats.seeded == 5
    bf.close()


# --------------------------------------------------------------- tradelog
def test_tradelog_writes_the_per_trader_tree(tmp_path):
    logger = TraderLogger(tmp_path / "logs")
    logger.log_trade(_trade())
    logger.flush_summary(WALLET)

    base = tmp_path / "logs" / WALLET
    assert (base / "trades.jsonl").exists()
    assert (base / "trades.log").exists()
    assert (base / "daily" / "2023-11-14.jsonl").exists()  # observed_at is fixed
    assert (base / "summary.json").exists()

    rec = json.loads((base / "trades.jsonl").read_text().strip())
    assert rec["trader"] == WALLET
    assert rec["dex"] == "raydium_amm"
    assert rec["slippage_basis"] == "exact"
    assert rec["slippage_bps"] == 42.0
    logger.close()


def test_tradelog_summary_accumulates_real_costs(tmp_path):
    logger = TraderLogger(tmp_path / "logs")
    logger.log_trade(_trade(amount=1_000.0, sig="s1"))
    logger.log_trade(_trade(amount=3_000.0, side=Side.SELL, sig="s2"))
    logger.flush_summary(WALLET)

    s = json.loads((tmp_path / "logs" / WALLET / "summary.json").read_text())
    assert s["trades"] == 2
    assert s["buys"] == 1
    assert s["sells"] == 1
    assert s["unique_mints"] == 1
    assert s["venues"] == {"raydium_amm": 2}
    assert s["exact_fills"] == 2
    assert s["fees_usd"] == 0.006
    assert s["avg_slippage_bps"] == 42.0
    assert s["slippage_bps_max"] == 42.0
    logger.close()


def test_tradelog_logs_closed_round_trips(tmp_path):
    logger = TraderLogger(tmp_path / "logs")
    closed = ClosedTrade(
        trader=WALLET, mint=MINT, entry_price=1e-4, exit_price=1.5e-4,
        amount=1_000.0, pnl_usd=5.0, pnl_pct=0.5, fees_usd=0.006,
        hold_seconds=3_600.0, opened_at=1_700_000_000.0,
    )
    logger.log_close(closed)
    logger.close()

    base = tmp_path / "logs" / WALLET
    rec = json.loads((base / "positions.jsonl").read_text().strip())
    assert rec["pnl_usd"] == 5.0
    assert "CLOSE" in (base / "trades.log").read_text()


def test_tradelog_keeps_file_handles_bounded_across_many_wallets(tmp_path):
    logger = TraderLogger(tmp_path / "logs")
    for i in range(_MAX_OPEN * 3):
        logger.log_trade(_trade(wallet=f"Wallet{i:040d}", sig=f"s{i}"))
    # 192 wallets visited, but the descriptor pool never grew past its cap.
    assert len(logger._files._open) <= _MAX_OPEN
    assert len(logger.tracked_wallets()) == _MAX_OPEN * 3
    logger.close()
    assert logger._files._open == {}


def test_tradelog_flush_all_summaries_writes_every_tracked_wallet(tmp_path):
    logger = TraderLogger(tmp_path / "logs")
    for i in range(3):
        logger.log_trade(_trade(wallet=f"Wallet{i:040d}", sig=f"s{i}"))
    assert logger.flush_all_summaries() == 3
    for i in range(3):
        assert (tmp_path / "logs" / f"Wallet{i:040d}" / "summary.json").exists()
    logger.close()


# ----------------------------------------------------------------- guards
def test_kill_switch_is_sticky_across_restarts(tmp_path):
    path = tmp_path / "kill.json"
    ks = KillSwitch(path)
    assert ks.engaged is False
    ks.engage("feed silent for 40 minutes")
    assert ks.engaged is True
    assert ks.reason == "feed silent for 40 minutes"

    # A restart must not quietly clear a fault that nobody has looked at.
    again = KillSwitch(path)
    assert again.engaged is True
    assert again.reason == "feed silent for 40 minutes"
    again.disengage()
    assert KillSwitch(path).engaged is False


def test_kill_switch_keeps_the_first_reason(tmp_path):
    ks = KillSwitch(tmp_path / "kill.json")
    ks.engage("first")
    ks.engage("second")
    assert ks.reason == "first"


def test_staleness_guard_is_quiet_before_the_first_event():
    g = StalenessGuard(threshold_s=120.0)
    assert g.silence_s is None
    assert g.is_stale() is False  # startup must not look like a fault
    assert g.events == 0


def test_staleness_guard_flags_a_feed_that_went_quiet():
    g = StalenessGuard(threshold_s=120.0)
    now = 1_700_000_000.0
    g.note_event(now)
    assert g.is_stale(now + 60) is False
    assert g.is_stale(now + 121) is True
    assert g.events == 1


def test_drawdown_guard_trips_on_giving_back_half_the_peak():
    g = DrawdownGuard(limit_pct=0.5)
    assert g.update(100.0) is False   # first sample sets the peak
    assert g.update(120.0) is False   # new peak
    assert g.update(90.0) is False    # 25% off the peak
    assert g.update(60.0) is True     # 50% off the peak
    assert g.tripped is True
    assert g.tripped_at is not None


def test_drawdown_guard_stays_tripped_once_tripped():
    g = DrawdownGuard(limit_pct=0.5)
    g.update(100.0)
    g.update(10.0)
    assert g.update(100.0) is True  # recovery does not un-trip a latch


def test_health_report_is_unhealthy_when_the_feed_or_the_latch_says_so():
    ok = HealthReport(feed={"stale": False}, kill={"engaged": False})
    assert ok.healthy is True
    assert HealthReport(feed={"stale": True}, kill={"engaged": False}).healthy is False
    assert HealthReport(feed={"stale": False}, kill={"engaged": True}).healthy is False
    assert ok.to_dict()["healthy"] is True
