"""SQLite persistence. One file, no server, safe for the paper-trading workload."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from .models import ClosedTrade, Position, Signal, Trader, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    source TEXT,
    label TEXT,
    tags TEXT,
    reported_realized_pnl REAL,
    reported_roi REAL,
    reported_win_rate REAL,
    reported_volume_usd REAL,
    reported_trades INTEGER,
    discovered_at REAL,
    fitness REAL,
    fitness_updated_at REAL,
    active INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    trader TEXT,
    mint TEXT,
    side TEXT,
    price REAL,
    amount REAL,
    signature TEXT,
    slot INTEGER,
    pool_liquidity_usd REAL,
    observed_at REAL,
    effective_price REAL,
    fees_usd REAL,
    slippage_bps REAL,
    mev_tax_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    trader TEXT,
    mint TEXT,
    entry_price REAL,
    amount REAL,
    entry_fees_usd REAL,
    entry_slippage_bps REAL,
    opened_at REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_trader ON positions(trader);
CREATE TABLE IF NOT EXISTS closed_trades (
    trader TEXT,
    mint TEXT,
    entry_price REAL,
    exit_price REAL,
    amount REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    fees_usd REAL,
    hold_seconds REAL,
    opened_at REAL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_closed_trader ON closed_trades(trader);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    trader TEXT,
    mint TEXT,
    direction REAL,
    weight REAL,
    fitness REAL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str | Path = "data/cse.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- traders
    def upsert_trader(self, t: Trader) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO traders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(address) DO UPDATE SET
                     source=excluded.source, label=excluded.label, tags=excluded.tags,
                     reported_realized_pnl=excluded.reported_realized_pnl,
                     reported_roi=excluded.reported_roi,
                     reported_win_rate=excluded.reported_win_rate,
                     reported_volume_usd=excluded.reported_volume_usd,
                     reported_trades=excluded.reported_trades,
                     active=excluded.active""",
                (
                    t.address, t.source, t.label, json.dumps(t.tags),
                    t.reported_realized_pnl, t.reported_roi, t.reported_win_rate,
                    t.reported_volume_usd, t.reported_trades, t.discovered_at,
                    t.fitness, t.fitness_updated_at, int(t.active),
                ),
            )
            self._conn.commit()

    def upsert_traders(self, traders: Iterable[Trader]) -> int:
        n = 0
        for t in traders:
            self.upsert_trader(t)
            n += 1
        return n

    def set_fitness(self, address: str, fitness: float, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE traders SET fitness=?, fitness_updated_at=? WHERE address=?",
                (fitness, ts, address),
            )
            self._conn.commit()

    def traders(self, active_only: bool = True) -> list[Trader]:
        q = "SELECT * FROM traders"
        if active_only:
            q += " WHERE active=1"
        rows = self._conn.execute(q).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            d["active"] = bool(d["active"])
            out.append(Trader.from_dict(d))
        return out

    def trader(self, address: str) -> Optional[Trader]:
        r = self._conn.execute("SELECT * FROM traders WHERE address=?", (address,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["active"] = bool(d["active"])
        return Trader.from_dict(d)

    def count_traders(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0]

    # ----------------------------------------------------------------- trades
    def insert_trade(self, t: Trade) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t.id, t.trader, t.mint, t.side.value, t.price, t.amount,
                    t.signature, t.slot, t.pool_liquidity_usd, t.observed_at,
                    t.effective_price, t.fees_usd, t.slippage_bps, t.mev_tax_usd,
                ),
            )
            self._conn.commit()

    def recent_trades(self, limit: int = 100) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY observed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Trade.from_dict(dict(r)) for r in rows]

    # -------------------------------------------------------------- positions
    def open_position(self, p: Position) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
                (p.id, p.trader, p.mint, p.entry_price, p.amount,
                 p.entry_fees_usd, p.entry_slippage_bps, p.opened_at),
            )
            self._conn.commit()

    def get_position(self, trader: str, mint: str) -> Optional[Position]:
        r = self._conn.execute(
            "SELECT * FROM positions WHERE trader=? AND mint=?", (trader, mint)
        ).fetchone()
        return Position.from_dict(dict(r)) if r else None

    def delete_position(self, trader: str, mint: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions WHERE trader=? AND mint=?", (trader, mint))
            self._conn.commit()

    def open_positions(self, trader: Optional[str] = None) -> list[Position]:
        if trader:
            rows = self._conn.execute("SELECT * FROM positions WHERE trader=?", (trader,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
        return [Position.from_dict(dict(r)) for r in rows]

    # ---------------------------------------------------------- closed trades
    def close_trade(self, ct: ClosedTrade) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ct.trader, ct.mint, ct.entry_price, ct.exit_price, ct.amount,
                 ct.pnl_usd, ct.pnl_pct, ct.fees_usd, ct.hold_seconds,
                 ct.opened_at, ct.closed_at),
            )
            self._conn.commit()

    def closed_trades(self, trader: Optional[str] = None) -> list[ClosedTrade]:
        if trader:
            rows = self._conn.execute(
                "SELECT * FROM closed_trades WHERE trader=? ORDER BY closed_at", (trader,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM closed_trades ORDER BY closed_at").fetchall()
        return [ClosedTrade.from_dict(dict(r)) for r in rows]

    # ---------------------------------------------------------------- signals
    def insert_signal(self, s: Signal) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?)",
                (s.id, s.trader, s.mint, s.direction, s.weight, s.fitness, s.created_at),
            )
            self._conn.commit()

    def signals_since(self, since: float, mint: Optional[str] = None) -> list[Signal]:
        if mint:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE created_at>=? AND mint=? ORDER BY created_at",
                (since, mint),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE created_at>=? ORDER BY created_at", (since,)
            ).fetchall()
        return [Signal.from_dict(dict(r)) for r in rows]

    # ------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value)
            )
            self._conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def close(self) -> None:
        self._conn.close()
