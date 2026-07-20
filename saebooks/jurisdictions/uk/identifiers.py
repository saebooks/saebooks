"""UK tax / registry identifier validation.

UK jurisdiction module. Format data lives beside the other UK seeds in
``seeds/jurisdictions/UK/tax_identifiers.yaml`` (a ``reference_seed:
false`` module-data file, the EE ``kmdtyyp_mapping.yaml`` precedent);
the check-digit COMPUTE lives here.

VAT registration numbers — dual checksum, accept either
-------------------------------------------------------
The final 2 digits of a 9-digit UK VAT number are check digits under
ONE of two schemes, depending on when the number was issued:

* **mod-97** (older registrations): weight the first 7 digits by
  8,7,6,5,4,3,2, sum, then repeatedly subtract 97 until the result is
  zero or negative; the absolute value must equal the 2 check digits.
* **mod-9755** (numbers issued since ~2010): identical, but add 55 to
  the weighted sum before the subtraction step.

A number is treated as valid when it passes EITHER scheme (strategy
§5.1: "dual Mod-97 + Mod-9755 validation — implement both, accept
either"). Algorithm confirmed against multiple published sources
2026-07-12; HMRC's own spec is distributed on request only.

UTR — format-only (check digit PARKED)
--------------------------------------
The 10-digit Unique Taxpayer Reference carries a check digit whose
algorithm is UNVERIFIED against any HMRC spec. :func:`validate_utr` is
deliberately FORMAT-ONLY; do not add a guessed algorithm.

CRN — format + registry lookup only
-----------------------------------
Company registration numbers have NO checksum; beyond the format the
authoritative check is a Companies House lookup (the UK lodgement
adapter's ``companies_house`` target).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- VAT ------------------------------------------------------------------

_VAT_WEIGHTS = (8, 7, 6, 5, 4, 3, 2)

_VAT_RE = re.compile(r"^(GB|XI)?(\d{9})(\d{3})?$")
_VAT_GD_HA_RE = re.compile(r"^(GB|XI)?(GD[0-4]\d{2}|HA[5-9]\d{2})$")

_CRN_RE = re.compile(r"^(\d{8}|(SC|NI|OC|SO|NC|LP|SL|CE|CS)\d{6})$")

# NINO: first letter never D/F/I/Q/U/V; second never D/F/I/O/Q/U/V;
# banned pairs BG/GB/KN/NK/NT/TN/ZZ (HMRC NIM39110).
_NINO_RE = re.compile(r"^[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]$")
_NINO_BANNED_PREFIXES = frozenset({"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"})

_PAYE_REF_RE = re.compile(r"^\d{3}/[A-Z0-9]{1,10}$")
_ACCOUNTS_OFFICE_RE = re.compile(r"^\d{3}P[A-Z]\d{7}[0-9X]$")
_UTR_RE = re.compile(r"^\d{10}$")


@dataclass(frozen=True, slots=True)
class VatCheckResult:
    """Outcome of a UK VAT-number validation, including WHICH checksum
    scheme accepted it (useful diagnostics; ``valid`` is the verdict)."""

    valid: bool
    passes_mod97: bool = False
    passes_mod9755: bool = False
    reason: str = ""


def _normalise(value: str) -> str:
    return re.sub(r"[\s.-]", "", (value or "")).upper()


def _vat_checksum_remainder(digits: str, *, offset: int) -> int:
    """The repeated-subtract-97 step, expressed as arithmetic: the
    weighted sum (+ offset) reduced by 97 until <= 0, made positive."""
    total = sum(int(d) * w for d, w in zip(digits[:7], _VAT_WEIGHTS)) + offset
    while total > 0:
        total -= 97
    return -total


def validate_vat_number(value: str) -> VatCheckResult:
    """Validate a UK VAT registration number (GB/XI prefix optional,
    spaces/dots/hyphens tolerated, 12-digit branch-trader suffix
    accepted, GD/HA ranges format-only)."""
    cleaned = _normalise(value)
    if _VAT_GD_HA_RE.match(cleaned):
        # Government department / health authority ranges use a
        # different (non-public) scheme — format acceptance only.
        return VatCheckResult(valid=True, reason="gd_ha_format_only")

    m = _VAT_RE.match(cleaned)
    if not m:
        return VatCheckResult(valid=False, reason="format")

    digits = m.group(2)
    check = int(digits[7:9])
    mod97 = _vat_checksum_remainder(digits, offset=0) == check
    mod9755 = _vat_checksum_remainder(digits, offset=55) == check
    return VatCheckResult(
        valid=mod97 or mod9755,
        passes_mod97=mod97,
        passes_mod9755=mod9755,
        reason="" if (mod97 or mod9755) else "checksum",
    )


# --- The format-only validators --------------------------------------------


def validate_crn(value: str) -> bool:
    """Company registration number — format only (8 digits, or a known
    2-letter prefix + 6 digits). Authoritative validity = Companies
    House lookup; no checksum exists."""
    return bool(_CRN_RE.match(_normalise(value)))


def validate_nino(value: str) -> bool:
    """National Insurance number — format + banned-prefix rules."""
    cleaned = _normalise(value)
    if not _NINO_RE.match(cleaned):
        return False
    return cleaned[:2] not in _NINO_BANNED_PREFIXES


def validate_paye_reference(value: str) -> bool:
    """PAYE employer reference — NNN/XXXXX format."""
    return bool(_PAYE_REF_RE.match(_normalise(value)))


def validate_accounts_office_reference(value: str) -> bool:
    """Accounts Office reference — the 13-character PAYMENT identifier
    (123PA1234567X shape), distinct from the PAYE employer reference."""
    return bool(_ACCOUNTS_OFFICE_RE.match(_normalise(value)))


def validate_utr(value: str) -> bool:
    """Unique Taxpayer Reference — FORMAT-ONLY (10 digits). The check-
    digit algorithm is PARKED as unverified (module docstring); this
    deliberately accepts any 10-digit string."""
    return bool(_UTR_RE.match(_normalise(value)))


__all__ = [
    "VatCheckResult",
    "validate_accounts_office_reference",
    "validate_crn",
    "validate_nino",
    "validate_paye_reference",
    "validate_utr",
    "validate_vat_number",
]
