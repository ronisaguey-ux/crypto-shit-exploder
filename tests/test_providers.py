"""Provider tests: request shaping and payload parsing, with no network.

Every provider is driven through an httpx.MockTransport so the assertions cover
the real code path (URL, params, headers, body) without touching the internet.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from cse.models import Side
from cse.providers.base import Provider, ProviderError
from cse.providers.birdeye import BirdeyeProvider, _time_frame
from cse.providers.helius import HeliusProvider, parse_helius_swap
from cse.providers.solanatracker import SolanaTrackerProvider
from cse.providers.solanatracker import _rows as st_rows
from cse.providers.vybe import VybeProvider, _resolution


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# ── base ───────────────────────────────────────────────────────────────────

def test_f_reads_nested_keys():
    d = {"period": {"realized": 12.5}, "winRate": "0.6"}
    assert Provider._f(d, "period.realized") == 12.5
    assert Provider._f(d, "winRate") == 0.6
    assert Provider._f(d, "missing", default=7.0) == 7.0


def test_available_gates_on_key():
    assert SolanaTrackerProvider(api_key="k").available() is True
    assert SolanaTrackerProvider(api_key="").available() is False


# ── solanatracker ──────────────────────────────────────────────────────────

def test_solanatracker_parses_and_sends_params():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        seen["key"] = req.headers.get("x-api-key")
        return httpx.Response(200, json={"traders": [
            {"wallet": "w1", "period": {"realized": 100.0, "roi": 1.5}, "winRate": 0.6,
             "volume": 5000, "trades": 10, "identity": {"name": "trader"}},
        ]})

    p = SolanaTrackerProvider(api_key="k", client=_client(handler))
    out = run(p.top_traders(limit=10, window_days=30, page_size=10))
    assert len(out) == 1
    assert out[0].address == "w1"
    assert out[0].reported_realized_pnl == 100.0
    assert out[0].reported_win_rate == 0.6
    assert out[0].reported_trades == 10
    assert out[0].label == "trader"
    assert seen["params"]["pnlMode"] == "adjusted"
    assert seen["params"]["days"] == "30"
    assert seen["key"] == "k"


def test_solanatracker_missing_key_raises():
    with pytest.raises(ProviderError):
        run(SolanaTrackerProvider(api_key="").top_traders())


def test_rows_unwraps_shapes():
    assert len(st_rows([{"a": 1}])) == 1
    assert len(st_rows({"data": {"traders": [{"a": 1}]}})) == 1
    assert st_rows({"nothing": 1}) == []


# ── vybe ───────────────────────────────────────────────────────────────────

def test_vybe_resolution_mapping():
    assert _resolution(1) == "1d"
    assert _resolution(7) == "7d"
    assert _resolution(30) == "30d"


def test_vybe_parses_and_sends_resolution():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        seen["key"] = req.headers.get("X-API-Key")
        return httpx.Response(200, json={"data": [
            {"traderAddress": "V1", "realizedPnlUsd": 900.0, "winRate": 0.7,
             "tradesVolumeUsd": 12345, "tradesCount": 42, "labels": ["sniper"]},
        ]})

    p = VybeProvider(api_key="k", client=_client(handler))
    out = run(p.top_traders(limit=10, window_days=30))
    assert out[0].address == "V1"
    assert out[0].reported_realized_pnl == 900.0
    assert out[0].reported_trades == 42
    assert out[0].tags == ["sniper"]
    assert seen["params"]["resolution"] == "30d"
    assert seen["key"] == "k"


# ── birdeye ────────────────────────────────────────────────────────────────

def test_birdeye_time_frame_mapping():
    assert _time_frame(3) == "3d"
    assert _time_frame(30) == "30d"
    assert _time_frame(365) == "90d"


def test_birdeye_requires_mints():
    with pytest.raises(ProviderError):
        run(BirdeyeProvider(api_key="k").top_traders())


def test_birdeye_sets_chain_header_and_dedupes_across_tokens():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["chain"] = req.headers.get("X-Chain")
        seen.setdefault("mints", []).append(req.url.params.get("address"))
        return httpx.Response(200, json={"data": {"items": [
            {"address": "B1", "realized_pnl": 50.0, "win_rate": 0.5, "trade_count": 3},
        ]}})

    p = BirdeyeProvider(api_key="k", client=_client(handler))
    out = run(p.top_traders(limit=10, mints=["m1", "m2"]))
    assert seen["chain"] == "solana"
    assert seen["mints"] == ["m1", "m2"]
    assert len(out) == 1  # the same trader on two tokens is counted once
    assert out[0].address == "B1"


# ── helius ─────────────────────────────────────────────────────────────────

def test_helius_create_webhook_shapes_body():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        seen["key"] = req.url.params.get("api-key")
        return httpx.Response(200, json={"webhookID": "wh-1"})

    p = HeliusProvider(api_key="hk", client=_client(handler))
    wid = run(p.create_webhook("https://example.com/hook", ["w1", "w2"], auth_header="s3cret"))
    assert wid == "wh-1"
    assert seen["body"]["webhookURL"] == "https://example.com/hook"
    assert seen["body"]["accountAddresses"] == ["w1", "w2"]
    assert seen["body"]["transactionTypes"] == ["SWAP"]
    assert seen["body"]["webhookType"] == "enhanced"
    assert seen["body"]["authHeader"] == "s3cret"
    assert seen["key"] == "hk"


def test_helius_create_webhook_requires_key():
    with pytest.raises(ProviderError):
        run(HeliusProvider(api_key="").create_webhook("https://x/hook", ["w"]))


def test_helius_wallet_swaps_parses_buy():
    payload = [{
        "feePayer": "walletA", "signature": "sig", "slot": 1, "timestamp": 1700000000,
        "nativeTransfers": [{"fromUserAccount": "walletA", "toUserAccount": "pool",
                             "amount": 1_000_000_000}],
        "tokenTransfers": [{"mint": "Mint1", "tokenAmount": 100.0,
                            "fromUserAccount": "pool", "toUserAccount": "walletA"}],
    }]

    p = HeliusProvider(api_key="hk", client=_client(lambda req: httpx.Response(200, json=payload)))
    trades = run(p.wallet_swaps("walletA", limit=5))
    assert len(trades) == 1
    assert trades[0].side == Side.BUY
    assert trades[0].trader == "walletA"
    assert trades[0].amount == 100.0
    assert trades[0].signature == "sig"


def test_helius_parse_swap_none_without_fee_payer():
    assert parse_helius_swap({"tokenTransfers": [{"mint": "m", "tokenAmount": 1}]}) is None
