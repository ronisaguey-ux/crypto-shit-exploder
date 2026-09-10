"""Benchmark the keyless RPC pool's sustained getTransaction throughput."""
import asyncio
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.rpc import RpcPool, default_endpoints

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


async def main():
    pool = RpcPool(default_endpoints(), timeout=30, max_attempts=8)
    rows = await pool.get_signatures_for_address(JUPITER_V6, limit=100)
    sigs = [r["signature"] for r in rows] * 2  # 200 fetches

    t0 = time.time()
    ok = 0
    fails = 0
    sem = asyncio.Semaphore(16)

    async def one(sig):
        nonlocal ok, fails
        async with sem:
            try:
                tx = await pool.get_transaction(sig)
                if tx is not None:
                    ok += 1
                else:
                    fails += 1
            except Exception:
                fails += 1

    await asyncio.gather(*(one(s) for s in sigs))
    dt = time.time() - t0
    print(f"fetches={len(sigs)} ok={ok} fails={fails} elapsed={dt:.1f}s rate={len(sigs)/dt:.2f}/s")
    print(f"stats={pool.summary()['stats']}")
    for e in pool.summary()["endpoints"]:
        print(f"  {e['name']}: served={e['served']} failures={e['failures']} healthy={e['healthy']}")
    await pool.aclose()


asyncio.run(main())
