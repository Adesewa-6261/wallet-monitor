"""
app/adapters/bitnob.py

Client for Bitnob's APIs.

The RPC gateway is a single endpoint for every chain — the chain and network go
in the request body rather than the URL. That is unusual compared to most node
providers, which give you a different URL per chain.

Note the request shape differs from the published documentation. The connection
guide shows the JSON-RPC call nested under a "payload" key; sending it that way
returns 400 "rpc method required". The gateway actually wants the JSON-RPC
fields flattened alongside "chain" and "network". Responses are also wrapped in
a "data" envelope rather than returned bare. Verified against the live API,
2026-08-11.

Requests are signed with HMAC rather than a bearer token. The signature covers
client ID, timestamp, nonce and body, joined with colons. The timestamp limits
replay attacks and the nonce stops the same signed request being accepted twice.
"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Optional

from ..core.config import config
from ..core.errors import RequestError
from ..core.http import fetch_json

NODES_URL = "https://api.bitnob.com/api/nodes"
API_BASE = "https://api.bitnob.com/api"

SUPPORTED_CHAINS = {"bitcoin", "ethereum", "base", "tron", "polygon", "bsc", "solana"}


def _signed_headers(body: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())

    payload = f"{config.bitnob.client_id}:{timestamp}:{nonce}:{body}"
    signature = hmac.new(
        config.bitnob.client_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "X-Auth-Client": config.bitnob.client_id,
        "X-Auth-Timestamp": timestamp,
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": signature,
    }


async def rpc(
    chain: str,
    method: str,
    params: Any = None,
    network: str = "mainnet",
) -> Any:
    """
    Make a JSON-RPC call through the gateway.

    `params` is a list for EVM and Bitcoin, but a dict for some Tron methods,
    so it is passed through as given rather than coerced.
    """
    if chain not in SUPPORTED_CHAINS:
        raise RequestError("UNSUPPORTED_INPUT", f"Unsupported chain: {chain}")

    request = {
        "chain": chain,
        "network": network,
        "jsonrpc": "2.0",
        "method": method,
        "params": params if params is not None else [],
        "id": 1,
    }

    # The signature covers the exact bytes sent, so the body is serialised once
    # here and reused rather than letting the HTTP client re-encode it.
    body = json.dumps(request)

    response = await fetch_json(
        NODES_URL,
        method="POST",
        headers=_signed_headers(body),
        content=body,
        label=f"bitnob-rpc/{chain}",
        timeout=15.0,
    )

    if not isinstance(response, dict):
        raise RequestError("UPSTREAM_UNAVAILABLE", f"{chain} {method}: unexpected response")

    # Gateway-level failure, e.g. an unsupported chain.
    if response.get("success") is False:
        message = (response.get("error") or {}).get("message", "call failed")
        raise RequestError("UPSTREAM_UNAVAILABLE", f"{chain} {method}: {message}")

    # The JSON-RPC response sits inside a "data" envelope.
    rpc_response = response.get("data", response)

    # JSON-RPC reports its own failures inside a 200, so they need checking
    # separately from HTTP status.
    if rpc_response.get("error"):
        message = rpc_response["error"].get("message", "call failed")
        raise RequestError("UPSTREAM_UNAVAILABLE", f"{chain} {method}: {message}")

    return rpc_response.get("result")


async def get_prices() -> dict[str, float]:
    """
    USD-equivalent rates for the assets Bitnob quotes.

    Prices come back as currency pairs rather than a flat USD map, and BTC is
    quoted against USDC rather than USD. Since USDC tracks the dollar closely we
    treat a USD or USDC quote as interchangeable for display purposes — the
    difference is well inside the noise of a portfolio figure, and using the
    stablecoin rate avoids needing a second price source.

    ETH and TRX are not quoted here at all, so those come from CoinGecko.
    """
    response = await fetch_json(
        f"{API_BASE}/trading/prices",
        headers=_signed_headers(""),
        label="bitnob-prices",
        timeout=10.0,
    )

    pairs = (response or {}).get("data", {}).get("prices", [])

    prices: dict[str, float] = {}
    for pair in pairs:
        base = (pair.get("base_currency") or "").upper()
        quote = (pair.get("quote_currency") or "").upper()
        raw = pair.get("price")

        if not base or raw is None:
            continue

        if quote in ("USD", "USDC", "USDT"):
            prices[base] = float(raw)
        elif base in ("USD", "USDC", "USDT") and quote == "NGN":
            # Gives us the naira rate, which the app needs for its NGN view.
            prices["NGN_PER_USD"] = float(raw)

    # Stablecoins are worth a dollar by definition unless something has gone
    # badly wrong, and a missing quote should not blank them out.
    prices.setdefault("USDT", 1.0)
    prices.setdefault("USDC", 1.0)

    return prices


async def health_check(chain: str = "ethereum") -> dict:
    """
    Confirm credentials and connectivity. Useful as a first call after setting
    up keys, and as something to point at when reporting a problem to Bitnob.
    """
    if chain == "bitcoin":
        result = await rpc("bitcoin", "getblockchaininfo")
    else:
        result = await rpc(chain, "eth_chainId")
    return {"chain": chain, "ok": True, "result": result}