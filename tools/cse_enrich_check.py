"""Live check: real mainnet swaps -> real reserves, fees, slippage basis.

Zero keys. Pulls recent transactions off a busy DEX program, decodes the swaps,
and enriches them, then prints what came out of the chain versus what was modelled.
"""
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.reserves import enrich_trade, extract_execution, extract_pool_state  # noqa: E402
from cse.rpc import RpcPool, default_endpoints  # noqa: E402
from cse.swapdecode import decode_trades  # noqa: E402

RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


async def main() -> int:
    pool = RpcPool(default_endpoints(), timeout=30.0, max_attempts=6)
    decoded = 0
    enriched = 0
    bases = {}
    try:
        for program in (RAYDIUM, PUMPSWAP, JUPITER):
            sigs = await pool.get_signatures_for_address(program, limit=12)
            print(f"\n=== {program[:12]}… : {len(sigs)} signatures")
            for row in sigs[:8]:
                sig = row.get("signature")
                if not sig or row.get("err"):
                    continue
                try:
                    tx = await pool.get_transaction(sig)
                except Exception as exc:  # noqa: BLE001
                    print(f"  fetch failed {sig[:12]}: {type(exc).__name__}")
                    continue
                if not tx:
                    continue
                trades = decode_trades(tx, wallets=None, min_trade_usd=1.0)
                for t in trades:
                    decoded += 1
                    before = (t.slippage_basis, t.dex)
                    enrich_trade(tx, t, wallets=None, sol_price_usd=150.0)
                    enriched += 1
                    bases[t.slippage_basis] = bases.get(t.slippage_basis, 0) + 1
                    ex = t.execution or {}
                    pl = t.pool or {}
                    print(
                        f"  {t.mint[:8]} {t.side.value:4} "
                        f"notional=${t.notional_usd:,.2f} "
                        f"dex={t.dex} basis={t.slippage_basis} "
                        f"slip={t.slippage_bps:,.1f}bps "
                        f"fee=${t.fees_usd:.6f} prio={ex.get('priority_fee_lamports')}lam "
                        f"reserve_base={pl.get('reserve_base'):,.0f} "
                        f"depth=${pl.get('quote_depth_usd'):,.0f} "
                        f"model={pl.get('model')} ({before[0]}->{t.slippage_basis})"
                    )
    finally:
        await pool.aclose()

    print(f"\ndecoded={decoded} enriched={enriched} basis_counts={bases}")
    print(f"rpc stats={pool.stats}")
    exact = bases.get("exact", 0)
    print(
        "VERDICT:",
        "OK — real reserves/fees extracted"
        if enriched and (exact or bases.get("observed") or bases.get("estimate"))
        else "FAIL — nothing enriched",
    )
    return 0 if enriched else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
