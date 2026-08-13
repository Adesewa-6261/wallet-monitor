"""
app/core/link_codes.py

Codes for connecting a Telegram chat to an account.

Three decisions here worth stating, because the obvious version of this is
weaker than it looks:

**The alphabet excludes ambiguous characters.** No 0/O, no 1/I/L. The user reads
this off one screen and types it into another, so a character that looks like
another character is a support ticket.

**Eight characters, not six digits.** Six digits is a million combinations. The
link endpoint is a natural brute-force target because an attacker does not need
to know whose code they are guessing — any valid code from any user would work.
Eight characters from a 32-symbol alphabet is roughly 1.1 trillion.

**Codes are stored hashed.** A database leak while a code is live would
otherwise let someone attach their own Telegram to another person's account, and
from there use every bot command as that user.
"""

import hashlib
import hmac
import secrets

from .config import config

# Crockford-style: no I, L, O or U. Reading a code aloud or copying it by eye
# should not be able to go wrong.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8


def generate() -> str:
    """A fresh code. Grouped for readability when displayed: ABCD-EFGH."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalise(code: str) -> str:
    """
    Accept what the user actually sends.

    They will paste it with the display hyphen, in lower case, or with a stray
    space. Rejecting those would be pedantry rather than security.
    """
    return code.strip().upper().replace("-", "").replace(" ", "")


def hash_code(code: str) -> str:
    """
    HMAC rather than a plain hash: the codes are short and drawn from a known
    alphabet, so an unkeyed hash could be reversed with a lookup table in
    seconds. The key makes that impossible without also stealing the secret.
    """
    return hmac.new(
        config.jwt_secret.encode(),
        normalise(code).encode(),
        hashlib.sha256,
    ).hexdigest()


def matches(code: str, stored_hash: str) -> bool:
    """Constant-time comparison, so timing cannot leak a partial match."""
    return hmac.compare_digest(hash_code(code), stored_hash)


def for_display(code: str) -> str:
    """ABCD-EFGH — easier to read back than eight unbroken characters."""
    return f"{code[:4]}-{code[4:]}"
