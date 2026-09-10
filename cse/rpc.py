"""Keyless-capable Solana JSON-RPC pool.

The whole point of this module is to run the tracker for free: no single
provider has to carry 5,000 wallets for six months, so requests are spread
across a pool of free/keyless endpoints, each with its own rate limit and its
own health state. A dead or throttled endpoint is parked and the pool moves on.

Design notes:
- Per-endpoint token bucket. Solana's public endpoint enforces limits in
  10-second windows and separately per method; we approximate with a
  conservative requests/second budget per endpoint and back off on 429.
- Heavy/archival methods (getTransaction, getSignaturesForAddress) are the
  expensive ones, so each endpoint carries a separate `heavy_rps`.
- Failures park an endpoint for a cooldown instead of retrying it forever.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("cse.rpc")

#: Methods that count against the expensive per-endpoint budget.
HEAVY_METHODS = {
    "getTransaction",
    "getSignaturesForAddress",
    "getProgramAccounts",
    "getBlock",
    "getBlocks",
    "getTokenAccountsByOwner",
}


class RpcError(RuntimeError):
    """A JSON-RPC call failed on every endpoint."""


class _Bucket:
    """Token bucket used to smooth a single endpoint's request rate."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(rate, 0.01)
        self.capacity = burst if burst is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(min(wait, 5.0))


@dataclass
class RpcEndpoint:
    """One Solana JSON-RPC HTTP endpoint."""

    url: str
    name: str = ""
    #: Requests/second for cheap methods.
    rps: float = 5.0
    #: Requests/second for heavy (archival) methods.
    heavy_rps: float = 1.0
    #: Relative credit cost, used to prefer the cheapest healthy endpoint.
    cost: float = 1.0
    #: Extra HTTP headers (e.g. an API key header, if a provider wants one).
    headers: dict[str, str] = field(default_factory=dict)

    # runtime state
    failures: int = 0
    parked_until: float = 0.0
    served: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.url.split("//")[-1].split("/")[0]
        self._cheap = _Bucket(self.rps)
        self._heavy = _Bucket(self.heavy_rps)

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.parked_until

    def park(self, seconds: float) -> None:
        self.parked_until = max(self.parked_until, time.monotonic() + seconds)
        log.warning("rpc %s parked for %.0fs (failures=%d)", self.name, seconds, self.failures)

    def note_success(self) -> None:
        self.failures = 0
        self.served += 1

    def note_failure(self, *, rate_limited: bool = False) -> None:
        self.failures += 1
        if rate_limited:
            # Rate limits are expected on free tiers: short, growing backoff.
            self.park(min(2.0 ** min(self.failures, 6), 120.0))
        elif self.failures >= 3:
            self.park(min(2.0 ** min(self.failures, 8), 600.0))


def default_endpoints(helius_key: str = "", alchemy_key: str = "") -> list[RpcEndpoint]:
    """Free/keyless endpoints first, then any keyed free tiers we were given.

    The keyless entries need no signup at all. They are deliberately budgeted
    low because they are shared infrastructure and will ban abusers.
    """
    eps = [
        # Solana Foundation public endpoint. Measured: parks quickly under load,
        # so it is budgeted low and treated as a bonus rather than the workhorse.
        RpcEndpoint(
            "https://api.mainnet-beta.solana.com",
            name="public",
            rps=3.0,
            heavy_rps=2.0,
            cost=0.0,
        ),
        # PublicNode: two independent hosts, no signup, no key. Measured to carry
        # ~2.7 req/s each without complaint, which is where the real budget lives.
        RpcEndpoint("https://solana-rpc.publicnode.com", name="publicnode", rps=4.0, heavy_rps=3.0, cost=0.0),
        RpcEndpoint("https://solana.publicnode.com", name="publicnode2", rps=4.0, heavy_rps=3.0, cost=0.0),
    ]
    if alchemy_key:
        eps.append(
            RpcEndpoint(
                f"https://solana-mainnet.g.alchemy.com/v2/{alchemy_key}",
                name="alchemy",
                rps=20.0,
                heavy_rps=10.0,
                cost=1.0,
            )
        )
    if helius_key:
        eps.append(
            RpcEndpoint(
                f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
                name="helius",
                rps=8.0,
                heavy_rps=4.0,
                cost=1.0,
            )
        )
    return eps


class RpcPool:
    """Round-robins JSON-RPC calls across a set of free endpoints."""

    def __init__(
        self,
        endpoints: list[RpcEndpoint],
        timeout: float = 30.0,
        max_attempts: int = 6,
        client: Optional[httpx.AsyncClient] = None,
        max_connections: int = 16,
    ):
        if not endpoints:
            raise ValueError("RpcPool needs at least one endpoint")
        self.endpoints = endpoints
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._client = client
        self._owns_client = client is None
        self.max_connections = max(2, int(max_connections))
        self._ids = itertools.count(1)
        #: Signatures already fetched, so a re-delivered notification is free.
        self._seen: set[str] = set()
        self._seen_cap = 200_000
        self.stats: dict[str, int] = {"calls": 0, "errors": 0, "rate_limited": 0}

    # ------------------------------------------------------------------ http
    async def _client_or_new(self) -> tuple[httpx.AsyncClient, bool]:
        if self._client is not None:
            return self._client, False
        # Bounded pools: httpx defaults to 100 connections with 20 keep-alive,
        # and every idle keep-alive socket holds read/write buffers for as long as
        # the process lives. A six-month run does not need 100 sockets to two hosts.
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=max(2, self.max_connections // 2),
            keepalive_expiry=30.0,
        )
        return httpx.AsyncClient(timeout=self.timeout, limits=limits), True

    def _order(self, heavy: bool) -> list[RpcEndpoint]:
        """Cheapest healthy endpoint first, so free tiers absorb the load."""
        live = [e for e in self.endpoints if e.healthy]
        if not live:
            live = self.endpoints  # everything parked: try anyway
        return sorted(live, key=lambda e: (e.cost, e.failures, e.served))

    async def call(
        self,
        method: str,
        params: Optional[list[Any]] = None,
        *,
        heavy: Optional[bool] = None,
    ) -> Any:
        """Call a JSON-RPC method, rotating endpoints until one answers."""
        if heavy is None:
            heavy = method in HEAVY_METHODS
        params = params or []
        client, created = await self._client_or_new()
        last: Optional[Exception] = None
        # Order once per call: within this call we walk the list rather than
        # re-sorting (which would keep re-selecting a just-failed endpoint).
        order = self._order(heavy)
        try:
            for attempt in range(self.max_attempts):
                ep = order[attempt % len(order)]
                bucket = ep._heavy if heavy else ep._cheap
                await bucket.acquire()
                payload = {
                    "jsonrpc": "2.0",
                    "id": next(self._ids),
                    "method": method,
                    "params": params,
                }
                try:
                    resp = await client.post(ep.url, json=payload, headers=ep.headers)
                    self.stats["calls"] += 1
                    if resp.status_code == 429:
                        ep.note_failure(rate_limited=True)
                        self.stats["rate_limited"] += 1
                        await asyncio.sleep(min(float(resp.headers.get("retry-after", 1)), 10))
                        continue
                    if resp.status_code >= 400:
                        # A per-endpoint rejection (plan limits, unsupported
                        # method, geo block) must not kill the call: park this
                        # endpoint and let the next one try.
                        ep.note_failure(rate_limited=True)
                        last = RpcError(f"{ep.name} HTTP {resp.status_code}: {resp.text[:200]}")
                        continue
                    body = resp.json()
                except (httpx.HTTPError, ValueError) as e:
                    last = e
                    ep.note_failure()
                    self.stats["errors"] += 1
                    continue

                err = body.get("error")
                if err:
                    code = err.get("code")
                    # Retryable, or a per-endpoint capability gap: either way,
                    # try the next endpoint rather than failing the whole call.
                    ep.note_failure(rate_limited=True)
                    last = RpcError(f"{ep.name} rpc {code}: {err.get('message')}")
                    continue
                ep.note_success()
                return body.get("result")
        finally:
            if created:
                await client.aclose()
        raise RpcError(f"{method} failed on all endpoints: {last}")

    # ------------------------------------------------------------- convenience
    def already_seen(self, signature: str) -> bool:
        return signature in self._seen

    def mark_seen(self, signature: str) -> None:
        if len(self._seen) >= self._seen_cap:
            # Cheap bounded eviction: drop the oldest half.
            self._seen = set(list(self._seen)[self._seen_cap // 2 :])
        self._seen.add(signature)

    async def get_transaction(self, signature: str, *, commitment: str = "confirmed") -> Optional[dict]:
        """Fetch a parsed transaction. Returns None when the node has pruned it."""
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": commitment,
                },
            ],
            heavy=True,
        )

    async def get_signatures_for_address(
        self, address: str, *, before: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        opts: dict[str, Any] = {"limit": min(limit, 1000)}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts], heavy=True) or []

    async def get_health(self) -> Optional[str]:
        return await self.call("getHealth", [], heavy=False)

    def summary(self) -> dict[str, Any]:
        return {
            "stats": dict(self.stats),
            "endpoints": [
                {
                    "name": e.name,
                    "served": e.served,
                    "failures": e.failures,
                    "healthy": e.healthy,
                }
                for e in self.endpoints
            ],
        }

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
