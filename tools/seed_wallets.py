"""Seed the pool with real active trader wallets pulled from live mainnet.

Keyless: reads fee-payers of recent Jupiter swaps over the free RPC pool and
writes them to the configured database (CSE_DB_PATH, else config db_path).

The same logic is available as the ``keyless`` discovery provider, so
``python -m cse discover`` populates the pool without running this script. This
exists as a standalone for seeding a specific target without a full discovery.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.config import load_config
from cse.db import Database
from cse.models import Trader
from cse.providers.keyless import KeylessProvider

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


async def main():
    cfg = load_config()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    provider = KeylessProvider(max_wallets=limit)
    traders = await provider.top_traders(limit=limit)
    if not traders:
        print("seeded 0 wallets; the RPC pool returned nothing (try again)")
        return
    db = Database(cfg.db_path)
    for t in traders:
        db.upsert_trader(Trader(address=t.address, source="keyless-seed"))
    print(f"seeded {len(traders)} wallets; db_total={db.count_traders()}")
    db.close()


asyncio.run(main())
