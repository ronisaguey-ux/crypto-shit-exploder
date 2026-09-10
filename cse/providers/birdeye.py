"""Birdeye token top-traders API.

Docs: https://docs.birdeye.so  (GET /defi/v2/tokens/top_traders)
Base: https://public-api.birdeye.so
Auth: X-API-KEY header + X-Chain header (solana).
"""
from __future__ import annotations

from typing import Any

from ..models import Trader
from .base import Provider, ProviderError


class BirdeyeProvider(Provider):
    name = "birdeye"
    base_url = "https://public-api.birdeye.so"
    auth_header = "X-API-KEY"

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h["X-Chain"] = "solana"
        return h

    async def top_traders(
        self, limit: int = 1000, window_days: int = 30, **kw
    ) -> list[Trader]:
        if not self.api_key:
            raise ProviderError("BIRDEYE_API_KEY not set")
        # Birdeye ranks traders per token, not globally. To build a global
        # leaderboard we seed from the hottest tokens and union the traders.
        mints = kw.get("mints") or []
        if not mints:
            raise ProviderError(
                "Birdeye requires token mints; pass mints=[...] or use another provider "
                "for the global leaderboard"
            )
        time_frame = kw.get("time_frame") or _time_frame(window_days)
        seen: dict[str, Trader] = {}
        per_token = max(1, limit // max(1, len(mints)))
        for mint in mints:
            try:
                data = await self._get(
                    "/defi/v2/tokens/top_traders",
                    {
                        "address": mint,
                        "time_frame": time_frame,
                        "sort_by": kw.get("sort_by", "total_pnl"),
                        "limit": min(per_token, 100),
                    },
                )
            except ProviderError:
                continue
            for r in _rows(data):
                t = self._parse(r, mint)
                if t and t.address not in seen:
                    seen[t.address] = t
        ranked = sorted(
            seen.values(), key=lambda x: x.reported_realized_pnl, reverse=True
        )
        return ranked[:limit]

    def _parse(self, r: dict[str, Any], mint: str) -> Trader | None:
        addr = r.get("address") or r.get("wallet") or r.get("owner")
        if not addr:
            return None
        tags = r.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return Trader(
            address=str(addr),
            source=self.name,
            label=r.get("name") or r.get("label"),
            tags=[str(x) for x in tags],
            reported_realized_pnl=self._f(r, "realized_pnl", "realizedPnl", "total_pnl"),
            reported_roi=self._f(r, "roi"),
            reported_win_rate=self._f(r, "win_rate", "winRate"),
            reported_volume_usd=self._f(r, "volume_usd", "volumeUsd", "volume"),
            reported_trades=int(self._f(r, "trade_count", "trades")),
        )


def _time_frame(window_days: int) -> str:
    for days, tf in ((2, "2d"), (3, "3d"), (7, "7d"), (14, "14d"),
                     (30, "30d"), (60, "60d"), (90, "90d")):
        if window_days <= days:
            return tf
    return "90d"


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        d = data.get("data", data)
        if isinstance(d, dict):
            for key in ("items", "traders", "results", "list"):
                v = d.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict)]
    return []
