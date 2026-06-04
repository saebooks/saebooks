"""Real-crypto tests for the JWT driver in services/licence/jwt.py.

We generate an Ed25519 keypair in-process, pin its base64 public key
into the module, sign a JWT by hand, write it to a temp file, and
confirm ``load_portal_jwt`` returns a populated ``ResolvedLicence``.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from saebooks.services.licence import jwt as jwt_driver


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encode_jwt(payload: dict, key: Ed25519PrivateKey) -> str:
    header = {"alg": "EdDSA", "typ": "JWT"}
    h = _b64url(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    p = _b64url(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    sig = key.sign(f"{h}.{p}".encode("ascii"))
    return f"{h}.{p}.{_b64url(sig)}"


@pytest.fixture
def keypair_b64(
    monkeypatch: pytest.MonkeyPatch,
) -> Ed25519PrivateKey:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(
        jwt_driver, "PORTAL_PUBKEY_B64", base64.b64encode(pub_raw).decode()
    )
    return priv


def test_returns_none_without_pubkey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(jwt_driver, "PORTAL_PUBKEY_B64", "")
    jwt_file = tmp_path / "x.jwt"
    jwt_file.write_text("irrelevant")
    assert jwt_driver.load_portal_jwt(str(jwt_file)) is None


def test_returns_none_when_file_missing(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    assert (
        jwt_driver.load_portal_jwt(str(tmp_path / "missing.jwt")) is None
    )


def test_valid_jwt_parsed_and_returned(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    exp = datetime.now(UTC) + timedelta(days=30)
    token = _encode_jwt(
        {
            "iss": "saebooks-portal",
            "sub": "acct-1",
            "exp": int(exp.timestamp()),
            "edition": "pro",
            "ledger_id": "Alice Co",
            "seat_admin_cap": 4,
            "seat_employee_cap": 12,
            "company_cap": 5,
        },
        keypair_b64,
    )
    path = tmp_path / "licence.jwt"
    path.write_text(token)

    lic = jwt_driver.load_portal_jwt(str(path))
    assert lic is not None
    assert lic.edition == "pro"
    assert lic.ledger_id == "Alice Co"
    # Overrides applied on top of edition caps.
    assert lic.caps.admin_seats == 4
    assert lic.caps.employee_seats == 12
    assert lic.caps.companies == 5


def test_bad_signature_returns_none(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    other = Ed25519PrivateKey.generate()
    exp = datetime.now(UTC) + timedelta(days=30)
    token = _encode_jwt(
        {
            "edition": "business",
            "exp": int(exp.timestamp()),
            "ledger_id": "X",
        },
        other,
    )
    path = tmp_path / "bad.jwt"
    path.write_text(token)
    assert jwt_driver.load_portal_jwt(str(path)) is None


def test_expired_past_grace_returns_none(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    exp = datetime.now(UTC) - timedelta(days=90)
    token = _encode_jwt(
        {
            "edition": "business",
            "exp": int(exp.timestamp()),
            "ledger_id": "X",
        },
        keypair_b64,
    )
    path = tmp_path / "old.jwt"
    path.write_text(token)
    assert jwt_driver.load_portal_jwt(str(path)) is None


def test_expired_within_grace_still_returned(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    # 20 days past exp — within the 60-day grace cascade.
    exp = datetime.now(UTC) - timedelta(days=20)
    token = _encode_jwt(
        {
            "edition": "business",
            "exp": int(exp.timestamp()),
            "ledger_id": "X",
        },
        keypair_b64,
    )
    path = tmp_path / "grace.jwt"
    path.write_text(token)
    lic = jwt_driver.load_portal_jwt(str(path))
    assert lic is not None
    assert lic.edition == "business"
    assert lic.expires_at is not None


def test_unknown_edition_rejected(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    exp = datetime.now(UTC) + timedelta(days=10)
    token = _encode_jwt(
        {
            "edition": "platinum",
            "exp": int(exp.timestamp()),
            "ledger_id": "X",
        },
        keypair_b64,
    )
    path = tmp_path / "bad-edition.jwt"
    path.write_text(token)
    assert jwt_driver.load_portal_jwt(str(path)) is None
