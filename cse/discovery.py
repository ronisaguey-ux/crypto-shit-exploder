"""Trader discovery: build the pool of wallets worth shadowing.

Providers are tried in priority order and their results unioned, deduplicated by
wallet address. A provider that is missing a key or fails is skipped rather than
fatal — the pool is still useful with one working source. Ranking uses the best
(not summed) leaderboard figures so a wallet appearing on two boards is not
double-counted.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import Config, DiscoveryConfig
from .db import Database
from .models import Trader
from .providers import ProviderError, get_provider

log = logging.getLogger("cse.discovery")


@dataclass
class DiscoveryResult:
    traders: list[Trader] = field(default_factory=list)
    per_provider: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.traders)


class TraderDiscovery:
    def __init__(self, cfg: Config, db: Database | None = None):
        self.cfg = cfg
        self.db = db

    def _provider(self, name: str):
        keys = self.cfg.provider_keys()
        return get_provider(name, api_key=keys.get(name, ""))

    async def _fetch_one(self, name: str, want: int) -> tuple[str, list[Trader], str]:
        try:
            p = self._provider(name)
            if not p.available():
                return name, [], f"{name}: no api key configured"
            traders = await p.top_traders(
                limit=want,
                window_days=self.cfg.discovery.window_days,
                pnl_mode=self.cfg.discovery.pnl_mode,
                page_size=self.cfg.discovery.page_size,
            )
            return name, traders, ""
        except ProviderError as e:
            return name, [], f"{name}: {e}"
        except Exception as e:  # a provider bug must not sink discovery
            return name, [], f"{name}: unexpected {type(e).__name__}: {e}"

    async def discover(self, limit: int | None = None) -> DiscoveryResult:
        d: DiscoveryConfig = self.cfg.discovery
        limit = limit or d.target_traders
        names = [n for n in d.providers if n != "birdeye"] or list(d.providers)
        # Birdeye needs token mints; only use it if explicitly configured with them.
        if "birdeye" in d.providers and getattr(self.cfg, "_birdeye_mints", None):
            names.append("birdeye")

        results = await asyncio.gather(*(self._fetch_one(n, limit) for n in names))

        merged: dict[str, Trader] = {}
        per_provider: dict[str, int] = {}
        errors: dict[str, str] = {}
        for name, traders, err in results:
            per_provider[name] = len(traders)
            if err:
                errors[name] = err
                log.warning("discovery: %s", err)
            for t in traders:
                if not t.address:
                    continue
                existing = merged.get(t.address)
                if existing is None:
                    merged[t.address] = t
                else:
                    _merge_best(existing, t)

        ranked = self._rank(list(merged.values()))
        ranked = [t for t in ranked if self._passes_filters(t)][:limit]
        out = DiscoveryResult(traders=ranked, per_provider=per_provider, errors=errors)
        if self.db is not None and ranked:
            self.db.upsert_traders(ranked)
        return out

    # -------------------------------------------------------------- ranking
    @staticmethod
    def _rank(traders: list[Trader]) -> list[Trader]:
        """A blended leaderboard rank: PnL first, then ROI and win rate."""
        def key(t: Trader) -> tuple:
            return (
                t.reported_realized_pnl,
                t.reported_roi,
                t.reported_win_rate,
                t.reported_volume_usd,
            )

        return sorted(traders, key=key, reverse=True)

    def _passes_filters(self, t: Trader) -> bool:
        d = self.cfg.discovery
        if t.reported_volume_usd and t.reported_volume_usd < d.min_volume_usd:
            return False
        if t.reported_trades and t.reported_trades < d.min_trades:
            return False
        return True


def _merge_best(dst: Trader, src: Trader) -> None:
    """Fold a duplicate wallet in, keeping the strongest figure for each field."""
    dst.reported_realized_pnl = max(dst.reported_realized_pnl, src.reported_realized_pnl)
    dst.reported_roi = max(dst.reported_roi, src.reported_roi)
    dst.reported_win_rate = max(dst.reported_win_rate, src.reported_win_rate)
    dst.reported_volume_usd = max(dst.reported_volume_usd, src.reported_volume_usd)
    dst.reported_trades = max(dst.reported_trades, src.reported_trades)
    if not dst.label and src.label:
        dst.label = src.label
    for tag in src.tags:
        if tag not in dst.tags:
            dst.tags.append(tag)
    if src.source not in (dst.source or ""):
        dst.source = f"{dst.source},{src.source}" if dst.source else src.source
