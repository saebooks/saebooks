"""LV identifier validators — reģistrācijas numurs, PVN number,
personas kods.

Format data + provenance live beside the other LV seeds in
``seeds/jurisdictions/LV/tax_identifiers.yaml`` (a ``reference_seed:
false`` module-data file, the UK precedent); the check-digit COMPUTE
lives here.

Three identifiers, three dispositions:

* **PVN number** (``lv_pvn`` scheme) — "LV" + 11 digits. Legal entities
  (first digit > 3): mod-11 check digit, weights (9,1,4,8,3,10,2,5,7,6)
  over digits 1-10, ``r = 3 - (sum mod 11)``; ``r == -1`` invalid;
  ``r < -1`` → check = r + 11; else check = r. Algorithm VERIFIED via
  two independent open-source implementations (python-stdnum +
  vat_validator) agreeing exactly, and empirically confirmed against
  eight real public registration numbers (airBaltic 40003245752,
  Swedbank 40003074764, LMT 50003050931, Latvenergo 40003009497, VID
  90000069281, ...). Natural persons (first digit <= 3, incl. the
  post-2017 "32" codes): the checksum variant is UNVERIFIED
  (python-stdnum's own comment says unconfirmed) — FORMAT-ONLY
  acceptance, never a guessed algorithm (the UK UTR discipline).

* **Reģistrācijas numurs** (``lv_regnum`` scheme) — 11 digits. No
  separately-published UR check-digit spec exists (NOT FOUND in the
  research pass), but for legal entities the VAT number's numeric body
  IS the registration number, and the VAT checksum validates every real
  registration number tested — so the same checksum is applied for
  first-digit>3 numbers, documented as empirically-confirmed rather
  than spec-published. First digit <= 3 (not a legal-entity shape):
  rejected — a registration number is an Uzņēmumu reģistrs identifier,
  never a personas kods.

* **Personas kods** — 11 digits, DDMMYY-XXXXX (or "32"-prefixed
  date-free codes since 2017-07-01, PMLP primary). Checksum VERIFIED
  (laacz.lv reference + python-stdnum structural corroboration):
  weights (1,6,3,7,9,10,5,8,4,2), ``check = ((1101 - sum) mod 11) mod
  10``. A PERSONAL identifier — plain module function (the NZ
  bank-account precedent), NOT registered as a business-identifier
  scheme.

The two business schemes register into
``services.business_identifiers`` at import — this module is imported
by ``lv.tax``/``lv.payroll`` (never by the module ``__init__``), so the
validators activate on first LV compute dispatch, exactly the NZ
``nz_nzbn`` lazy-registration discipline. Until then
``validate("lv_pvn", ...)`` stays no-opinion (``None``) — the pre-module
core behaviour, a clean degrade.
"""
from __future__ import annotations

import re

from saebooks.services.business_identifiers import register_validator

#: PVN number shape — optional "LV" prefix + 11 digits.
PVN_NUMBER_REGEX = r"^(LV)?\d{11}$"

#: Registration number shape — 11 digits.
REGNUM_REGEX = r"^\d{11}$"

#: Personas kods shape — 6+5 digits, optional hyphen.
PERSONAS_KODS_REGEX = r"^\d{6}-?\d{5}$"

_PVN_RE = re.compile(PVN_NUMBER_REGEX)
_PK_RE = re.compile(PERSONAS_KODS_REGEX)

_VAT_WEIGHTS = (9, 1, 4, 8, 3, 10, 2, 5, 7, 6)
_PK_WEIGHTS = (1, 6, 3, 7, 9, 10, 5, 8, 4, 2)


def _legal_entity_check_digit_ok(digits: str) -> bool:
    """The verified mod-11 check over an 11-digit legal-entity number."""
    total = sum(w * int(d) for w, d in zip(_VAT_WEIGHTS, digits[:10], strict=True))
    r = 3 - (total % 11)
    if r == -1:
        return False
    check = r + 11 if r < -1 else r
    return check == int(digits[10])


@register_validator("lv_pvn")
def validate_pvn_number(value: str) -> bool:
    """Latvian VAT number check — "LV" + 11 digits.

    Legal entities (first digit > 3): verified mod-11 check digit.
    Natural persons (first digit <= 3): FORMAT-ONLY (the natural-person
    checksum variant is unverified — see module docstring); ``True``
    here means "correctly shaped", not "checksum-verified".
    """
    cleaned = value.strip().upper().replace(" ", "")
    if not _PVN_RE.match(cleaned):
        return False
    digits = cleaned[2:] if cleaned.startswith("LV") else cleaned
    if int(digits[0]) > 3:
        return _legal_entity_check_digit_ok(digits)
    return True


@register_validator("lv_regnum")
def validate_regnum(value: str) -> bool:
    """Uzņēmumu reģistrs registration number — 11 digits.

    First digit must be > 3 (legal-entity range; a <=3 first digit is a
    personas kods shape, not a registration number). Check digit: the
    empirically-confirmed VAT checksum (see module docstring).
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) != 11 or int(digits[0]) <= 3:
        return False
    return _legal_entity_check_digit_ok(digits)


def validate_personas_kods(value: str) -> bool:
    """Personas kods checksum (verified — laacz.lv algorithm).

    Applies to both the legacy DDMMYY-XXXXX codes and the post-2017
    "32"-prefixed date-free codes (same checksum). Plain module
    function — a personal identifier is not a business-identifier
    scheme.
    """
    if not _PK_RE.match(value.strip()):
        return False
    digits = value.strip().replace("-", "")
    total = sum(w * int(d) for w, d in zip(_PK_WEIGHTS, digits[:10], strict=True))
    return ((1101 - total) % 11) % 10 == int(digits[10])


__all__ = [
    "PERSONAS_KODS_REGEX",
    "PVN_NUMBER_REGEX",
    "REGNUM_REGEX",
    "validate_personas_kods",
    "validate_pvn_number",
    "validate_regnum",
]
