"""Test JSON-RPC batching: one HTTP POST carrying N getTransaction calls.

If this works, throughput is bounded by HTTP round-trips, not by per-method
rate limits, which is the difference between 492k fetches/day and millions.
"""
import asyncio
import json
import time

import httpx

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://solana.publicnode.com",
]
OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}


async def get_sigs(client, url, n=200):
    r = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [JUPITER_V6, {"limit": n}],
        },
    )
    body = r.json()
    if "error" in body:
        return []
    return [x["signature"] for x in body.get("result") or []]


async def try_batch(client, url, sigs, size):
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": "getTransaction", "params": [s, OPTS]}
        for i, s in enumerate(sigs[:size])
    ]
    t0 = time.time()
    try:
        r = await client.post(url, json=payload)
    except Exception as e:
        return {"size": size, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    dt = time.time() - t0
    if r.status_code != 200:
        return {"size": size, "http": r.status_code, "body": r.text[:150]}
    body = r.json()
    if isinstance(body, dict):
        return {"size": size, "error": f"not-a-batch: {str(body)[:150]}"}
    ok = sum(1 for item in body if isinstance(item, dict) and item.get("result"))
    errs = [item.get("error") for item in body if isinstance(item, dict) and item.get("error")]
    return {
        "size": size,
        "returned": len(body),
        "ok": ok,
        "elapsed_s": round(dt, 2),
        "per_s": round(len(body) / dt, 1) if dt > 0 else None,
        "first_err": str(errs[0])[:110] if errs else None,
    }


async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        for url in ENDPOINTS:
            sigs = await get_sigs(c, url)
            print(f"\n{url}  (sigs={len(sigs)})")
            if not sigs:
                print("  no signatures")
                continue
            for size in (1, 10, 25, 50, 100):
                res = await try_batch(c, url, sigs, size)
                print(f"  batch={size:<4} {json.dumps(res)}")
                await asyncio.sleep(1.0)


asyncio.run(main())
