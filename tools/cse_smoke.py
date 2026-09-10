#!/usr/bin/env python3
"""End-to-end smoke test for crypto-shit-exploder on a scratch DB.

Seeds two wallets' observed trades, replays them through the real CLI
(simulate -> score -> report -> aggregate) and asserts the whole chain works.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
import time

ROOT = str(Path(__file__).resolve().parent.parent)
DB = "/tmp/cse_smoke.db"
for suf in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suf)
    except FileNotFoundError:
        pass

env = dict(os.environ, CSE_DB_PATH=DB, CSE_SCORING__MIN_TRADES="5",
           CSE_AGGREGATION__MIN_FITNESS="0.05")
os.environ.update({k: v for k, v in env.items() if k.startswith("CSE_")})
sys.path.insert(0, ROOT)

from cse.config import load_config
from cse.db import Database
from cse.models import Side, Signal, Trade, Trader

cfg = load_config()
assert cfg.db_path == DB, f"db_path override failed: {cfg.db_path}"
assert cfg.scoring.min_trades == 5, cfg.scoring.min_trades

db = Database(DB)
now = time.time()
books = {"winner": [2.0, 1.8, 2.2, 1.9, 2.1, 2.0], "loser": [0.5, 0.6, 0.4, 0.55, 0.45, 0.5]}
n = 0
for w, exits in books.items():
    db.upsert_trader(Trader(address=w, source="smoke", label=w, active=True))
    for i, xp in enumerate(exits):
        ts = now + n
        db.insert_trade(Trade(trader=w, mint=f"{w}-m{i}", side=Side.BUY, price=1.0,
                              amount=1000, observed_at=ts, pool_liquidity_usd=500_000))
        db.insert_trade(Trade(trader=w, mint=f"{w}-m{i}", side=Side.SELL, price=xp,
                              amount=1000, observed_at=ts + 1, pool_liquidity_usd=500_000))
        n += 2
print(f"seeded {db.count_traders()} traders, {len(db.recent_trades(999))} trades")
db.close()


def run(*args):
    r = subprocess.run([sys.executable, "-m", "cse", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("STDOUT:", r.stdout)
        print("STDERR:", r.stderr)
        raise SystemExit(f"`cse {' '.join(args)}` exited {r.returncode}")
    return json.loads(r.stdout)


sim = run("simulate")
print("simulate portfolios:", json.dumps(sim["portfolios"], indent=2))
assert sim["replayed"] == 24, sim["replayed"]
by = {p["trader"]: p for p in sim["portfolios"]}
assert by["winner"]["return_pct"] > 0 > by["loser"]["return_pct"], by

sc = run("score")
print("score:", sc["scored"], "scored; top:", [t["trader"] for t in sc["top"]])
assert sc["scored"] == 2
assert sc["top"][0]["trader"] == "winner", sc["top"]

rep = run("report")
print("report:", [(r["trader"], r["fitness"]) for r in rep["leaderboard"]])
assert rep["leaderboard"][0]["trader"] == "winner"

db = Database(DB)
for k, fit in (("s1", 0.9), ("s2", 0.8)):
    db.insert_signal(Signal(trader=k, mint="HOT", direction=1.0, weight=fit ** 2,
                            fitness=fit, created_at=now))
db.close()
ag = run("aggregate", "--window-hours", "24")
print("aggregate:", json.dumps(ag, indent=2))
assert ag["signals"] == 2, ag
assert ag["actionable"] and ag["actionable"][0]["action"] == "buy", ag

print("\nSMOKE OK")
