"""Composite fitness scoring.

The job is to separate skill from luck. Raw PnL rewards a single lucky trade; the
composite rewards a trader whose *return distribution* is good — risk-adjusted,
consistent, and not dependent on one outlier. Every component is squashed with
tanh so no single metric can dominate, and a trader with too few trades is
rejected outright rather than scored on noise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..config import ScoringConfig
from ..models import ClosedTrade, Trader


@dataclass
class FitnessReport:
    trader: str
    fitness: float
    n_trades: int
    total_pnl_usd: float
    win_rate: float
    sharpe: float
    sortino: float
    profit_factor: float
    max_drawdown_pct: float
    avg_return: float
    std_return: float
    components: dict[str, float] = field(default_factory=dict)
    eligible: bool = True
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "trader": self.trader,
            "fitness": round(self.fitness, 6),
            "n_trades": self.n_trades,
            "total_pnl_usd": round(self.total_pnl_usd, 4),
            "win_rate": round(self.win_rate, 6),
            "sharpe": round(self.sharpe, 6),
            "sortino": round(self.sortino, 6),
            "profit_factor": round(self.profit_factor, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "avg_return": round(self.avg_return, 6),
            "std_return": round(self.std_return, 6),
            "components": {k: round(v, 6) for k, v in self.components.items()},
            "eligible": self.eligible,
            "reason": self.reason,
        }


def _max_drawdown(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    # Guard against a zero/negative peak (a blown-up account).
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - equity) / peak, 0.0)
    return float(np.max(dd)) if dd.size else 0.0


def _equity_curve(trades: list[ClosedTrade], starting: float = 1.0) -> np.ndarray:
    eq = starting
    out = [eq]
    for t in trades:
        eq += t.pnl_usd
        out.append(eq)
    return np.array(out, dtype=float)


def compute_fitness(
    trades: list[ClosedTrade],
    cfg: ScoringConfig | None = None,
    starting_equity: float = 10_000.0,
) -> FitnessReport:
    """Compute a composite fitness score for one trader's closed trades."""
    cfg = cfg or ScoringConfig()
    trader = trades[0].trader if trades else ""
    n = len(trades)
    pnls = np.array([t.pnl_usd for t in trades], dtype=float)

    if n < cfg.min_trades:
        return FitnessReport(
            trader=trader, fitness=-1.0, n_trades=n,
            total_pnl_usd=float(pnls.sum()) if n else 0.0,
            win_rate=float((pnls > 0).mean()) if n else 0.0,
            sharpe=0.0, sortino=0.0, profit_factor=0.0, max_drawdown_pct=0.0,
            avg_return=0.0, std_return=0.0,
            eligible=False, reason=f"only {n} trades (< {cfg.min_trades})",
        )

    # Return per trade, relative to the capital committed to it.
    cost_basis = np.array([max(t.amount * t.entry_price, 1e-9) for t in trades], dtype=float)
    returns = pnls / cost_basis

    avg = float(returns.mean())
    std = float(returns.std(ddof=1)) if n > 1 else 0.0
    win_rate = float((pnls > 0).mean())

    downside = returns[returns < 0]
    dstd = float(downside.std(ddof=1)) if downside.size > 1 else 0.0

    ann = math.sqrt(max(cfg.annualization, 1.0))
    # Zero variance is the BEST case when the mean is positive — a trader with a
    # perfectly steady return is not "no signal", it is the strongest signal.
    if std > 1e-12:
        sharpe = (avg / std) * ann
    elif avg > 1e-12:
        sharpe = 10.0
    elif avg < -1e-12:
        sharpe = -10.0
    else:
        sharpe = 0.0
    sortino = (avg / dstd) * ann if dstd > 1e-12 else (10.0 if avg > 0 else 0.0)

    gains = float(pnls[pnls > 0].sum())
    losses = float(-pnls[pnls < 0].sum())
    if losses > 1e-12:
        profit_factor = gains / losses
    else:
        profit_factor = 10.0 if gains > 0 else 0.0

    equity = _equity_curve(trades, starting_equity)
    mdd = _max_drawdown(equity)

    # Squash every unbounded metric into 0..1. Drawdown is already 0..1 but is
    # inverted (a smaller drawdown is better).
    s = max(cfg.tanh_scale, 1e-9)
    comp = {
        "sharpe": 0.5 * (1 + math.tanh(sharpe / s)),
        "sortino": 0.5 * (1 + math.tanh(sortino / s)),
        "win_rate": win_rate,
        "profit_factor": 0.5 * (1 + math.tanh((profit_factor - 1.0) / s)),
        "max_drawdown": 1.0 - min(mdd, 1.0),
    }
    w = cfg.weights
    total_w = sum(w.get(k, 0.0) for k in comp) or 1.0
    fitness = sum(comp[k] * w.get(k, 0.0) for k in comp) / total_w

    # A trader that never made money cannot outrank one that did.
    if gains <= 0:
        fitness = min(fitness, 0.0)

    return FitnessReport(
        trader=trader,
        fitness=float(fitness),
        n_trades=n,
        total_pnl_usd=float(pnls.sum()),
        win_rate=win_rate,
        sharpe=sharpe,
        sortino=sortino,
        profit_factor=profit_factor,
        max_drawdown_pct=mdd,
        avg_return=avg,
        std_return=std,
        components=comp,
        eligible=True,
        reason="",
    )


def score_traders(
    trades_by_trader: dict[str, list[ClosedTrade]],
    cfg: ScoringConfig | None = None,
    starting_equity: float = 10_000.0,
) -> list[FitnessReport]:
    """Score every trader, best first. Ineligible traders sort last."""
    reports = [
        compute_fitness(ts, cfg, starting_equity)
        for ts in trades_by_trader.values()
        if ts
    ]
    reports.sort(key=lambda r: (r.eligible, r.fitness), reverse=True)
    return reports


def apply_scores(
    traders: list[Trader],
    reports: list[FitnessReport],
    db=None,
    now: float | None = None,
) -> list[Trader]:
    """Write each report's fitness back onto the Trader and optionally the DB."""
    import time as _time

    now = now or _time.time()
    by_addr = {r.trader: r for r in reports}
    for t in traders:
        r = by_addr.get(t.address)
        if r is None:
            continue
        t.fitness = r.fitness
        t.fitness_updated_at = now
        if db is not None:
            db.set_fitness(t.address, r.fitness, now)
    return traders
