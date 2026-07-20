"""LT identifier validators — company code, PVM (VAT) number, asmens
kodas (personal code).

LT jurisdiction module (the NZ ``identifiers.py`` precedent: check-digit
COMPUTE lives here; the validators register themselves into
``services.business_identifiers`` at import — i.e. lazily, on first LT
compute dispatch, because this module is imported by ``tax.py``/
``payroll.py``, never by the import-light package ``__init__``).

All three algorithms were primary-verified 2026-07-12:

* **Company code** (juridinio asmens kodas, Registrų centras) — 9
  digits; the OECD AEOI TIN sheet for Lithuania (authored by the
  Lithuanian authorities) publishes the rule: weight digits 1-8 by
  1,2,3,4,5,6,7,8, sum, mod 11 — the remainder IS the 9th (check)
  digit, and a code whose remainder would be 10 is never issued (the
  registry redraws), so remainder 10 = invalid. Verified against a real
  code (Telia Lietuva 121215434 -> 103 mod 11 = 4). Older 7-digit codes
  exist historically but are not validated here (format lore only,
  UNVERIFIED — a 7-digit value returns False).

* **PVM mokėtojo kodas** ("LT" + 9 or 12 digits) — the two-pass mod-11
  scheme from the EU VIES national routines (verified via two
  independent open-source implementations agreeing + a real published
  number, LT212154314): first-pass weights ``1 + (i mod 9)``
  (9-digit: 1,2,3,4,5,6,7,8; 12-digit: 1,2,3,4,5,6,7,8,9,1,2); if the
  sum mod 11 is 10, second-pass weights ``1 + ((i+2) mod 9)``
  (3,4,5,6,7,8,9,1 / 3,4,5,6,7,8,9,1,2,3,4); check digit = result
  mod 11 mod 10. Structural rules: a 9-digit number's 8th digit must
  be 1; a 12-digit number's 11th digit must be 1.

* **Asmens kodas** (personal code) — 11 digits, structure
  C1(1-6) YY MM DD NNN C11; checksum from the same OECD TIN sheet:
  weights 1,2,3,4,5,6,7,8,9,1 over C1-C10, mod 11; if 10, reweigh
  3,4,5,6,7,8,9,1,2,3, mod 11; if 10 again the check digit is 0.
  Verified against the sheet's own worked example (33309240064 -> 4).
  Date-structure plausibility (month 1-12, day 1-31) is enforced; the
  conventional century/gender meaning of C1 is NOT relied on (the
  official sheet only states 1-6 — the century mapping is UNVERIFIED,
  so only membership in 1-6 is checked).
"""
from __future__ import annotations

import re

from saebooks.services.business_identifiers import register_validator

_COMPANY_CODE_WEIGHTS = (1, 2, 3, 4, 5, 6, 7, 8)
_PERSONAL_WEIGHTS_1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 1)
_PERSONAL_WEIGHTS_2 = (3, 4, 5, 6, 7, 8, 9, 1, 2, 3)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


@register_validator("lt_company_code")
def validate_company_code(value: str) -> bool:
    """Juridinio asmens kodas — 9 digits, weights 1-8, mod 11.

    The remainder IS the check digit; remainder 10 is never issued
    (the registry redraws from the code pool), so it is invalid here.
    """
    digits = _digits_only(value)
    if len(digits) != 9:
        return False
    total = sum(
        int(d) * w
        for d, w in zip(digits[:8], _COMPANY_CODE_WEIGHTS, strict=True)
    )
    remainder = total % 11
    if remainder == 10:
        return False
    return remainder == int(digits[8])


def _vat_check_digit(payload: str) -> int:
    """The VIES two-pass mod-11 check digit for an LT VAT payload
    (the number without its final check digit)."""
    check = sum((1 + i % 9) * int(d) for i, d in enumerate(payload)) % 11
    if check == 10:
        check = sum((1 + (i + 2) % 9) * int(d) for i, d in enumerate(payload))
    return check % 11 % 10


@register_validator("lt_vat")
def validate_vat_number(value: str) -> bool:
    """PVM mokėtojo kodas — "LT" prefix optional; 9 digits (legal
    entities, 8th digit must be 1) or 12 digits (temporarily
    registered / natural persons, 11th digit must be 1); two-pass
    mod-11 check digit."""
    cleaned = re.sub(r"[\s.-]", "", (value or "")).upper()
    if cleaned.startswith("LT"):
        cleaned = cleaned[2:]
    if not cleaned.isdigit() or len(cleaned) not in (9, 12):
        return False
    if cleaned[-2] != "1":  # 8th of 9 / 11th of 12 — same index from the end
        return False
    return _vat_check_digit(cleaned[:-1]) == int(cleaned[-1])


@register_validator("lt_personal_code")
def validate_personal_code(value: str) -> bool:
    """Asmens kodas — 11 digits, date-structure plausibility + the
    published two-pass mod-11 checksum."""
    digits = _digits_only(value)
    if len(digits) != 11:
        return False
    if not (1 <= int(digits[0]) <= 6):
        return False
    month = int(digits[3:5])
    day = int(digits[5:7])
    # Plausible-range check per the official sheet's stated structure
    # (month 1-12); exact calendar validity (e.g. Feb 30) is not
    # enforced — the checksum, not the calendar, is the published rule.
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return False
    d = [int(c) for c in digits]
    remainder = sum(
        x * w for x, w in zip(d[:10], _PERSONAL_WEIGHTS_1, strict=True)
    ) % 11
    if remainder == 10:
        remainder = sum(
            x * w for x, w in zip(d[:10], _PERSONAL_WEIGHTS_2, strict=True)
        ) % 11
        if remainder == 10:
            remainder = 0
    return remainder == d[10]


__all__ = [
    "validate_company_code",
    "validate_personal_code",
    "validate_vat_number",
]
