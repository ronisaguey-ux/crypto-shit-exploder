"""Sharded Solana WebSocket subscriptions for thousands of wallets.

`logsSubscribe` accepts exactly ONE address per subscription (passing more is an
error), so watching N wallets needs N subscriptions. Free tiers make that
workable but only if you shard correctly:

    Helius free : 5 connections x 1,000 subscriptions = 5,000 wallets exactly
    public RPC  : ~100 subscriptions per connection

This pool spreads the wallet list across connections, keeps a
`subscription id -> wallet` map per socket, reconnects with backoff, and
re-subscribes automatically. Notifications land on a bounded queue; if the
consumer falls behind we drop the oldest and count it rather than stalling the
socket, because a stalled socket gets killed by the provider.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Optional

from .swapdecode import is_swap_candidate

log = logging.getLogger("cse.ws")

try:  # websockets >= 12
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - surfaced with a clear message at runtime
    websockets = None  # type: ignore

    class ConnectionClosed(Exception):  # type: ignore
        pass


@dataclass
class WsEndpoint:
    url: str
    name: str = ""
    #: How many subscriptions this socket will hold.
    subs_per_connection: int = 1000
    #: How many sockets we may open against this endpoint.
    max_connections: int = 5

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.url.split("//")[-1].split("/")[0].split("?")[0]


@dataclass
class Notification:
    wallet: str
    signature: str
    logs: list[str] = field(default_factory=list)
    slot: Optional[int] = None
    received_at: float = field(default_factory=time.time)


class SubscriptionPool:
    """Fans a wallet list out across sharded `logsSubscribe` sockets."""

    def __init__(
        self,
        endpoints: list[WsEndpoint],
        *,
        queue_size: int = 100_000,
        swap_filter: bool = True,
        commitment: str = "confirmed",
        ping_interval: float = 30.0,
        reconnect_base: float = 1.0,
        reconnect_max: float = 60.0,
    ):
        if not endpoints:
            raise ValueError("SubscriptionPool needs at least one endpoint")
        if websockets is None:
            raise RuntimeError(
                "the 'websockets' package is required for live watching: pip install websockets"
            )
        self.endpoints = endpoints
        self.queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=queue_size)
        self.swap_filter = swap_filter
        self.commitment = commitment
        self.ping_interval = ping_interval
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max

        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._shards: list[list[str]] = []
        self._ids = itertools.count(1)
        self.stats = {
            "notifications": 0,
            "swaps": 0,
            "filtered": 0,
            "dropped": 0,
            "reconnects": 0,
            "subscriptions": 0,
        }

    # ------------------------------------------------------------- planning
    def plan_shards(self, wallets: Iterable[str]) -> list[tuple[WsEndpoint, list[str]]]:
        """Split wallets across (endpoint, connection) slots respecting caps."""
        wallets = list(dict.fromkeys(wallets))
        slots: list[tuple[WsEndpoint, list[str]]] = []
        i = 0
        for ep in self.endpoints:
            for _ in range(max(ep.max_connections, 0)):
                if i >= len(wallets):
                    break
                chunk = wallets[i : i + ep.subs_per_connection]
                i += len(chunk)
                if chunk:
                    slots.append((ep, chunk))
            if i >= len(wallets):
                break
        if i < len(wallets):
            log.warning(
                "wallet capacity %d < %d wallets requested; %d unwatched",
                i,
                len(wallets),
                len(wallets) - i,
            )
        return slots

    def capacity(self) -> int:
        return sum(ep.subs_per_connection * ep.max_connections for ep in self.endpoints)

    # ---------------------------------------------------------------- run
    async def start(self, wallets: Iterable[str]) -> int:
        """Open every shard. Returns the number of wallets actually watched."""
        self._running = True
        slots = self.plan_shards(wallets)
        watched = 0
        for idx, (ep, chunk) in enumerate(slots):
            self._shards.append(chunk)
            watched += len(chunk)
            task = asyncio.create_task(
                self._serve(ep, chunk, idx), name=f"ws-{ep.name}-{idx}"
            )
            self._tasks.append(task)
        log.info(
            "watching %d wallets over %d sockets across %d endpoint(s)",
            watched,
            len(slots),
            len({ep.name for ep, _ in slots}),
        )
        return watched

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _serve(self, ep: WsEndpoint, wallets: list[str], shard: int) -> None:
        """Keep one socket alive, subscribed, and draining notifications."""
        backoff = self.reconnect_base
        while self._running:
            try:
                async with websockets.connect(
                    ep.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=max(self.ping_interval * 0.7, 10.0),
                    close_timeout=5,
                    max_size=None,
                ) as sock:
                    log.info("ws %s#%d connected (%d wallets)", ep.name, shard, len(wallets))
                    backoff = self.reconnect_base
                    await self._subscribe_all(sock, wallets)
                    await self._drain(sock, wallets)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any socket error means reconnect
                log.warning("ws %s#%d dropped: %s", ep.name, shard, e)
            if not self._running:
                break
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max)

    async def _subscribe_all(self, sock, wallets: list[str]) -> None:
        for wallet in wallets:
            await sock.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": next(self._ids),
                        "method": "logsSubscribe",
                        "params": [{"mentions": [wallet]}, {"commitment": self.commitment}],
                    }
                )
            )
            self.stats["subscriptions"] += 1
            # Yield so the read loop can drain acks on very large shards.
            if self.stats["subscriptions"] % 200 == 0:
                await asyncio.sleep(0)

    async def _drain(self, sock, wallets: list[str]) -> None:
        """Read messages until the socket dies; map subs back to wallets.

        A socket answers subscribe requests in the order they were sent, and a
        notification for a subscription can only arrive after that
        subscription's ack. Processing messages strictly in order therefore
        guarantees the map is populated before any notification needs it.
        """
        sub_to_wallet: dict[int, str] = {}
        acked = 0
        async for raw in sock:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if msg.get("method") == "logsNotification":
                params = msg.get("params") or {}
                sub = params.get("subscription")
                wallet = sub_to_wallet.get(sub)
                if wallet is None:
                    continue
                value = (params.get("result") or {}).get("value") or {}
                sig = value.get("signature")
                if not sig:
                    continue
                logs = value.get("logs") or []
                self.stats["notifications"] += 1
                if self.swap_filter and not is_swap_candidate(logs):
                    self.stats["filtered"] += 1
                    continue
                self.stats["swaps"] += 1
                self._push(
                    Notification(
                        wallet=wallet,
                        signature=sig,
                        logs=logs,
                        slot=(params.get("result") or {}).get("context", {}).get("slot"),
                    )
                )
            elif "id" in msg and msg.get("id") is not None:
                # Subscription ack: {"result": <sub id>}, in send order.
                result = msg.get("result")
                if isinstance(result, int) and acked < len(wallets):
                    sub_to_wallet[result] = wallets[acked]
                elif msg.get("error"):
                    log.debug("subscribe error: %s", msg["error"])
                acked += 1

    def _push(self, note: Notification) -> None:
        try:
            self.queue.put_nowait(note)
        except asyncio.QueueFull:
            # Consumer is behind: drop the oldest rather than block the socket.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.stats["dropped"] += 1
            try:
                self.queue.put_nowait(note)
            except asyncio.QueueFull:
                pass

    async def notifications(self) -> AsyncIterator[Notification]:
        """Yield notifications until stopped."""
        while self._running or not self.queue.empty():
            try:
                yield await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    def summary(self) -> dict:
        return {"stats": dict(self.stats), "queued": self.queue.qsize()}
