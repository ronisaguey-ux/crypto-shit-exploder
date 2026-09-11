"""Transaction-cost model: Solana base fee + priority fee + Jito tip, in USD.

A flat 5,000-lamport + fixed-priority model understates real mainnet cost badly:
during congestion the priority fee is a bidding war (compute-unit price in the
hundreds of thousands of micro-lamports) and guaranteed inclusion is bought with
a Jito bundle tip. Modelling neither made paper fills look better than live ones,
which is the one direction a simulator must never err.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeeModel:
    base_fee_lamports: int = 5000
    priority_fee_lamports: int = 50_000
    lamports_per_sol: int = 1_000_000_000
    sol_price_usd: float = 150.0
    #: Jito bundle tip, in lamports, paid on top of the transaction fee when the
    #: order is submitted as a bundle. 0 disables the tip term.
    jito_tip_lamports: int = 0
    #: Compute-unit price in micro-lamports; when > 0 it drives the priority fee
    #: from real compute consumption instead of the flat field above.
    compute_unit_price_micro_lamports: int = 0
    compute_unit_limit: int = 200_000

    @property
    def fee_sol(self) -> float:
        return (
            self.base_fee_lamports + self.priority_fee_lamports + self.jito_tip_lamports
        ) / self.lamports_per_sol

    def priority_fee_lamports_for(
        self, priority_multiplier: float = 1.0, compute_units: int | None = None
    ) -> int:
        """Priority fee in lamports for one transaction.

        When a compute-unit price is configured the fee is derived from real
        compute consumption (price * units / 1e6), which is how the network
        actually charges; otherwise the flat field is used and scaled by the
        congestion multiplier.
        """
        if self.compute_unit_price_micro_lamports > 0:
            units = compute_units if compute_units is not None else self.compute_unit_limit
            base = (self.compute_unit_price_micro_lamports * units) // 1_000_000
            return int(base * max(priority_multiplier, 0.0))
        return int(self.priority_fee_lamports * max(priority_multiplier, 0.0))

    def fee_usd(
        self,
        priority_multiplier: float = 1.0,
        *,
        compute_units: int | None = None,
        include_jito_tip: bool = True,
    ) -> float:
        """Total fee in USD for one transaction.

        priority_multiplier lets callers model congestion (e.g. 5x during a hot
        mint). The base fee is fixed; the priority fee scales, and the Jito tip
        is included when the caller is modelling bundle submission.
        """
        prio = self.priority_fee_lamports_for(priority_multiplier, compute_units)
        lamports = self.base_fee_lamports + prio
        if include_jito_tip:
            lamports += self.jito_tip_lamports
        return (lamports / self.lamports_per_sol) * self.sol_price_usd

    def round_trip_usd(
        self,
        priority_multiplier: float = 1.0,
        *,
        compute_units: int | None = None,
        include_jito_tip: bool = True,
    ) -> float:
        """Fees for entering and exiting a position."""
        return 2.0 * self.fee_usd(
            priority_multiplier,
            compute_units=compute_units,
            include_jito_tip=include_jito_tip,
        )
