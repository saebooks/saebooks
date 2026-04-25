"""Parse + introspect an ATO RAM Machine Credential keystore.

The Machine Credential Downloader Chrome extension exports a
``keystore.xml`` file in the ATO's proprietary SBRCredentialStore XML
format (namespace ``http://auth.abr.gov.au/credential/xsd/SBRCredentialStore``).
The file contains:

* ``publicCertificate``  — the cert chain, PKCS7 SignedData (DER, BER indefinite-length)
* ``protectedPrivateKey`` — the private key, encrypted PKCS8 using a custom
  ATO key-derivation scheme (password + per-credential salt).
* ``salt``, ``credentialSalt`` — used for private-key decryption (not needed
  for cert-metadata display).

This module extracts cert metadata from ``publicCertificate`` without needing
the password (PKCS7 is unencrypted).  The password is stored encrypted at rest
and passed to the lodgement layer when STP/BAS submissions are made; if it is
wrong the first EVTE ping will surface the error.

Older code expected a PKCS12 blob (either bare or XML-wrapped).  Those paths
are preserved for compatibility with non-ATO keystores.

Flow:

1. ``load_keystore(data, password)`` tries to parse ``data`` as
   bare PKCS12. If that fails, tries the ATO SBR CredentialStore XML
   format (PKCS7 publicCertificate). If that also fails, tries the
   generic XML-wrapped-PKCS12 pattern.
2. Success returns a ``LoadedKeystore`` dataclass with the cert's
   subject CN, issuer CN, serial, and validity window already
   extracted.

Callers handle ``KeystoreError`` for anything unparseable.
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

_SBR_NS = "http://auth.abr.gov.au/credential/xsd/SBRCredentialStore"


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

    Raises ``KeystoreError`` if no supported format can be parsed.
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

    # Path 2: ATO SBR CredentialStore XML (RAM Machine Credential).
    # The publicCertificate element holds a PKCS7 SignedData cert chain —
    # no password needed. The private key is encrypted separately; the
    # password will be verified on the first EVTE ping.
    sbr_cert = _extract_ato_sbr_cert(data)
    if sbr_cert is not None:
        return sbr_cert

    # Path 3: generic XML envelope around a base64 PKCS12 blob.
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


def _extract_ato_sbr_cert(data: bytes) -> x509.Certificate | None:
    """Extract the end-entity cert from an ATO SBR CredentialStore XML.

    Returns ``None`` when the data is not in that format so the caller can
    fall through to the next parser.
    """
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    if root.tag != f"{{{_SBR_NS}}}credentialStore":
        return None
    elem = root.find(f".//{{{_SBR_NS}}}publicCertificate")
    if elem is None or not (elem.text or "").strip():
        return None
    stripped = re.sub(r"\s+", "", elem.text.strip())
    try:
        cert_bytes = base64.b64decode(stripped, validate=True)
    except (ValueError, binascii.Error):
        return None

    # publicCertificate is PKCS7 SignedData (DER with BER indefinite length).
    from cryptography.hazmat.primitives.serialization import pkcs7
    try:
        certs = pkcs7.load_der_pkcs7_certificates(cert_bytes)
        if certs:
            return certs[0]
    except Exception:
        pass

    # Fallback: bare DER X.509 (not expected for RAM creds but tolerate it).
    try:
        return x509.load_der_x509_certificate(cert_bytes)
    except Exception:
        return None


def _unwrap_xml_envelope(data: bytes) -> bytes | None:
    """Best-effort: pull a base64 PKCS12 blob out of an XML document.

    Returns ``None`` if the bytes don't even look like XML or no
    plausible blob is found.
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
