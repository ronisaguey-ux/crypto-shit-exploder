#!/usr/bin/env python3
"""Decisive memory attribution: tracemalloc top allocation sites."""
from __future__ import annotations

import asyncio
import os
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from cse.config import load_config  # noqa: E402
from cse.db import Database  # noqa: E402
from cse.models import Trader  # noqa: E402
from cse.runner import build_watcher  # noqa: E402

DB = "/tmp/cse_diag2/diag.db"
Path("/tmp/cse_diag2").mkdir(exist_ok=True)
for f in Path("/tmp/cse_diag2").glob("*"):
    f.unlink()

WALLETS = [
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "GDfnEsia2WLAW5t8yx2X5j2mkfA74i5kwGdDuZHt7XmG",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
]


def rss_mb() -> float:
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


async def main() -> None:
    os.environ["CSE_DB_PATH"] = DB
    cfg = load_config()
    db = Database(DB)
    for i, a in enumerate(WALLETS):
        db.upsert_trader(Trader(address=a, source="diag", label=f"d{i}", active=True))

    w = build_watcher(cfg, db)
    await w.start()
    run = asyncio.create_task(w.run())

    try:
        await asyncio.sleep(20)
        print(f"baseline rss {rss_mb():.1f} MB")
        tracemalloc.start(15)
        snap1 = tracemalloc.take_snapshot()

        for i in range(6):
            await asyncio.sleep(20)
            print(f"  t={(i + 1) * 20:>4}s rss={rss_mb():>7.1f}MB "
                  f"qpend={w.queue.stats()['pending']:>6}")

        snap2 = tracemalloc.take_snapshot()
        print("\n=== TOP GROWTH SINCE BASELINE (tracemalloc) ===")
        for stat in snap2.compare_to(snap1, "lineno")[:12]:
            print(f"  {stat.size_diff / 1e6:>+8.2f} MB  {stat.count_diff:>+8}  {stat.traceback.format()[-1][:110]}")
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await w.stop()
        db.close()


asyncio.run(main())
