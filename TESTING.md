# Testing the monitor

There are two ways in: the Flutter app, and `/docs`. Use the app for what the
product actually does, and `/docs` when something looks wrong and you need to
know whether the fault is in the API or in the app. Sections 9 and 10 cover
those.

## 1. Start the server

```
uvicorn app.main:app --reload
```

The log should show `monitor started, polling every 2 minutes`.

httpx request logging is turned down to WARNING, so the log shows cycle results
rather than a line per outbound call. If you want to see every request while
debugging, drop that line in `app/main.py`.

## 2. Sign in and add a wallet

At http://127.0.0.1:8000/docs

- `POST /api/auth/login` — copy the `token`
- Click **Authorize** at the top right, paste `Bearer YOUR_TOKEN`
- `POST /api/wallets` with a real address

Good test addresses, all with live activity:

| chain | address |
|---|---|
| Ethereum | `0x28C6c06298d514Db089934071355E5743bf21d60` |
| Bitcoin | `bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97` |

## 2a. Rename a wallet

`PATCH /api/wallets/{wallet_id}` with `{"label": "Treasury"}`. Send
`{"label": null}` to clear it back to the default name.

The label is the only editable field, by design. A wallet's key is what its sync
position and every stored transaction are tied to, so changing it would leave
both describing a wallet we are no longer watching — that is a remove and
re-add. Any other field in the body is ignored rather than applied, which is
worth confirming if you are testing the app's edit screen.

Renaming does not clear the 60-second balance cache, so `GET /api/balances` can
report the old label for up to a minute. The wallet list updates immediately.

## 3. Force a poll

`POST /api/monitor/run` returns:

```json
{"wallets": 1, "new_transactions": 12, "alerts_sent": 0, "digests_sent": 0}
```

`alerts_sent` stays 0 until a Telegram chat is linked — the transactions are
still recorded.

It counts *messages*, not transactions. Above five pending for one wallet the
poller sends a single grouped summary instead of individual alerts, so
`alerts_sent: 1` can represent a hundred transactions.

## 4. See what it found

`GET /api/transactions`

Filters: `wallet_id`, `symbol`, `limit` (max 100), and `before` as an ISO
timestamp cursor for paging.

## 5. Check balances

- `GET /api/balances` — every wallet plus a combined `total_usd`. This is what
  the dashboard's headline figure comes from.
- `GET /api/wallets/{wallet_id}/balance` — one wallet.

Two things to look at rather than skim past:

**A wallet that failed still returns 200.** It carries an `error` string and
`holdings: null`, and the others still add up. A blank screen because one node
was unreachable would be worse than a partial one.

**A missing price is null, never zero.** If a holding shows `value_usd: null`,
its wallet's `value_usd` is null too and that wallet is left out of `total_usd`.
That is deliberate — showing zero where we do not know the price is a lie about
someone's money. Do not "fix" it by summing the priced holdings only; that
understates the wallet just as badly.

Results are cached per wallet for 60 seconds, so a second call within the minute
returns the same numbers without re-scanning. An xpub scan is dozens of HTTP
requests, so this is what makes pull-to-refresh usable.

## 6. Check it is actually watching

`GET /api/monitor/status` — `healthy`, how many wallets, and any that have
stopped updating:

```json
{"healthy": true, "wallets": 3, "stale_wallets": [], "last_sync_at": "..."}
```

A wallet counts as stale after 30 minutes without a sync, or if it recorded an
error. The app should surface this: a balance that quietly stopped updating two
days ago is worse than showing nothing.

`GET /api/wallets/{wallet_id}/debug` is the next step down when one wallet is
behaving differently from another:

```json
{
  "wallet_id": "...",
  "chain": "bitcoin",
  "input_type": "address",
  "address_type": "Native SegWit",
  "watched_addresses_count": 1,
  "last_block": 962018,
  "last_synced_at": "...",
  "last_error": null,
  "transactions_stored": 74,
  "latest_transaction_at": "...",
  "unalerted_count": 0
}
```

Reading it:

- `last_error` set, `last_block` not advancing between polls → the sync itself
  is failing, and the message says why.
- `last_block` advancing but `transactions_stored` flat → sync is fine, there is
  genuinely nothing new.
- `unalerted_count` climbing → nothing is claiming these rows. The poller only
  claims transactions whose account has a Telegram link, so an unlinked account
  is the usual cause; the backlog is kept and goes out once the link exists. If
  the account *is* linked and the number still climbs, the poll cycle is not
  finishing. Note a claimed row is stamped whether or not a message goes out, so
  this counts what has not been picked up, not what failed to send.
- Two accounts watching the same address with different `last_block` → they were
  added at different times, so their initial lookback windows differed.

## 7. Alert settings

- `GET /api/alerts/settings`
- `PATCH /api/alerts/settings` — any subset of `enabled`, `min_value_usd`,
  `alert_on_receive`, `alert_on_send`, `daily_digest`

Worth testing `min_value_usd`: set it above a transaction's value, force a poll,
and that transaction should be marked alerted without a message being sent. It
is not left pending, or it would be re-evaluated forever.

## 8. Link Telegram

- `POST /api/auth/telegram/code` → returns an eight-character code formatted
  `ABCD-EFGH`
- Your telegram bot calls `POST /api/auth/telegram/link` with that code and the
  chat id
- Next cycle delivers alerts

The code is eight characters from a 32-symbol alphabet, not six digits. The link
endpoint is a natural brute-force target — an attacker does not need to know
whose code they are guessing, since any valid code would do — so the extra
length matters. The alphabet has no `0`/`O` or `1`/`I`/`L`, and the hyphen, case
and stray spaces are all accepted on the way back in.

Codes expire after ten minutes, are deleted on use, and are stored hashed. Five
wrong attempts from one chat id locks that chat out for fifteen minutes, so
testing failures repeatedly will lock you out — the lockout clears on a
successful link, or delete the row from `telegram_link_attempts`.

To test without the bot, call `/api/auth/telegram/link` directly with your own
chat id. Message @userinfobot on Telegram to find it.

## 9. Testing through the Flutter app

This is the test that counts. `/docs` proves an endpoint answers; the app is
what shows whether the product works.

Point the app at your local server. On the Android emulator `localhost` is the
emulator itself, so use `http://10.0.2.2:8000`; on the iOS simulator
`http://127.0.0.1:8000` works. On a physical device use your machine's LAN
address and make sure both are on the same network.

Then walk the real path, in this order:

1. **Sign up, then sign in again.** The token is what every other screen
   depends on, so a broken session shows up everywhere at once.
2. **Add a wallet by pasting, and again by scanning a QR code.** Scanning is
   where malformed input tends to arrive.
3. **Check the total.** It should match `total_usd` from `GET /api/balances`.
   If it does not, the app is computing its own figure somewhere.
4. **Pull to refresh, twice, quickly.** The second pull should return within the
   60-second cache and not re-scan.
5. **Send a small amount to a watched address.** Within a poll cycle it should
   appear in the feed and arrive as a Telegram alert.
6. **Turn the wifi off and refresh.** Then turn it back on. The app should
   recover rather than stay stuck on an error.

Things to check that only show up in the app:

- **Wallets that failed still render.** One failing wallet should show its error
  on that row while the rest display normally.
- **Unknown prices show blank, not `$0.00`.** A holding with `value_usd: null`
  must not render as zero.
- **Small amounts read as decimals.** `0.00000546`, never `5.46E-6`.
- **Stale monitoring is visible.** If `/api/monitor/status` says `healthy:
  false`, the app should say so rather than displaying old numbers as if they
  were current.

## 10. Deciding whether it is the API or the app

When something looks wrong in the app, call the same endpoint in `/docs` with
the same account before doing anything else. That single check tells you which
half to debug:

- **Wrong in `/docs` too** → the API. Everything above applies.
- **Right in `/docs`, wrong in the app** → the app. Usually formatting, caching
  a stale response, or computing a number locally that the API already returns.

Sign in as the same user, or you will be comparing two different sets of
wallets. `/docs` is a diagnostic tool, not the acceptance test — an endpoint
returning correct JSON is not the same as the product working.

## What to watch for

**Bitcoin `internal` transactions.** If you add an xpub that has moved coins
between its own addresses, those should appear as `internal` with only the fee —
never as a send. Getting this wrong tells the user they sent their entire
balance.

**Second run finds nothing new.** Running `/api/monitor/run` twice should return
`new_transactions: 0` the second time. If it re-finds the same transactions, the
sync position is not being stored.

**Alerts fire once.** A transaction already alerted has `alerted_at` set and is
never re-sent, even across restarts.

**Balances never show a zero we are not sure of.** A failed lookup is an error,
and a missing price is null. Neither is zero.
