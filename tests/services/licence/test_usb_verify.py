"""Real-crypto tests for the USB driver in services/licence/usb.py."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from saebooks.services.licence import usb as usb_driver


def _sign_blob(payload: dict, key: Ed25519PrivateKey) -> bytes:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig = key.sign(canonical)
    return base64.b64encode(sig) + b"\n" + canonical


@pytest.fixture
def keypair_b64(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(
        usb_driver, "USB_PUBKEY_B64", base64.b64encode(pub_raw).decode()
    )
    return priv


def test_returns_none_without_pubkey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(usb_driver, "USB_PUBKEY_B64", "")
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(b"irrelevant")
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None


def test_returns_none_when_no_file(
    keypair_b64: Ed25519PrivateKey, tmp_path: Path
) -> None:
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None


def test_valid_blob_parsed(
    monkeypatch: pytest.MonkeyPatch,
    keypair_b64: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    payload = {
        "edition": "offline",
        "usb_uuid": "AAAA-BBBB",
        "licence_id": "SAE-OFFLINE-2026-XXXXX",
        "ledger_id": "Acme Pty Ltd",
        "licensed_to": "Acme Pty Ltd",
        "updates_until": "2027-04-22",
    }
    blob = _sign_blob(payload, keypair_b64)
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(blob)

    # Bypass findmnt — return the expected UUID so the equality passes.
    monkeypatch.setattr(
        usb_driver, "_filesystem_uuid_for", lambda _p: "AAAA-BBBB"
    )

    lic = usb_driver.load_usb_licence((str(tmp_path),))
    assert lic is not None
    assert lic.edition == "offline"
    assert lic.usb_uuid == "AAAA-BBBB"
    assert lic.licence_id == "SAE-OFFLINE-2026-XXXXX"


def test_uuid_mismatch_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    keypair_b64: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    payload = {
        "edition": "offline",
        "usb_uuid": "AAAA-BBBB",
        "licence_id": "L",
        "updates_until": "2027-04-22",
    }
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(
        _sign_blob(payload, keypair_b64)
    )
    monkeypatch.setattr(
        usb_driver, "_filesystem_uuid_for", lambda _p: "CCCC-DDDD"
    )
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None


def test_bad_signature_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    keypair_b64: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    other = Ed25519PrivateKey.generate()
    payload = {
        "edition": "offline",
        "usb_uuid": "AAAA-BBBB",
        "licence_id": "L",
        "updates_until": "2027-04-22",
    }
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(
        _sign_blob(payload, other)
    )
    monkeypatch.setattr(
        usb_driver, "_filesystem_uuid_for", lambda _p: "AAAA-BBBB"
    )
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None


def test_unknown_edition_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    keypair_b64: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    payload = {
        "edition": "ultra",
        "usb_uuid": "AAAA-BBBB",
        "licence_id": "L",
    }
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(
        _sign_blob(payload, keypair_b64)
    )
    monkeypatch.setattr(
        usb_driver, "_filesystem_uuid_for", lambda _p: "AAAA-BBBB"
    )
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None


def test_malformed_blob_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    keypair_b64: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    # No newline separator.
    (tmp_path / usb_driver.LICENCE_FILENAME).write_bytes(b"not a blob")
    monkeypatch.setattr(
        usb_driver, "_filesystem_uuid_for", lambda _p: "AAAA-BBBB"
    )
    assert usb_driver.load_usb_licence((str(tmp_path),)) is None
