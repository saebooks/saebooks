"""Parse + introspect an ATO RAM Machine Credential keystore —
COMMUNITY EDITION STUB.

The full implementation parses the Machine Credential Downloader's
``keystore.xml`` export (ATO's proprietary SBRCredentialStore XML format,
PKCS7/PKCS12 cert extraction, XML-wrapped-PKCS12 fallback) to surface cert
metadata for the onboarding wizard. Parsing + validating a real ATO RAM
Machine Credential is part of the commercial SAE Books e-lodgement feature
— see CHARTER.md / LICENSING.md. ``load_keystore`` raises
``NotImplementedError`` in this edition; ``KeystoreError`` and
``LoadedKeystore`` stay defined so callers keep their import surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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

    COMMUNITY EDITION STUB — always raises. See module docstring.
    """
    raise NotImplementedError(
        "Certified e-lodgement is a commercial SAE Books feature; the community "
        "edition ships box definitions + the return calculator but not the "
        "regulator transmission adapters. See CHARTER.md / LICENSING.md."
    )
