"""LT identifier validators — company code, PVM number, asmens kodas.

Vectors: real published numbers (Telia Lietuva's company code
121215434 and VAT number LT212154314), the OECD TIN sheet's own
worked personal-code example (33309240064 — exercises the second
weighting pass), and the python-stdnum reference vectors for the
9- and 12-digit VAT variants (100004801610 exercises the
double-second-pass -> mod-10 fold).

Also the deterministic positive pin the business-identifiers test
NOTE prescribes for module-registered validators (the
test_nz_nzbn_validator_registered_by_nz_module precedent).
"""
from __future__ import annotations

import pytest

from saebooks.jurisdictions.lt.identifiers import (
    validate_company_code,
    validate_personal_code,
    validate_vat_number,
)
from saebooks.services import business_identifiers as bi_svc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("121215434", True),      # Telia Lietuva — real code, check 103 % 11 = 4
        ("121 215 434", True),    # grouping tolerated
        ("121215435", False),     # mutated check digit
        ("100000085", False),     # payload 10000008 -> remainder 10 (never issued)
        ("12121543", False),      # 8 digits (historic 7/8-digit codes not validated)
        ("1212154340", False),    # 10 digits
        ("", False),
    ],
)
def test_company_code(value: str, expected: bool) -> None:
    assert validate_company_code(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("LT212154314", True),     # Telia Lietuva — real published VAT number
        ("212154314", True),       # prefix optional
        ("lt 212 154 314", True),  # case/space tolerated
        ("LT212154315", False),    # mutated check digit
        ("LT119511515", True),     # stdnum 9-digit reference vector
        ("LT100004801610", True),  # stdnum 12-digit — second pass -> mod-10 fold
        ("LT100004801611", False),
        ("LT212154324", False),    # 8th digit != 1 (structural rule)
        ("LT2121543", False),      # wrong length
        ("LTABC154314", False),
    ],
)
def test_vat_number(value: str, expected: bool) -> None:
    assert validate_vat_number(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("33309240064", True),   # OECD sheet worked example (second pass -> 4)
        ("33309240065", False),  # mutated check digit
        ("33313240064", False),  # month 13 — implausible date structure
        ("73309240064", False),  # C1 = 7, outside the published 1-6 range
        ("3330924006", False),   # 10 digits
        ("", False),
    ],
)
def test_personal_code(value: str, expected: bool) -> None:
    assert validate_personal_code(value) is expected


def test_lt_validators_registered_into_core_schemes() -> None:
    """Deterministic pin of the LT module's lazy registrations (the
    NOTE in test_business_identifiers.py): importing the module (done
    at the top of this file) registers all three lt_* validators, and
    the scheme keys round-trip through the core registry."""
    assert {"lt_company_code", "lt_vat", "lt_personal_code"} <= bi_svc.KNOWN_SCHEMES
    assert bi_svc.validate("lt_company_code", "121215434") is True
    assert bi_svc.validate("lt_company_code", "121215435") is False
    assert bi_svc.validate("lt_vat", "LT212154314") is True
    assert bi_svc.validate("lt_vat", "LT212154315") is False
    assert bi_svc.validate("lt_personal_code", "33309240064") is True
    assert bi_svc.validate("lt_personal_code", "33309240065") is False
