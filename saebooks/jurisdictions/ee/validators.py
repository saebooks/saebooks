"""EE company-registry identifier format validators.

``registrikood`` (Estonian business-registry code, äriregistri kood) and
``kmv_number`` (Estonian VAT number, "käibemaksukohustuslase number") are
stored as ``business_identifiers`` rows under the ``ee_regcode`` /
``ee_vat`` schemes — the äriregistri kood is the EE primary registry
identifier (``business_identifiers._PRIMARY_SCHEME_BY_JURISDICTION``),
exactly as the ABN is for AU under ``au_abn``.

These are the *raising* validators the API schema layer
(``CompanyCreate`` / ``CompanyUpdate``) calls to reject a malformed value
with a 422 — the EE-specific format rule lives here, at the EE module
surface, not hardcoded in core schema code. The non-raising ``bool``
form used for the ``business_identifiers`` ``check_digit_valid`` column
lives in the sibling ``identifiers.py`` (the lv precedent). Callers apply
these only when the company's jurisdiction is ``"EE"``.
"""
from __future__ import annotations

import re

REGISTRIKOOD_RE = re.compile(r"^\d{8}$")
KMV_NUMBER_RE = re.compile(r"^EE\d{9}$")


def validate_registrikood(value: str) -> str:
    """Raise ``ValueError`` unless ``value`` is exactly 8 digits."""
    if not REGISTRIKOOD_RE.match(value):
        raise ValueError("registrikood must be exactly 8 digits")
    return value


def validate_kmv_number(value: str) -> str:
    """Raise ``ValueError`` unless ``value`` is 'EE' + 9 digits."""
    if not KMV_NUMBER_RE.match(value):
        raise ValueError("kmv_number must be 'EE' followed by 9 digits")
    return value
