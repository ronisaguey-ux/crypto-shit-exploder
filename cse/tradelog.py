"""Per-trader log tree.

The database is for querying; this is for *reading*. Six months of 5,000 wallets
is a pile of numbers nobody can eyeball, so every wallet gets its own directory
holding the evidence behind its score:

    logs/traders/<wallet>/
        trades.jsonl     one full record per observed trade
        trades.log       the same thing as a human-readable line
        daily/<date>.jsonl   that day's trades, for slicing without grepping
        positions.jsonl  round trips, as the shadow portfolio closes them
        summary.json     rolling counters: volume, fees, slippage, PnL

Writes are append-only and crash-safe: a partially written final line is the
worst case, and the JSONL reader tolerates it. Handles are pooled and evicted so
5,000 wallets do not turn into 5,000 open file descriptors.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("cse.tradelog")

#: Open file handles kept at once. Wallets are visited in bursts, so a small pool
#: captures nearly all the reuse without risking the descriptor limit.
_MAX_OPEN = 64


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _HandlePool:
    """Bounded LRU of append-only file handles."""

    def __init__(self, cap: int = _MAX_OPEN):
        self.cap = cap
        self._open: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def append(self, path: Path, line: str) -> None:
        key = str(path)
        with self._lock:
            fh = self._open.get(key)
            if fh is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(path, "a", encoding="utf-8")
                self._open[key] = fh
                while len(self._open) > self.cap:
                    _, old = self._open.popitem(last=False)
                    try:
                        old.close()
                    except OSError:
                        pass
            else:
                self._open.move_to_end(key)
            fh.write(line)
            fh.flush()

    def close(self) -> None:
        with self._lock:
            for fh in self._open.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self._open.clear()


class TraderLogger:
    """Writes each trader's trades, positions and rolling summary to disk."""

    def __init__(self, root: str | Path = "logs/traders"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._files = _HandlePool()
        self._summaries: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths
    def dir_for(self, wallet: str) -> Path:
        return self.root / wallet

    # ------------------------------------------------------------------ writes
    def log_trade(self, trade, *, extra: Optional[dict[str, Any]] = None) -> None:
        """Append one observed trade to the trader's JSONL, daily file and log."""
        wallet = trade.trader
        record = trade.to_dict()
        record["logged_at"] = time.time()
        if extra:
            record.update(extra)
        line = json.dumps(record, separators=(",", ":"), default=str)

        base = self.dir_for(wallet)
        self._files.append(base / "trades.jsonl", line + "\n")
        self._files.append(base / "daily" / f"{_day(trade.observed_at)}.jsonl", line + "\n")
        self._files.append(base / "trades.log", self._format(trade) + "\n")

        self._accumulate(trade)

    def log_close(self, closed) -> None:
        """Append a realized round trip to the trader's position ledger."""
        wallet = closed.trader
        rec = closed.to_dict()
        rec["logged_at"] = time.time()
        base = self.dir_for(wallet)
        self._files.append(
            base / "positions.jsonl", json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        )
        pnl = rec.get("pnl_usd", 0.0)
        sign = "+" if pnl >= 0 else ""
        self._files.append(
            base / "trades.log",
            f"{_iso(rec.get('closed_at', time.time()))}  CLOSE  {closed.mint[:12]:<12}"
            f"  {sign}{pnl:,.4f} USD  ({sign}{rec.get('pnl_pct', 0.0) * 100:.2f}%)"
            f"  held {closed.hold_seconds:,.0f}s\n",
        )

    def log_note(self, wallet: str, message: str) -> None:
        """Free-form line in the trader's human-readable log."""
        self._files.append(self.dir_for(wallet) / "trades.log", f"{_iso(time.time())}  {message}\n")

    # ------------------------------------------------------------------ summary
    def _accumulate(self, trade) -> None:
        with self._lock:
            s = self._summaries.get(trade.trader)
            if s is None:
                s = {
                    "wallet": trade.trader,
                    "first_seen": trade.observed_at,
                    "last_seen": trade.observed_at,
                    "trades": 0,
                    "buys": 0,
                    "sells": 0,
                    "notional_usd": 0.0,
                    "fees_usd": 0.0,
                    "slippage_bps_sum": 0.0,
                    "slippage_bps_max": 0.0,
                    "exact_fills": 0,
                    "mints": [],
                    "venues": {},
                }
                self._summaries[trade.trader] = s

            s["trades"] += 1
            s["buys" if trade.side.value == "buy" else "sells"] += 1
            s["notional_usd"] += trade.notional_usd
            s["fees_usd"] += trade.fees_usd or 0.0
            s["slippage_bps_sum"] += trade.slippage_bps or 0.0
            s["slippage_bps_max"] = max(s["slippage_bps_max"], trade.slippage_bps or 0.0)
            if trade.slippage_basis == "exact":
                s["exact_fills"] += 1
            if trade.dex:
                s["venues"][trade.dex] = s["venues"].get(trade.dex, 0) + 1
            s["last_seen"] = max(s["last_seen"], trade.observed_at)
            if trade.mint not in s["mints"]:
                s["mints"].append(trade.mint)
                if len(s["mints"]) > 500:
                    s["mints"] = s["mints"][-500:]

    def flush_summary(self, wallet: str) -> None:
        """Write the rolling summary for one wallet to disk."""
        with self._lock:
            s = self._summaries.get(wallet)
            if not s:
                return
            out = dict(s)
            out["mints"] = list(s["mints"])
            out["venues"] = dict(s["venues"])
            n = max(1, s["trades"])
            out["avg_slippage_bps"] = round(s["slippage_bps_sum"] / n, 4)
            out["slippage_bps_sum"] = round(s["slippage_bps_sum"], 4)
            out["notional_usd"] = round(s["notional_usd"], 4)
            out["fees_usd"] = round(s["fees_usd"], 8)
            out["unique_mints"] = len(s["mints"])
            out["written_at"] = time.time()

        path = self.dir_for(wallet) / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def flush_all_summaries(self) -> int:
        with self._lock:
            wallets = list(self._summaries)
        for w in wallets:
            self.flush_summary(w)
        return len(wallets)

    def summary_for(self, wallet: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return dict(self._summaries.get(wallet) or {}) or None

    def tracked_wallets(self) -> list[str]:
        with self._lock:
            return list(self._summaries)

    # ------------------------------------------------------------------- format
    @staticmethod
    def _format(trade) -> str:
        """One aligned human-readable line per trade."""
        pnl_side = "BUY " if trade.side.value == "buy" else "SELL"
        price = f"${trade.price:.10f}".rstrip("0").rstrip(".")
        slip = trade.slippage_bps or 0.0
        basis = trade.slippage_basis or "none"
        fee = trade.fees_usd or 0.0
        venue = trade.dex or "?"
        sig = (trade.signature or "")[:16]
        return (
            f"{_iso(trade.observed_at)}  {pnl_side}  {trade.mint[:14]:<14}"
            f"  {trade.amount:>18,.4f} @ {price:<20}"
            f"  ${trade.notional_usd:>12,.2f}"
            f"  slip {slip:>8,.1f}bps({basis})"
            f"  fee ${fee:.6f}  {venue:<14}  {sig}"
        )

    def close(self) -> None:
        try:
            self.flush_all_summaries()
        finally:
            self._files.close()
