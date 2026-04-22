"""Tests for the RAM Machine Credential keystore parser.

Pure unit tests — we synthesize a PKCS12 ourselves and feed it in via
both the bare-binary and XML-wrapped paths. No DB / no network.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from saebooks.services.ato_sbr.keystore import (
    KeystoreError,
    load_keystore,
)


def _build_pkcs12(*, password: str = "pw", cn: str = "Test Cred") -> bytes:
    key = rsa.generate_private_key(65537, 2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(0xABCDEF01)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"alias",
        key,
        cert,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )


def test_loads_bare_pkcs12() -> None:
    data = _build_pkcs12(cn="Plain Credential")
    loaded = load_keystore(data, "pw")
    assert loaded.subject_cn == "Plain Credential"
    assert loaded.issuer_cn == "Plain Credential"
    assert loaded.serial == "abcdef01"
    assert loaded.not_after > datetime.now(UTC)


def test_loads_xml_envelope_around_pkcs12() -> None:
    """RAM exports wrap the PKCS12 in XML — we should tolerate either shape."""
    pfx = _build_pkcs12(cn="XML Wrapped")
    b64 = base64.b64encode(pfx).decode("ascii")
    wrapped = (
        f'<?xml version="1.0"?>\n'
        f'<CredentialContainer>\n'
        f'  <CredentialId>test</CredentialId>\n'
        f'  <KeystoreData format="PKCS12">{b64}</KeystoreData>\n'
        f'</CredentialContainer>\n'
    ).encode("ascii")
    loaded = load_keystore(wrapped, "pw")
    assert loaded.subject_cn == "XML Wrapped"


def test_wrong_password_raises() -> None:
    data = _build_pkcs12(password="correct")
    with pytest.raises(KeystoreError):
        load_keystore(data, "wrong")


def test_garbage_bytes_raises() -> None:
    with pytest.raises(KeystoreError):
        load_keystore(b"not a keystore at all", "pw")


def test_xml_without_pkcs12_blob_raises() -> None:
    # Plausible-looking XML that contains no PKCS12 payload.
    data = b'<?xml version="1.0"?><root><note>no blob here</note></root>'
    with pytest.raises(KeystoreError):
        load_keystore(data, "pw")


def test_multi_rdn_subject_picks_cn() -> None:
    key = rsa.generate_private_key(65537, 2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sauer Pty Ltd"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Machine Cred 1"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pfx = pkcs12.serialize_key_and_certificates(
        b"alias", key, cert, None, serialization.BestAvailableEncryption(b"pw")
    )
    loaded = load_keystore(pfx, "pw")
    assert loaded.subject_cn == "Machine Cred 1"
