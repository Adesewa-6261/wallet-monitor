# WalletNest

Wallet monitoring for Bitnob. Watches crypto wallets across Bitcoin, Ethereum,
Base and Tron, and reports transactions as they happen.

Two clients consume this API: a Flutter mobile app, and a Telegram bot
(`@thewalletmonitorbot`) which lives in `bot/` and runs in the same process.

## Stack

FastAPI · Postgres · Bitnob Blockchain RPC · deployed on Render

## Running locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env.local      # then fill in the values
```

Create the database tables:

```bash
psql "$DATABASE_URL" -f schema.sql
```

Start the server:

```bash
uvicorn app.main:app --reload
```

Interactive API docs at http://127.0.0.1:8000/docs

## Layout

```
app/
├── main.py          FastAPI app and router wiring
├── db.py            connection pool
├── core/            config, errors, http, cache, security
├── adapters/        one module per data source
├── api/routes/      HTTP endpoints
├── schemas/         request and response models
└── services/        business logic
scripts/             manual checks, not part of the app
schema.sql           database tables
```

Dependencies point one way — `adapters` and `core` never import from `routes`,
so any adapter can be tested without running the server.

## Data sources

| Chain | Balances | History |
|---|---|---|
| Ethereum, Base | Bitnob RPC | Bitnob RPC — `eth_getLogs` for tokens; balance/nonce delta plus `trace_block` for native ETH |
| Bitcoin | mempool.space, Blockstream fallback | same, paged |
| Tron | Bitnob RPC | TronGrid |
| Prices: BTC, USDT, USDC, NGN | Bitnob Trading | |
| Prices: ETH, TRX | CoinGecko | |

Tron history comes from TronGrid for the same reason Bitcoin balances come from
Esplora: it is an index, and Bitnob proxies the node rather than the index.
Both are isolated behind their adapter.

### Seeing native ETH

A plain ETH transfer emits no event log, so `eth_getLogs` cannot see it.
`trace_filter` would answer this in one call, but the gateway returns -32601 for
it on both chains. What the gateway does expose is archive state, so native
movement is found by comparing balance and nonce against the previous cycle
and — only when something moved — binary searching the range to locate the
block, then tracing that block.

Two calls per cycle for a quiet wallet, about twelve for one that moved. The
nonce matters as much as the balance: it only ever increases, so it catches an
outgoing transfer even when money out and money in net to zero within a cycle.

Bitcoin balances come from Esplora rather than a node RPC, because address-level queries need an address index that bitcoind does not maintain. Changing source later would touch only app/adapters/bitcoin.py.

## How monitoring works

A background loop runs every two minutes, started with the app. For each wallet
it asks the chain what has happened since the last recorded position, stores
anything new, and sends an alert per transaction.

Three properties make restarts safe:

- **Position, not time.** Sync works from `wallet_sync_state.last_block`, not
  "the last two minutes". A time-based window would silently drop everything
  that happened while the service was down.
- **Idempotent inserts.** `transactions` is unique on
  `(wallet_id, tx_hash, symbol, direction)`.
- **Idempotent alerts.** `alerted_at` is null until delivery succeeds, so a
  crash mid-cycle cannot double-notify.

Trigger a cycle manually with `POST /api/monitor/run`. Set `MONITOR_ENABLED=false`
to stop the loop entirely — worth doing whenever a local checkout points at the
production database, because otherwise it delivers real alerts to real people.

### Falling behind beats going blind

Every chain caps how much it processes in one cycle, because an exchange hot
wallet can produce thousands of transfers an hour and would otherwise hang the
loop. When a cap trims the batch, the sync position is held *below* what was
dropped rather than advanced to the chain tip, so the remainder is re-read next
cycle. The wallet falls behind temporarily; it never skips.

### Not shouting on arrival

A wallet's first sync, or one resuming after more than six hours, is treated as
a backfill: rows are stored with `alerted_at` already set. The feed fills
immediately and the phone stays quiet, because that history is something the
user already knows about. Without it, adding a wallet means a wall of
notifications for transactions going back years.

### Knowing it still works

A monitoring product has a failure mode most apps do not: when the poller stops,
nothing happens — which looks exactly like nothing happening. A user seeing no
alerts cannot tell whether their wallets are quiet or whether we stopped
watching two days ago.

So a daily digest goes out even when there is nothing to report, and
`GET /api/monitor/status` exposes per-wallet sync freshness so a client can show
a warning rather than silently displaying stale balances.

### Bitcoin net effect

Bitcoin has no accounts. A transaction consumes whole previous outputs and
creates new ones, so spending 0.02 BTC from a 1.9 BTC chunk means the entire 1.9
leaves and 1.88 returns as change to a fresh address the same wallet owns.

Read naively that reports "sent 1.9 BTC", which is alarming and wrong. So
`_bitcoin_net_effect` computes value in minus value out. When every output
belongs to the user, nothing left their control — that is labelled `internal`
and shows only the network fee.

## Conventions

**Crypto amounts are strings; fiat values are floats.** An 8-decimal Bitcoin
amount does not survive a float. Parse with `Decimal`.

**Errors have two levels.** A whole-request failure returns an HTTP error status.
A single wallet failing returns HTTP 200 with an `error` object on that wallet,
so one unreachable provider never blanks the user's screen.

**Every outbound call goes through `core/http.py`**, which adds timeouts, retry
with jittered backoff, and a concurrency limiter. It never includes a provider's
response body in an error — those can echo back the request URL, which contains
our API key.

## The bot

`bot/` is the command surface — linking, listing, settings. It does **not**
deliver alerts; the poller does that directly in `app/services/monitor.py`.

It long-polls rather than using a webhook, so it needs no public URL and behaves
identically on a laptop and on Render. It does not start if `TELEGRAM_BOT_TOKEN`
is unset.

The bot is not a user. It holds no password, proves itself to the API with
`BOT_SHARED_SECRET`, and exchanges a `chat_id` for a normal user token via
`POST /api/auth/telegram/session` — which only ever works for a chat that has
already completed the link flow.

```
/link CODE   connect this chat        /mute /unmute   pause and resume
/wallets     what is being watched    /digest         toggle daily summary
/recent      latest transactions      /threshold 5    ignore incoming under $5
/status      is monitoring working    /stop           disconnect
/settings    alert preferences        /help
```

## Tests

```bash
python -m pytest tests/ -q
```

Covers the two pure functions carrying the most risk: input detection, which is
the boundary stopping a private key from reaching the database, and the Bitcoin
net-effect arithmetic. Neither touches the network or the database.

## Scripts

Manual checks, run from the project root with the venv active:

```bash
python scripts/check_bitnob.py    # which chains the API key can reach
python scripts/probe_bitnob.py    # request/response shapes, for debugging
```
