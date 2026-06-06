"""Per-edge key + token helpers for the intercompany REMOTE relay.

SHIP-SAFE, INERT (Phase 3a). Nothing in the running app calls these yet; the
authoriser edge-enable flow (Phase 3c) will. Built and unit-tested first so the
key/token envelope is pinned before any edge stores a private key.

Two independent secrets per edge (defence-in-depth — §4.4 of the relay plan):

1. **Ed25519 signing keypair.** The private key never leaves its tenant: it is
   Fernet-wrapped (``services.crypto.encrypt_field``) and stored as
   ``ic_edges.relay_privkey_ciphertext``; the public key is stored in the
   *partner's* ``relay_pubkey``. A stolen private key alone can't deliver
   (it isn't registered with the broker / has no token).

2. **Per-edge scoped bearer token** (the ``api_tokens`` prefix+bcrypt pattern).
   The broker presents it to the receiver; the receiver looks it up by prefix
   (O(1)) and bcrypt-verifies before doing anything else. A stolen token alone
   can't forge a message (no private key). Both are required.

This module deliberately reuses the existing primitives rather than inventing a
new envelope: ``crypto.encrypt_field`` for the private-key wrap (same Fernet key
``SAEBOOKS_FIELD_ENCRYPTION_KEY`` the bank-feed creds use) and the same
bcrypt(prefix + hash) shape as ``services/api_tokens.py`` for the token.
"""
from __future__ import annotations

import base64
import secrets

import bcrypt

from saebooks.config import Settings
from saebooks.services import crypto
from saebooks.services.ic_relay import signing

# Bcrypt work factor — match services/api_tokens.BCRYPT_WORK_FACTOR (10).
BCRYPT_WORK_FACTOR = 10

# Token shape: a printable prefix for O(1) lookup + a high-entropy body. We mint
# 32 bytes of entropy (256 bits) like api_tokens.
_TOKEN_HEADER = "icrl_"  # "intercompany relay" — analogous to api_tokens saebk_
_TOKEN_ENTROPY_BYTES = 32
_PREFIX_LEN = 12  # printable chars after the header used as the lookup prefix


# --------------------------------------------------------------------------- #
# Ed25519 signing-key lifecycle
# --------------------------------------------------------------------------- #


def new_signing_key() -> tuple[bytes, bytes]:
    """Return ``(private_raw_32, public_raw_32)`` for a fresh edge keypair."""
    return signing.generate_keypair()


def wrap_private_key(private_raw: bytes, *, settings: Settings | None = None) -> str:
    """Fernet-wrap a raw Ed25519 private key for storage.

    The raw bytes are base64-encoded to an ASCII string first (``encrypt_field``
    works on text), then encrypted. Returns the ciphertext string destined for
    ``ic_edges.relay_privkey_ciphertext``. Raises
    ``crypto.FieldEncryptionNotConfiguredError`` if the Fernet key is unset —
    fail closed, never persist a plaintext key.
    """
    b64 = base64.b64encode(private_raw).decode("ascii")
    return crypto.encrypt_field(b64, settings=settings)


def unwrap_private_key(ciphertext: str, *, settings: Settings | None = None) -> bytes:
    """Reverse ``wrap_private_key`` — return the raw 32-byte private key.

    Raises ``crypto.FieldDecryptionError`` on a wrong-key / tampered ciphertext
    (never returns garbage).
    """
    b64 = crypto.decrypt_field(ciphertext, settings=settings)
    return base64.b64decode(b64.encode("ascii"))


# --------------------------------------------------------------------------- #
# Per-edge scoped token lifecycle (api_tokens prefix+bcrypt pattern)
# --------------------------------------------------------------------------- #


def generate_edge_token() -> tuple[str, str]:
    """Return ``(cleartext, lookup_prefix)`` for a fresh per-edge token.

    Cleartext is shown/stored once (it goes into each tenant's encrypted edge
    row and the broker's secret store). The lookup prefix is stored verbatim in
    ``ic_edges.relay_token_prefix`` for O(1) lookup; the full cleartext is
    bcrypt-hashed into ``relay_token_hash``.
    """
    body = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES).rstrip("=")
    full = _TOKEN_HEADER + body
    lookup_prefix = body[:_PREFIX_LEN]
    return full, lookup_prefix


def hash_edge_token(cleartext: str) -> str:
    """Return the bcrypt hash of a per-edge token cleartext."""
    return bcrypt.hashpw(
        cleartext.encode("utf-8"),
        bcrypt.gensalt(BCRYPT_WORK_FACTOR),
    ).decode("ascii")


def verify_edge_token(cleartext: str, token_hash: str) -> bool:
    """Constant-time bcrypt check of a presented token against its hash.

    Returns ``False`` (never raises) on a malformed hash so the verify path
    fails closed exactly like ``signing.verify``.
    """
    try:
        return bcrypt.checkpw(
            cleartext.encode("utf-8"), token_hash.encode("ascii")
        )
    except (ValueError, TypeError):
        return False


def token_lookup_prefix(cleartext: str) -> str:
    """Extract the lookup prefix from a presented token cleartext.

    Returns ``""`` if the token doesn't carry the expected header (so an
    unknown bearer can't accidentally match a stored prefix).
    """
    if not cleartext.startswith(_TOKEN_HEADER):
        return ""
    body = cleartext[len(_TOKEN_HEADER):]
    return body[:_PREFIX_LEN]
