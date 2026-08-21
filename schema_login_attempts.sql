-- Run this against the existing database. Safe to run more than once.
--
-- Brute-force protection for POST /api/auth/login.
--
-- Until now the login endpoint had none: an attacker could guess passwords at
-- it indefinitely, which mattered more than the Telegram link endpoint that
-- already had a lockout, since a password is worth more than a link code.
--
-- Keyed on email rather than user id, because a failed attempt has to be
-- recorded before we know whether the account exists — looking the user up
-- first and only counting real accounts would tell an attacker which emails
-- are registered.
create table if not exists login_attempts (
    email           text primary key,
    failed_count    integer not null default 0,
    first_failed_at timestamptz not null default now(),
    locked_until    timestamptz
);
