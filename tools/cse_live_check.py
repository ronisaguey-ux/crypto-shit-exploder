"""Real-endpoint verification of the free stack (no API keys)."""
import asyncio
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.prices import PriceOracle
from cse.rpc import RpcPool, default_endpoints
from cse.swapdecode import decode_trades

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


async def main():
    out = {}
    pool = RpcPool(default_endpoints(), timeout=30, max_attempts=8)

    # 1. keyless RPC health
    try:
        out["health"] = await pool.get_health()
    except Exception as e:
        out["health"] = f"FAILED: {e}"

    # 2. recent real signatures from the Jupiter program (keyless)
    sigs = []
    try:
        rows = await pool.get_signatures_for_address(JUPITER_V6, limit=10)
        sigs = [r["signature"] for r in rows]
        out["signatures_fetched"] = len(sigs)
    except Exception as e:
        out["signatures_error"] = str(e)

    # 3. fetch + decode real transactions with no DEX-specific parser
    decoded = []
    fetched = 0
    for sig in sigs[:6]:
        try:
            tx = await pool.get_transaction(sig)
        except Exception:
            continue
        if not tx:
            continue
        fetched += 1
        trades = decode_trades(tx, sol_price_usd=150.0)
        for t in trades:
            decoded.append(
                {
                    "sig": (t.signature or "")[:16],
                    "mint": t.mint[:12],
                    "side": t.side.value,
                    "amount": round(t.amount, 6),
                    "price": round(t.price, 10),
                }
            )
    out["tx_fetched"] = fetched
    out["decoded_trades"] = len(decoded)
    out["sample"] = decoded[:5]

    # 4. keyless pricing
    oracle = PriceOracle(ttl_seconds=60)
    try:
        prices = await oracle.get([BONK, WIF])
        out["prices"] = {
            k: {"usd": v.usd, "liq": round(v.liquidity_usd, 2), "src": v.source}
            for k, v in prices.items()
        }
    except Exception as e:
        out["prices_error"] = str(e)

    out["rpc_stats"] = pool.summary()["stats"]
    out["endpoints"] = pool.summary()["endpoints"]
    await pool.aclose()
    await oracle.aclose()
    print(json.dumps(out, indent=2))


asyncio.run(main())
