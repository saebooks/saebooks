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

Key rotation is out of scope for v1 — when we need it, the shape is a
JSON list of keys in the env var with the first one being the "encrypt"
key and the rest being "decrypt-only". That future change can happen
behind the current API without touching callers.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

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


def _fernet(settings: Settings | None = None) -> Fernet:
    effective = settings if settings is not None else _default_settings
    if not effective.field_encryption_key:
        raise FieldEncryptionNotConfiguredError(
            "Field encryption not configured — set SAEBOOKS_FIELD_ENCRYPTION_KEY. "
            "Generate a key with: python -c "
            "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        return Fernet(effective.field_encryption_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise FieldEncryptionNotConfiguredError(
            f"Field encryption key is not a valid Fernet key: {exc}"
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
