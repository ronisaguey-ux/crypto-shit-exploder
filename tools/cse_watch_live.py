"""Live end-to-end check of the accuracy pipeline, zero keys.

Seeds real active wallets (fee payers of recent swaps), then runs the watcher for
a bounded window and verifies the whole chain fired: notifications -> pre-filter ->
durable queue -> drain -> enrich -> DB -> per-trader log tree on disk.
"""
import asyncio
import json
import os
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cse.config import Config  # noqa: E402
from cse.db import Database  # noqa: E402
from cse.models import Trader  # noqa: E402
from cse.rpc import RpcPool, default_endpoints  # noqa: E402
from cse.runner import run_watch  # noqa: E402

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

RUN = "/tmp/cse_watch_live"
LOGS = f"{RUN}/logs"


async def seed_wallets(n: int = 40) -> list[str]:
    pool = RpcPool(default_endpoints(), timeout=30.0, max_attempts=6)
    found: list[str] = []
    try:
        for program in (PUMPSWAP, RAYDIUM):
            sigs = await pool.get_signatures_for_address(program, limit=40)
            for row in sigs:
                if len(found) >= n:
                    break
                sig = row.get("signature")
                if not sig or row.get("err"):
                    continue
                try:
                    tx = await pool.get_transaction(sig)
                except Exception:  # noqa: BLE001
                    continue
                if not tx:
                    continue
                msg = (tx.get("transaction") or {}).get("message") or {}
                keys = msg.get("accountKeys") or []
                if not keys:
                    continue
                first = keys[0]
                payer = first if isinstance(first, str) else first.get("pubkey")
                if payer and payer not in found:
                    found.append(payer)
    finally:
        await pool.aclose()
    return found


async def main() -> int:
    shutil.rmtree(RUN, ignore_errors=True)
    os.makedirs(RUN, exist_ok=True)

    wallets = await seed_wallets()
    print(f"seeded {len(wallets)} real wallets from recent swaps")
    if not wallets:
        print("FAIL — could not discover any wallets")
        return 1

    cfg = Config()
    cfg.watch.log_dir = LOGS
    cfg.watch.refresh_seconds = 20.0
    db = Database(f"{RUN}/cse.db")
    for w in wallets:
        db.upsert_trader(Trader(address=w, source="live-check"))

    print(f"running watcher for 75s over {len(wallets)} wallets (zero keys)...")
    summary = await run_watch(cfg, db, duration=75.0, heartbeat=25.0)
    db.close()

    print("\n--- run summary ---")
    print(json.dumps({k: v for k, v in summary.items() if k != "stats"}, indent=2, default=str))

    queue_db = f"{RUN}/queue.db"
    backfill_db = f"{RUN}/backfill.db"
    print(f"\nqueue.db exists={os.path.exists(queue_db)} backfill.db exists={os.path.exists(backfill_db)}")

    import sqlite3

    conn = sqlite3.connect(queue_db)
    qstats = dict(conn.execute("SELECT status, COUNT(*) FROM fetch_queue GROUP BY status").fetchall())
    conn.close()
    print(f"queue rows by status: {qstats}")

    trades_db = sqlite3.connect(f"{RUN}/cse.db")
    n_trades = trades_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_observed = trades_db.execute("SELECT COUNT(*) FROM trades WHERE kind='observed'").fetchone()[0]
    n_sim = trades_db.execute("SELECT COUNT(*) FROM trades WHERE kind='simulated'").fetchone()[0]
    n_closed = trades_db.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
    bases = dict(trades_db.execute(
        "SELECT slippage_basis, COUNT(*) FROM trades WHERE kind='observed' GROUP BY slippage_basis"
    ).fetchall())
    dexes = dict(trades_db.execute(
        "SELECT dex, COUNT(*) FROM trades WHERE kind='observed' AND dex IS NOT NULL GROUP BY dex"
    ).fetchall())
    trades_db.close()
    print(f"trades={n_trades} (observed={n_observed}, simulated={n_sim}) closed={n_closed}")
    print(f"observed slippage_basis: {bases}")
    print(f"observed venues: {dexes}")

    trader_dirs = []
    if os.path.isdir(LOGS):
        trader_dirs = [d for d in os.listdir(LOGS) if os.path.isdir(os.path.join(LOGS, d))]
    print(f"per-trader log dirs: {len(trader_dirs)}")
    with_logs = 0
    for d in trader_dirs:
        p = os.path.join(LOGS, d, "trades.jsonl")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with_logs += 1
    print(f"dirs with a non-empty trades.jsonl: {with_logs}")
    if trader_dirs:
        sample = sorted(trader_dirs)[0]
        for name in ("trades.jsonl", "trades.log", "summary.json"):
            fp = os.path.join(LOGS, sample, name)
            print(f"  sample {name}: exists={os.path.exists(fp)}")
        logp = os.path.join(LOGS, sample, "trades.log")
        if os.path.exists(logp):
            with open(logp) as fh:
                print("  sample trades.log:")
                for line in fh.readlines()[:3]:
                    print("   ", line.rstrip())

    ok = (
        qstats.get("done", 0) > 0
        and n_observed > 0
        and n_sim > 0
        and with_logs > 0
    )
    print("\nVERDICT:", "OK — queue drained, trades enriched, per-trader logs written" if ok
          else "FAIL — pipeline did not complete end to end")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
