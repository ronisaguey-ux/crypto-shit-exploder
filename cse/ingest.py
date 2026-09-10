"""Trade ingestion: webhook payloads and Helius enhanced websocket frames.

Both paths land in the same place: a list of `Trade` objects fed to the paper
engine. Parsing is defensive — a malformed payload is skipped, never fatal,
because a webhook stream is untrusted input arriving at high rate.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .models import Side, Trade

log = logging.getLogger("cse.ingest")

WSOL = "So11111111111111111111111111111111111111112"


def parse_helius_webhook(payload: Any, sol_price_usd: float = 0.0) -> list[Trade]:
    """Parse a Helius `enhanced` webhook body (a list of transactions)."""
    if isinstance(payload, dict):
        payload = payload.get("transactions") or payload.get("data") or [payload]
    if not isinstance(payload, list):
        return []
    out: list[Trade] = []
    for tx in payload:
        if not isinstance(tx, dict):
            continue
        t = parse_helius_swap(tx, sol_price_usd)
        if t is not None:
            out.append(t)
    return out


def parse_helius_swap(tx: dict[str, Any], sol_price_usd: float = 0.0) -> Optional[Trade]:
    """One enhanced SWAP payload -> Trade.

    The feePayer is the trader. Side is inferred from the SOL leg: SOL leaving the
    wallet means a buy, SOL arriving means a sell. Price is derived from the SOL
    leg when a SOL/USD price is known.
    """
    fee_payer = tx.get("feePayer")
    if not fee_payer:
        return None
    if (tx.get("type") or "").upper() != "SWAP":
        # Webhooks can be configured for multiple types; only swaps matter here.
        if not tx.get("tokenTransfers"):
            return None

    transfers = tx.get("tokenTransfers") or []
    native = tx.get("nativeTransfers") or []

    sol_out = sum(
        int(n.get("amount") or 0) for n in native if n.get("fromUserAccount") == fee_payer
    )
    sol_in = sum(
        int(n.get("amount") or 0) for n in native if n.get("toUserAccount") == fee_payer
    )

    legs = [t for t in transfers if t.get("mint") and t.get("mint") != WSOL]
    if not legs:
        return None
    leg = max(legs, key=lambda t: abs(float(t.get("tokenAmount") or 0)))
    amount = abs(float(leg.get("tokenAmount") or 0))
    if amount <= 0:
        return None

    side = Side.BUY if sol_out >= sol_in else Side.SELL
    sol_delta = abs(sol_out - sol_in) / 1e9
    price = (sol_delta / amount) * sol_price_usd if sol_price_usd > 0 and sol_delta > 0 else 0.0

    return Trade(
        trader=str(fee_payer),
        mint=str(leg["mint"]),
        side=side,
        price=price,
        amount=amount,
        signature=tx.get("signature"),
        slot=tx.get("slot"),
        observed_at=float(tx.get("timestamp") or 0) or None,
    )


def parse_ws_notification(msg: dict[str, Any], sol_price_usd: float = 0.0) -> list[Trade]:
    """Parse a `transactionNotification` frame from Helius Enhanced WebSockets."""
    if msg.get("method") != "transactionNotification":
        return []
    result = (msg.get("params") or {}).get("result") or {}
    # The ws frame carries signature/slot; the parsed tx may be nested under
    # `transaction` or arrive separately depending on transactionDetails.
    tx = result.get("transaction") or result
    if not isinstance(tx, dict):
        return []
    tx = dict(tx)
    # The frame's signature/slot are authoritative for this notification.
    tx["signature"] = result.get("signature") or tx.get("signature")
    tx["slot"] = result.get("slot") or tx.get("slot")
    t = parse_helius_swap(tx, sol_price_usd)
    return [t] if t else []


def filter_tracked(trades: Iterable[Trade], tracked: set[str]) -> list[Trade]:
    """Keep only trades from wallets we are shadowing."""
    if not tracked:
        return list(trades)
    return [t for t in trades if t.trader in tracked]
