"""Vybe Network top-traders API.

Docs: https://docs.vybenetwork.com/docs/top-traders-pnl
Endpoint: GET /v4/wallets/top-traders  (base https://api.vybenetwork.xyz)
Auth: X-API-Key header.
"""
from __future__ import annotations

from typing import Any

from ..models import Trader
from .base import Provider, ProviderError


class VybeProvider(Provider):
    name = "vybe"
    base_url = "https://api.vybenetwork.xyz"
    auth_header = "X-API-Key"

    async def top_traders(
        self, limit: int = 1000, window_days: int = 30, **kw
    ) -> list[Trader]:
        if not self.api_key:
            raise ProviderError("VYBE_API_KEY not set")

        resolution = kw.get("resolution") or _resolution(window_days)
        sort_desc = kw.get("sort_by_desc", "realizedPnlUsd")
        out: list[Trader] = []
        page = 0
        page_size = min(int(kw.get("page_size", 1000)) or 1000, 1000)
        while len(out) < limit:
            data = await self._get(
                "/v4/wallets/top-traders",
                {
                    "resolution": resolution,
                    "sortByDesc": sort_desc,
                    "limit": min(page_size, limit - len(out)),
                    "page": page,
                },
            )
            rows = _rows(data)
            if not rows:
                break
            for r in rows:
                t = self._parse(r)
                if t:
                    out.append(t)
            if len(rows) < page_size:
                break
            page += 1
        return out[:limit]

    def _parse(self, r: dict[str, Any]) -> Trader | None:
        addr = r.get("traderAddress") or r.get("accountAddress") or r.get("wallet")
        if not addr:
            return None
        labels = r.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return Trader(
            address=str(addr),
            source=self.name,
            label=r.get("name"),
            tags=[str(x) for x in labels],
            reported_realized_pnl=self._f(r, "realizedPnlUsd", "realizedPnl"),
            reported_roi=self._f(r, "roi", "roiPercent"),
            reported_win_rate=self._f(r, "winRate"),
            reported_volume_usd=self._f(r, "tradesVolumeUsd", "totalVolumeUsd"),
            reported_trades=int(self._f(r, "tradesCount", "trades")),
        )


def _resolution(window_days: int) -> str:
    if window_days <= 1:
        return "1d"
    if window_days <= 7:
        return "7d"
    return "30d"


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "traders", "results", "items", "wallets"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                return _rows(v)
    return []
