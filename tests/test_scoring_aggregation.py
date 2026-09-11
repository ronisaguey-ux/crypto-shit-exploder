"""Tests for scoring, aggregation, and payload parsing."""
from __future__ import annotations

import time

import pytest

from cse.aggregation import Aggregator
from cse.config import AggregationConfig, ScoringConfig
from cse.ingest import filter_tracked, parse_helius_swap, parse_helius_webhook, parse_ws_notification
from cse.models import ClosedTrade, Side, Signal, Trader
from cse.scoring import compute_fitness, score_traders


def _trade(trader: str, pnl: float, basis: float = 1000.0, i: int = 0) -> ClosedTrade:
    return ClosedTrade(
        trader=trader, mint=f"m{i}", entry_price=1.0, exit_price=1.0 + pnl / basis,
        amount=basis, pnl_usd=pnl, pnl_pct=pnl / basis, fees_usd=0.0,
        hold_seconds=60.0, opened_at=0.0, closed_at=float(i),
    )


# ── scoring ────────────────────────────────────────────────────────────────

def test_too_few_trades_is_ineligible():
    cfg = ScoringConfig(min_trades=20)
    r = compute_fitness([_trade("w", 10.0, i=i) for i in range(5)], cfg)
    assert not r.eligible
    assert r.fitness == -1.0


def test_consistent_winner_beats_volatile_one():
    cfg = ScoringConfig(min_trades=10)
    steady = [_trade("steady", 20.0, i=i) for i in range(20)]
    volatile = [_trade("volatile", 200.0 if i % 2 else -180.0, i=i) for i in range(20)]
    rs = compute_fitness(steady, cfg)
    rv = compute_fitness(volatile, cfg)
    assert rs.fitness > rv.fitness


def test_never_profitable_is_capped_at_zero():
    cfg = ScoringConfig(min_trades=10)
    r = compute_fitness([_trade("loser", -5.0, i=i) for i in range(20)], cfg)
    assert r.fitness <= 0.0


def test_fitness_in_unit_interval_for_profitable():
    cfg = ScoringConfig(min_trades=10)
    r = compute_fitness([_trade("w", 15.0, i=i) for i in range(20)], cfg)
    assert 0.0 <= r.fitness <= 1.0


def test_profit_factor_and_win_rate():
    cfg = ScoringConfig(min_trades=4)
    trades = [_trade("w", 100.0, i=0), _trade("w", -50.0, i=1),
              _trade("w", 100.0, i=2), _trade("w", -50.0, i=3)]
    r = compute_fitness(trades, cfg)
    assert r.win_rate == pytest.approx(0.5)
    assert r.profit_factor == pytest.approx(2.0)


def test_score_traders_sorted_best_first():
    cfg = ScoringConfig(min_trades=5)
    by = {
        "good": [_trade("good", 30.0, i=i) for i in range(10)],
        "bad": [_trade("bad", -30.0, i=i) for i in range(10)],
    }
    reports = score_traders(by, cfg)
    assert reports[0].trader == "good"


def test_zero_variance_does_not_crash():
    cfg = ScoringConfig(min_trades=5)
    r = compute_fitness([_trade("w", 10.0, i=i) for i in range(10)], cfg)
    assert r.sharpe >= 0  # std=0 guard


# ── aggregation ────────────────────────────────────────────────────────────

def _sig(trader: str, mint: str, direction: float, fitness: float, ts: float = 0.0) -> Signal:
    return Signal(trader=trader, mint=mint, direction=direction,
                  weight=fitness, fitness=fitness, created_at=ts)


def test_aggregate_below_threshold_holds():
    agg = Aggregator(AggregationConfig(confidence_threshold=0.5))
    now = 1000.0
    d = agg.aggregate_mint("m", [_sig("a", "m", 1.0, 0.2, now)], now)
    assert d is not None and d.action == "hold"


def test_aggregate_strong_buy_fires():
    agg = Aggregator(AggregationConfig(confidence_threshold=0.5, min_fitness=0.1))
    now = 1000.0
    sigs = [_sig(f"w{i}", "m", 1.0, 0.9, now) for i in range(5)]
    d = agg.aggregate_mint("m", sigs, now)
    assert d.action == "buy" and d.score > 0.5


def test_high_fitness_outvotes_crowd():
    """Two strong traders should beat eight weak ones."""
    agg = Aggregator(AggregationConfig(confidence_threshold=0.5, min_fitness=0.05,
                                       weight_power=2.0))
    now = 1000.0
    sigs = [_sig(f"strong{i}", "m", 1.0, 1.0, now) for i in range(2)]
    sigs += [_sig(f"weak{i}", "m", -1.0, 0.2, now) for i in range(8)]
    d = agg.aggregate_mint("m", sigs, now)
    assert d.score > 0  # the strong side wins on weight, not count


def test_signals_below_min_fitness_ignored():
    agg = Aggregator(AggregationConfig(min_fitness=0.5))
    now = 1000.0
    assert agg.aggregate_mint("m", [_sig("a", "m", 1.0, 0.1, now)], now) is None


def test_signal_decay_reduces_weight():
    agg = Aggregator(AggregationConfig(signal_half_life_hours=1.0))
    fresh = agg.weight_of(1.0, 0.0)
    old = agg.weight_of(1.0, 3600.0)
    assert old == pytest.approx(fresh * 0.5, rel=1e-6)


def test_dedupe_window_suppresses_repeats():
    agg = Aggregator(AggregationConfig(dedupe_window_seconds=60))
    s1 = _sig("a", "m", 1.0, 0.9, 100.0)
    s2 = _sig("a", "m", 1.0, 0.9, 110.0)
    assert agg.is_duplicate(s1, 100.0) is False
    assert agg.is_duplicate(s2, 110.0) is True


def test_signals_from_trades_filters_low_fitness():
    agg = Aggregator(AggregationConfig(min_fitness=0.3))
    traders = {"a": Trader(address="a", fitness=0.9), "b": Trader(address="b", fitness=0.1)}
    from cse.models import Trade
    trades = [
        Trade(trader="a", mint="m", side=Side.BUY, price=1.0, amount=1.0),
        Trade(trader="b", mint="m", side=Side.BUY, price=1.0, amount=1.0),
    ]
    sigs = agg.signals_from_trades(trades, traders)
    assert len(sigs) == 1 and sigs[0].trader == "a"


def test_aggregate_all_sorts_actionable_first():
    agg = Aggregator(AggregationConfig(confidence_threshold=0.5, min_fitness=0.1))
    now = 1000.0
    sigs = [_sig("a", "strong", 1.0, 0.9, now), _sig("b", "weak", 1.0, 0.2, now)]
    out = agg.aggregate_all(sigs, now)
    assert out[0].mint == "strong"


# ── ingest / parsing ───────────────────────────────────────────────────────

def _swap_payload(fee_payer="walletA", sol_out=1_000_000_000, token_amount=100.0):
    return {
        "type": "SWAP",
        "feePayer": fee_payer,
        "signature": "sig1",
        "slot": 123,
        "timestamp": 1700000000,
        "nativeTransfers": [{"fromUserAccount": fee_payer, "toUserAccount": "pool",
                             "amount": sol_out}],
        "tokenTransfers": [{"mint": "TokenMint1111111111111111111111111111111111",
                            "tokenAmount": token_amount, "fromUserAccount": "pool",
                            "toUserAccount": fee_payer}],
    }


def test_parse_swap_infers_buy():
    t = parse_helius_swap(_swap_payload(), sol_price_usd=150.0)
    assert t is not None
    assert t.side == Side.BUY
    assert t.trader == "walletA"
    assert t.amount == pytest.approx(100.0)
    assert t.price > 0  # 1 SOL / 100 tokens * $150


def test_parse_swap_infers_sell():
    p = _swap_payload()
    p["nativeTransfers"] = [{"fromUserAccount": "pool", "toUserAccount": "walletA",
                             "amount": 1_000_000_000}]
    t = parse_helius_swap(p, sol_price_usd=150.0)
    assert t is not None and t.side == Side.SELL


def test_parse_swap_without_transfers_is_none():
    assert parse_helius_swap({"feePayer": "w"}) is None


def test_parse_swap_without_fee_payer_is_none():
    assert parse_helius_swap({"tokenTransfers": [{"mint": "m", "tokenAmount": 1}]}) is None


def test_parse_webhook_list_and_dict():
    p = _swap_payload()
    assert len(parse_helius_webhook([p])) == 1
    assert len(parse_helius_webhook({"transactions": [p]})) == 1
    assert len(parse_helius_webhook(p)) == 1


def test_parse_ws_notification():
    frame = {
        "method": "transactionNotification",
        "params": {"result": {"signature": "s", "slot": 9,
                              "transaction": _swap_payload()}},
    }
    trades = parse_ws_notification(frame, sol_price_usd=150.0)
    assert len(trades) == 1
    assert trades[0].signature == "s"


def test_parse_ws_ignores_other_methods():
    assert parse_ws_notification({"method": "accountNotification"}) == []


def test_filter_tracked():
    from cse.models import Trade
    a = Trade(trader="a", mint="m", side=Side.BUY, price=1.0, amount=1.0)
    b = Trade(trader="b", mint="m", side=Side.BUY, price=1.0, amount=1.0)
    assert filter_tracked([a, b], {"a"}) == [a]
    assert filter_tracked([a, b], set()) == [a, b]


# ── exact composite fitness (F-004: the mutation this must kill) ────────────
# The audit found that changing the composite formula's weights or dropping a
# component left the suite green. This pins the arithmetic to the last digit, so
# any mutation of the weights, the tanh scale or the annualization fails.

def test_composite_fitness_matches_the_hand_computed_formula():
    import math
    from cse.config import ScoringConfig
    from cse.models import ClosedTrade
    from cse.scoring.fitness import score_traders

    cfg = ScoringConfig()
    # 20 trades (min_trades): a mix of wins and losses so every component has a
    # real denominator (an all-win set leaves profit_factor's loss leg at zero).
    pnls = [2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 2.5, -1.5, 1.0, -0.5,
            2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 2.5, -1.5, 1.0, -0.5]
    trades = []
    for i, pnl in enumerate(pnls):
        trades.append(ClosedTrade(
            trader="w1", mint=f"m{i}", entry_price=1.0, exit_price=1.0 + pnl / 10.0,
            amount=10.0, pnl_usd=pnl, pnl_pct=pnl / 10.0, fees_usd=0.0,
            hold_seconds=60.0, opened_at=0.0, closed_at=float(i),
        ))
    rep = score_traders({"w1": trades}, cfg, starting_equity=10_000)[0]

    returns = [t.pnl_usd / (t.amount * t.entry_price) for t in trades]
    n = len(returns)
    avg = sum(returns) / n
    var = sum((r - avg) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    downside = [r for r in returns if r < 0]
    win_rate = sum(1 for r in returns if r > 0) / n
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    profit_factor = gains / losses

    ann = math.sqrt(max(cfg.annualization, 1.0))
    sharpe = (avg / std) * ann
    sortino = (avg / (math.sqrt(sum((r - 0) ** 2 for r in downside) / (len(downside) - 1))) * ann) if len(downside) > 1 else (10.0 if avg > 0 else 0.0)
    s = cfg.tanh_scale
    comp = {
        "sharpe": 0.5 * (1 + math.tanh(sharpe / s)),
        "sortino": 0.5 * (1 + math.tanh(sortino / s)),
        "win_rate": win_rate,
        "profit_factor": 0.5 * (1 + math.tanh((profit_factor - 1.0) / s)),
        "max_drawdown": 1.0 - rep.max_drawdown_pct,
    }
    w = cfg.weights
    total_w = sum(w[k] for k in comp)
    expected = sum(comp[k] * w[k] for k in comp) / total_w

    assert rep.fitness == pytest.approx(expected, abs=1e-9)
    # and the components themselves, so a swapped weight is caught too
    for k in comp:
        assert rep.components[k] == pytest.approx(comp[k], abs=1e-9)
