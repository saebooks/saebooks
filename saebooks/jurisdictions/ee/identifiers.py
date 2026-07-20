"""EE identifier validators — registrikood + KMV (VAT) number.

The non-raising ``bool`` form of ``validators.py``, registered into
``services.business_identifiers`` so a stored ``ee_regcode`` / ``ee_vat``
row gets a ``check_digit_valid`` opinion — the same posture as
``jurisdictions/lv/identifiers.py``. Format-only (Estonia publishes no
public check-digit algorithm for either identifier that this repo has
verified against a worked example; the UK UTR / LV natural-person
discipline — never a guessed checksum), so ``True`` here means
"correctly shaped", not "checksum-verified".

The two schemes register into ``services.business_identifiers`` at
import; this module is imported on first EE onboarding dispatch
(``jurisdictions.ee.chart``), exactly the ``lv_pvn`` / ``nz_nzbn`` lazy-
registration discipline. Until then ``validate("ee_regcode", ...)`` stays
no-opinion (``None``) — the pre-module core behaviour, a clean degrade.
"""
from __future__ import annotations

from saebooks.jurisdictions.ee.validators import KMV_NUMBER_RE, REGISTRIKOOD_RE
from saebooks.services.business_identifiers import register_validator


@register_validator("ee_regcode")
def validate_registrikood(value: str) -> bool:
    """äriregistri kood shape — exactly 8 digits."""
    return REGISTRIKOOD_RE.match(value.strip()) is not None


@register_validator("ee_vat")
def validate_kmv_number(value: str) -> bool:
    """KMV (VAT) number shape — 'EE' + 9 digits."""
    return KMV_NUMBER_RE.match(value.strip().upper()) is not None


__all__ = ["validate_kmv_number", "validate_registrikood"]
