"""Field-level symmetric encryption for secrets stored in the database.

First user is Batch II (per-company SISS credentials). The wrapper is
kept deliberately tiny — ``encrypt_field`` / ``decrypt_field`` / a boolean
``is_configured()`` probe — so new callers don't invent their own envelope.

Uses ``cryptography.fernet``:

* AES-128 in CBC mode + HMAC-SHA256 for integrity (Fernet's spec).
* URL-safe base64 transport. The key is a 32-byte random key wrapped in
  url-safe-b64 — generate with ``Fernet.generate_key()``.
* Built-in timestamp on every token (not enforced here; we don't rotate
  via TTL, we rotate via re-encrypt).

Configuration: ``SAEBOOKS_FIELD_ENCRYPTION_KEY`` env var (see
``config.py``). Empty means "encryption disabled" — encrypt/decrypt
raise ``FieldEncryptionNotConfiguredError`` rather than falling back to
plaintext, so a misconfigured install can't silently persist a plaintext
secret into a column the schema promised was ciphertext.

Key rotation (v2, per ``docs/security/key-management-plan.md``): the env
var accepts a **comma-separated key list**. Index 0 is the primary — it
encrypts everything new; every listed key decrypts. Rotation is:

1. prepend the new key → deploy (``new,old``);
2. ``python -m saebooks.cli rotate-field-keys`` — re-encrypts every
   stored ciphertext under the primary (idempotent, batched, resumable);
3. drop the old key from the list → deploy.

``rotate_token`` never exposes plaintext to the caller — it uses
``MultiFernet.rotate`` under the hood.
"""
from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from saebooks.config import Settings
from saebooks.config import settings as _default_settings


class FieldEncryptionError(RuntimeError):
    """Base class for all crypto failures surfaced to callers."""


class FieldEncryptionNotConfiguredError(FieldEncryptionError):
    """Raised when the Fernet key is empty at encrypt/decrypt time.

    Callers should catch this and surface "encryption not configured"
    in the UI rather than letting it bubble as a 500 — same pattern as
    ``SissNotConfiguredError`` on the bank-feeds side.
    """


class FieldDecryptionError(FieldEncryptionError):
    """Raised when a ciphertext can't be decrypted (bad key / tampered)."""


def is_configured(settings: Settings | None = None) -> bool:
    """Return ``True`` when the Fernet key is non-empty."""
    effective = settings if settings is not None else _default_settings
    return bool(effective.field_encryption_key)


def _keys(settings: Settings | None = None) -> list[str]:
    effective = settings if settings is not None else _default_settings
    return [k.strip() for k in effective.field_encryption_key.split(",") if k.strip()]


def _fernet(settings: Settings | None = None) -> MultiFernet:
    keys = _keys(settings)
    if not keys:
        raise FieldEncryptionNotConfiguredError(
            "Field encryption not configured — set SAEBOOKS_FIELD_ENCRYPTION_KEY. "
            "Generate a key with: python -c "
            "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        # Index 0 encrypts; every key decrypts (comma-separated rotation list).
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise FieldEncryptionNotConfiguredError(
            f"Field encryption key is not a valid Fernet key: {exc}"
        ) from exc


def key_fingerprints(settings: Settings | None = None) -> list[str]:
    """Short SHA-256 fingerprints of the configured keys, primary first.

    Safe to log — fingerprints identify a key for audit without exposing it.
    """
    return [hashlib.sha256(k.encode("ascii")).hexdigest()[:12] for k in _keys(settings)]


def rotate_token(ciphertext: str, *, settings: Settings | None = None) -> str:
    """Re-encrypt ``ciphertext`` under the primary key, without exposing plaintext.

    Idempotent: a token already under the primary key comes back valid (with a
    fresh timestamp). Raises ``FieldDecryptionError`` if no configured key can
    decrypt the token — rotation must never silently drop a stored secret.
    """
    if ciphertext == "":
        return ""
    try:
        return _fernet(settings).rotate(ciphertext.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise FieldDecryptionError(
            "Ciphertext could not be rotated — no configured key decrypts it."
        ) from exc


def encrypt_field(plaintext: str, *, settings: Settings | None = None) -> str:
    """Return an ASCII-safe ciphertext for ``plaintext``.

    Empty plaintext is returned as an empty ciphertext — the caller is
    responsible for deciding whether an empty secret is meaningful. This
    keeps the "user cleared the secret" UX path identical to the "never
    set" path: one empty-string column, no NULL/"" distinction needed.
    """
    if plaintext == "":
        return ""
    token = _fernet(settings).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_field(ciphertext: str, *, settings: Settings | None = None) -> str:
    """Return the plaintext for ``ciphertext``.

    Empty ciphertext returns empty plaintext (symmetric with
    ``encrypt_field``). Tampered or wrong-key ciphertext raises
    ``FieldDecryptionError`` — never returns garbage.
    """
    if ciphertext == "":
        return ""
    try:
        return _fernet(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise FieldDecryptionError(
            "Ciphertext could not be decrypted — wrong key, tampered, or not "
            "produced by encrypt_field."
        ) from exc
