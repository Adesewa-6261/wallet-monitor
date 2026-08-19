-- schema.sql
--
-- Run once against the database. Postgres. Safe to re-run: every statement is
-- idempotent, so this doubles as the migration for an existing deployment.
--
-- The shape follows from what the app has to do: know who owns which wallets,
-- remember which transactions have already been seen so alerts fire once, and
-- keep enough transaction detail to render a feed without re-fetching chains.

create extension if not exists "uuid-ossp";

-- ---------------------------------------------------------------- users

create table if not exists users (
    id              uuid primary key default uuid_generate_v4(),
    email           text not null unique,
    password_hash   text not null,
    display_name    text,
    created_at      timestamptz not null default now()
);

-- Telegram is linked after signup with a short-lived code, never by putting
-- credentials in a chat. One account can link one chat.
create table if not exists telegram_links (
    user_id         uuid primary key references users(id) on delete cascade,
    chat_id         bigint not null unique,
    linked_at       timestamptz not null default now()
);

-- Only the hash of a link code is stored. A database leak while a code is live
-- would otherwise let someone attach their own Telegram to another account and
-- from there use every bot command as that user.
create table if not exists telegram_link_codes (
    code_hash       text primary key,
    user_id         uuid not null references users(id) on delete cascade,
    expires_at      timestamptz not null
);

-- Failed link attempts, so brute force can be locked out.
--
-- The link endpoint has no natural protection: an attacker does not need to
-- know whose code they are guessing, because any valid code from any user
-- would work. This table caps the attempts.
create table if not exists telegram_link_attempts (
    chat_id         bigint primary key,
    failed_count    integer not null default 0,
    first_failed_at timestamptz not null default now(),
    locked_until    timestamptz
);

-- ---------------------------------------------------------------- wallets

create table if not exists wallets (
    id              uuid primary key default uuid_generate_v4(),
    user_id         uuid not null references users(id) on delete cascade,
    label           text,
    -- Exactly what the user pasted. Kept verbatim so we can re-derive.
    input           text not null,
    input_type      text not null,          -- address | xpub | descriptor
    chain           text not null,          -- bitcoin | ethereum | base | tron
    address_type    text,                   -- Bitcoin only
    -- For xpubs: the derived addresses we are watching. Refreshed when the scan
    -- finds new used addresses, so the poller does not re-derive every cycle.
    watched_addresses text[] not null default '{}',
    created_at      timestamptz not null default now(),

    unique (user_id, input)
);

create index if not exists wallets_user_idx on wallets(user_id);

-- ------------------------------------------------------------ transactions

-- One row per transaction per wallet. A transaction touching two of the user's
-- wallets produces two rows, because the net effect differs for each.
create table if not exists transactions (
    id              uuid primary key default uuid_generate_v4(),
    wallet_id       uuid not null references wallets(id) on delete cascade,
    tx_hash         text not null,
    chain           text not null,
    -- receive | send | internal
    -- 'internal' is a Bitcoin case: the user moved coins between their own
    -- addresses, so nothing actually left their control and only the fee moved.
    direction       text not null,
    symbol          text not null,
    -- String, not numeric. An 8-decimal Bitcoin amount does not survive a float.
    amount          text not null,
    fee             text,
    counterparty    text,
    block_time      timestamptz not null,
    confirmations   integer,
    value_usd       numeric(20, 2),
    -- Set once an alert has been delivered, so a restart cannot double-notify.
    -- Also set immediately on a backfill, so importing history does not fire
    -- hundreds of alerts for transactions the user already knows about.
    alerted_at      timestamptz,
    created_at      timestamptz not null default now(),

    unique (wallet_id, tx_hash, symbol, direction)
);

create index if not exists transactions_wallet_time_idx on transactions(wallet_id, block_time desc);
create index if not exists transactions_pending_alert_idx on transactions(alerted_at) where alerted_at is null;

-- ------------------------------------------------------------ alert settings

create table if not exists alert_settings (
    user_id             uuid primary key references users(id) on delete cascade,
    enabled             boolean not null default true,
    -- Ignore INCOMING transactions below this USD value. Defaults to 0 (alert
    -- on everything). Applies to receives only, deliberately: dust spam and
    -- address-poisoning attacks are almost entirely inbound, while a small
    -- outgoing amount is often the more urgent signal — an attacker draining a
    -- wallet frequently tests with a small transfer first.
    min_value_usd       numeric(20, 2) not null default 0,
    alert_on_receive    boolean not null default true,
    alert_on_send       boolean not null default true,
    -- A daily message even when nothing happened, so that silence from the bot
    -- means something is wrong rather than nothing is happening.
    daily_digest        boolean not null default true,
    last_digest_at      timestamptz
);

alter table alert_settings add column if not exists daily_digest boolean not null default true;
alter table alert_settings add column if not exists last_digest_at timestamptz;

-- ------------------------------------------------------------ poll tracking

-- Keeps the poller cheap: we ask each chain only for what happened after this
-- point rather than re-reading full history every cycle.
create table if not exists wallet_sync_state (
    wallet_id           uuid primary key references wallets(id) on delete cascade,
    -- A position, not a time. For EVM and Bitcoin this is a block height; for
    -- Tron it is a millisecond timestamp, because TronGrid pages by time rather
    -- than height. Either way it means "we have processed up to here".
    last_block          bigint,
    last_synced_at      timestamptz,
    last_error          text,
    -- Native coin tracking for EVM. eth_getLogs cannot see a plain ETH transfer
    -- because it emits no event log, so movement is detected by comparing these
    -- against the current values instead.
    --
    -- The nonce matters as much as the balance: it only ever increases, so it
    -- catches an outgoing transfer even when money out and money in happen to
    -- net to roughly zero within one cycle.
    last_native_balance text,
    last_nonce          bigint
);

alter table wallet_sync_state add column if not exists last_native_balance text;
alter table wallet_sync_state add column if not exists last_nonce bigint;

-- Note: a `login_attempts` table exists on the deployed database but is
-- referenced nowhere in the code. It is left out here deliberately rather than
-- enshrined; drop it once confirmed unused.
