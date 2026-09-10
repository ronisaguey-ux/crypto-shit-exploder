"""The aggregation algorithm.

Each surviving trader is a signal generator. A signal's influence is its fitness
raised to a power, decayed by age, and normalized across everything currently
pointing at the same mint. The result is a single score in -1..1; a trade fires
only when the score clears the confidence threshold.

The design goal is that a handful of genuinely good traders should be able to
outvote a crowd of mediocre ones, which is what the `weight_power` exponent buys.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from ..config import AggregationConfig
from ..models import AggregateDecision, Side, Signal, Trader

log = logging.getLogger("cse.aggregation")


@dataclass
class _Contribution:
    signal: Signal
    weight: float


class Aggregator:
    def __init__(self, cfg: AggregationConfig):
        self.cfg = cfg
        self._seen: dict[tuple[str, str], float] = {}  # (trader, mint) -> last ts

    # --------------------------------------------------------------- weights
    def weight_of(self, fitness: float, age_seconds: float = 0.0) -> float:
        """fitness^power, decayed by age with the configured half-life."""
        if fitness <= 0:
            return 0.0
        w = fitness ** self.cfg.weight_power
        if self.cfg.signal_half_life_hours > 0 and age_seconds > 0:
            half_life = self.cfg.signal_half_life_hours * 3600.0
            w *= 0.5 ** (age_seconds / half_life)
        return w

    def is_duplicate(self, signal: Signal, now: Optional[float] = None) -> bool:
        """Same trader, same mint, inside the dedupe window."""
        now = now or signal.created_at
        key = (signal.trader, signal.mint)
        last = self._seen.get(key)
        if last is not None and (now - last) < self.cfg.dedupe_window_seconds:
            return True
        self._seen[key] = now
        return False

    # ------------------------------------------------------------- aggregate
    def aggregate_mint(
        self, mint: str, signals: Iterable[Signal], now: Optional[float] = None
    ) -> Optional[AggregateDecision]:
        """Collapse every live signal on one mint into a single decision."""
        now = now or time.time()
        contribs: list[_Contribution] = []
        for s in signals:
            if s.mint != mint:
                continue
            if s.fitness < self.cfg.min_fitness:
                continue
            age = max(0.0, now - s.created_at)
            w = self.weight_of(s.fitness, age)
            if w <= 0:
                continue
            contribs.append(_Contribution(s, w))
        if not contribs:
            return None

        total_w = sum(c.weight for c in contribs) or 1.0
        long_w = sum(c.weight for c in contribs if c.signal.direction > 0)
        short_w = sum(c.weight for c in contribs if c.signal.direction < 0)
        # Direction is +1/-1; a signal's conviction scales it (clamped to +-1).
        raw = sum(
            max(-1.0, min(1.0, c.signal.direction)) * c.weight for c in contribs
        ) / total_w
        # Normalized direction alone is meaningless on thin evidence: one lone
        # signal always normalizes to +-1. Scale by total conviction so a
        # decision needs weight behind it, not just unanimity.
        conviction = total_w / (total_w + 1.0)
        score = raw * conviction

        if score >= self.cfg.confidence_threshold:
            action = "buy"
        elif score <= -self.cfg.confidence_threshold:
            action = "sell"
        else:
            action = "hold"

        return AggregateDecision(
            mint=mint,
            score=score,
            long_weight=long_w / total_w,
            short_weight=short_w / total_w,
            n_signals=len(contribs),
            action=action,
            decided_at=now,
        )

    def aggregate_all(
        self, signals: list[Signal], now: Optional[float] = None
    ) -> list[AggregateDecision]:
        now = now or time.time()
        mints = {s.mint for s in signals}
        out = []
        for mint in mints:
            d = self.aggregate_mint(mint, signals, now)
            if d is not None:
                out.append(d)
        # Strongest conviction first; only actionable decisions are interesting.
        out.sort(key=lambda d: (d.action != "hold", abs(d.score)), reverse=True)
        return out

    # -------------------------------------------------------------- signals
    def signals_from_trades(
        self,
        trades: Iterable,
        traders: dict[str, Trader],
        now: Optional[float] = None,
    ) -> list[Signal]:
        """Turn observed trades into weighted signals.

        A trader's fitness is the conviction; the side is the direction. Trades
        from ineligible traders are dropped, and duplicates are suppressed.
        """
        now = now or time.time()
        out: list[Signal] = []
        for t in trades:
            tr = traders.get(t.trader)
            if tr is None or tr.fitness < self.cfg.min_fitness:
                continue
            direction = 1.0 if t.side == Side.BUY else -1.0
            s = Signal(
                trader=t.trader,
                mint=t.mint,
                direction=direction,
                weight=self.weight_of(tr.fitness, 0.0),
                fitness=tr.fitness,
                created_at=t.observed_at or now,
            )
            if self.is_duplicate(s, now):
                continue
            out.append(s)
        return out


# Backwards-friendly alias.
SignalAggregator = Aggregator
