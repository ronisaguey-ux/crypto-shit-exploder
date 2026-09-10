"""FastAPI webhook receiver for Helius enhanced transactions.

Point a Helius webhook at POST /webhook (see cse.providers.helius). The receiver
authenticates via the `Authorization` header Helius sends, parses swaps, runs
them through the paper engine, and stores the resulting signals.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .aggregation import Aggregator
from .config import Config, load_config
from .db import Database
from .ingest import filter_tracked, parse_helius_webhook
from .models import Trader
from .paper import PaperTradingEngine

log = logging.getLogger("cse.webhook")

app = FastAPI(title="crypto-shit-exploder webhook", version="0.1.0")

_cfg: Config | None = None
_db: Database | None = None
_engine: PaperTradingEngine | None = None
_agg: Aggregator | None = None
_tracked: set[str] = set()


def _ensure() -> tuple[Config, Database, PaperTradingEngine, Aggregator]:
    global _cfg, _db, _engine, _agg
    if _cfg is None:
        _cfg = load_config()
        _db = Database(_cfg.db_path)
        _engine = PaperTradingEngine(_cfg.paper, _db)
        _agg = Aggregator(_cfg.aggregation)
        _tracked.update(t.address for t in _db.traders(active_only=True))
    assert _db and _engine and _agg
    return _cfg, _db, _engine, _agg


@app.get("/health")
def health() -> dict[str, Any]:
    cfg, db, _, _ = _ensure()
    return {
        "status": "ok",
        "tracked_wallets": len(_tracked),
        "traders_in_db": db.count_traders(),
        "db": cfg.db_path,
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg, db, engine, agg = _ensure()
    secret = cfg.helius_webhook_secret or os.getenv("HELIUS_WEBHOOK_SECRET", "")
    if secret and authorization != secret:
        raise HTTPException(status_code=401, detail="bad webhook auth header")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON") from None

    trades = parse_helius_webhook(payload, sol_price_usd=cfg.paper.sol_price_usd)
    trades = filter_tracked(trades, _tracked)

    closed = []
    for t in trades:
        db.insert_trade(t)
        ct = engine.on_trade(t)
        if ct is not None:
            closed.append(ct)

    # Refresh tracked set if the pool grew (discovery runs out of band).
    if db.count_traders() != len(_tracked):
        _tracked.update(x.address for x in db.traders(active_only=True))

    traders = {x.address: x for x in db.traders(active_only=True)}
    signals = agg.signals_from_trades(trades, traders)
    for s in signals:
        db.insert_signal(s)

    return {
        "received": len(payload) if isinstance(payload, list) else 1,
        "tracked_trades": len(trades),
        "closed_trades": len(closed),
        "signals": len(signals),
    }


def main() -> None:
    import uvicorn

    host = os.getenv("CSE_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("CSE_WEBHOOK_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
