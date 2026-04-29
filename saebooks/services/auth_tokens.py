"""Token generation + hashing for the public-auth flows.

Three single-use token kinds — verification, password reset, magic
link. All share the same primitive: 32 random URL-safe bytes for the
raw token (returned to the user inside an email), and the SHA-256 hex
digest stored in the DB. Lookups happen by hash, so a DB compromise
doesn't hand the attacker live tokens.

Why SHA-256 instead of bcrypt/PBKDF2:
- Tokens are already 256 bits of entropy from ``secrets.token_urlsafe``,
  brute-force on the hash is cosmically infeasible.
- Lookup-by-hash needs to be cheap (one DB row), not slow.

Expiries by kind (see ``expiry_for``):

* ``verification`` — 24h (user might check email later in the day)
* ``reset``        — 1h  (sensitive — short window)
* ``magic``        — 15min (one-time login link, no resend tolerance)
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

TokenKind = Literal["verification", "reset", "magic"]

_EXPIRY_MINUTES: dict[str, int] = {
    "verification": 24 * 60,
    "reset": 60,
    "magic": 15,
}


def generate_token() -> str:
    """Return a 32-byte URL-safe token string (the value that goes in
    the email link). Length is ~43 base64-url chars.
    """
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the lowercase hex SHA-256 of ``raw`` — what gets stored.

    SHA-256 hex is exactly 64 chars, matching the ``CHAR(64)`` column
    type. We don't strip / normalise the input — the caller passes the
    same string the user clicks, byte-for-byte.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expiry_for(kind: TokenKind) -> datetime:
    """Return an absolute UTC timestamp ``now + ttl(kind)``."""
    minutes = _EXPIRY_MINUTES.get(kind)
    if minutes is None:
        raise ValueError(f"Unknown token kind: {kind!r}")
    return datetime.now(UTC) + timedelta(minutes=minutes)


__all__ = ["TokenKind", "expiry_for", "generate_token", "hash_token"]
