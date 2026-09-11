"""Slippage and MEV models.

The simulation is deliberately pessimistic: every fill pays slippage, a jitter
multiplier, an MEV adverse-selection tax, and network fees. A strategy that only
works with zero-cost fills is not a strategy.

Two impact models live here:

* ``sqrt`` — the legacy square-root market-impact curve. It is a *fit*, not the
  AMM's own maths, so it is used only when no pool state is available.
* ``constant_product`` — the exact x*y=k output for the order, walked against the
  real reserves. When the caller passes pool reserves (the enricher does, for
  every trade it decoded) this is the model that runs, because a linear or
  fitted curve understates impact on exactly the orders where it matters.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..models import Side


def constant_product_slippage_bps(
    reserve_in: float, reserve_out: float, amount_in: float, fee_bps: float = 0.0
) -> float:
    """Exact x*y=k price impact for ``amount_in``, in bps.

    Impact is the gap between the pool's marginal price and the average price
    the order achieves, net of the venue fee (which is accounted separately).
    Returns 0.0 when the pool is unusable rather than raising.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0.0
    net_in = amount_in * (1.0 - fee_bps / 10_000.0)
    if net_in <= 0:
        return 0.0
    out = reserve_out * net_in / (reserve_in + net_in)
    if out <= 0:
        return 0.0
    mid = reserve_out / reserve_in
    avg = out / amount_in
    if avg <= 0:
        return 0.0
    return max(0.0, (mid / avg - 1.0) * 10_000.0)


@dataclass
class Fill:
    """Result of pricing one simulated fill."""

    effective_price: float
    slippage_bps: float
    mev_tax_usd: float
    notional_usd: float


class SlippageModel:
    """Computes an adverse fill price for a given side and size.

    Three modes:
    * fixed  — `slippage_bps` applied against us, times a uniform jitter draw.
    * dynamic — slippage scales with the fraction of pool liquidity consumed,
      using a square-root market-impact curve, floored by the fixed rate.
    * exact — when ``pool_reserve_in`` / ``pool_reserve_out`` are supplied, the
      real constant-product invariant is walked instead of fitted.
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
    def bps_for(
        self,
        notional_usd: float,
        pool_liquidity_usd: float | None,
        *,
        amount_in: float | None = None,
        pool_reserve_in: float | None = None,
        pool_reserve_out: float | None = None,
        fee_bps: float = 0.0,
    ) -> float:
        base = self.slippage_bps
        # Exact path: the caller handed us real reserves, so walk the AMM curve
        # rather than fitting an exponent to it. The venue fee is added on top
        # because the invariant output is already net of nothing.
        if amount_in and pool_reserve_in and pool_reserve_out:
            exact = constant_product_slippage_bps(
                pool_reserve_in, pool_reserve_out, amount_in, fee_bps=0.0
            )
            if exact > 0:
                return (exact + fee_bps) * self.rng.uniform(*self.jitter)
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
        *,
        pool_reserve_in: float | None = None,
        pool_reserve_out: float | None = None,
        fee_bps: float = 0.0,
    ) -> Fill:
        notional = abs(price * amount)
        bps = self.bps_for(
            notional,
            pool_liquidity_usd,
            amount_in=amount,
            pool_reserve_in=pool_reserve_in,
            pool_reserve_out=pool_reserve_out,
            fee_bps=fee_bps,
        )
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
