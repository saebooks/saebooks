"""NZ identifier validators — IRD number, NZBN, bank account format.

Source: ~/records/saebooks/nz-market-entry-strategy.md §5.7.

Three identifiers, three dispositions:

* **IRD number** (mod-11 double-pass) — ALREADY IMPLEMENTED IN CORE:
  ``services.business_identifiers._validate_nz_ird`` (registered for
  scheme ``nz_ird``, verified against IRD's published example
  49-091-850). Re-exported here as :func:`validate_ird_number` so the
  NZ module surface exposes it without duplicating the algorithm;
  tests/services/test_nz_identifiers.py exercises the double-pass with
  vectors whose primary weighting yields 10.

* **NZBN** (13 digits, GS1 GLN format) — the GS1 check-digit validator
  is implemented here and registered into the core scheme registry for
  ``nz_nzbn`` at import (the registry decorator exists precisely so a
  bolt-on module can add a validator without editing core — before
  this, ``validate("nz_nzbn", ...)`` returned ``None``/no-opinion).

* **Bank account** (BB-bbbb-AAAAAAA-SS(S)) — FORMAT validation only.
  The per-bank modulus checksums + branch-range table are
  validator-repo-sourced and flagged "confirm against the Payments NZ
  register at build time" in the research — an unconfirmed checksum
  table is not implemented (never a silent wrong number); format-only
  matches the T9 convention where several core schemes are also
  format-only. There is no ``nz_bank_account`` scheme in
  ``KNOWN_SCHEMES`` (bank accounts are not business identifiers), so
  :func:`validate_bank_account_format` is a plain module function for
  payroll/contact plumbing to call directly.

Seed note: ``tax_id_validation_patterns`` (the reference-DB regex
catalogue) has NO unique constraint, so the idempotent seed loader
cannot upsert rows into it — the NZ patterns live here in code and the
table gap is flagged in the build summary (needs a reference migration
owned by the engine lane).
"""
from __future__ import annotations

import re

from saebooks.services.business_identifiers import (
    _validate_nz_ird,
    register_validator,
)

#: IRD number shape (8-9 digits, optionally hyphen/space grouped).
IRD_NUMBER_REGEX = r"^\d{2,3}[- ]?\d{3}[- ]?\d{3}$"

#: NZBN shape — 13 digits, GS1 GLN format (all NZBNs today start with
#: the GS1 New Zealand prefix 94, per nzbn.govt.nz).
NZBN_REGEX = r"^94\d{11}$"

#: NZ bank account: bank (2) - branch (4) - body (7) - suffix (2-3),
#: hyphen/space tolerant.
BANK_ACCOUNT_REGEX = r"^\d{2}[- ]?\d{4}[- ]?\d{7}[- ]?\d{2,3}$"

_BANK_ACCOUNT_RE = re.compile(BANK_ACCOUNT_REGEX)


def validate_ird_number(value: str) -> bool:
    """IRD number mod-11 double-pass check (§5.7).

    Delegates to the core ``nz_ird`` validator (primary weights
    [3,2,7,6,5,4,3,2]; secondary [7,4,3,2,5,2,7,6] when the primary
    check digit comes out 10; invalid if both passes yield 10). Range
    (10,000,000-150,000,000) is additionally enforced here per §5.7 —
    the core validator checks digits/checksum only.
    """
    digits = re.sub(r"\D", "", value)
    if not digits.isdigit() or not digits:
        return False
    n = int(digits)
    if not (10_000_000 <= n <= 150_000_000):
        return False
    return _validate_nz_ird(digits)


@register_validator("nz_nzbn")
def validate_nzbn(value: str) -> bool:
    """NZBN check — 13 digits in GS1 GLN format with a GS1 check digit.

    GS1 mod-10: over the first 12 digits, weight alternately 1,3,1,3,...
    from the LEFT (13-digit codes); check digit = (10 - sum mod 10)
    mod 10 and must equal the 13th digit. Registered into
    ``services.business_identifiers`` for scheme ``nz_nzbn`` (previously
    unvalidated / no-opinion).
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) != 13:
        return False
    total = sum(
        int(c) * (3 if i % 2 else 1) for i, c in enumerate(digits[:12])
    )
    check = (10 - total % 10) % 10
    return check == int(digits[12])


def validate_bank_account_format(value: str) -> bool:
    """NZ bank account FORMAT check: BB-bbbb-AAAAAAA-SS(S).

    Format only — the per-bank modulus checksum + branch-range table is
    deliberately NOT implemented (unconfirmed source; see module
    docstring). A ``True`` here means "correctly shaped", not "exists".
    """
    return _BANK_ACCOUNT_RE.match(value.strip()) is not None


__all__ = [
    "BANK_ACCOUNT_REGEX",
    "IRD_NUMBER_REGEX",
    "NZBN_REGEX",
    "validate_bank_account_format",
    "validate_ird_number",
    "validate_nzbn",
]
