"""Keyless USD pricing for Solana tokens.

Replaces Birdeye (30k CU/month dies in days). Both sources below are free and
need no account:

  * DexScreener   - no key, ~300 req/min, up to 30 mints per call, and it
                    returns pool liquidity which the slippage model needs.
  * GeckoTerminal - no key, ~30 req/min, batched simple price endpoint; used as
                    the fallback when DexScreener has no pair.

Prices are cached with a TTL because most of the time nothing has moved, and
the free rate limits are the binding constraint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger("cse.prices")

DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
#: DexScreener accepts at most 30 comma-separated addresses per request.
DEXSCREENER_BATCH = 30


@dataclass
class TokenPrice:
    mint: str
    usd: float
    liquidity_usd: float = 0.0
    source: str = ""
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "usd": self.usd,
            "liquidity_usd": self.liquidity_usd,
            "source": self.source,
            "at": self.at,
        }


class PriceOracle:
    """Batched, cached, keyless token pricing."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        timeout: float = 20.0,
        min_interval: float = 0.25,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.ttl = ttl_seconds
        self.timeout = timeout
        self._min_interval = min_interval
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, TokenPrice] = {}
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self.stats = {"dexscreener": 0, "geckoterminal": 0, "misses": 0}

    async def _client_or_new(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self.timeout), True

    async def _throttle(self) -> None:
        """Keep well clear of the free-tier request rates."""
        async with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            self._last_call = time.monotonic()

    # -------------------------------------------------------------- sources
    async def _dexscreener(self, mints: list[str]) -> dict[str, TokenPrice]:
        out: dict[str, TokenPrice] = {}
        client, created = await self._client_or_new()
        try:
            for i in range(0, len(mints), DEXSCREENER_BATCH):
                chunk = mints[i : i + DEXSCREENER_BATCH]
                await self._throttle()
                url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(chunk)}"
                try:
                    resp = await client.get(url, headers={"Accept": "application/json"})
                    if resp.status_code == 429:
                        await asyncio.sleep(2.0)
                        continue
                    if resp.status_code >= 400:
                        continue
                    pairs = (resp.json() or {}).get("pairs") or []
                except (httpx.HTTPError, ValueError) as e:
                    log.debug("dexscreener failed: %s", e)
                    continue
                self.stats["dexscreener"] += 1
                # Best (deepest) pair wins per mint.
                best: dict[str, TokenPrice] = {}
                for p in pairs:
                    base = (p.get("baseToken") or {}).get("address")
                    quote = (p.get("quoteToken") or {}).get("address")
                    try:
                        usd = float(p.get("priceUsd") or 0)
                    except (TypeError, ValueError):
                        continue
                    if usd <= 0:
                        continue
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    for mint in (base, quote):
                        if not mint or mint not in chunk:
                            continue
                        prev = best.get(mint)
                        if prev is None or liq > prev.liquidity_usd:
                            best[mint] = TokenPrice(
                                mint=mint,
                                usd=usd,
                                liquidity_usd=liq,
                                source="dexscreener",
                                at=time.time(),
                            )
                out.update(best)
        finally:
            if created:
                await client.aclose()
        return out

    async def _geckoterminal(self, mints: list[str]) -> dict[str, TokenPrice]:
        if not mints:
            return {}
        out: dict[str, TokenPrice] = {}
        client, created = await self._client_or_new()
        try:
            await self._throttle()
            url = f"{GECKO_BASE}/simple/networks/solana/token_price/{','.join(mints)}"
            resp = await client.get(
                url,
                headers={
                    "Accept": "application/json;version=20230302",
                    "User-Agent": "crypto-shit-exploder/0.1",
                },
            )
            if resp.status_code == 429:
                return {}
            if resp.status_code >= 400:
                return {}
            prices = (
                ((resp.json() or {}).get("data") or {}).get("attributes", {}).get("token_prices", {})
            )
            self.stats["geckoterminal"] += 1
            for mint, val in prices.items():
                try:
                    usd = float(val)
                except (TypeError, ValueError):
                    continue
                if usd > 0:
                    out[mint] = TokenPrice(mint=mint, usd=usd, source="geckoterminal", at=time.time())
        except (httpx.HTTPError, ValueError) as e:
            log.debug("geckoterminal failed: %s", e)
        finally:
            if created:
                await client.aclose()
        return out

    # ------------------------------------------------------------------ api
    def cached(self, mint: str) -> Optional[TokenPrice]:
        p = self._cache.get(mint)
        if p and (time.time() - p.at) < self.ttl:
            return p
        return None

    async def get(self, mints: list[str]) -> dict[str, TokenPrice]:
        """Prices for many mints, serving fresh cache entries for free."""
        wanted = [m for m in dict.fromkeys(mints) if m]
        result: dict[str, TokenPrice] = {}
        missing: list[str] = []
        for m in wanted:
            hit = self.cached(m)
            if hit is not None:
                result[m] = hit
            else:
                missing.append(m)
        if not missing:
            return result

        found = await self._dexscreener(missing)
        result.update(found)
        still = [m for m in missing if m not in found]
        if still:
            result.update(await self._geckoterminal(still))
        self.stats["misses"] += len(missing)
        self._cache.update(result)
        return result

    async def price(self, mint: str) -> Optional[float]:
        hit = self.cached(mint)
        if hit is not None:
            return hit.usd
        got = await self.get([mint])
        p = got.get(mint)
        return p.usd if p else None

    async def liquidity(self, mint: str) -> Optional[float]:
        hit = self.cached(mint)
        if hit is not None:
            return hit.liquidity_usd
        got = await self.get([mint])
        p = got.get(mint)
        return p.liquidity_usd if p else None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
