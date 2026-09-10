"""Tests for the paper-trading engine: slippage, fees, and PnL accounting."""
from __future__ import annotations

import random

import pytest

from cse.config import PaperConfig
from cse.db import Database
from cse.models import Side, Trade
from cse.paper import FeeModel, PaperTradingEngine, SlippageModel


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def _engine(tmp_path, **over):
    kw = dict(
        starting_equity_usd=10_000,
        position_pct=0.10,
        slippage_bps=100,
        dynamic_slippage=False,
        slippage_jitter=[1.0, 1.0],  # deterministic for assertions
        latency_seconds=0.0,
        mev_tax_bps=0.0,
        base_fee_lamports=0,
        priority_fee_lamports=0,
    )
    kw.update(over)
    cfg = PaperConfig(**kw)
    return PaperTradingEngine(cfg, Database(tmp_path / "p.db")), cfg


# ── slippage model ─────────────────────────────────────────────────────────

def test_buy_fill_is_adverse():
    m = SlippageModel(slippage_bps=100, dynamic=False, jitter=(1.0, 1.0), rng=random.Random(0))
    f = m.fill(Side.BUY, price=100.0, amount=1.0)
    assert f.effective_price == pytest.approx(101.0)  # pays 1% more
    assert f.slippage_bps == pytest.approx(100.0)


def test_sell_fill_is_adverse():
    m = SlippageModel(slippage_bps=100, dynamic=False, jitter=(1.0, 1.0), rng=random.Random(0))
    f = m.fill(Side.SELL, price=100.0, amount=1.0)
    assert f.effective_price == pytest.approx(99.0)  # receives 1% less


def test_dynamic_slippage_grows_with_size():
    m = SlippageModel(slippage_bps=100, dynamic=True, jitter=(1.0, 1.0),
                      max_liquidity_impact_pct=0.05, rng=random.Random(0))
    small = m.bps_for(notional_usd=1_000, pool_liquidity_usd=1_000_000)
    large = m.bps_for(notional_usd=40_000, pool_liquidity_usd=1_000_000)
    assert large > small


def test_dynamic_slippage_never_below_fixed_floor():
    m = SlippageModel(slippage_bps=150, dynamic=True, jitter=(1.0, 1.0), rng=random.Random(0))
    assert m.bps_for(1.0, 1_000_000) >= 150


def test_jitter_stays_in_band():
    m = SlippageModel(slippage_bps=100, dynamic=False, jitter=(0.5, 1.5), rng=random.Random(7))
    seen = {m.bps_for(1000, None) for _ in range(200)}
    assert min(seen) >= 50 - 1e-9
    assert max(seen) <= 150 + 1e-9


# ── fee model ──────────────────────────────────────────────────────────────

def test_fee_usd_accounts_for_both_fees():
    fm = FeeModel(base_fee_lamports=5000, priority_fee_lamports=50_000,
                  lamports_per_sol=1_000_000_000, sol_price_usd=150.0)
    assert fm.fee_usd() == pytest.approx(55_000 / 1e9 * 150.0)
    assert fm.round_trip_usd() == pytest.approx(2 * fm.fee_usd())


def test_priority_multiplier_scales_priority_only():
    fm = FeeModel(base_fee_lamports=5000, priority_fee_lamports=50_000,
                  lamports_per_sol=1e9, sol_price_usd=150.0)
    base = fm.fee_usd(1.0)
    hot = fm.fee_usd(5.0)
    assert hot > base
    # base fee is fixed; only priority scales
    assert hot - base == pytest.approx((50_000 * 4) / 1e9 * 150.0)


# ── engine round trips ─────────────────────────────────────────────────────

def test_round_trip_realizes_pnl(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=2.0, amount=1000))
    assert closed is not None
    assert closed.pnl_usd > 0
    assert closed.pnl_pct > 0


def test_losing_round_trip_is_negative(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=0.5, amount=1000))
    assert closed is not None
    assert closed.pnl_usd < 0


def test_sell_without_position_is_ignored(tmp_path):
    eng, cfg = _engine(tmp_path)
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=1.0, amount=10))
    assert closed is None


def test_malformed_trade_is_skipped(tmp_path):
    eng, cfg = _engine(tmp_path)
    assert eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=0.0, amount=0.0)) is None


def test_fees_and_slippage_reduce_pnl(tmp_path):
    """The same price move must net less than the naive (frictionless) number."""
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=2.0, amount=1000))
    naive = (2.0 - 1.0) * 1000
    assert closed.pnl_usd < naive


def test_win_rate_tracks_outcomes(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=2.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m2", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m2", side=Side.SELL, price=0.5, amount=1000))
    p = eng.portfolio("w1")
    assert p.n_trades == 2
    assert p.win_rate == pytest.approx(0.5)


def test_max_drawdown_is_recorded(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=0.1, amount=1000))
    assert eng.portfolio("w1").max_drawdown_pct > 0


def test_latency_makes_entry_worse(tmp_path):
    eng, cfg = _engine(tmp_path, latency_seconds=5.0)
    # Latency applies adverse drift, so the simulated fill is above the observed price.
    eff, _, _, _ = eng.simulate_fill(Side.BUY, price=1.0, amount=100)
    assert eff > 1.0


def test_summary_sorted_by_equity(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=3.0, amount=1000))
    eng.on_trade(Trade(trader="w2", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w2", mint="m1", side=Side.SELL, price=0.5, amount=1000))
    s = eng.summary()
    assert s[0]["trader"] == "w1"
