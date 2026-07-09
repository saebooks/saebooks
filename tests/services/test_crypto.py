"""Tests for ``saebooks.services.crypto``.

Pure unit tests — no DB / no network. Covers:

* Key-unconfigured path raises ``FieldEncryptionNotConfiguredError``
  from both encrypt_field and decrypt_field.
* Round-trip with a valid Fernet key.
* Empty-string passthrough on both sides (symmetric).
* Tampered ciphertext raises ``FieldDecryptionError``.
* A bogus key string raises ``FieldEncryptionNotConfiguredError``
  (caller gets one class of error to handle, not two).
* ``is_configured`` boolean.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from saebooks.config import Settings
from saebooks.services.crypto import (
    FieldDecryptionError,
    FieldEncryptionNotConfiguredError,
    decrypt_field,
    encrypt_field,
    is_configured,
)


def _settings(key: str = "") -> Settings:
    return Settings(SAEBOOKS_FIELD_ENCRYPTION_KEY=key)


def _good_settings() -> Settings:
    return _settings(Fernet.generate_key().decode())


# ---------------------------------------------------------------------- #
# is_configured                                                          #
# ---------------------------------------------------------------------- #


def test_is_configured_false_with_empty_key() -> None:
    assert is_configured(_settings("")) is False


def test_is_configured_true_with_populated_key() -> None:
    assert is_configured(_good_settings()) is True


# ---------------------------------------------------------------------- #
# encrypt_field / decrypt_field                                          #
# ---------------------------------------------------------------------- #


def test_encrypt_without_key_raises() -> None:
    with pytest.raises(FieldEncryptionNotConfiguredError):
        encrypt_field("sekret", settings=_settings(""))


def test_decrypt_without_key_raises() -> None:
    # Can't produce a ciphertext without a key — just assert on the
    # "has ciphertext, no key" path by passing a bogus token.
    with pytest.raises(FieldEncryptionNotConfiguredError):
        decrypt_field("gAAAAABbogus", settings=_settings(""))


def test_round_trip() -> None:
    s = _good_settings()
    ct = encrypt_field("hello world", settings=s)
    assert ct != "hello world"
    assert decrypt_field(ct, settings=s) == "hello world"


def test_round_trip_unicode() -> None:
    s = _good_settings()
    ct = encrypt_field("piña colada 😎", settings=s)
    assert decrypt_field(ct, settings=s) == "piña colada 😎"


def test_empty_string_passthrough_encrypt() -> None:
    # No key needed — the empty-in / empty-out contract is symmetric
    # with decrypt so "never set" and "cleared" columns look identical.
    assert encrypt_field("", settings=_settings("")) == ""


def test_empty_string_passthrough_decrypt() -> None:
    assert decrypt_field("", settings=_settings("")) == ""


def test_same_plaintext_produces_different_ciphertexts() -> None:
    """Fernet includes an IV — two encrypts of the same plaintext diverge."""
    s = _good_settings()
    a = encrypt_field("dupe", settings=s)
    b = encrypt_field("dupe", settings=s)
    assert a != b
    assert decrypt_field(a, settings=s) == "dupe"
    assert decrypt_field(b, settings=s) == "dupe"


def test_tampered_ciphertext_raises() -> None:
    s = _good_settings()
    ct = encrypt_field("original", settings=s)
    # Flip a middle char. Fernet's HMAC catches it.
    tampered = ct[:20] + ("A" if ct[20] != "A" else "B") + ct[21:]
    with pytest.raises(FieldDecryptionError):
        decrypt_field(tampered, settings=s)


def test_wrong_key_cannot_decrypt() -> None:
    enc_settings = _good_settings()
    ct = encrypt_field("cross-tenant secret", settings=enc_settings)
    other = _good_settings()
    with pytest.raises(FieldDecryptionError):
        decrypt_field(ct, settings=other)


def test_bogus_key_raises_not_configured() -> None:
    """Caller handles one class of error, not 'what is a Fernet key'."""
    with pytest.raises(FieldEncryptionNotConfiguredError):
        encrypt_field("x", settings=_settings("not-a-valid-fernet-key"))
