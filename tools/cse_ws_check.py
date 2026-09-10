"""Live WebSocket verification: logsSubscribe -> notification -> fetch -> decode."""
import asyncio
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.rpc import RpcPool, default_endpoints
from cse.swapdecode import decode_trades
from cse.ws import SubscriptionPool, WsEndpoint

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


async def main():
    pool = SubscriptionPool(
        [
            WsEndpoint(
                "wss://api.mainnet-beta.solana.com",
                name="public-ws",
                subs_per_connection=100,
                max_connections=1,
            )
        ],
        swap_filter=True,
    )
    # The Jupiter program id is mentioned by every Jupiter swap, so this
    # guarantees traffic and proves the socket + filter + parser end to end.
    watched = await pool.start([JUPITER_V6])
    print(f"subscribed: {watched}")

    rpc = RpcPool(default_endpoints(), timeout=30, max_attempts=6)
    deadline = time.time() + 90
    seen = []
    try:
        async for note in pool.notifications():
            seen.append(note.signature)
            print(f"notification #{len(seen)} sig={note.signature[:20]} logs={len(note.logs)}")
            tx = await rpc.get_transaction(note.signature)
            if tx is None:
                print("  fetch: null (pruned/not yet available)")
            else:
                trades = decode_trades(tx, sol_price_usd=150.0)
                print(f"  fetch: ok, decoded {len(trades)} trade(s)")
                for t in trades[:3]:
                    print(
                        f"    {t.side.value} mint={t.mint[:14]} amt={t.amount:.6f} "
                        f"price=${t.price:.8f}"
                    )
                if trades:
                    break
            if time.time() > deadline:
                break
    finally:
        await pool.stop()
        await rpc.aclose()

    print(json.dumps({"notifications": len(seen), "ws": pool.summary()}, indent=2))


asyncio.run(main())
