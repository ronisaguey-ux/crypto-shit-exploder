"""Decode swaps out of a Solana transaction without per-DEX parsers.

Rather than pattern-matching Jupiter / Raydium / Pump.fun / Meteora instruction
layouts (which change, and which would need a new branch per launchpad), this
reads the transaction's own balance accounting:

    meta.preTokenBalances / meta.postTokenBalances  -> per-owner SPL deltas
    meta.preBalances      / meta.postBalances       -> per-account SOL deltas

Whatever a swap did internally, the wallet's net position change is what a
copy-trader actually experiences. Diffing balances is DEX-agnostic, survives new
launchpads, and gives the *realized on-chain price* directly from the counter
leg (SOL or a stablecoin), with no external price feed required.

A wallet whose token balance went up while SOL/stable went down bought that
token; the reverse is a sell.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

from .models import Side, Trade

log = logging.getLogger("cse.decode")

#: Wrapped SOL. Native SOL shows up in pre/postBalances, not token balances.
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

#: Assets we treat as the *quote* leg rather than the traded "shitcoin".
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",  # USDS
}
QUOTE_MINTS = STABLE_MINTS | {SOL_MINT}

#: Ignore dust: a "trade" smaller than this many USD is not worth shadowing.
MIN_TRADE_USD = 1.0


def _raw_amount(entry: dict[str, Any]) -> int:
    """Raw base-unit amount from a token-balance entry, as an exact integer.

    ``uiAmount`` is a float and a lossy one for large balances; ``amount`` is the
    raw u64 as a decimal string. Reserve maths uses this so the integer value
    never round-trips through a float.
    """
    amt = entry.get("uiTokenAmount") or {}
    raw = amt.get("amount")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _decimals(entry: dict[str, Any]) -> int:
    amt = entry.get("uiTokenAmount") or {}
    try:
        return int(amt.get("decimals", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _ui_amount(entry: dict[str, Any]) -> float:
    """UI amount from a token-balance entry, tolerating a missing uiAmount."""
    amt = entry.get("uiTokenAmount") or {}
    ui = amt.get("uiAmount")
    if ui is not None:
        try:
            return float(ui)
        except (TypeError, ValueError):
            pass
    raw = amt.get("amount")
    decimals = amt.get("decimals", 0) or 0
    try:
        return float(raw) / (10 ** int(decimals))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _account_keys(tx: dict) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    out: list[str] = []
    for k in keys:
        if isinstance(k, str):
            out.append(k)
        elif isinstance(k, dict):
            out.append(k.get("pubkey", ""))
        else:
            out.append("")
    return out


def _token_deltas(meta: dict) -> dict[str, dict[str, float]]:
    """owner -> mint -> net UI delta across the transaction."""
    deltas: dict[str, dict[str, float]] = {}

    def collect(key: str, sign: float) -> None:
        for entry in meta.get(key) or []:
            owner = entry.get("owner")
            mint = entry.get("mint")
            if not owner or not mint:
                continue
            deltas.setdefault(owner, {})
            deltas[owner][mint] = deltas[owner].get(mint, 0.0) + sign * _ui_amount(entry)

    collect("preTokenBalances", -1.0)
    collect("postTokenBalances", 1.0)
    return deltas


def _sol_deltas(tx: dict, meta: dict) -> dict[str, float]:
    """account pubkey -> net SOL delta (fees included)."""
    keys = _account_keys(tx)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    out: dict[str, float] = {}
    for i, key in enumerate(keys):
        if not key or i >= len(pre) or i >= len(post):
            continue
        out[key] = (post[i] - pre[i]) / LAMPORTS_PER_SOL
    return out


def _leg_usd(mint: str, amount: float, sol_price_usd: float) -> Optional[float]:
    """USD value of a balance leg, when the asset has a known value."""
    if mint in STABLE_MINTS:
        return abs(amount)
    if mint == SOL_MINT:
        return abs(amount) * sol_price_usd
    return None


def decode_trades(
    tx: dict,
    *,
    wallets: Optional[Iterable[str]] = None,
    sol_price_usd: float = 150.0,
    min_trade_usd: float = MIN_TRADE_USD,
) -> list[Trade]:
    """Extract one Trade per (wallet, traded mint) from a parsed transaction.

    Returns an empty list for failed transactions, plain transfers, and dust.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []  # a failed swap moved nothing
    signature = tx.get("transaction", {}).get("signatures", [None])[0]
    slot = tx.get("slot")
    block_time = tx.get("blockTime")

    token_deltas = _token_deltas(meta)
    sol_deltas = _sol_deltas(tx, meta)
    wanted = set(wallets) if wallets else None

    fee_lamports = meta.get("fee") or 0
    fee_usd = (fee_lamports / LAMPORTS_PER_SOL) * sol_price_usd

    trades: list[Trade] = []
    owners = set(token_deltas) | set(sol_deltas)
    for owner in owners:
        if wanted is not None and owner not in wanted:
            continue
        deltas = dict(token_deltas.get(owner, {}))
        sol_delta = sol_deltas.get(owner, 0.0)
        if abs(sol_delta) > 0:
            deltas[SOL_MINT] = deltas.get(SOL_MINT, 0.0) + sol_delta

        # The traded asset is the largest non-quote leg that actually moved.
        candidates = [
            (mint, amt)
            for mint, amt in deltas.items()
            if mint not in QUOTE_MINTS and abs(amt) > 0
        ]
        if not candidates:
            continue
        mint, token_delta = max(candidates, key=lambda kv: abs(kv[1]))

        # Counter value: everything the wallet paid or received that is not the
        # traded mint. Prefer a stablecoin leg, else fall back to SOL.
        counter_usd = 0.0
        for other, amt in deltas.items():
            if other == mint or abs(amt) == 0:
                continue
            leg = _leg_usd(other, amt, sol_price_usd)
            if leg is not None:
                counter_usd += leg
        if counter_usd <= 0:
            # Token-for-token: we know the direction but not the price. Emit a
            # trade with price 0 and let the price oracle fill it in later.
            counter_usd = 0.0

        amount = abs(token_delta)
        if amount <= 0:
            continue
        price = counter_usd / amount if counter_usd > 0 else 0.0
        side = Side.BUY if token_delta > 0 else Side.SELL

        # Reject dust and fee-only noise.
        notional = counter_usd if counter_usd > 0 else 0.0
        if notional and notional < min_trade_usd:
            continue

        trades.append(
            Trade(
                trader=owner,
                mint=mint,
                side=side,
                price=price,
                amount=amount,
                signature=signature,
                slot=slot,
                observed_at=float(block_time) if block_time else time.time(),
                fees_usd=round(fee_usd, 6),
            )
        )
    return trades


#: Known DEX / aggregator program ids. Matching one of these in the logs is a
#: far stronger swap signal than string-matching log text, and it survives
#: launchpads renaming their instructions.
DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter v4
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora AMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",  # Phoenix
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",  # OpenBook
    "SoLFiHG9TfgtdUXUjWAxi3LtvYuFyDLVhBWxdMZxyCe",  # SolFi
    "obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y",  # Obric v2
}

_SWAP_WORDS = ("instruction: swap", "instruction: route", "instruction: buy", "instruction: sell")


def is_swap_candidate(logs: Optional[list[str]]) -> bool:
    """Cheap pre-filter: does this notification look like a DEX swap at all?

    Used to avoid spending a getTransaction on plain transfers, mints, and
    account housekeeping — which is most of what a busy wallet emits. Erring
    toward inclusion is deliberate: a missed swap is unrecoverable, an extra
    fetch just costs one call.
    """
    if not logs:
        return False
    for line in logs:
        for prog in DEX_PROGRAMS:
            if prog in line:
                return True
        low = line.lower()
        if any(w in low for w in _SWAP_WORDS):
            return True
    return False
