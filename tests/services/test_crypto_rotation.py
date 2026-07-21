"""Key-list crypto + rotate-field-keys registry coverage (pure — no DB)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from saebooks.config import Settings
from saebooks.services import crypto
from saebooks.services.crypto import (
    FieldDecryptionError,
    decrypt_field,
    encrypt_field,
    key_fingerprints,
    rotate_token,
)


def _settings(*keys: str) -> Settings:
    # The field is alias-bound (SAEBOOKS_FIELD_ENCRYPTION_KEY) and Settings
    # ignores unknown init kwargs, so construct via the alias — a bare
    # ``field_encryption_key=`` kwarg would be silently dropped in favour of
    # the process env (exactly what happened in CI on first commit).
    return Settings(SAEBOOKS_FIELD_ENCRYPTION_KEY=",".join(keys))


KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def test_single_key_roundtrip_unchanged():
    s = _settings(KEY_A)
    token = encrypt_field("tfn-123456782", settings=s)
    assert decrypt_field(token, settings=s) == "tfn-123456782"


def test_key_list_decrypts_old_encrypts_new():
    old = _settings(KEY_A)
    token_old = encrypt_field("secret", settings=old)

    # Rotation config: new primary first, old key still present.
    both = _settings(KEY_B, KEY_A)
    assert decrypt_field(token_old, settings=both) == "secret"  # old still readable
    token_new = encrypt_field("secret", settings=both)
    # New tokens are NOT readable by the old key alone — primary is index 0.
    with pytest.raises(FieldDecryptionError):
        decrypt_field(token_new, settings=old)


def test_rotate_token_moves_to_primary_without_plaintext_change():
    old = _settings(KEY_A)
    token_old = encrypt_field("keystore-password", settings=old)

    both = _settings(KEY_B, KEY_A)
    rotated = rotate_token(token_old, settings=both)
    # Rotated token now reads under the NEW key alone (old key retired).
    new_only = _settings(KEY_B)
    assert decrypt_field(rotated, settings=new_only) == "keystore-password"


def test_rotate_token_unknown_key_fails_loud():
    stranger = encrypt_field("x", settings=_settings(Fernet.generate_key().decode()))
    with pytest.raises(FieldDecryptionError):
        rotate_token(stranger, settings=_settings(KEY_A))


def test_rotate_token_empty_is_identity():
    assert rotate_token("", settings=_settings(KEY_A)) == ""


def test_key_fingerprints_are_loggable_and_ordered():
    fps = key_fingerprints(settings=_settings(KEY_B, KEY_A))
    assert len(fps) == 2
    assert all(re.fullmatch(r"[0-9a-f]{12}", f) for f in fps)
    assert KEY_A not in fps[1] and KEY_B not in fps[0]  # never the key itself


def test_registry_covers_every_encrypted_model_column():
    """Adding a *_encrypted / *_ciphertext column requires a registry entry."""
    from saebooks.cli.rotate_field_keys import REGISTRY

    registered = {c for spec in REGISTRY for c in spec.columns}
    models_dir = Path(crypto.__file__).parents[1] / "models"
    pattern = re.compile(r"^\s+(\w+(?:_encrypted|_ciphertext)):\s+Mapped", re.M)
    found = set()
    for path in models_dir.rglob("*.py"):
        found.update(pattern.findall(path.read_text()))
    missing = found - registered
    assert not missing, (
        f"encrypted columns missing from rotate_field_keys.REGISTRY: {sorted(missing)}"
    )
