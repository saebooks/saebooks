"""Parse + introspect an ATO RAM Machine Credential keystore.

The Machine Credential Downloader Chrome extension exports a
``keystore.xml`` file containing the certificate + private key
protected by a user-chosen password. In practice the format is a
PKCS12 (PFX) blob wrapped in an XML envelope, though some RAM
exports produce a bare PKCS12 binary with the ``.xml`` extension.

This module intentionally tolerates both shapes without hard-coding
the exact XML schema (RAM's XML structure has changed at least once
historically, and we haven't yet round-tripped a real credential
through this code — the goal is to store + display metadata, not to
be picky about the envelope).

Flow:

1. ``load_keystore(data, password)`` tries to parse ``data`` as
   PKCS12 directly. If that fails, strips any XML envelope by
   walking the DOM, base64-decoding the largest text chunk found,
   and retrying PKCS12 on the result.
2. Success returns a ``LoadedKeystore`` dataclass with the cert's
   subject CN, issuer CN, serial, and validity window already
   extracted — the caller never needs to touch the private key in
   normal onboarding use (that's for the lodgement layer).

Callers handle ``KeystoreError`` for anything unparseable or wrong
password. The onboarding router surfaces the error message verbatim;
nothing here is user-input-sensitive enough to need redaction.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12


class KeystoreError(Exception):
    """Raised when the uploaded bytes + password can't produce a keypair."""


@dataclass(frozen=True)
class LoadedKeystore:
    subject_cn: str | None
    issuer_cn: str | None
    serial: str
    not_before: datetime
    not_after: datetime


def load_keystore(data: bytes, password: str) -> LoadedKeystore:
    """Parse a RAM Machine Credential keystore and surface its cert metadata.

    Raises ``KeystoreError`` if neither the bare-PKCS12 nor the
    XML-wrapped-PKCS12 parse succeeds — typically wrong password,
    wrong file, or corrupted bytes.
    """
    cert = _extract_cert(data, password)
    return LoadedKeystore(
        subject_cn=_cn_from(cert.subject),
        issuer_cn=_cn_from(cert.issuer),
        serial=format(cert.serial_number, "x"),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
    )


def _extract_cert(data: bytes, password: str) -> x509.Certificate:
    pwd = password.encode("utf-8") if password else None

    # Path 1: bare PKCS12.
    try:
        _, cert, _ = pkcs12.load_key_and_certificates(data, pwd)
    except ValueError:
        cert = None
    if cert is not None:
        return cert

    # Path 2: XML envelope around a base64 PKCS12 blob.
    pkcs12_bytes = _unwrap_xml_envelope(data)
    if pkcs12_bytes is None:
        raise KeystoreError(
            "File is not recognised as a PKCS12 keystore or an XML "
            "envelope around one. Re-export the credential from RAM."
        )
    try:
        _, cert, _ = pkcs12.load_key_and_certificates(pkcs12_bytes, pwd)
    except ValueError as exc:
        raise KeystoreError(
            f"Keystore decoded from XML envelope but PKCS12 parse failed "
            f"(wrong password?): {exc}"
        ) from exc
    if cert is None:
        raise KeystoreError("Keystore contained no certificate.")
    return cert


def _unwrap_xml_envelope(data: bytes) -> bytes | None:
    """Best-effort: pull a base64 PKCS12 blob out of an XML document.

    Returns ``None`` if the bytes don't even look like XML or no
    plausible blob is found. We don't assert a specific RAM schema
    because we've seen multiple shapes reported (``<Keystore>...``,
    ``<CredentialData>...``); instead we look for the largest base64
    chunk inside any text node and hope it decodes.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None

    candidates: list[str] = []
    for elem in root.iter():
        text = (elem.text or "").strip()
        if len(text) < 100:
            continue
        stripped = re.sub(r"\s+", "", text)
        if re.fullmatch(r"[A-Za-z0-9+/=]+", stripped):
            candidates.append(stripped)

    candidates.sort(key=len, reverse=True)
    for b64 in candidates:
        try:
            decoded = base64.b64decode(b64, validate=True)
        except (ValueError, binascii.Error):
            continue
        if decoded.startswith(b"\x30"):  # PKCS12 / ASN.1 SEQUENCE prefix
            return decoded
    return None


def _cn_from(name: x509.Name) -> str | None:
    for attr in name.get_attributes_for_oid(x509.NameOID.COMMON_NAME):
        value = attr.value
        return value if isinstance(value, str) else value.decode("utf-8")
    return None
