"""Tests for ``saebooks.services.backup_crypto`` (Wave E — client-
passphrase envelope encryption for scheduled backups).

Pure module, no DB — round-trip, wrong-passphrase, tamper-detection,
and the weak-passphrase floor.
"""
from __future__ import annotations

import pytest

from saebooks.services.backup_crypto import (
    MAGIC,
    DecryptionError,
    WeakPassphraseError,
    decrypt_export,
    encrypt_export,
    validate_passphrase_strength,
)


def test_round_trip() -> None:
    plaintext = b'{"hello": "world", "rows": [1, 2, 3]}'
    envelope = encrypt_export(plaintext, "correct horse battery staple")
    assert envelope.startswith(MAGIC)
    assert decrypt_export(envelope, "correct horse battery staple") == plaintext


def test_envelope_does_not_contain_plaintext() -> None:
    plaintext = b"a very distinctive marker STRING12345"
    envelope = encrypt_export(plaintext, "correct horse battery staple")
    assert plaintext not in envelope


def test_wrong_passphrase_fails_closed() -> None:
    envelope = encrypt_export(b"secret tenant data", "correct horse battery staple")
    with pytest.raises(DecryptionError):
        decrypt_export(envelope, "wrong passphrase entirely")


def test_tampered_ciphertext_fails_closed() -> None:
    envelope = encrypt_export(b"secret tenant data", "correct horse battery staple")
    tampered = envelope[:-1] + bytes([envelope[-1] ^ 0xFF])
    with pytest.raises(DecryptionError):
        decrypt_export(tampered, "correct horse battery staple")


def test_truncated_envelope_fails_closed() -> None:
    with pytest.raises(DecryptionError):
        decrypt_export(b"too short", "correct horse battery staple")


def test_wrong_magic_fails_closed() -> None:
    envelope = encrypt_export(b"secret tenant data", "correct horse battery staple")
    forged = b"NOTREAL0" + envelope[len(MAGIC) :]
    with pytest.raises(DecryptionError):
        decrypt_export(forged, "correct horse battery staple")


def test_weak_passphrase_rejected() -> None:
    with pytest.raises(WeakPassphraseError):
        validate_passphrase_strength("short")


def test_weak_passphrase_rejected_at_encrypt_time() -> None:
    with pytest.raises(WeakPassphraseError):
        encrypt_export(b"data", "short")


def test_two_encryptions_of_same_plaintext_differ() -> None:
    """Random salt + nonce per call — no two envelopes of the same
    plaintext/passphrase should be byte-identical (defends against
    ciphertext-comparison leaking equality of two exports)."""
    a = encrypt_export(b"same plaintext", "correct horse battery staple")
    b = encrypt_export(b"same plaintext", "correct horse battery staple")
    assert a != b
    assert decrypt_export(a, "correct horse battery staple") == b"same plaintext"
    assert decrypt_export(b, "correct horse battery staple") == b"same plaintext"
