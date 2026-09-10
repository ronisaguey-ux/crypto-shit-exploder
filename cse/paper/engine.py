"""The paper-trading engine.

Replays a tracked wallet's trades through a shadow portfolio with a deliberately
pessimistic fill model: slippage (fixed or liquidity-derived), an MEV
adverse-selection tax, a latency assumption, and Solana network fees. The engine
is the arbiter of truth for the scoring phase — no signal is scored until it has
survived the simulation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import PaperConfig
from ..db import Database
from ..models import ClosedTrade, Position, Side, Trade
from .costs import FeeModel
from .slippage import SlippageModel

log = logging.getLogger("cse.paper")


@dataclass
class ShadowPortfolio:
    """Per-trader simulated equity and open positions."""

    trader: str
    equity_usd: float
    starting_equity_usd: float
    realized_pnl_usd: float = 0.0
    fees_paid_usd: float = 0.0
    n_trades: int = 0
    n_wins: int = 0
    peak_equity_usd: float = 0.0
    max_drawdown_pct: float = 0.0

    def mark(self) -> None:
        if self.equity_usd > self.peak_equity_usd:
            self.peak_equity_usd = self.equity_usd
        if self.peak_equity_usd > 0:
            dd = (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd
            if dd > self.max_drawdown_pct:
                self.max_drawdown_pct = dd

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades else 0.0


class PaperTradingEngine:
    def __init__(
        self,
        cfg: PaperConfig,
        db: Database,
        slippage: Optional[SlippageModel] = None,
        fees: Optional[FeeModel] = None,
    ):
        self.cfg = cfg
        self.db = db
        self.slippage = slippage or SlippageModel(
            slippage_bps=cfg.slippage_bps,
            dynamic=cfg.dynamic_slippage,
            jitter=tuple(cfg.slippage_jitter),
            mev_tax_bps=cfg.mev_tax_bps,
            max_liquidity_impact_pct=cfg.max_liquidity_impact_pct,
        )
        self.fees = fees or FeeModel(
            base_fee_lamports=cfg.base_fee_lamports,
            priority_fee_lamports=cfg.priority_fee_lamports,
            lamports_per_sol=cfg.lamports_per_sol,
            sol_price_usd=cfg.sol_price_usd,
        )
        self._portfolios: dict[str, ShadowPortfolio] = {}

    # ------------------------------------------------------------- portfolio
    def portfolio(self, trader: str) -> ShadowPortfolio:
        p = self._portfolios.get(trader)
        if p is None:
            p = ShadowPortfolio(
                trader=trader,
                equity_usd=self.cfg.starting_equity_usd,
                starting_equity_usd=self.cfg.starting_equity_usd,
                peak_equity_usd=self.cfg.starting_equity_usd,
            )
            self._portfolios[trader] = p
        return p

    # ------------------------------------------------------------------ fill
    def simulate_fill(
        self,
        side: Side,
        price: float,
        amount: float,
        pool_liquidity_usd: Optional[float] = None,
        priority_multiplier: float = 1.0,
    ) -> tuple[float, float, float, float]:
        """Return (effective_price, slippage_bps, fees_usd, mev_tax_usd)."""
        f = self.slippage.fill(side, price, amount, pool_liquidity_usd)
        fee = self.fees.fee_usd(priority_multiplier)
        return f.effective_price, f.slippage_bps, fee, f.mev_tax_usd

    # --------------------------------------------------------------- signals
    def on_trade(self, trade: Trade, priority_multiplier: float = 1.0) -> Optional[ClosedTrade]:
        """Feed one observed trade through the shadow portfolio.

        A BUY opens (or adds to) a position; a SELL closes the whole position and
        realizes PnL. Returns the ClosedTrade when a round trip completes.
        """
        if trade.price <= 0 or trade.amount <= 0:
            log.debug("skipping malformed trade %s", trade.id)
            return None

        # Model our own latency: we fill at the price *after* the signal ages.
        price = self._apply_latency(trade.price)

        port = self.portfolio(trade.trader)
        pos = self.db.get_position(trade.trader, trade.mint)

        if trade.side == Side.BUY:
            return self._open(trade, price, port, pos, priority_multiplier)
        return self._close(trade, price, port, pos, priority_multiplier)

    def _apply_latency(self, price: float) -> float:
        """Pessimistic latency: assume the price moved against us while we filled."""
        if self.cfg.latency_seconds <= 0:
            return price
        # Deterministic, side-agnostic adverse drift: we never assume the delay helps.
        drift_bps = min(self.cfg.latency_seconds, 10.0) * 2.0  # ~2 bps per second, capped
        return price * (1 + drift_bps / 10_000.0)

    def _open(
        self,
        trade: Trade,
        price: float,
        port: ShadowPortfolio,
        pos: Optional[Position],
        priority_multiplier: float,
    ) -> None:
        budget = port.equity_usd * self.cfg.position_pct
        if budget <= 0:
            return
        eff_price, slip_bps, fee, mev = self.simulate_fill(
            Side.BUY, price, budget / price, trade.pool_liquidity_usd, priority_multiplier
        )
        amount = budget / eff_price
        cost = amount * eff_price + fee + mev
        if cost > port.equity_usd:
            return

        if pos is None:
            pos = Position(
                trader=trade.trader,
                mint=trade.mint,
                entry_price=eff_price,
                amount=amount,
                entry_fees_usd=fee + mev,
                entry_slippage_bps=slip_bps,
                opened_at=trade.observed_at or time.time(),
            )
            self.db.open_position(pos)
        else:
            # Average into the existing position.
            total = pos.amount + amount
            pos.entry_price = (pos.entry_price * pos.amount + eff_price * amount) / total
            pos.amount = total
            pos.entry_fees_usd += fee + mev
            self.db.open_position(pos)

        # Cash out the full cost now; the position is marked back in on close, so
        # equity never double-counts the tokens (slippage is already in eff_price).
        port.equity_usd -= cost
        port.fees_paid_usd += fee + mev
        self.db.insert_trade(_simulated(trade, eff_price, slip_bps, fee, mev))
        return None

    def _close(
        self,
        trade: Trade,
        price: float,
        port: ShadowPortfolio,
        pos: Optional[Position],
        priority_multiplier: float,
    ) -> Optional[ClosedTrade]:
        if pos is None:
            return None  # nothing to sell
        eff_price, slip_bps, fee, mev = self.simulate_fill(
            Side.SELL, price, pos.amount, trade.pool_liquidity_usd, priority_multiplier
        )
        proceeds = pos.amount * eff_price
        exit_costs = fee + mev
        pnl = proceeds - exit_costs - (pos.amount * pos.entry_price) - pos.entry_fees_usd
        cost_basis = pos.amount * pos.entry_price + pos.entry_fees_usd

        port.equity_usd += proceeds - exit_costs
        port.realized_pnl_usd += pnl
        port.fees_paid_usd += exit_costs
        port.n_trades += 1
        if pnl > 0:
            port.n_wins += 1
        port.mark()

        closed = ClosedTrade(
            trader=trade.trader,
            mint=trade.mint,
            entry_price=pos.entry_price,
            exit_price=eff_price,
            amount=pos.amount,
            pnl_usd=pnl,
            pnl_pct=(pnl / cost_basis) if cost_basis else 0.0,
            fees_usd=pos.entry_fees_usd + exit_costs,
            hold_seconds=max(0.0, (trade.observed_at or time.time()) - pos.opened_at),
            opened_at=pos.opened_at,
            closed_at=trade.observed_at or time.time(),
        )
        self.db.close_trade(closed)
        self.db.delete_position(trade.trader, trade.mint)
        self.db.insert_trade(_simulated(trade, eff_price, slip_bps, fee, mev))
        return closed

    # ---------------------------------------------------------------- report
    def summary(self) -> list[dict]:
        return [
            {
                "trader": p.trader,
                "equity_usd": round(p.equity_usd, 2),
                "return_pct": round(
                    (p.equity_usd - p.starting_equity_usd) / p.starting_equity_usd * 100, 2
                )
                if p.starting_equity_usd
                else 0.0,
                "realized_pnl_usd": round(p.realized_pnl_usd, 2),
                "trades": p.n_trades,
                "win_rate": round(p.win_rate, 4),
                "max_drawdown_pct": round(p.max_drawdown_pct, 4),
                "fees_paid_usd": round(p.fees_paid_usd, 2),
            }
            for p in sorted(self._portfolios.values(), key=lambda x: x.equity_usd, reverse=True)
        ]


def _simulated(trade: Trade, eff_price: float, slip_bps: float, fee: float, mev: float) -> Trade:
    """A copy of the observed trade carrying the simulated fill details."""
    return Trade(
        trader=trade.trader,
        mint=trade.mint,
        side=trade.side,
        price=trade.price,
        amount=trade.amount,
        signature=trade.signature,
        slot=trade.slot,
        pool_liquidity_usd=trade.pool_liquidity_usd,
        observed_at=trade.observed_at,
        effective_price=eff_price,
        fees_usd=fee,
        slippage_bps=slip_bps,
        mev_tax_usd=mev,
    )
