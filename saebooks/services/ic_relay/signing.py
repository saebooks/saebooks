"""Ed25519 detached-signature primitives for the intercompany REMOTE relay.

SHIP-SAFE, INERT (Phase 3a). Nothing in the running app calls these yet — the
dispatcher (originator side) and ``/ic/accept`` webhook (receiver side) that use
them land in the live-relay phase. This module is the cryptographic core, built
and unit-tested first so the wire protocol is pinned before any code relays
money-adjacent events.

Why this exists
---------------
The REMOTE relay replaces the LOCAL single-transaction pair with two
independently-atomic local txns made eventually-consistent by a **signed,
idempotent** message. The signature is what lets the receiver post a reciprocal
leg to its OWN books from an externally-supplied signal without trusting the
transport: it binds the whole canonical body (``ic_txn_id``, ``nonce``,
``issued_at``, ``amount``, ``entry_date``, ``edge_id``, both tenant ids,
``description``) to the originator's per-edge private key.

Canonicalisation is load-bearing
---------------------------------
A sign/verify mismatch from non-deterministic JSON is the classic silent failure
here, so the signed bytes are a *deterministic* JSON encoding:

* keys sorted (``sort_keys=True``),
* no insignificant whitespace (``separators=(",", ":")``),
* ``ensure_ascii=False`` then UTF-8 encoded, so a unicode description round-trips
  byte-for-byte instead of being escaped differently by different encoders,
* ``amount`` is required to be a **string** in the canonical fixed-decimal form
  (the caller formats it; we refuse a float — floats are non-deterministic).

Two parties on different Python versions / libraries must produce identical
bytes for the same logical payload, or the signature will not verify.

The ``verify`` contract
-----------------------
``verify`` returns ``True``/``False`` and never raises on a bad signature, a
malformed signature, or a malformed key — the caller turns a ``False`` into a
flat 400 and must NOT leak which check failed (timing or message). This mirrors
the constant-time, fail-closed posture of the paperless-webhook HMAC check.
"""
from __future__ import annotations

import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class CanonicalisationError(ValueError):
    """Raised when a payload cannot be canonicalised deterministically.

    The most common cause is a float where a fixed-decimal string was
    required (floats do not have a single deterministic textual form across
    encoders / platforms, so they would silently break sign/verify).
    """


# The fields whose value MUST be a string for deterministic signing. ``amount``
# is the dangerous one — a Decimal/float would serialise differently per encoder.
_REQUIRE_STR_FIELDS = ("amount",)


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Return the deterministic UTF-8 bytes that get signed/verified.

    Determinism rules (see module docstring): sorted keys, no whitespace,
    ``ensure_ascii=False``. ``amount`` (and any other money field) must already
    be a fixed-format decimal *string* — a non-string value raises
    ``CanonicalisationError`` rather than risk a non-deterministic float
    encoding. The same ``dict`` always yields the same bytes; two dicts that
    differ only in key order yield identical bytes.
    """
    for field in _REQUIRE_STR_FIELDS:
        if field in payload and not isinstance(payload[field], str):
            raise CanonicalisationError(
                f"canonical payload field {field!r} must be a string "
                f"(fixed-decimal form), got {type(payload[field]).__name__} — "
                f"non-string money fields break deterministic signing"
            )
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalisationError(
            f"payload is not canonicalisable to deterministic JSON: {exc}"
        ) from exc
    return text.encode("utf-8")


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_raw_32, public_raw_32)`` — raw Ed25519 key bytes.

    Raw 32-byte encodings (not PEM) keep the DB columns compact and the wire
    format unambiguous. The private bytes must be Fernet-wrapped before storage
    (see ``keys.wrap_private_key``); the public bytes are stored as-is in the
    partner's ``relay_pubkey``.
    """
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes_raw()
    pub_raw = priv.public_key().public_bytes_raw()
    return priv_raw, pub_raw


def public_key_for(private_raw: bytes) -> bytes:
    """Derive the raw public key bytes for a raw Ed25519 private key."""
    priv = Ed25519PrivateKey.from_private_bytes(private_raw)
    return priv.public_key().public_bytes_raw()


def sign(canonical_bytes: bytes, private_raw: bytes) -> bytes:
    """Return the detached Ed25519 signature over ``canonical_bytes``.

    ``private_raw`` is the raw 32-byte private key (already Fernet-decrypted by
    the caller via ``keys.unwrap_private_key``). Raises ``ValueError`` if the
    key bytes are not a valid Ed25519 private key — a programming/config error
    on the SIGNING side, which is ours, so it is allowed to surface (unlike
    ``verify``, which fails closed on attacker-supplied input).
    """
    priv = Ed25519PrivateKey.from_private_bytes(private_raw)
    return priv.sign(canonical_bytes)


def verify(canonical_bytes: bytes, signature: bytes, public_raw: bytes) -> bool:
    """Return ``True`` iff ``signature`` is valid for ``canonical_bytes``.

    NEVER raises: a bad signature, a malformed signature, or a malformed public
    key all return ``False``. The caller turns ``False`` into a flat 400 and
    must not reveal which check failed. This is the trust boundary — every input
    here is attacker-controllable, so it fails closed and silent.
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(public_raw)
        pub.verify(signature, canonical_bytes)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
