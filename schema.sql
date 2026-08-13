-- schema.sql
--
-- Run once against the database. Postgres.
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

create table if not exists telegram_link_codes (
    code            char(6) primary key,
    user_id         uuid not null references users(id) on delete cascade,
    expires_at      timestamptz not null
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
    -- Ignore transactions below this USD value. Defaults to 0 (alert on
    -- everything). Exists from day one because dust spam and address-poisoning
    -- attacks generate a lot of tiny incoming transactions, and filtering them
    -- is both noise reduction and a safety measure.
    min_value_usd       numeric(20, 2) not null default 0,
    alert_on_receive    boolean not null default true,
    alert_on_send       boolean not null default true
);

-- ------------------------------------------------------------ poll tracking

-- Keeps the poller cheap: we ask each chain only for what happened after this
-- point rather than re-reading full history every cycle.
create table if not exists wallet_sync_state (
    wallet_id           uuid primary key references wallets(id) on delete cascade,
    last_block          bigint,
    last_synced_at      timestamptz,
    last_error          text
);