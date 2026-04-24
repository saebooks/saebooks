"""Lightweight JWT implementation using stdlib only (hmac + hashlib).

Uses HS256 (HMAC-SHA256) — the only signing algorithm needed for v1.
No external jwt/jose dependency required; ``cryptography`` is already
in the project but we don't need its hazmat layer for symmetric signing.

Public API
----------
``create_access_token(payload, expires_in_seconds)`` — returns a signed JWT string.
``decode_access_token(token)`` — verifies + decodes; raises ``JWTError`` on any failure.
``hash_password(plaintext)`` — returns a PBKDF2-HMAC-SHA256 hash string.
``verify_password(plaintext, stored_hash)`` — constant-time comparison.

Token format
------------
Header: {"alg": "HS256", "typ": "JWT"}
Payload: arbitrary dict; ``exp`` (Unix timestamp) added by ``create_access_token``.
Signature: HMAC-SHA256 over ``<header_b64>.<payload_b64>`` using the process secret.

Secret key
----------
Reads ``SAEBOOKS_SECRET_KEY`` from ``saebooks.config.settings``. When the
setting is empty a per-process random key is generated once and cached in
``_SECRET``. In production the env var must be set so tokens survive restarts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

logger = logging.getLogger("saebooks.jwt")

# -----------------------------------------------------------------------
# Secret resolution — lazy so imports don't trigger settings side-effects
# in tests that don't need JWT.
# -----------------------------------------------------------------------

_SECRET: str | None = None


def _secret_key() -> str:
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    # Import here to avoid a circular import at module level.
    from saebooks.config import settings as _settings  # noqa: PLC0415

    key = _settings.secret_key.strip()
    if key:
        _SECRET = key
        return _SECRET
    # No env var — generate once per process.
    generated = secrets.token_urlsafe(32)
    logger.info(
        "SAEBOOKS_SECRET_KEY not set; using ephemeral per-process JWT key. "
        "Tokens will be invalidated on restart."
    )
    os.environ["SAEBOOKS_SECRET_KEY"] = generated
    _SECRET = generated
    return _SECRET


def _reset_secret_cache() -> None:
    """Testing hook — resets the cached key so tests can control the value."""
    global _SECRET
    _SECRET = None


# -----------------------------------------------------------------------
# Base64url helpers (no padding — RFC 7515)
# -----------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# -----------------------------------------------------------------------
# JWT error
# -----------------------------------------------------------------------


class JWTError(ValueError):
    """Raised by ``decode_access_token`` for any verification failure."""


# -----------------------------------------------------------------------
# Token creation and decoding
# -----------------------------------------------------------------------

_HEADER = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())


def create_access_token(
    payload: dict[str, Any],
    *,
    expires_in_seconds: int = 28800,  # 8 hours
) -> str:
    """Return a signed HS256 JWT.

    ``exp`` is added automatically (current UTC time + ``expires_in_seconds``).
    Any ``uuid.UUID`` values in the payload are serialised as strings.
    """
    now = int(time.time())
    # Serialise UUIDs
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        cleaned[k] = str(v) if isinstance(v, uuid.UUID) else v
    cleaned["exp"] = now + expires_in_seconds
    cleaned.setdefault("iat", now)

    payload_b64 = _b64url_encode(json.dumps(cleaned, separators=(",", ":")).encode())
    signing_input = f"{_HEADER}.{payload_b64}"
    sig = hmac.new(
        _secret_key().encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT.

    Raises ``JWTError`` on:
    * Malformed token (not three dot-separated segments)
    * Invalid signature
    * Expired token (``exp`` in the past)
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("Malformed token")

    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(
        _secret_key().encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    try:
        presented_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise JWTError("Malformed signature") from exc

    if not hmac.compare_digest(expected_sig, presented_sig):
        raise JWTError("Invalid signature")

    try:
        claims: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise JWTError("Malformed payload") from exc

    exp = claims.get("exp")
    if exp is None or int(time.time()) > exp:
        raise JWTError("Token expired")

    return claims


# -----------------------------------------------------------------------
# Password hashing — PBKDF2-HMAC-SHA256 via stdlib hashlib
# -----------------------------------------------------------------------

_ITERATIONS = 260_000  # OWASP 2023 recommendation for PBKDF2-SHA256
_HASH_PREFIX = "pbkdf2sha256"


def hash_password(plaintext: str) -> str:
    """Return a salted PBKDF2-HMAC-SHA256 hash suitable for storing in the DB.

    Format: ``pbkdf2sha256$<iterations>$<salt_hex>$<hash_hex>``
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        plaintext.encode("utf-8"),
        salt.encode("utf-8"),
        _ITERATIONS,
    )
    return f"{_HASH_PREFIX}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Constant-time comparison of ``plaintext`` against ``stored_hash``.

    Returns ``False`` (rather than raising) for any malformed hash — lets
    the caller issue a generic 401 without distinguishing "bad format" from
    "wrong password".
    """
    try:
        prefix, iterations_str, salt, expected_hex = stored_hash.split("$")
    except ValueError:
        return False
    if prefix != _HASH_PREFIX:
        return False
    try:
        iterations = int(iterations_str)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        plaintext.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return hmac.compare_digest(digest.hex(), expected_hex)
