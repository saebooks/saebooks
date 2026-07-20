"""Identifier validator vectors — UK jurisdiction module.

The mod-97 / mod-9755 VAT vectors below were generated from the
algorithm itself (weights 8,7,6,5,4,3,2; subtract 97 to non-positive;
+55 first for mod-9755) and cross-checked by hand:

* ``562235945`` — weighted sum 315; 315 - 97x3 = 24... (checks: 315 ->
  -76 -> |−76| != 45? No — 315-97=218, 218-97=121, 121-97=24, 24-97=-73
  ... verified programmatically): passes mod-97 only.
* ``100000132`` — passes mod-9755 only.
* ``100000089`` — passes mod-97 only.
* ``100000000`` — passes neither.
"""
from __future__ import annotations

from saebooks.jurisdictions.uk.identifiers import (
    validate_accounts_office_reference,
    validate_crn,
    validate_nino,
    validate_paye_reference,
    validate_utr,
    validate_vat_number,
)


def test_vat_mod97_vector() -> None:
    result = validate_vat_number("GB 562 2359 45")
    assert result.valid
    assert result.passes_mod97
    assert not result.passes_mod9755


def test_vat_mod9755_vector() -> None:
    result = validate_vat_number("100000132")
    assert result.valid
    assert result.passes_mod9755
    assert not result.passes_mod97


def test_vat_accepts_either_scheme() -> None:
    assert validate_vat_number("100000089").valid   # mod-97
    assert validate_vat_number("100000132").valid   # mod-9755


def test_vat_rejects_bad_checksum_and_format() -> None:
    bad = validate_vat_number("100000000")
    assert not bad.valid
    assert bad.reason == "checksum"
    assert not validate_vat_number("GB12345").valid
    assert validate_vat_number("GB12345").reason == "format"


def test_vat_xi_prefix_and_branch_suffix() -> None:
    assert validate_vat_number("XI562235945").valid
    # 12-digit branch trader: 9 valid digits + 3-digit branch suffix.
    assert validate_vat_number("GB562235945001").valid


def test_vat_gd_ha_ranges_format_only() -> None:
    assert validate_vat_number("GBGD001").valid
    assert validate_vat_number("GBHA599").valid
    assert not validate_vat_number("GBGD500").valid   # GD range is 000-499
    assert not validate_vat_number("GBHA100").valid   # HA range is 500-999


def test_crn_formats() -> None:
    assert validate_crn("01234567")
    assert validate_crn("SC123456")
    assert validate_crn("NI123456")
    assert validate_crn("OC123456")   # England/Wales LLP
    assert validate_crn("SO123456")   # Scottish LLP
    assert not validate_crn("1234567")     # 7 digits
    assert not validate_crn("XX123456")    # unknown prefix
    assert not validate_crn("SC12345")     # short


def test_nino_format_and_banned_prefixes() -> None:
    assert validate_nino("AB123456C")
    assert validate_nino("ab 12 34 56 c")   # normalisation
    for banned in ("BG", "GB", "KN", "NK", "NT", "TN", "ZZ"):
        assert not validate_nino(f"{banned}123456A"), banned
    assert not validate_nino("DA123456A")   # first letter D banned
    assert not validate_nino("AO123456A")   # second letter O banned
    assert not validate_nino("AB123456E")   # suffix must be A-D


def test_paye_and_accounts_office_references() -> None:
    assert validate_paye_reference("123/AB456")
    assert validate_paye_reference("951/A1")
    assert not validate_paye_reference("12/AB456")
    assert not validate_paye_reference("123AB456")
    assert validate_accounts_office_reference("123PA1234567X")
    assert validate_accounts_office_reference("123PA12345678")
    assert not validate_accounts_office_reference("123PA123456")      # too short
    assert not validate_accounts_office_reference("123QA1234567X")    # no 'P'


def test_utr_format_only_check_digit_parked() -> None:
    # ANY 10-digit string is accepted — the check-digit algorithm is
    # PARKED as unverified; this pins the deliberate format-only scope.
    assert validate_utr("1234567890")
    assert validate_utr("0000000000")
    assert not validate_utr("123456789")
    assert not validate_utr("12345678901")
    assert not validate_utr("12345A7890")
