"""Seed a scratch DB with real active trader wallets pulled from live mainnet."""
import asyncio
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.db import Database
from cse.models import Trader
from cse.rpc import RpcPool, default_endpoints

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
DB = "/tmp/cse_watch_test.db"


def fee_payer(tx: dict) -> str:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return ""
    k = keys[0]
    return k.get("pubkey", "") if isinstance(k, dict) else str(k)


async def main():
    pool = RpcPool(default_endpoints(), timeout=30, max_attempts=8)
    rows = await pool.get_signatures_for_address(JUPITER_V6, limit=80)
    sigs = [r["signature"] for r in rows]
    wallets = set()
    for sig in sigs:
        try:
            tx = await pool.get_transaction(sig)
        except Exception:
            continue
        if not tx:
            continue
        w = fee_payer(tx)
        if w:
            wallets.add(w)
        if len(wallets) >= 60:
            break

    db = Database(DB)
    for w in wallets:
        db.upsert_trader(Trader(address=w, source="live-seed"))
    print(f"seeded {len(wallets)} wallets; db_total={db.count_traders()}")
    db.close()
    await pool.aclose()


asyncio.run(main())
