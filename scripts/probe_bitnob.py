"""
Probe what Bitnob's RPC gateway actually supports for our use case.

Three questions:

1. Is Tron's 503 transient, or is the chain unavailable?
2. What shape does Get Prices return?
3. Can we query Bitcoin ADDRESS balances?

Question 3 is the important one. Plain bitcoind has no address index — it can
tell you about blocks and transactions, but not "what does this address hold".
Wallet software solves that with Electrum, which maintains the index. If the
gateway only exposes core bitcoind methods, we cannot scan an xpub through it
and will need a different source for Bitcoin.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv(".env.local")

CLIENT_ID = os.environ["BITNOB_CLIENT_ID"]
CLIENT_SECRET = os.environ["BITNOB_CLIENT_SECRET"]


def signed_post(url: str, request: dict | None) -> tuple[int, str]:
    body = json.dumps(request) if request is not None else ""
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    signature = hmac.new(
        CLIENT_SECRET.encode(),
        f"{CLIENT_ID}:{timestamp}:{nonce}:{body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Auth-Client": CLIENT_ID,
        "X-Auth-Timestamp": timestamp,
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": signature,
    }

    if request is None:
        response = httpx.get(url, headers=headers, timeout=20)
    else:
        response = httpx.post(url, headers=headers, content=body, timeout=20)

    return response.status_code, response.text[:400]


def rpc_body(chain: str, method: str, params) -> dict:
    return {
        "chain": chain,
        "network": "mainnet",
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }


NODES = "https://api.bitnob.com/api/nodes"

print("=" * 70)
print("1. TRON — retrying, and trying a Tron-native method")
print("=" * 70)

for method, params in [
    ("eth_blockNumber", []),
    ("getnowblock", {}),
    ("getaccount", {"address": "TDTGBxfoa12LrwnomU76YfdJEMWEYcoUQN", "visible": True}),
]:
    status, text = signed_post(NODES, rpc_body("tron", method, params))
    print(f"\n  {method}: {status}")
    print(f"    {text[:200]}")

print()
print("=" * 70)
print("2. BITCOIN — can we query an address balance?")
print("=" * 70)

# The Bitcoin genesis address. If any of these return a balance, we can scan.
ADDRESS = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
SCRIPTHASH = "8b01df4e368ea28f8dc0423bcf7a4923e3a12d307c875e47a0cfbf90b5c39161"

for method, params in [
    ("validateaddress", [ADDRESS]),
    ("blockchain.scripthash.get_balance", [SCRIPTHASH]),
    ("blockchain.address.get_balance", [ADDRESS]),
    ("scantxoutset", ["start", [f"addr({ADDRESS})"]]),
    ("getaddressinfo", [ADDRESS]),
]:
    status, text = signed_post(NODES, rpc_body("bitcoin", method, params))
    ok = "OK " if status == 200 and '"error":null' not in text and "error" not in text[:120] else "   "
    print(f"\n  {ok}{method}: {status}")
    print(f"    {text[:250]}")

print()
print("=" * 70)
print("3. PRICES — what shape does it return?")
print("=" * 70)

for path in [
    "https://api.bitnob.com/api/trading/prices",
    "https://api.bitnob.com/api/prices",
    "https://api.bitnob.com/api/trading/rates",
]:
    status, text = signed_post(path, None)
    print(f"\n  {path.split('/api/')[1]}: {status}")
    print(f"    {text[:300]}")