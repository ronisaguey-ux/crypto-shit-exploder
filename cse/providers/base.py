"""Provider base class and shared HTTP helpers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from ..models import Trader

log = logging.getLogger("cse.providers")


class ProviderError(RuntimeError):
    pass


class Provider:
    """Base class for a trader-discovery provider."""

    name = "base"
    base_url = ""
    #: header used for the API key; None means the key goes in a query param
    auth_header: Optional[str] = None
    auth_param: Optional[str] = None
    requires_key = True

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 30.0,
        max_retries: int = 3,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "crypto-shit-exploder/0.1"}
        if self.api_key and self.auth_header:
            h[self.auth_header] = self.api_key
        return h

    def _params(self, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        p = dict(params or {})
        if self.api_key and self.auth_param:
            p[self.auth_param] = self.api_key
        return p

    async def _client_or_new(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.AsyncClient(timeout=self.timeout), True

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        client, created = await self._client_or_new()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last: Optional[Exception] = None
        try:
            for attempt in range(self.max_retries):
                try:
                    resp = await client.get(
                        url, params=self._params(params), headers=self._headers()
                    )
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("retry-after", 2 ** attempt))
                        log.warning("%s rate-limited, sleeping %.1fs", self.name, wait)
                        await asyncio.sleep(min(wait, 30))
                        continue
                    if resp.status_code >= 400:
                        raise ProviderError(
                            f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}"
                        )
                    return resp.json()
                except (httpx.HTTPError, ProviderError) as e:
                    last = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(0.5 * (2 ** attempt))
            raise ProviderError(f"{self.name} failed after {self.max_retries} tries: {last}")
        finally:
            if created:
                await client.aclose()

    async def _post(self, path: str, json_body: dict[str, Any]) -> Any:
        client, created = await self._client_or_new()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            resp = await client.post(
                url, json=json_body, headers=self._headers(), params=self._params()
            )
            if resp.status_code >= 400:
                raise ProviderError(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        finally:
            if created:
                await client.aclose()

    # -------------------------------------------------------------- contract
    async def top_traders(self, limit: int = 1000, window_days: int = 30, **kw) -> list[Trader]:
        """Return ranked traders. Subclasses must implement."""
        raise NotImplementedError

    def available(self) -> bool:
        return bool(self.api_key) or not self.requires_key

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _f(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        """First present numeric key, coerced to float."""
        for k in keys:
            v = d.get(k)
            if v is None:
                # support nested a.b
                cur: Any = d
                for part in k.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        cur = None
                        break
                v = cur
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return default
