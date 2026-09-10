#!/usr/bin/env python3
"""Attribute the watcher's memory: which container is actually growing."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from cse.config import load_config  # noqa: E402
from cse.db import Database  # noqa: E402
from cse.models import Trader  # noqa: E402
from cse.runner import build_watcher  # noqa: E402

DB = "/tmp/cse_diag/diag.db"
Path("/tmp/cse_diag").mkdir(exist_ok=True)
for f in Path("/tmp/cse_diag").glob("*"):
    f.unlink()

WALLETS = [
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "GDfnEsia2WLAW5t8yx2X5j2mkfA74i5kwGdDuZHt7XmG",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "3XL5B9C7QnFGGkFrvFxFkPCLPFqQqKcFdDgKzNBrxGmN",
    "CuieVDEDtLo7FypA9SbLM9saXFdb1dsshEkyErMqkRQq",
]


async def main() -> None:
    os.environ["CSE_DB_PATH"] = DB
    cfg = load_config()
    db = Database(DB)
    for i, a in enumerate(WALLETS):
        db.upsert_trader(Trader(address=a, source="diag", label=f"d{i}", active=True))

    w = build_watcher(cfg, db)
    await w.start()
    run = asyncio.create_task(w.run())

    print(f"{'t':>5} {'rss_MB':>8} {'ws_q':>7} {'seen':>7} {'price':>6} {'summ':>5} "
          f"{'qpend':>7} {'evict':>6}")
    try:
        for i in range(11):
            await asyncio.sleep(30)
            fp = w.footprint()
            ws_q = w.ws.queue.qsize() if w.ws else -1
            m = w.maintain()
            print(
                f"{i * 30 + 30:>5} {fp['rss_bytes'] / 1e6:>8.1f} {ws_q:>7} "
                f"{fp['seen_signatures']:>7} {fp['price_cache']:>6} "
                f"{fp['summaries_held']:>5} {w.queue.stats()['pending']:>7} "
                f"{w.prices.stats['evicted']:>6}  trim->{m['rss_bytes']/1e6:>6.1f}MB "
                f"pruned={m.get('queue_pruned', 0)}"
            )
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await w.stop()
        db.close()


asyncio.run(main())
