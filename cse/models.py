"""Core domain models for crypto-shit-exploder."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Trader:
    """A wallet we are tracking."""

    address: str
    source: str = "unknown"
    label: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    # Leaderboard snapshot (USD) at discovery time.
    reported_realized_pnl: float = 0.0
    reported_roi: float = 0.0
    reported_win_rate: float = 0.0
    reported_volume_usd: float = 0.0
    reported_trades: int = 0
    discovered_at: float = field(default_factory=time.time)
    # Rolling fitness, filled by the scoring phase.
    fitness: float = 0.0
    fitness_updated_at: float = 0.0
    active: bool = True

    @property
    def wallet(self) -> str:
        return self.address

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trader":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Trade:
    """A trade observed from a tracked wallet, or one we simulated."""

    trader: str
    mint: str
    side: Side
    # Observed on-chain execution price (USD per token).
    price: float
    # Token amount (UI units).
    amount: float
    # Slot/signature for on-chain trades.
    signature: Optional[str] = None
    slot: Optional[int] = None
    # USD value of the pool/liquidity at the time, if known (for dynamic slippage).
    pool_liquidity_usd: Optional[float] = None
    observed_at: float = field(default_factory=time.time)
    # Filled in by the paper engine for simulated trades.
    effective_price: Optional[float] = None
    fees_usd: float = 0.0
    slippage_bps: float = 0.0
    mev_tax_usd: float = 0.0
    # Venue and the real market state read out of the transaction (see reserves.py).
    dex: Optional[str] = None
    slippage_basis: str = "none"  # exact | observed | estimate | none
    pool: Optional[dict[str, Any]] = None
    execution: Optional[dict[str, Any]] = None
    # "observed" is what the trader did; "simulated" is our shadow fill of it.
    # They share a signature on purpose, so the row key has to include this.
    kind: str = "observed"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def notional_usd(self) -> float:
        return abs(self.amount * self.price)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        d = dict(d)
        d["side"] = Side(d["side"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Position:
    """A simulated open position held by a single trader's shadow portfolio."""

    trader: str
    mint: str
    entry_price: float
    amount: float
    entry_fees_usd: float = 0.0
    entry_slippage_bps: float = 0.0
    opened_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ClosedTrade:
    """A realized round-trip for a trader's shadow portfolio."""

    trader: str
    mint: str
    entry_price: float
    exit_price: float
    amount: float
    pnl_usd: float
    pnl_pct: float
    fees_usd: float
    hold_seconds: float
    opened_at: float
    closed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClosedTrade":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Signal:
    """A weighted directional signal emitted by a trader for the aggregator."""

    trader: str
    mint: str
    direction: float  # +1 buy, -1 sell, scaled by conviction
    weight: float
    fitness: float
    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AggregateDecision:
    """The portfolio-level decision derived from many signals."""

    mint: str
    score: float  # weighted average in -1..1
    long_weight: float
    short_weight: float
    n_signals: int
    action: str  # buy | sell | hold
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
