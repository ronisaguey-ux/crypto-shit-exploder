"""Real execution-cost extraction from an observed transaction.

``paper/slippage.py`` guesses impact from a fitted square-root curve. This module
replaces the guess with the actual market state, read out of the very transaction
the trader executed in:

    meta.preTokenBalances / postTokenBalances  -> every SPL vault, pool vaults included
    meta.preBalances      / postBalances       -> native SOL accounts
    meta.fee, meta.computeUnitsConsumed        -> what the trader really paid

An AMM's own vaults appear in those balance lists, so the reserve on each side of
the pool is observable at the exact slot of the trade. With reserves in hand the
constant-product curve is *exact* for AMM venues (Raydium v4/CPMM, Pump.fun,
Meteora AMM, PumpSwap), so a copy-trade of any size can be priced against the real
curve rather than an exponent someone fitted. Concentrated-liquidity and routed
venues do not follow that curve; there the trade's own realised slippage is the
anchor and the result is labelled an estimate instead of being dressed up as exact.

Everything here comes out of the transaction, so it costs no extra RPC call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .swapdecode import (
    LAMPORTS_PER_SOL,
    SOL_MINT,
    STABLE_MINTS,
    _account_keys,
    _decimals,
    _raw_amount,
    _ui_amount,
)

log = logging.getLogger("cse.exec")

#: Base fee every transaction pays regardless of priority. Priority fee is
#: therefore meta.fee minus this, times the number of signatures.
BASE_FEE_LAMPORTS = 5_000

#: Published standard swap-fee tier per venue, in basis points. Concentrated
#: liquidity venues set this per pool, so their entry is a default and is flagged
#: by ``fee_is_exact`` rather than presented as the pool's real tier.
DEX_FEE_BPS: dict[str, float] = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": 25.0,   # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": 25.0,    # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": 25.0,    # Raydium CLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": 100.0,   # Pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": 25.0,    # PumpSwap
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": 20.0,    # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": 30.0,    # Meteora AMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": 30.0,    # Orca Whirlpool
}

#: Venues whose pools follow x*y=k, so constant-product maths is exact there.
_CP_VENUES = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
}
# pump.fun (6EF8rre...) is deliberately NOT here. Its bonding curve does not hold
# a plain two-vault x*y=k pair, so ``_market_side`` picks up an unrelated WSOL
# account and the result was labelled "exact constant_product". It now falls
# through to the empirical/estimate path, which is the honest label.

DEX_NAMES = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_amm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter_v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "jupiter_v4",
}


@dataclass
class ExecutionInfo:
    """What the observed trader actually paid in fees and compute."""

    total_fee_lamports: int = 0
    base_fee_lamports: int = 0
    priority_fee_lamports: int = 0
    compute_units: int = 0
    n_signatures: int = 0
    fee_usd: float = 0.0
    priority_fee_usd: float = 0.0
    dex: Optional[str] = None
    dex_fee_bps: float = 0.0
    fee_is_exact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_fee_lamports": self.total_fee_lamports,
            "base_fee_lamports": self.base_fee_lamports,
            "priority_fee_lamports": self.priority_fee_lamports,
            "compute_units": self.compute_units,
            "n_signatures": self.n_signatures,
            "fee_usd": round(self.fee_usd, 8),
            "priority_fee_usd": round(self.priority_fee_usd, 8),
            "dex": self.dex,
            "dex_fee_bps": self.dex_fee_bps,
            "fee_is_exact": self.fee_is_exact,
        }


@dataclass
class PoolState:
    """Observable market state on the two sides of the pool the trade used."""

    mint: str
    quote_mint: str
    #: UI units of the traded mint on the market side, after the trade.
    reserve_base: float = 0.0
    #: UI units of the quote asset on the market side, after the trade.
    reserve_quote: float = 0.0
    #: Raw base-unit (u64) reserves. These are the exact on-chain integers and
    #: are what the curve maths must use; the UI floats above are derived.
    reserve_base_raw: int = 0
    reserve_quote_raw: int = 0
    #: Decimal scaling of each raw reserve, so raw -> UI stays lossless.
    base_decimals: int = 0
    quote_decimals: int = 0
    #: USD value of the quote-side reserve — the depth impact is measured against.
    quote_depth_usd: float = 0.0
    #: Both sides of the pool in USD, for reporting.
    liquidity_usd: float = 0.0
    #: Marginal price at the post-trade reserves, quote per base.
    mid_price: float = 0.0
    #: USD value of one quote unit, so a quote-denominated mid can be compared
    #: against a USD executed price. 1.0 for stables, sol_price_usd for SOL.
    quote_usd_price: float = 1.0
    model: str = "unknown"      # constant_product | empirical | unknown
    confidence: str = "none"    # exact | estimate | none
    dex: Optional[str] = None
    fee_bps: float = 0.0

    @property
    def usable(self) -> bool:
        return (
            self.reserve_base > 0
            and self.reserve_quote > 0
            and self.quote_depth_usd > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "quote_mint": self.quote_mint,
            "reserve_base": self.reserve_base,
            "reserve_quote": self.reserve_quote,
            "quote_depth_usd": round(self.quote_depth_usd, 4),
            "liquidity_usd": round(self.liquidity_usd, 4),
            "mid_price": self.mid_price,
            "model": self.model,
            "confidence": self.confidence,
            "dex": self.dex,
            "fee_bps": self.fee_bps,
        }


@dataclass
class MevInfo:
    """Adverse-ordering evidence for the slot the trade landed in."""

    same_slot_swaps: int = 0
    sandwich_suspect: bool = False
    estimated_tax_bps: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "same_slot_swaps": self.same_slot_swaps,
            "sandwich_suspect": self.sandwich_suspect,
            "estimated_tax_bps": round(self.estimated_tax_bps, 4),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------- venues
def identify_dex(tx: dict) -> Optional[str]:
    """Program id of the first known DEX the transaction invoked."""
    for key in _account_keys(tx):
        if key in DEX_NAMES:
            return key
    return None


def dex_fee_bps(program_id: Optional[str]) -> tuple[float, bool]:
    """(fee_bps, is_exact) for a venue. CLMM tiers vary per pool, so not exact."""
    if not program_id:
        return 0.0, False
    bps = DEX_FEE_BPS.get(program_id)
    if bps is None:
        return 0.0, False
    return bps, program_id in _CP_VENUES


# ----------------------------------------------------------------------- fees
def extract_execution(tx: dict, *, sol_price_usd: float = 150.0) -> ExecutionInfo:
    """Pull real fee and compute numbers out of a parsed transaction."""
    meta = tx.get("meta") or {}
    n_sigs = len((tx.get("transaction") or {}).get("signatures") or [])

    total = int(meta.get("fee") or 0)
    base = BASE_FEE_LAMPORTS * max(n_sigs, 1)
    priority = max(0, total - base)
    cu = int(meta.get("computeUnitsConsumed") or 0)

    program_id = identify_dex(tx)
    bps, exact = dex_fee_bps(program_id)

    return ExecutionInfo(
        total_fee_lamports=total,
        base_fee_lamports=base,
        priority_fee_lamports=priority,
        compute_units=cu,
        n_signatures=n_sigs,
        fee_usd=(total / LAMPORTS_PER_SOL) * sol_price_usd,
        priority_fee_usd=(priority / LAMPORTS_PER_SOL) * sol_price_usd,
        dex=DEX_NAMES.get(program_id, program_id),
        dex_fee_bps=bps,
        fee_is_exact=exact,
    )


# -------------------------------------------------------------------- reserves
def _market_side_raw(meta: dict, mint: str, tracked: set[str]) -> tuple[int, int]:
    """Raw (u64) reserve of ``mint`` on the market side, plus its decimals.

    Mirrors :func:`_market_side` but keeps the integer value intact, so the
    bonding-curve maths never touches a float until the very last conversion.
    Returns ``(raw_total, decimals)``.
    """
    pre: dict[int, int] = {}
    post: dict[int, tuple[Optional[str], int]] = {}
    decimals = 0
    for key, is_post in (("preTokenBalances", False), ("postTokenBalances", True)):
        for entry in meta.get(key) or []:
            if entry.get("mint") != mint:
                continue
            idx = entry.get("accountIndex")
            if idx is None:
                continue
            raw = _raw_amount(entry)
            if is_post:
                post[idx] = (entry.get("owner"), raw)
                if not decimals:
                    decimals = _decimals(entry)
            else:
                pre[idx] = raw
    total = 0
    for idx, (owner, after) in post.items():
        if owner in tracked:
            continue
        if after == pre.get(idx, 0):
            continue  # untouched holder, not part of this trade's market
        total += after
    return total, decimals


def _market_side(meta: dict, mint: str, tracked: set[str]) -> float:
    """Reserve of ``mint`` on the market side of the trade.

    The market side is every account holding this mint whose balance *moved* and
    whose owner is not one of the wallets we track. In a swap that is the pool
    vault — or, on a routed trade, each vault it hopped through — so the sum is
    the depth the trade actually met. Accounts that did not move are ignored,
    which keeps unrelated holders out of the total.
    """
    pre: dict[int, float] = {}
    post: dict[int, tuple[Optional[str], float]] = {}
    for key, is_post in (("preTokenBalances", False), ("postTokenBalances", True)):
        for entry in meta.get(key) or []:
            if entry.get("mint") != mint:
                continue
            idx = entry.get("accountIndex")
            if idx is None:
                continue
            if is_post:
                post[idx] = (entry.get("owner"), _ui_amount(entry))
            else:
                pre[idx] = _ui_amount(entry)

    total = 0.0
    for idx, (owner, after) in post.items():
        if owner in tracked:
            continue
        if abs(after - pre.get(idx, 0.0)) <= 0:
            continue  # untouched holder, not part of this trade's market
        total += after
    return total


def _quote_usd(quote_mint: str, amount: float, sol_price_usd: float) -> float:
    if quote_mint in STABLE_MINTS:
        return abs(amount)
    if quote_mint == SOL_MINT:
        return abs(amount) * sol_price_usd
    return 0.0


def extract_pool_state(
    tx: dict,
    mint: str,
    quote_mint: str,
    *,
    wallets: Optional[Iterable[str]] = None,
    sol_price_usd: float = 150.0,
) -> PoolState:
    """Read the pool's two-sided reserves out of the observed transaction."""
    meta = tx.get("meta") or {}
    tracked = set(wallets or ())
    program_id = identify_dex(tx)
    bps, _ = dex_fee_bps(program_id)

    state = PoolState(
        mint=mint,
        quote_mint=quote_mint,
        dex=DEX_NAMES.get(program_id, program_id),
        fee_bps=bps,
    )

    reserve_base = _market_side(meta, mint, tracked)
    reserve_quote = _market_side(meta, quote_mint, tracked)
    if reserve_base <= 0 or reserve_quote <= 0:
        return state

    # Raw u64 reserves are the source of truth; the UI floats are derived from
    # them so the curve maths and the reported numbers cannot disagree.
    base_raw, base_dec = _market_side_raw(meta, mint, tracked)
    quote_raw, quote_dec = _market_side_raw(meta, quote_mint, tracked)

    state.reserve_base = reserve_base
    state.reserve_quote = reserve_quote
    state.reserve_base_raw = base_raw
    state.reserve_quote_raw = quote_raw
    state.base_decimals = base_dec
    state.quote_decimals = quote_dec
    state.mid_price = reserve_quote / reserve_base

    depth = _quote_usd(quote_mint, reserve_quote, sol_price_usd)
    state.quote_depth_usd = depth
    state.liquidity_usd = depth * 2.0  # balanced pool: both sides are worth the same
    # One quote unit in USD. The mid below is quote-per-base, so comparing it to
    # a USD executed price needs this conversion or the result is meaningless.
    state.quote_usd_price = (depth / reserve_quote) if reserve_quote else 1.0

    if program_id in _CP_VENUES:
        state.model = "constant_product"
        state.confidence = "exact"
    else:
        state.model = "empirical"
        state.confidence = "estimate"
    return state


# ------------------------------------------------------------------ AMM maths
def _ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division. denominator must be > 0."""
    return -(-numerator // denominator)


def constant_product_out_exact(
    reserve_in: int, reserve_out: int, amount_in: int, fee_bps: float = 0.0
) -> int:
    """Exact x*y=k output in raw integer units, rounding in the pool's favour.

    This is the on-chain formula, in integers end to end. The float version
    below is kept for callers holding already-scaled UI amounts, but anything
    derived from lamports must go through here: converting a u64 balance to a
    float first loses the low bits, and across a bonding curve those losses
    accumulate into a reserve that no longer matches the chain.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0
    if fee_bps > 0:
        # fee is taken in integer basis points, rounded up like the programs do.
        fee = _ceil_div(int(round(amount_in * fee_bps)), 10_000)
        amount_in = amount_in - fee
    if amount_in <= 0:
        return 0
    # out = reserve_out * amount_in / (reserve_in + amount_in), ceil-rounded so
    # the pool never pays out a lamport more than the invariant allows.
    numerator = reserve_out * amount_in
    denominator = reserve_in + amount_in
    if denominator <= 0:
        return 0
    return _ceil_div(numerator, denominator)


def constant_product_out(
    reserve_in: float, reserve_out: float, amount_in: float, fee_bps: float = 0.0
) -> float:
    """Exact x*y=k output for an input, net of the venue fee (UI-unit float).

    Prefer :func:`constant_product_out_exact` when the inputs are raw integer
    balances; this wrapper exists for the UI-denominated path and for tests.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0.0
    net_in = amount_in * (1.0 - fee_bps / 10_000.0)
    if net_in <= 0:
        return 0.0
    return reserve_out * net_in / (reserve_in + net_in)


def price_impact_bps_exact(
    reserve_in: int, reserve_out: int, amount_in: int, fee_bps: float = 0.0
) -> float:
    """Price impact of ``amount_in`` in integer space, reported in bps.

    Integer arithmetic throughout: the only float is the final bps ratio, so
    the impact itself carries no accumulated rounding error.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0.0
    out = constant_product_out_exact(reserve_in, reserve_out, amount_in, fee_bps=0.0)
    if out <= 0:
        return 0.0
    # mid = reserve_out / reserve_in ; avg = out / amount_in
    # impact = (mid/avg - 1) = (reserve_out*amount_in) / (reserve_in*out) - 1
    num = reserve_out * amount_in
    den = reserve_in * out
    if den <= 0:
        return 0.0
    return max(0.0, (num / den - 1.0) * 10_000.0)


def price_impact_bps(
    reserve_in: float, reserve_out: float, amount_in: float, fee_bps: float = 0.0
) -> float:
    """Price impact of ``amount_in`` against the real curve, in bps.

    Impact is the gap between the pool's marginal price (the reserve ratio) and
    the average price the trade actually achieves. It excludes the venue fee,
    which is accounted separately so the two costs stay distinguishable.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0.0
    out = constant_product_out(reserve_in, reserve_out, amount_in, fee_bps=0.0)
    if out <= 0:
        return 0.0
    mid = reserve_out / reserve_in
    avg = out / amount_in
    if avg <= 0:
        return 0.0
    return max(0.0, (mid / avg - 1.0) * 10_000.0)


def effective_bps(
    pool: PoolState,
    notional_usd: float,
    *,
    observed_slippage_bps: Optional[float] = None,
) -> tuple[float, str]:
    """Total adverse bps for a copy-trade of ``notional_usd``, and its basis.

    Exact path (constant-product pool): walk the real curve. Estimate path
    (CLMM / routed): scale the trader's own realised slippage by the square root
    of the size ratio — the standard depth approximation — and label it as an
    estimate rather than selling it as measurement.
    """
    if not pool.usable or notional_usd <= 0:
        return 0.0, "none"

    if pool.model == "constant_product":
        # Prefer the raw-integer curve when both raw reserves are present: it is
        # the exact on-chain formula. Fall back to the UI-float walk only when a
        # transaction carried no raw amounts.
        if pool.reserve_base_raw > 0 and pool.reserve_quote_raw > 0:
            # notional_usd is a USD size; scale it into raw quote units using the
            # observed quote depth, then walk the integer curve.
            quote_ui = pool.reserve_quote
            if quote_ui > 0:
                amount_in_raw = int(notional_usd / pool.quote_usd_price) if pool.quote_usd_price else 0
                if amount_in_raw > 0:
                    impact = price_impact_bps_exact(
                        pool.reserve_quote_raw, pool.reserve_base_raw, amount_in_raw, fee_bps=0.0
                    )
                    return impact + pool.fee_bps, "exact"
        impact = price_impact_bps(
            pool.quote_depth_usd, pool.reserve_base, notional_usd, fee_bps=0.0
        )
        return impact + pool.fee_bps, "exact"

    if observed_slippage_bps and observed_slippage_bps > 0:
        ratio = max(notional_usd / pool.quote_depth_usd, 1e-9)
        scaled = observed_slippage_bps * (ratio ** 0.5)
        return min(scaled, 10_000.0), "estimate"
    return 0.0, "none"


def observed_slippage_bps(
    pool: PoolState, executed_price: float, side_is_buy: bool
) -> Optional[float]:
    """Slippage the tracked trader actually paid, from their fill vs the pool mid.

    The mid is taken after their trade, which biases this slightly *downward*
    (the pool has already moved). That direction is deliberate: it never flatters
    the trader, so a copy-trade priced from it is not made to look better than it
    was.
    """
    if not pool.usable or executed_price <= 0 or pool.mid_price <= 0:
        return None
    # ``mid_price`` is quote-per-base; ``executed_price`` is USD-per-base. Convert
    # the mid to USD first, or this compares two different units and returns a
    # meaningless ~1.36e6 bps for every non-constant-product venue.
    mid_usd = pool.mid_price * pool.quote_usd_price
    if mid_usd <= 0:
        return None
    if side_is_buy:
        # Bought above the mid = paid the impact.
        return max(0.0, (executed_price / mid_usd - 1.0) * 10_000.0)
    return max(0.0, (1.0 - executed_price / mid_usd) * 10_000.0)


# ------------------------------------------------------------------------ MEV
def detect_mev(tx: dict, slot_swaps: list[dict]) -> MevInfo:
    """Look for a sandwich around this transaction inside its own slot.

    A sandwich needs the victim bracketed by swaps on the same mint from one
    attacker wallet within the slot. We claim a suspect only when that shape is
    present; a busy slot on its own is not evidence of anything.
    """
    info = MevInfo()
    if not slot_swaps:
        return info
    info.same_slot_swaps = len(slot_swaps)

    this_sig = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
    meta = tx.get("meta") or {}
    this_mints: set[str] = set()
    for entry in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        if entry.get("mint"):
            this_mints.add(entry["mint"])

    by_wallet: dict[str, int] = {}
    for other in slot_swaps:
        if other.get("signature") == this_sig:
            continue
        if not (set(other.get("mints") or ()) & this_mints):
            continue
        signer = other.get("signer")
        if signer:
            by_wallet[signer] = by_wallet.get(signer, 0) + 1

    repeat = {w: n for w, n in by_wallet.items() if n >= 2}
    if repeat:
        info.sandwich_suspect = True
        info.notes.append(f"{len(repeat)} wallet(s) traded the same mint twice in this slot")
    return info


# ------------------------------------------------------------------ reporting
def assert_slippage_within(
    intended_price: float, mark_price: float, max_bps: float = 500.0
) -> None:
    """Reject a decoded price that is implausibly far from the pool's mark.

    The reserves decoder computes slippage but never refuses a fill, so a trade
    whose decoded price is an order of magnitude off the pool mid (a mis-signed
    balance delta, the wrong vault picked, a fee-inclusive leg) was accepted and
    paper-filled as if it were real. This is the gate that rejects it.

    Uses a relative-to-max denominator so a zero or negative mark fails closed
    instead of raising ZeroDivisionError.
    """
    if max_bps <= 0:
        raise ValueError("max_bps must be > 0")
    denom = max(abs(mark_price), 1e-9)
    if abs(intended_price - mark_price) / denom > max_bps / 10_000.0:
        raise ValueError(
            f"price deviates beyond slippage limit: "
            f"intended={intended_price!r} mark={mark_price!r} max_bps={max_bps!r}"
        )


def enrich_trade(tx: dict, trade, *, wallets: Optional[Iterable[str]] = None,
                 sol_price_usd: float = 150.0, quote_mint: str = SOL_MINT,
                 max_price_deviation_bps: float = 500.0):
    """Attach real pool depth, exact slippage and real fees to a decoded Trade.

    Mutates ``trade`` in place and returns it. Prices only get *better* grounded:
    the executed price stays the trader's own fill, while ``effective_price`` is
    recomputed against the real curve so a copy at a different size is priced
    honestly rather than inheriting the whale's rate.
    """
    pool = extract_pool_state(
        tx, trade.mint, quote_mint, wallets=wallets, sol_price_usd=sol_price_usd
    )
    exec_info = extract_execution(tx, sol_price_usd=sol_price_usd)

    if pool.usable:
        trade.pool_liquidity_usd = pool.quote_depth_usd
        # Refuse an implausible fill before any of it reaches the paper engine.
        # The mark is quote-per-base, so convert it to USD to match trade.price.
        mark_usd = pool.mid_price * pool.quote_usd_price
        if mark_usd > 0 and trade.price > 0:
            assert_slippage_within(trade.price, mark_usd, max_bps=max_price_deviation_bps)

    observed = observed_slippage_bps(pool, trade.price, trade.side.value == "buy")
    notional = trade.notional_usd
    bps, basis = effective_bps(pool, notional, observed_slippage_bps=observed)

    # Fall back to the trader's own realised slip when the curve is unavailable —
    # it is measured, not modelled, and strictly better than a fixed guess.
    if basis == "none" and observed:
        bps, basis = observed, "observed"

    trade.slippage_bps = round(bps, 4)
    trade.fees_usd = round(exec_info.fee_usd, 8)
    trade.dex = exec_info.dex
    trade.slippage_basis = basis
    trade.pool = pool.to_dict()
    trade.execution = exec_info.to_dict()
    return trade

