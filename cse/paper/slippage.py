"""Slippage and MEV models.

The simulation is deliberately pessimistic: every fill pays slippage, a jitter
multiplier, an MEV adverse-selection tax, and network fees. A strategy that only
works with zero-cost fills is not a strategy.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..models import Side


@dataclass
class Fill:
    """Result of pricing one simulated fill."""

    effective_price: float
    slippage_bps: float
    mev_tax_usd: float
    notional_usd: float


class SlippageModel:
    """Computes an adverse fill price for a given side and size.

    Two modes:
    * fixed  — `slippage_bps` applied against us, times a uniform jitter draw.
    * dynamic — slippage scales with the fraction of pool liquidity consumed,
      using a square-root market-impact curve, floored by the fixed rate.
    """

    def __init__(
        self,
        slippage_bps: float = 150.0,
        dynamic: bool = True,
        jitter: tuple[float, float] = (0.5, 1.5),
        mev_tax_bps: float = 25.0,
        max_liquidity_impact_pct: float = 0.05,
        rng: random.Random | None = None,
    ):
        self.slippage_bps = slippage_bps
        self.dynamic = dynamic
        self.jitter = jitter
        self.mev_tax_bps = mev_tax_bps
        self.max_liquidity_impact_pct = max_liquidity_impact_pct
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------ core
    def bps_for(self, notional_usd: float, pool_liquidity_usd: float | None) -> float:
        base = self.slippage_bps
        if self.dynamic and pool_liquidity_usd and pool_liquidity_usd > 0:
            impact_frac = min(notional_usd / pool_liquidity_usd, self.max_liquidity_impact_pct * 4)
            # sqrt market impact: doubling size increases the penalty ~41%. The
            # dynamic term rides ON TOP of the base rate, so the base is a floor.
            dynamic_bps = base * (1.0 + (impact_frac / max(self.max_liquidity_impact_pct, 1e-9)) ** 0.5)
            base = dynamic_bps
        jitter = self.rng.uniform(*self.jitter)
        return base * jitter

    def fill(
        self,
        side: Side,
        price: float,
        amount: float,
        pool_liquidity_usd: float | None = None,
    ) -> Fill:
        notional = abs(price * amount)
        bps = self.bps_for(notional, pool_liquidity_usd)
        slip_frac = bps / 10_000.0
        if side == Side.BUY:
            effective = price * (1 + slip_frac)  # pay more
        else:
            effective = price * (1 - slip_frac)  # receive less
        mev = notional * (self.mev_tax_bps / 10_000.0)
        return Fill(
            effective_price=max(effective, 1e-18),
            slippage_bps=bps,
            mev_tax_usd=mev,
            notional_usd=notional,
        )
