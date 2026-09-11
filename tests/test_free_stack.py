"""Tests for the free/keyless ingest stack: rpc, ws, prices, swap decoding."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from cse.aggregation import Aggregator
from cse.config import Config
from cse.db import Database
from cse.models import Side, Trader
from cse.paper import PaperTradingEngine
from cse.prices import PriceOracle
from cse.queue import FetchQueue
from cse.rpc import RpcEndpoint, RpcPool
from cse.runner import build_ws_pool
from cse.swapdecode import DEX_PROGRAMS, decode_trades, is_swap_candidate
from cse.tradelog import TraderLogger
from cse.watcher import Notification, Watcher

WALLET = "Wallet111111111111111111111111111111111111111"
POOL = "PooL11111111111111111111111111111111111111111"
MINT = "Mint11111111111111111111111111111111111111111"
SOL = "So11111111111111111111111111111111111111112"

#: A log line naming a real DEX program, which is what the swap pre-filter keys on.
_SWAP_LOG = "Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 invoke [1]"


def _tx(*, sig: str, sol_delta: float, token_delta: float, err=None, block_time=1_700_000_000):
    """A synthetic parsed transaction moving SOL and one SPL token for WALLET."""
    pre_sol = 10_000_000_000
    post_sol = pre_sol + int(sol_delta * 1e9)
    pre_tok = 1000.0
    post_tok = pre_tok + token_delta
    pre_balances = [{"owner": WALLET, "mint": MINT, "uiTokenAmount": {"uiAmount": pre_tok}}]
    post_balances = [{"owner": WALLET, "mint": MINT, "uiTokenAmount": {"uiAmount": post_tok}}]
    return {
        "slot": 123,
        "blockTime": block_time,
        "transaction": {
            "signatures": [sig],
            "message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": POOL}]},
        },
        "meta": {
            "err": err,
            "fee": 5000,
            "preBalances": [pre_sol, 5_000_000_000],
            "postBalances": [post_sol, 5_000_000_000 - int(sol_delta * 1e9)],
            "preTokenBalances": pre_balances,
            "postTokenBalances": post_balances,
        },
    }


# ------------------------------------------------------------------ decoding
def test_decode_buy_derives_price_from_sol_leg():
    trades = decode_trades(
        _tx(sig="sig-buy", sol_delta=-1.0, token_delta=1000.0),
        wallets=[WALLET],
        sol_price_usd=150.0,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.side == Side.BUY
    assert t.mint == MINT
    assert t.trader == WALLET
    assert t.signature == "sig-buy"
    assert abs(t.amount - 1000.0) < 1e-9
    assert abs(t.price - 0.15) < 1e-9  # 1 SOL @ $150 / 1000 tokens


def test_decode_sell_is_opposite_side():
    trades = decode_trades(
        _tx(sig="sig-sell", sol_delta=1.0, token_delta=-1000.0),
        wallets=[WALLET],
        sol_price_usd=150.0,
    )
    assert len(trades) == 1
    assert trades[0].side == Side.SELL
    assert abs(trades[0].price - 0.15) < 1e-9


def test_decode_skips_failed_transaction():
    trades = decode_trades(
        _tx(sig="sig-fail", sol_delta=-1.0, token_delta=1000.0, err={"InstructionError": [0, 1]}),
        wallets=[WALLET],
    )
    assert trades == []


def test_decode_skips_dust():
    # 0.001 SOL ~= $0.15, below the $1 dust floor.
    trades = decode_trades(
        _tx(sig="sig-dust", sol_delta=-0.001, token_delta=1.0),
        wallets=[WALLET],
        sol_price_usd=150.0,
    )
    assert trades == []


def test_decode_ignores_unrequested_wallets():
    trades = decode_trades(_tx(sig="s", sol_delta=-1.0, token_delta=1000.0), wallets=["Other"])
    assert trades == []


def test_swap_candidate_matches_dex_program():
    prog = next(iter(DEX_PROGRAMS))
    assert is_swap_candidate([f"Program {prog} invoke [1]"])
    assert is_swap_candidate(["Program log: Instruction: Swap"])
    assert not is_swap_candidate(["Program log: Instruction: Transfer"])
    assert not is_swap_candidate([])


# ------------------------------------------------------------------ rpc pool
def _rpc_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_rpc_pool_rotates_off_rate_limited_endpoint():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        if "public" in host:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    pool = RpcPool(
        [
            RpcEndpoint("https://public.example/rpc", name="public", rps=1000, heavy_rps=1000),
            RpcEndpoint("https://backup.example/rpc", name="backup", rps=1000, heavy_rps=1000),
        ],
        client=_rpc_client(handler),
    )
    assert await pool.call("getHealth") == "ok"
    assert pool.stats["rate_limited"] == 1
    assert not pool.endpoints[0].healthy  # the 429 endpoint got parked
    await pool.aclose()


async def test_rpc_pool_raises_when_all_endpoints_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    pool = RpcPool(
        [RpcEndpoint("https://dead.example/rpc", rps=1000, heavy_rps=1000)],
        max_attempts=2,
        client=_rpc_client(handler),
    )
    with pytest.raises(Exception):
        await pool.call("getHealth")
    await pool.aclose()


def test_rpc_pool_dedupe_is_bounded():
    pool = RpcPool([RpcEndpoint("https://a.example/rpc", rps=1000, heavy_rps=1000)])
    assert not pool.already_seen("sig")
    pool.mark_seen("sig")
    assert pool.already_seen("sig")


# --------------------------------------------------------------------- prices
async def test_price_oracle_dexscreener_and_cache():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(
            200,
            json={
                "pairs": [
                    {
                        "baseToken": {"address": MINT},
                        "quoteToken": {"address": SOL},
                        "priceUsd": "0.0042",
                        "liquidity": {"usd": 55_000.0},
                    }
                ]
            },
        )

    oracle = PriceOracle(ttl_seconds=300, client=_rpc_client(handler))
    got = await oracle.get([MINT])
    assert abs(got[MINT].usd - 0.0042) < 1e-12
    assert got[MINT].liquidity_usd == 55_000.0
    assert got[MINT].source == "dexscreener"

    # Second call must be served from cache (no extra request).
    again = await oracle.get([MINT])
    assert again[MINT].usd == got[MINT].usd
    assert hits["n"] == 1
    await oracle.aclose()


async def test_price_oracle_falls_back_to_geckoterminal():
    def handler(request: httpx.Request) -> httpx.Response:
        if "dexscreener" in request.url.host:
            return httpx.Response(200, json={"pairs": []})
        return httpx.Response(
            200,
            json={"data": {"attributes": {"token_prices": {MINT: "0.5"}}}},
        )

    oracle = PriceOracle(ttl_seconds=300, min_interval=0, client=_rpc_client(handler))
    got = await oracle.get([MINT])
    assert abs(got[MINT].usd - 0.5) < 1e-12
    assert got[MINT].source == "geckoterminal"
    await oracle.aclose()


# ---------------------------------------------------------------- ws capacity
def test_ws_capacity_keyless_only():
    pool = build_ws_pool(Config())
    # 4 connections x 100 subscriptions on the public endpoint.
    assert pool.capacity() == 400


def test_ws_capacity_with_free_helius_reaches_5000():
    cfg = Config()
    cfg.helius_api_key = "fake-free-key"
    pool = build_ws_pool(cfg)
    # 5 x 1000 (Helius) + 4 x 100 (public) = 5400, comfortably covering 5000.
    assert pool.capacity() == 5400
    assert pool.plan_shards([f"w{i}" for i in range(5000)])[0][0].name == "helius"


# -------------------------------------------------------------------- watcher
class _StubRpc:
    def __init__(self, txs: dict[str, dict]):
        self.txs = txs
        self.seen: set[str] = set()

    def already_seen(self, sig: str) -> bool:
        return sig in self.seen

    def mark_seen(self, sig: str) -> None:
        self.seen.add(sig)

    async def get_transaction(self, sig: str, **kwargs):
        return self.txs.get(sig)

    def summary(self) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _StubPrices:
    async def get(self, mints):
        return {}

    async def aclose(self) -> None:
        pass


async def test_watcher_records_trade_and_closes_round_trip(tmp_path):
    db = Database(tmp_path / "watch.db")
    db.upsert_trader(Trader(address=WALLET, source="test"))

    cfg = Config()
    txs = {
        "buy-sig": _tx(sig="buy-sig", sol_delta=-1.0, token_delta=1000.0),
        "sell-sig": _tx(sig="sell-sig", sol_delta=1.0, token_delta=-1000.0),
    }
    queue = FetchQueue(tmp_path / "queue.db")
    watcher = Watcher(
        cfg,
        db,
        rpc=_StubRpc(txs),  # type: ignore[arg-type]
        ws=None,
        prices=_StubPrices(),  # type: ignore[arg-type]
        engine=PaperTradingEngine(cfg.paper, db),
        aggregator=Aggregator(cfg.aggregation),
        queue=queue,
        trader_log=TraderLogger(tmp_path / "logs"),
    )
    watcher._sem = asyncio.Semaphore(4)

    # Ingest only queues; a swap-looking log is what gets past the pre-filter.
    await watcher._handle(Notification(wallet=WALLET, signature="buy-sig", logs=[_SWAP_LOG]))
    await watcher._handle(Notification(wallet=WALLET, signature="sell-sig", logs=[_SWAP_LOG]))
    assert watcher.stats.queued == 2
    assert watcher.stats.trades == 0  # nothing fetched yet

    # Draining is what actually fetches, decodes and records.
    handled = await watcher.drain_once()
    assert handled == 2

    assert watcher.stats.trades == 2
    assert watcher.stats.closed == 1
    assert len(db.closed_trades()) == 1
    # recent_trades() is the observed feed only — the simulated fills those
    # replays wrote are a separate kind and must not be replayed again.
    assert len(db.recent_trades(limit=50)) == 2
    assert len(db.recent_trades(limit=50, kind="simulated")) == 2

    # A re-delivered notification is counted, not re-queued or re-fetched.
    await watcher._handle(Notification(wallet=WALLET, signature="buy-sig", logs=[_SWAP_LOG]))
    assert watcher.stats.duplicates == 1
    assert watcher.stats.trades == 2

    # Every trade is on disk in the trader's own log directory.
    tdir = tmp_path / "logs" / WALLET
    assert (tdir / "trades.jsonl").exists()
    assert (tdir / "trades.log").exists()
    assert len((tdir / "trades.jsonl").read_text().strip().splitlines()) == 2
    queue.close()
    db.close()


async def test_watcher_ignores_non_swap_notification(tmp_path):
    """A notification whose logs show no DEX never costs a fetch."""
    db = Database(tmp_path / "watch3.db")
    db.upsert_trader(Trader(address=WALLET, source="test"))
    cfg = Config()
    queue = FetchQueue(tmp_path / "queue3.db")
    watcher = Watcher(
        cfg,
        db,
        rpc=_StubRpc({}),  # type: ignore[arg-type]
        ws=None,
        prices=_StubPrices(),  # type: ignore[arg-type]
        queue=queue,
    )
    watcher._sem = asyncio.Semaphore(4)
    await watcher._handle(Notification(wallet=WALLET, signature="plain", logs=["Program log: transfer"]))
    assert watcher.stats.filtered == 1
    assert watcher.stats.queued == 0
    assert queue.stats()["total"] == 0
    queue.close()
    db.close()


async def test_watcher_ignores_unparseable_transaction(tmp_path):
    db = Database(tmp_path / "watch2.db")
    db.upsert_trader(Trader(address=WALLET, source="test"))
    cfg = Config()
    queue = FetchQueue(tmp_path / "queue2.db")
    watcher = Watcher(
        cfg,
        db,
        rpc=_StubRpc({"sig-x": {"slot": 1, "meta": {"err": None}}}),  # type: ignore[arg-type]
        ws=None,
        prices=_StubPrices(),  # type: ignore[arg-type]
        queue=queue,
    )
    watcher._sem = asyncio.Semaphore(4)
    await watcher._handle(Notification(wallet=WALLET, signature="sig-x", logs=[_SWAP_LOG]))
    await watcher.drain_once()
    assert watcher.stats.trades == 0
    # Decoded nothing, so the signature is finished rather than retried forever.
    assert queue.stats()["done"] == 1
    queue.close()
    db.close()
