"""Command-line interface.

    python -m cse discover          # build the trader pool from the leaderboards
    python -m cse simulate          # replay stored trades through the paper engine
    python -m cse score             # compute fitness for every trader with history
    python -m cse aggregate         # turn the freshest signals into decisions
    python -m cse report            # show the shadow-portfolio leaderboard
    python -m cse webhook           # run the Helius webhook receiver
    python -m cse pipeline          # discover -> score -> aggregate in one pass
    python -m cse watch             # live: watch wallets, paper-trade every trade
    python -m cse run               # discover -> watch -> periodic rescore (6mo)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

from .aggregation import Aggregator
from .config import load_config
from .db import Database
from .discovery import TraderDiscovery
from .models import Trader, Trade
from .paper import PaperTradingEngine
from .runner import run_supervisor, run_watch
from .scoring import apply_scores, score_traders


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = Database(cfg.db_path)
    disc = TraderDiscovery(cfg, db)
    # --target is the name the README documents; --limit is the older spelling.
    limit = getattr(args, "target", None) or getattr(args, "limit", None)
    result = asyncio.run(disc.discover(limit=limit))
    print(json.dumps({
        "discovered": result.total,
        "per_provider": result.per_provider,
        "errors": result.errors,
        "db_total": db.count_traders(),
    }, indent=2))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = Database(cfg.db_path)
    engine = PaperTradingEngine(cfg.paper, db)
    trades = db.recent_trades(limit=args.limit)
    # Oldest first: the shadow portfolio must see trades in time order.
    for t in sorted(trades, key=lambda x: x.observed_at or 0):
        engine.on_trade(t)
    print(json.dumps({"replayed": len(trades), "portfolios": engine.summary()}, indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = Database(cfg.db_path)
    closed = db.closed_trades()
    by_trader: dict[str, list] = {}
    for ct in closed:
        by_trader.setdefault(ct.trader, []).append(ct)
    reports = score_traders(by_trader, cfg.scoring, cfg.paper.starting_equity_usd)
    traders = db.traders(active_only=False)
    apply_scores(traders, reports, db=db)
    top = [r.to_dict() for r in reports[: args.top]]
    print(json.dumps({"scored": len(reports), "top": top}, indent=2))
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = Database(cfg.db_path)
    agg = Aggregator(cfg.aggregation)
    since = time.time() - args.window_hours * 3600
    signals = db.signals_since(since)
    decisions = agg.aggregate_all(signals)
    actionable = [d.to_dict() for d in decisions if d.action != "hold"]
    print(json.dumps({
        "signals": len(signals),
        "decisions": len(decisions),
        "actionable": actionable[: args.top],
    }, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = Database(cfg.db_path)
    engine = PaperTradingEngine(cfg.paper, db)
    closed = db.closed_trades()
    by_trader: dict[str, list] = {}
    for ct in closed:
        by_trader.setdefault(ct.trader, []).append(ct)
    reports = {r.trader: r for r in score_traders(by_trader, cfg.scoring)}
    rows = []
    for p in db.traders(active_only=False):
        r = reports.get(p.address)
        rows.append({
            "trader": p.address,
            "label": p.label,
            "source": p.source,
            "fitness": round(p.fitness, 4),
            "trades": r.n_trades if r else 0,
            "pnl_usd": round(r.total_pnl_usd, 2) if r else 0.0,
            "sharpe": round(r.sharpe, 3) if r else 0.0,
            "max_dd": round(r.max_drawdown_pct, 3) if r else 0.0,
        })
    rows.sort(key=lambda x: x["fitness"], reverse=True)
    print(json.dumps({"traders": len(rows), "leaderboard": rows[: args.top]}, indent=2))
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    from .webhook_server import main as run

    run()
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """discover -> (existing history) score -> aggregate."""
    rc = cmd_discover(args)
    if rc:
        return rc
    cmd_score(args)
    cmd_aggregate(args)
    return cmd_report(args)


def cmd_watch(args: argparse.Namespace) -> int:
    """Live ingest: watch the pool and paper-trade every observed swap."""
    cfg = load_config()
    db = Database(cfg.db_path)
    summary = asyncio.run(run_watch(cfg, db, duration=args.duration))
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """The six-month shape: discover -> watch -> periodic rescore."""
    cfg = load_config()
    db = Database(cfg.db_path)
    summary = asyncio.run(
        run_supervisor(
            cfg,
            db,
            discover=not args.no_discover,
            target=args.target,
            maintenance_hours=args.maintenance_hours,
            duration=args.duration,
        )
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cse", description="aggregate top on-chain traders")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        return sp

    d = add("discover", cmd_discover, "build the trader pool from leaderboards")
    d.add_argument("--limit", type=int, default=None)
    # The README documents --target; keep --limit working for existing callers.
    d.add_argument("--target", type=int, default=None,
                   help="wallet target (alias of --limit)")

    s = add("simulate", cmd_simulate, "replay stored trades through the paper engine")
    s.add_argument("--limit", type=int, default=1000)

    sc = add("score", cmd_score, "compute fitness for every trader with history")
    sc.add_argument("--top", type=int, default=20)

    a = add("aggregate", cmd_aggregate, "turn fresh signals into decisions")
    a.add_argument("--window-hours", type=float, default=24.0)
    a.add_argument("--top", type=int, default=20)

    r = add("report", cmd_report, "show the shadow-portfolio leaderboard")
    r.add_argument("--top", type=int, default=20)

    add("webhook", cmd_webhook, "run the Helius webhook receiver")
    pl = add("pipeline", cmd_pipeline, "discover -> score -> aggregate")
    # pipeline delegates to cmd_discover/cmd_score/cmd_aggregate/cmd_report, so
    # it needs every flag those read. Without these the first delegation raised
    # AttributeError: 'Namespace' object has no attribute 'limit'.
    pl.add_argument("--limit", type=int, default=None)
    pl.add_argument("--target", type=int, default=None, help="wallet target (alias of --limit)")
    pl.add_argument("--top", type=int, default=20)
    pl.add_argument("--window-hours", type=float, default=24.0)

    w = add("watch", cmd_watch, "live: watch wallets and paper-trade every trade")
    w.add_argument("--duration", type=float, default=None, help="stop after N seconds")

    rn = add("run", cmd_run, "discover -> watch -> periodic rescore (the 6-month shape)")
    rn.add_argument("--target", type=int, default=None, help="wallet target (default from config)")
    rn.add_argument("--no-discover", action="store_true", help="skip discovery, watch the existing pool")
    rn.add_argument("--maintenance-hours", type=float, default=24.0, help="rescore interval")
    rn.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
