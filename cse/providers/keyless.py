"""Keyless trader discovery: fee-payers of recent DEX swaps, read from the chain.

No signup and no API key. This is what makes the documented "it runs for free"
claim true: the pool of wallets to shadow is seeded from recent mainnet
transactions rather than a leaderboard that wants a key.

The sample is unranked by design — these wallets carry no reported PnL, so they
sort below any keyed provider's output. That is the honest position: with zero
keys you get a small live pool, not a leaderboard.
"""
from __future__ import annotations

import logging
import os

from ..models import Trader
from ..rpc import RpcPool, default_endpoints
from .base import Provider

log = logging.getLogger("cse.providers.keyless")

#: Programs busy enough that their recent transactions are a decent sample of
#: active wallets. Jupiter is the aggregator most retail flow passes through;
#: Raydium adds wallets that never touched the aggregator.
SEED_PROGRAMS = (
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
)


def fee_payer(tx: dict) -> str:
    """The wallet that paid for a transaction — accountKeys[0], dict or string."""
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return ""
    k = keys[0]
    return k.get("pubkey", "") if isinstance(k, dict) else str(k)


class KeylessProvider(Provider):
    """Seed the pool from fee-payers of recent swaps. No key required."""

    name = "keyless"
    requires_key = False

    def __init__(
        self,
        api_key: str = "",
        *,
        max_wallets: int | None = None,
        sigs_per_program: int = 100,
        **kw,
    ):
        super().__init__(api_key=api_key, **kw)
        if max_wallets is None:
            max_wallets = int(os.getenv("CSE_KEYLESS_WALLETS", "60"))
        self.max_wallets = max(1, max_wallets)
        self.sigs_per_program = max(1, sigs_per_program)

    async def top_traders(self, limit: int = 1000, window_days: int = 30, **kw) -> list[Trader]:
        want = min(limit, self.max_wallets) if limit else self.max_wallets
        pool = RpcPool(default_endpoints(), timeout=self.timeout, max_attempts=4)
        seen: dict[str, int] = {}
        try:
            for program in SEED_PROGRAMS:
                if len(seen) >= want:
                    break
                try:
                    rows = await pool.get_signatures_for_address(
                        program, limit=self.sigs_per_program
                    )
                except Exception as e:  # a bad endpoint must not sink discovery
                    log.warning("keyless: signatures for %s failed: %s", program[:8], e)
                    continue
                for row in rows:
                    if len(seen) >= want:
                        break
                    sig = (row or {}).get("signature")
                    if not sig or pool.already_seen(sig):
                        continue
                    pool.mark_seen(sig)
                    try:
                        tx = await pool.get_transaction(sig)
                    except Exception:
                        continue
                    if not tx:
                        continue
                    wallet = fee_payer(tx)
                    if wallet:
                        seen[wallet] = seen.get(wallet, 0) + 1
        finally:
            await pool.aclose()

        # Most-repeated fee payer first: a wallet paying for several swaps in the
        # sample is more likely to be an active trader than a one-off.
        ranked = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:want]
        log.info("keyless: seeded %d wallets from %d programs", len(ranked), len(SEED_PROGRAMS))
        return [Trader(address=addr, source="keyless") for addr, _ in ranked]
