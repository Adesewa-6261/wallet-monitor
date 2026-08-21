-- Password reset codes, delivered over Telegram.
--
-- The code goes to the user's linked chat and is typed back into the app, not
-- into Telegram. That direction matters: a new password typed into a chat would
-- sit in its history indefinitely, which is the same reason the account-linking
-- flow sends a code out rather than taking a password in.
--
-- Safe to run more than once.

create table if not exists password_reset_codes (
    -- One live code per user. Requesting again replaces the previous one, so a
    -- code that has been read by the wrong person stops working the moment the
    -- real owner asks for another.
    user_id     uuid primary key references users(id) on delete cascade,
    code_hash   text        not null,
    expires_at  timestamptz not null,
    -- Wrong guesses against this code. The code is short enough to type, so it
    -- is short enough to guess without this.
    attempts    integer     not null default 0,
    created_at  timestamptz not null default now()
);

create index if not exists password_reset_codes_hash_idx
    on password_reset_codes (code_hash);
