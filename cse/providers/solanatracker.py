"""SolanaTracker PnL V2 leaderboard.

Docs: https://www.solanatracker.io/resources/solana-pnl-leaderboard-api
Endpoint: GET /v2/pnl/leaderboard/top  (base https://data.solanatracker.io)
Auth: x-api-key header.
"""
from __future__ import annotations

from typing import Any

from ..models import Trader
from .base import Provider, ProviderError


class SolanaTrackerProvider(Provider):
    name = "solanatracker"
    base_url = "https://data.solanatracker.io"
    auth_header = "x-api-key"

    async def top_traders(
        self, limit: int = 1000, window_days: int = 30, pnl_mode: str = "adjusted", **kw
    ) -> list[Trader]:
        if not self.api_key:
            raise ProviderError("SOLANATRACKER_API_KEY not set")

        out: list[Trader] = []
        page = 1
        page_size = min(limit, int(kw.get("page_size", 1000)) or 1000)
        while len(out) < limit:
            data = await self._get(
                "/v2/pnl/leaderboard/top",
                {
                    "days": window_days,
                    "limit": min(page_size, limit - len(out)),
                    "page": page,
                    "pnlMode": pnl_mode,
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
        addr = (
            r.get("wallet")
            or r.get("address")
            or r.get("trader")
            or r.get("traderAddress")
        )
        if not addr:
            return None
        period = r.get("period") or {}
        identity = r.get("identity") or {}
        tags = r.get("tags") or identity.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return Trader(
            address=str(addr),
            source=self.name,
            label=identity.get("name") or r.get("name") or r.get("label"),
            tags=[str(x) for x in tags],
            reported_realized_pnl=self._f(period, "realized") or self._f(r, "realizedPnl", "pnl"),
            reported_roi=self._f(period, "roi") or self._f(r, "roi"),
            reported_win_rate=self._f(r, "winRate", "win_rate", "winrate"),
            reported_volume_usd=self._f(r, "volume", "volumeUsd", "totalVolumeUsd"),
            reported_trades=int(self._f(r, "trades", "tradesCount", "numTrades")),
        )


def _rows(data: Any) -> list[dict[str, Any]]:
    """Unwrap the several shapes SolanaTracker has used for list payloads."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("traders", "data", "results", "rows", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                return _rows(v)
    return []
