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
    """F-022: this used to call simulate_fill with latency_seconds set but never
    assert that latency was the cause — any slippage at all passed it. Compare
    the resulting ENTRY PRICE with latency off and on, so a mutation that drops
    the latency term from on_trade fails here."""
    off, _ = _engine(tmp_path, latency_seconds=0.0)
    on, _ = _engine(tmp_path, latency_seconds=5.0)
    off.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=100))
    on.on_trade(Trade(trader="w2", mint="m1", side=Side.BUY, price=1.0, amount=100))
    # Both spend the same budget, so the tell is the entry price: the latency
    # model fills higher, buying fewer tokens for the same cash.
    assert on.db.get_position("w2", "m1").entry_price > off.db.get_position("w1", "m1").entry_price


def test_summary_sorted_by_equity(tmp_path):
    eng, cfg = _engine(tmp_path)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=3.0, amount=1000))
    eng.on_trade(Trade(trader="w2", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    eng.on_trade(Trade(trader="w2", mint="m1", side=Side.SELL, price=0.5, amount=1000))
    s = eng.summary()
    assert s[0]["trader"] == "w1"


# ── exact-value accounting (F-004: the mutations these must kill) ──────────
# Every assertion below pins an exact number rather than a direction, so zeroing
# the MEV tax, flipping the latency sign, dropping an exit cost or changing the
# composite formula all fail the suite instead of passing silently.

def test_latency_is_adverse_on_both_sides(tmp_path):
    """A mutation that applies the drift with one sign must fail here."""
    eng, cfg = _engine(tmp_path, latency_seconds=5.0)
    buy = eng._apply_latency(1.0, Side.BUY)
    sell = eng._apply_latency(1.0, Side.SELL)
    # 5s * 2bps/s = 10 bps = 0.001
    assert buy == pytest.approx(1.001)
    assert sell == pytest.approx(0.999)
    assert buy > 1.0 > sell


def test_latency_is_a_noop_at_zero(tmp_path):
    eng, cfg = _engine(tmp_path, latency_seconds=0.0)
    assert eng._apply_latency(1.0, Side.BUY) == 1.0
    assert eng._apply_latency(1.0, Side.SELL) == 1.0


def test_mev_tax_is_charged_and_scales_with_notional(tmp_path):
    """Zeroing mev_tax_bps must change the fill; 25 bps of notional is exact."""
    eng, cfg = _engine(tmp_path, mev_tax_bps=25, slippage_bps=0, dynamic_slippage=False)
    _, _, _, mev = eng.simulate_fill(Side.BUY, price=1.0, amount=10_000)
    assert mev == pytest.approx(10_000 * 25 / 10_000.0)  # 25 USD
    _, _, _, mev2 = eng.simulate_fill(Side.BUY, price=1.0, amount=1_000)
    assert mev2 == pytest.approx(mev / 10.0)


def test_mev_tax_is_zero_when_configured_off(tmp_path):
    eng, cfg = _engine(tmp_path, mev_tax_bps=0, slippage_bps=0, dynamic_slippage=False)
    _, _, _, mev = eng.simulate_fill(Side.BUY, price=1.0, amount=10_000)
    assert mev == 0.0


def test_round_trip_pnl_is_exact_with_known_costs(tmp_path):
    """entry 1.0 -> exit 2.0, 100 bps slip, no fee/mev/latency.

    The engine sizes by BUDGET (position_pct * equity), not by the observed
    amount: budget = 10000 * 0.10 = 1000 USD. At an effective entry of 1.01 that
    buys 1000/1.01 units, which exit at 1.98 -> 960.396 USD, so PnL is
    960.396 - 1000 = -39.60.
    """
    eng, cfg = _engine(tmp_path, slippage_bps=100, dynamic_slippage=False,
                       slippage_jitter=[1.0, 1.0], mev_tax_bps=0, latency_seconds=0.0,
                       base_fee_lamports=0, priority_fee_lamports=0)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=2.0, amount=1000))
    units = 1000.0 / 1.01
    assert closed.entry_price == pytest.approx(1.01)
    assert closed.exit_price == pytest.approx(1.98)
    assert closed.amount == pytest.approx(units)
    assert closed.pnl_usd == pytest.approx(units * 1.98 - 1000.0)


def test_exit_costs_are_subtracted_from_proceeds(tmp_path):
    """Dropping the exit fee from the PnL formula must fail this."""
    eng, cfg = _engine(tmp_path, slippage_bps=0, dynamic_slippage=False,
                       slippage_jitter=[1.0, 1.0], mev_tax_bps=0, latency_seconds=0.0,
                       base_fee_lamports=5000, priority_fee_lamports=50_000)
    eng.on_trade(Trade(trader="w1", mint="m1", side=Side.BUY, price=1.0, amount=1000))
    closed = eng.on_trade(Trade(trader="w1", mint="m1", side=Side.SELL, price=1.0, amount=1000))
    fee = (5000 + 50_000) / 1e9 * 150.0          # 0.00825 USD per tx
    assert closed is not None
    # flat price, so the only thing that can make PnL non-zero is the fees
    assert closed.pnl_usd == pytest.approx(-2 * fee)
    assert eng.portfolio("w1").fees_paid_usd == pytest.approx(2 * fee)
