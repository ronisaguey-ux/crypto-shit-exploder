"""Probe candidate keyless Solana RPC endpoints for real usability."""
import asyncio
import json

import httpx

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

CANDIDATES = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://solana.publicnode.com",
    "https://solana.api.onfinality.io/public",
    "https://api.metaplex.solana.com",
    "https://solana-mainnet.public.blastapi.io",
    "https://solana.drpc.org",
    "https://1rpc.io/solana",
    "https://rpc.ankr.com/solana",
]


async def probe(client, url, method, params):
    try:
        r = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:90]}"
        body = r.json()
        if "error" in body:
            return f"RPC {body['error'].get('code')}: {body['error'].get('message','')[:80]}"
        res = body.get("result")
        if isinstance(res, list):
            return f"OK list[{len(res)}]"
        if isinstance(res, dict):
            return f"OK dict({len(res)} keys)"
        return f"OK {str(res)[:40]}"
    except Exception as e:
        return f"ERR {type(e).__name__}: {str(e)[:70]}"


async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        for url in CANDIDATES:
            health = await probe(c, url, "getHealth", [])
            sigs = await probe(c, url, "getSignaturesForAddress", [JUPITER_V6, {"limit": 3}])
            print(f"{url}\n   health : {health}\n   sigs   : {sigs}")


asyncio.run(main())
