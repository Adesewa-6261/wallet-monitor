-- Run this against the existing database. Safe to run more than once.

-- Link codes are now stored hashed. Existing rows cannot be migrated (we no
-- longer know the plaintext), so clear them — they expire in ten minutes
-- anyway and any pending link can simply be redone.
delete from telegram_link_codes;

alter table telegram_link_codes rename column code to code_hash;
alter table telegram_link_codes alter column code_hash type text;

-- Failed link attempts, so brute force can be locked out.
--
-- Six digits is a million combinations and the link endpoint has no natural
-- protection: an attacker does not need to know whose code they are guessing,
-- because any valid code from any user would work. Longer codes raise the cost
-- and this table caps the attempts.
create table if not exists telegram_link_attempts (
    chat_id         bigint primary key,
    failed_count    integer not null default 0,
    first_failed_at timestamptz not null default now(),
    locked_until    timestamptz
);

-- When each user was last sent a health digest, so it goes out once a day
-- rather than every poll cycle.
alter table alert_settings
    add column if not exists daily_digest boolean not null default true;

alter table alert_settings
    add column if not exists last_digest_at timestamptz;
