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
    mev_tax_usd REAL,
    dex TEXT,
    slippage_basis TEXT,
    pool_json TEXT,
    execution_json TEXT,
    kind TEXT NOT NULL DEFAULT 'observed'
);
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
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    trader TEXT,
    mint TEXT,
    direction REAL,
    weight REAL,
    fitness REAL,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

#: Indexes are created only after ``_migrate()`` has added the columns they
#: reference. ``idx_trades_unique_kind`` names the ``kind`` column, which does
#: not exist in a database written by an older collector — creating it inside
#: SCHEMA made opening such a database raise ``no such column: kind`` before the
#: migration could add it.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_trader ON trades(trader);
CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
-- One observed trade is one row, however many times we see it (websocket, then
-- backfill). ``kind`` keeps the shadow fill of that same trade — which shares the
-- signature by design — from colliding with it. Partial index so simulated trades
-- carrying no signature are still free to repeat.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique_kind
    ON trades(signature, trader, mint, kind) WHERE signature IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_positions_trader ON positions(trader);
CREATE INDEX IF NOT EXISTS idx_closed_trader ON closed_trades(trader);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint);
"""


class Database:
    def __init__(self, path: str | Path = "data/cse.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # A long-lived writer must not let the WAL grow for the whole run; the
        # maintenance loop also calls checkpoint() explicitly. NORMAL is the right
        # durability level here: a crash can lose the last transaction, never the
        # database, and this collector re-derives anything it missed from chain.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        # Indexes last: idx_trades_unique_kind names ``kind``, which only exists
        # once _migrate() has added it to an older database.
        self._conn.executescript(SCHEMA_INDEXES)
        self._conn.commit()

    #: Columns added to ``trades`` after the first release. ``CREATE TABLE IF NOT
    #: EXISTS`` does nothing to a database that already exists, so an older
    #: collector needs the columns added explicitly or every insert fails.
    _ADDED_TRADE_COLUMNS = {
        "dex": "TEXT",
        "slippage_basis": "TEXT",
        "pool_json": "TEXT",
        "execution_json": "TEXT",
        "kind": "TEXT NOT NULL DEFAULT 'observed'",
    }

    def _migrate(self) -> None:
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(trades)")}
        for name, sqltype in self._ADDED_TRADE_COLUMNS.items():
            if name not in have:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {sqltype}")
        # Superseded by idx_trades_unique_kind; the old key collided observed rows
        # with their own simulated fills.
        self._conn.execute("DROP INDEX IF EXISTS idx_trades_unique")

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
    def insert_trade(self, t: Trade) -> bool:
        """Record an observed trade. Returns True if it was new.

        Idempotent on (signature, trader, mint): the websocket and the backfill
        poller both surface the same swap, and a six-month collector that counted
        it twice would inflate every downstream statistic.
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO trades
                   (id, trader, mint, side, price, amount, signature, slot,
                    pool_liquidity_usd, observed_at, effective_price, fees_usd,
                    slippage_bps, mev_tax_usd, dex, slippage_basis,
                    pool_json, execution_json, kind)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.id, t.trader, t.mint, t.side.value, t.price, t.amount,
                    t.signature, t.slot, t.pool_liquidity_usd, t.observed_at,
                    t.effective_price, t.fees_usd, t.slippage_bps, t.mev_tax_usd,
                    t.dex, t.slippage_basis,
                    json.dumps(t.pool) if t.pool else None,
                    json.dumps(t.execution) if t.execution else None,
                    t.kind,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def recent_trades(self, limit: int = 100, kind: str = "observed") -> list[Trade]:
        """Stored trades, newest first, filtered to ``kind``.

        ``cse simulate`` replays what the trader actually did; without the filter
        it also replayed the shadow fills those replays wrote back, so every run
        doubled the positions and closed trades.
        """
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE kind = ? ORDER BY observed_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
        return [self._trade_from_row(dict(r)) for r in rows]

    def trades_for(self, trader: str, limit: int = 1000) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE trader=? ORDER BY observed_at DESC LIMIT ?",
            (trader, limit),
        ).fetchall()
        return [self._trade_from_row(dict(r)) for r in rows]

    def count_trades(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    @staticmethod
    def _trade_from_row(d: dict) -> Trade:
        for col, attr in (("pool_json", "pool"), ("execution_json", "execution")):
            raw = d.pop(col, None)
            if raw:
                try:
                    d[attr] = json.loads(raw)
                except (TypeError, ValueError):
                    d[attr] = None
        return Trade.from_dict(d)

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

    # ------------------------------------------------------------------ upkeep
    def checkpoint(self) -> None:
        """Fold the WAL back into the database file and truncate it."""
        with self._lock:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def size_bytes(self) -> int:
        """On-disk size of the database including its WAL and shared-memory file."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += (self.path.parent / (self.path.name + suffix)).stat().st_size
            except OSError:
                pass
        return total

    def row_counts(self) -> dict[str, int]:
        """Cheap table census, so a run's growth is observable not guessed."""
        out: dict[str, int] = {}
        with self._lock:
            for table in ("traders", "trades", "positions", "closed_trades", "signals"):
                try:
                    r = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    out[table] = int(r[0]) if r else 0
                except sqlite3.Error:
                    continue
        return out

    def close(self) -> None:
        try:
            self.checkpoint()
        except sqlite3.Error:
            pass
        self._conn.close()
