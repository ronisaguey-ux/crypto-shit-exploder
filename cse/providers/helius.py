"""Helius: webhook management + parsed-transaction history.

Docs: https://www.helius.dev/docs
- Webhooks:  POST/GET/PUT/DELETE https://api-mainnet.helius-rpc.com/v0/webhooks?api-key=KEY
- Parsed tx: POST https://api-mainnet.helius-rpc.com/v0/transactions?api-key=KEY
- History:   GET  https://api-mainnet.helius-rpc.com/v0/addresses/{wallet}/transactions
- Enhanced WSS: wss://mainnet.helius-rpc.com/?api-key=KEY (transactionSubscribe)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from ..models import Side, Trade
from .base import Provider, ProviderError

log = logging.getLogger("cse.providers.helius")


class HeliusProvider(Provider):
    name = "helius"
    base_url = "https://api-mainnet.helius-rpc.com"
    auth_param = "api-key"

    # ------------------------------------------------------------- webhooks
    async def create_webhook(
        self,
        webhook_url: str,
        account_addresses: Iterable[str],
        transaction_types: Optional[list[str]] = None,
        webhook_type: str = "enhanced",
        auth_header: str = "",
    ) -> str:
        """Create an enhanced webhook. Returns the webhookID."""
        if not self.api_key:
            raise ProviderError("HELIUS_API_KEY not set")
        addrs = list(account_addresses)
        # Helius caps a single webhook's address list; chunk to be safe.
        if len(addrs) > 100_000:
            raise ProviderError("too many addresses for one webhook (max 100k)")
        body = {
            "webhookURL": webhook_url,
            "transactionTypes": transaction_types or ["SWAP"],
            "accountAddresses": addrs,
            "webhookType": webhook_type,
        }
        if auth_header:
            body["authHeader"] = auth_header
        data = await self._post("/v0/webhooks", body)
        wid = (data or {}).get("webhookID")
        if not wid:
            raise ProviderError(f"webhook creation returned no id: {data}")
        return wid

    async def list_webhooks(self) -> list[dict[str, Any]]:
        data = await self._get("/v0/webhooks")
        return data if isinstance(data, list) else (data or {}).get("webhooks", [])

    async def update_webhook_addresses(self, webhook_id: str, addresses: Iterable[str]) -> None:
        body = {"accountAddresses": list(addresses)}
        client = self._client
        if client is None:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.put(
                    f"{self.base_url}/v0/webhooks/{webhook_id}",
                    json=body, headers=self._headers(), params=self._params(),
                )
                if r.status_code >= 400:
                    raise ProviderError(f"update_webhook HTTP {r.status_code}: {r.text[:200]}")
            return
        r = await client.put(
            f"{self.base_url}/v0/webhooks/{webhook_id}",
            json=body, headers=self._headers(), params=self._params(),
        )
        if r.status_code >= 400:
            raise ProviderError(f"update_webhook HTTP {r.status_code}: {r.text[:200]}")

    async def delete_webhook(self, webhook_id: str) -> None:
        client = self._client
        if client is None:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.delete(
                    f"{self.base_url}/v0/webhooks/{webhook_id}",
                    headers=self._headers(), params=self._params(),
                )
                if r.status_code >= 400:
                    raise ProviderError(f"delete_webhook HTTP {r.status_code}: {r.text[:200]}")
            return
        r = await client.delete(
            f"{self.base_url}/v0/webhooks/{webhook_id}",
            headers=self._headers(), params=self._params(),
        )
        if r.status_code >= 400:
            raise ProviderError(f"delete_webhook HTTP {r.status_code}: {r.text[:200]}")

    # ---------------------------------------------------------- parsed trades
    async def wallet_swaps(self, wallet: str, limit: int = 100) -> list[Trade]:
        """Parsed SWAP history for a wallet (REST enrichment path)."""
        data = await self._get(
            f"/v0/addresses/{wallet}/transactions",
            {"type": "SWAP", "limit": limit},
        )
        rows = data if isinstance(data, list) else []
        return [t for t in (parse_helius_swap(r) for r in rows) if t]


def parse_helius_swap(tx: dict[str, Any]) -> Optional[Trade]:
    """Convert a Helius enhanced SWAP payload into a Trade.

    Helius gives tokenTransfers plus nativeTransfers. For a swap the feePayer is
    the trader; the non-SOL token leg is what they bought/sold. We infer side
    from whether the SOL leg is inbound (buy) or outbound (sell).
    """
    fee_payer = tx.get("feePayer") or tx.get("feePayer")
    if not fee_payer:
        return None
    transfers = tx.get("tokenTransfers") or []
    if not transfers:
        return None

    native = tx.get("nativeTransfers") or []
    sol_out = sum(
        (n.get("amount") or 0)
        for n in native
        if n.get("fromUserAccount") == fee_payer
    )
    sol_in = sum(
        (n.get("amount") or 0)
        for n in native
        if n.get("toUserAccount") == fee_payer
    )

    # The traded token is the largest non-wSOL transfer.
    WSOL = "So11111111111111111111111111111111111111112"
    legs = [t for t in transfers if t.get("mint") and t.get("mint") != WSOL]
    if not legs:
        return None
    leg = max(legs, key=lambda t: abs(t.get("tokenAmount") or 0))
    amount = abs(float(leg.get("tokenAmount") or 0))
    if amount <= 0:
        return None

    side = Side.BUY if sol_out >= sol_in else Side.SELL
    # Derive a USD price when possible.
    sol_delta = abs(sol_out - sol_in) / 1e9
    price = 0.0
    if sol_delta > 0:
        price_sol = sol_delta / amount
        price = price_sol * float(tx.get("_sol_price_usd") or 0.0)

    return Trade(
        trader=fee_payer,
        mint=str(leg["mint"]),
        side=side,
        price=price,
        amount=amount,
        signature=tx.get("signature"),
        slot=tx.get("slot"),
        observed_at=float(tx.get("timestamp") or 0) or None,
    )


def swap_to_json(tx: dict[str, Any]) -> str:
    return json.dumps(tx, separators=(",", ":"))
