"""Transaction-cost model: Solana base fee + priority fee, in USD."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeeModel:
    base_fee_lamports: int = 5000
    priority_fee_lamports: int = 50_000
    lamports_per_sol: int = 1_000_000_000
    sol_price_usd: float = 150.0

    @property
    def fee_sol(self) -> float:
        return (self.base_fee_lamports + self.priority_fee_lamports) / self.lamports_per_sol

    def fee_usd(self, priority_multiplier: float = 1.0) -> float:
        """Total fee in USD for one transaction.

        priority_multiplier lets callers model congestion (e.g. 5x during a hot
        mint). The base fee is fixed; only the priority fee scales.
        """
        prio = self.priority_fee_lamports * max(priority_multiplier, 0.0)
        lamports = self.base_fee_lamports + prio
        return (lamports / self.lamports_per_sol) * self.sol_price_usd

    def round_trip_usd(self, priority_multiplier: float = 1.0) -> float:
        """Fees for entering and exiting a position."""
        return 2.0 * self.fee_usd(priority_multiplier)
