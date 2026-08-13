# Testing the monitor

## 1. Start the server

```
uvicorn app.main:app --reload
```

The log should show `monitor started, polling every 2 minutes`.

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

## 3. Force a poll

`POST /api/monitor/run` returns:

```json
{"wallets": 1, "new_transactions": 12, "alerts_sent": 0}
```

`alerts_sent` stays 0 until a Telegram chat is linked — the transactions are
still recorded.

## 4. See what it found

`GET /api/transactions`

## 5. Link Telegram

- `POST /api/auth/telegram/code` → returns a six-digit code
- Your telegram bot calls `POST /api/auth/telegram/link` with that code and
  the chat id
- Next cycle delivers alerts

To test without the bot, call `/api/auth/telegram/link` directly with your own
chat id. Message @userinfobot on Telegram to find it.

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
