"""NZ identifier validators — IRD (mod-11 double-pass), NZBN (GS1), bank format.

IRD vectors:
* 49-091-850 — IRD's published worked example (primary pass).
* 100000131 / 100000273 / 100000305 — bases whose PRIMARY weighting
  yields 10, so validity comes from the SECONDARY pass
  [7,4,3,2,5,2,7,6] (computed against the §5.7 algorithm; these
  exercise the double-pass branch specifically).
* 10001064x — a base where BOTH passes yield 10: invalid for every
  check digit.

NZBN vector 9429041234563: prefix 942904123456 with GS1 mod-10 check
digit 3 (weights 1,3,1,3,... over the first 12 digits).
"""
from __future__ import annotations

import pytest

from saebooks.jurisdictions.nz.identifiers import (
    validate_bank_account_format,
    validate_ird_number,
    validate_nzbn,
)
from saebooks.services.business_identifiers import validate

# ---------------------------------------------------------------------------
# IRD number.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["49-091-850", "49091850", "49 091 850", "136410132"],
)
def test_ird_valid_primary_pass(value: str) -> None:
    assert validate_ird_number(value) is True


@pytest.mark.parametrize("value", ["100000131", "100000273", "100000305"])
def test_ird_valid_via_secondary_pass(value: str) -> None:
    # These bases yield 10 on the primary weighting — validity is
    # decided by the secondary [7,4,3,2,5,2,7,6] pass.
    assert validate_ird_number(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "49091851",     # wrong check digit
        "136410133",    # wrong check digit
        "100000130",    # secondary-pass base, wrong check digit
    ],
)
def test_ird_invalid_check_digit(value: str) -> None:
    assert validate_ird_number(value) is False


@pytest.mark.parametrize("check", list("0123456789"))
def test_ird_double_ten_base_invalid_for_every_check_digit(check: str) -> None:
    # Base 10001064 yields 10 on BOTH weightings — no valid check digit
    # exists (the algorithm's explicit both-10 -> invalid branch).
    assert validate_ird_number("10001064" + check) is False


@pytest.mark.parametrize(
    "value",
    [
        "",             # empty
        "1234567",      # 7 digits — below range/length
        "9125568",      # 7 digits
        "1500000001",   # 10 digits
        "5000000",      # in-length-range digits but below 10,000,000? (7 digits — invalid)
        "999999999",    # above the 150,000,000 range ceiling
        "abcdefgh",
    ],
)
def test_ird_shape_and_range_rejected(value: str) -> None:
    assert validate_ird_number(value) is False


def test_ird_core_scheme_registration_agrees() -> None:
    # The core nz_ird validator (which the module re-exports) must give
    # the same verdicts through the scheme registry.
    assert validate("nz_ird", "49-091-850") is True
    assert validate("nz_ird", "49-091-851") is False


# ---------------------------------------------------------------------------
# NZBN (GS1 GLN check digit).
# ---------------------------------------------------------------------------


def test_nzbn_valid_gs1_check_digit() -> None:
    assert validate_nzbn("9429041234563") is True


@pytest.mark.parametrize(
    "value",
    [
        "9429041234562",   # wrong check digit
        "942904123456",    # 12 digits
        "94290412345631",  # 14 digits
        "",
        "abcdefghijklm",
    ],
)
def test_nzbn_invalid(value: str) -> None:
    assert validate_nzbn(value) is False


def test_nzbn_registered_into_core_scheme_registry() -> None:
    # Before the NZ module, validate("nz_nzbn", ...) returned None
    # (no-opinion). The module registers the GS1 validator.
    assert validate("nz_nzbn", "9429041234563") is True
    assert validate("nz_nzbn", "9429041234562") is False


# ---------------------------------------------------------------------------
# Bank account (format only — checksum table deliberately not implemented).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "01-0902-0068389-00",
        "01-0902-0068389-000",
        "010902006838900",
        "01 0902 0068389 00",
    ],
)
def test_bank_account_format_valid(value: str) -> None:
    assert validate_bank_account_format(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "1-0902-0068389-00",    # 1-digit bank
        "01-902-0068389-00",    # 3-digit branch
        "01-0902-068389-00",    # 6-digit body
        "01-0902-0068389-0",    # 1-digit suffix
        "01-0902-0068389-0000", # 4-digit suffix
        "",
    ],
)
def test_bank_account_format_invalid(value: str) -> None:
    assert validate_bank_account_format(value) is False
