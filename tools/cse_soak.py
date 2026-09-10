#!/usr/bin/env python3
"""Six-month soak: run the real watcher, hard-kill it, prove nothing was lost.

Answers three questions with measurements rather than assurances:

  1. Does resident memory grow? (RSS sampled from /proc/<pid>/statm)
  2. Is data checkpoint-safe? (SIGKILL mid-run, then reopen every store)
  3. Does it autosave to the per-trader log folders without a clean stop?

Run:  python3 /tmp/cse_soak.py
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/cse_soak")
RUN_SECONDS = int(os.environ.get("SOAK_SECONDS", "150"))
SAMPLE_EVERY = 5.0


def sh(cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=str(ROOT), capture_output=True, text=True, **kw)


def rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError):
        return -1.0


def main() -> int:
    if SCRATCH.exists():
        subprocess.run(["rm", "-rf", str(SCRATCH)], check=False)
    (SCRATCH / "logs").mkdir(parents=True)

    db = SCRATCH / "cse.db"
    print(f"=== SOAK: {RUN_SECONDS}s, hard kill at the end ===")
    print(f"db: {db}")

    # Seed a wallet pool so the watcher has real work: reuse the live seed script's
    # approach via the discovery-free path (watch reads traders from the DB).
    seed = sh(
        "python3 - <<'PY'\n"
        "import sys, sqlite3, time, os\n"
        "sys.path.insert(0, '.')\n"
        "from cse.db import Database\n"
        "from cse.models import Trader\n"
        "db = Database(os.environ['CSE_DB_PATH'])\n"
        "addrs = ['5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1',\n"
        "         '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM',\n"
        "         'GDfnEsia2WLAW5t8yx2X5j2mkfA74i5kwGdDuZHt7XmG',\n"
        "         '2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S',\n"
        "         '3XL5B9C7QnFGGkFrvFxFkPCLPFqQqKcFdDgKzNBrxGmN',\n"
        "         'CuieVDEDtLo7FypA9SbLM9saXFdb1dsshEkyErMqkRQq']\n"
        "for i, a in enumerate(addrs):\n"
        "    db.upsert_trader(Trader(address=a, source='soak', label=f'soak{i}', active=True))\n"
        "print('seeded', db.count_traders())\n"
        "db.close()\n"
        "PY",
        env={**os.environ, "CSE_DB_PATH": str(db)},
    )
    if seed.returncode != 0:
        print("SEED FAILED:", seed.stderr[-800:])
        return 1
    print(seed.stdout.strip())

    # maintenance every 15s so autosave/prune/checkpoint are exercised in the window.
    cfg_patch = SCRATCH / "config.yaml"
    base = (ROOT / "config" / "config.yaml").read_text()
    base = base.replace("maintenance_seconds: 300.0", "maintenance_seconds: 15.0")
    base = base.replace("queue_retention_hours: 72.0", "queue_retention_hours: 0.02")
    base = base.replace("queue_max_pending: 500000", "queue_max_pending: 2000")
    base = base.replace("log_dir: logs/traders", f"log_dir: {SCRATCH}/logs")
    base = base.replace("path: data/cse.db", f"path: {db}")
    cfg_patch.write_text(base)

    log_path = SCRATCH / "run.log"
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cse", "watch",
             "--duration", str(RUN_SECONDS + 600)],
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env={**os.environ, "CSE_DB_PATH": str(db),
                 "CSE_CONFIG_PATH": str(cfg_patch)},
        )

    samples: list[tuple[float, float]] = []
    started = time.time()
    print(f"pid {proc.pid}; sampling RSS every {SAMPLE_EVERY}s")
    while time.time() - started < RUN_SECONDS:
        time.sleep(SAMPLE_EVERY)
        if proc.poll() is not None:
            print(f"!! process exited early rc={proc.returncode}")
            break
        mb = rss_mb(proc.pid)
        samples.append((round(time.time() - started, 1), mb))
        print(f"  t={samples[-1][0]:>6.1f}s  rss={mb:8.1f} MB")

    alive = proc.poll() is None
    print(f"\n=== {'SIGKILL' if alive else 'process already gone'} ===")
    if alive:
        os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    print("killed, rc:", proc.returncode)

    # ---------------------------------------------------------------- verify
    print("\n=== WHAT SURVIVED THE KILL ===")
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        observed = con.execute(
            "SELECT COUNT(*) FROM trades WHERE kind='observed'"
        ).fetchone()[0]
        closed = con.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
        hb = con.execute(
            "SELECT value FROM meta WHERE key='watch_heartbeat'"
        ).fetchone()
        stopped = con.execute(
            "SELECT value FROM meta WHERE key='watch_stopped_at'"
        ).fetchone()
        print(f"  trades in db           : {trades} (observed {observed})")
        print(f"  closed round trips     : {closed}")
        print(f"  heartbeat written      : {'yes' if hb else 'no'}")
        print(f"  clean-stop marker      : {'yes (unexpected)' if stopped else 'no (killed hard, as intended)'}")
    except sqlite3.Error as e:
        print("  db read failed:", e)
    con.close()

    print("\n=== SHED (queue cap) ===")
    for line in log_path.read_text().splitlines():
        if "shed" in line.lower():
            print("  " + line[-140:])

    for name in ("queue.db", "backfill.db"):
        p = SCRATCH / name
        if not p.exists():
            print(f"  {name:<22} : MISSING")
            continue
        c = sqlite3.connect(str(p))
        try:
            if name == "queue.db":
                rows = dict(
                    c.execute("SELECT status, COUNT(*) FROM fetch_queue GROUP BY status")
                )
                print(f"  queue after kill       : {rows or '{} (pruned clean)'}")
            else:
                n = c.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0]
                print(f"  backfill cursors       : {n}")
        finally:
            c.close()

    # per-trader log folders
    logs_root = SCRATCH / "logs"
    dirs = [d for d in logs_root.iterdir() if d.is_dir()] if logs_root.exists() else []
    print(f"  per-trader log folders : {len(dirs)}")
    autosaved = 0
    total_lines = 0
    for d in dirs:
        tj = d / "trades.jsonl"
        if tj.exists():
            total_lines += len(tj.read_text().strip().splitlines())
        if (d / "summary.json").exists():
            autosaved += 1
    print(f"  trades.jsonl lines     : {total_lines}")
    print(f"  summary.json written   : {autosaved} of {len(dirs)} (autosaved, no clean stop)")

    if samples:
        first = [m for _, m in samples[:3] if m > 0]
        last = [m for _, m in samples[-3:] if m > 0]
        if first and last:
            f = sum(first) / len(first)
            l = sum(last) / len(last)
            print(f"\n=== MEMORY ===")
            print(f"  rss first ~{f:.1f} MB -> last ~{l:.1f} MB  (delta {l - f:+.1f} MB)")
            peak = max(m for _, m in samples if m > 0)
            print(f"  peak rss {peak:.1f} MB over {samples[-1][0]:.0f}s")

    print("\n=== maintenance log lines ===")
    for line in log_path.read_text().splitlines():
        if "maintenance" in line or "tracked" in line or "watching" in line:
            print("  " + line[-160:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
