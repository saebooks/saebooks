"""Client-passphrase envelope encryption for scheduled-backup exports.

NOT the same thing as ``services/crypto.py``. That module wraps a
SAE-managed, server-side ``SAEBOOKS_FIELD_ENCRYPTION_KEY`` (a secret SAE
holds) — the right tool for encrypting a field SAE itself needs to read
back later (e.g. a stored SISS credential). This module wraps a
CLIENT-SUPPLIED passphrase that SAE Books never persists anywhere —
the caller passes it in, we derive a key, encrypt, and the passphrase
falls out of scope. That is the entire point: per
``[[saebooks-liability-pricing-principle]]`` / planned-modules decision
6, "encrypted on download" is the open, self-managed baseline and the
LIMIT of SAE's responsibility — SAE must be structurally INCAPABLE of
decrypting a client's export after the fact, not merely promise not to.
Once ``encrypt_export`` returns, the plaintext passphrase and plaintext
export are both gone from this process; only ciphertext is ever
written to disk (see ``services/scheduled_backups.py``).

No new dependency: ``cryptography`` is already a pyproject dependency
(``services/crypto.py`` uses ``cryptography.fernet``); this module uses
``cryptography.hazmat`` directly for a passphrase-derived AEAD instead
of Fernet, because Fernet's envelope wants a raw 32-byte *key*, not a
human passphrase — deriving one requires a slow KDF (scrypt) to resist
offline brute force, which hazmat exposes and Fernet does not wrap.

Envelope format (all fields fixed-width, no length-prefixing needed)::

    MAGIC (8 bytes)  b"SAEBKX01"
    salt   (16 bytes)  random, scrypt salt
    nonce  (12 bytes)  random, AES-GCM nonce
    ciphertext (variable, includes the 16-byte GCM tag appended by
               ``AESGCM.encrypt``)

KDF: scrypt(n=2**14, r=8, p=1, dklen=32) — interactive-login-strength
cost (RFC 7914's "interactive" parameters), matching the threat model
(offline dictionary attack on a stolen encrypted export). Documented
here, not just in code, because a client needs these exact parameters
to write their own decryptor if they ever lose ours — the algorithm
must be reproducible by a third party from documentation alone; that
is what "self-managed baseline" means in practice.
"""
from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SAEBKX01"
_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

_MIN_PASSPHRASE_LEN = 12


class BackupCryptoError(RuntimeError):
    """Base class for envelope encrypt/decrypt failures."""


class WeakPassphraseError(BackupCryptoError):
    """Raised when a passphrase is too short to be worth encrypting with."""


class DecryptionError(BackupCryptoError):
    """Wrong passphrase, corrupted artifact, or not a SAEBKX01 envelope."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(
        salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return kdf.derive(passphrase.encode("utf-8"))


def validate_passphrase_strength(passphrase: str) -> None:
    """Raise ``WeakPassphraseError`` for an obviously-too-weak passphrase.

    Deliberately minimal (length only, no composition rules — composition
    rules are a well-documented anti-pattern per NIST 800-63B). This is a
    courtesy floor, not a guarantee of a strong secret: the client owns
    the strength of their own passphrase under the self-managed-baseline
    model, same as they own a BYO certificate's quality.
    """
    if len(passphrase) < _MIN_PASSPHRASE_LEN:
        raise WeakPassphraseError(
            f"Passphrase must be at least {_MIN_PASSPHRASE_LEN} characters — "
            "this passphrase is the ONLY way to decrypt the export; SAE "
            "Books does not store it and cannot recover it."
        )


def encrypt_export(plaintext: bytes, passphrase: str) -> bytes:
    """Encrypt ``plaintext`` under ``passphrase``. Returns the envelope.

    The passphrase is used once, here, to derive a key via scrypt; the
    derived key is used once, here, to AES-256-GCM-encrypt. Neither the
    passphrase nor the derived key is returned, logged, or retained —
    the only thing that survives this call is the envelope, which is
    useless without the passphrase again.
    """
    validate_passphrase_strength(passphrase)
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data=MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt_export(envelope: bytes, passphrase: str) -> bytes:
    """Decrypt an ``encrypt_export`` envelope. Raises ``DecryptionError``
    on wrong passphrase, truncation, or tampering (GCM tag mismatch).

    SAE Books' own API never calls this for a client's own export — the
    server discards the passphrase at encryption time (see module
    docstring). This function exists for (a) our own round-trip tests
    and (b) as the reference implementation a client's own tooling can
    port if they don't want to depend on this codebase to read their
    backups back.
    """
    if len(envelope) < len(MAGIC) + _SALT_LEN + _NONCE_LEN:
        raise DecryptionError("Envelope too short to be a SAEBKX01 export")
    magic = envelope[: len(MAGIC)]
    if magic != MAGIC:
        raise DecryptionError(f"Not a SAEBKX01 envelope (got magic {magic!r})")
    offset = len(MAGIC)
    salt = envelope[offset : offset + _SALT_LEN]
    offset += _SALT_LEN
    nonce = envelope[offset : offset + _NONCE_LEN]
    offset += _NONCE_LEN
    ciphertext = envelope[offset:]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data=MAGIC)
    except InvalidTag as exc:
        raise DecryptionError(
            "Could not decrypt — wrong passphrase or corrupted/tampered artifact."
        ) from exc
