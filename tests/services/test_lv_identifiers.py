"""LV identifier validator tests — registration number, PVN number,
personas kods.

Vector provenance:
* Registration numbers: REAL public Uzņēmumu reģistrs numbers
  (airBaltic, Swedbank, LMT, Latvenergo, VID) — the empirical
  confirmation set for the mod-11 checksum (see the module docstring);
  invalid vectors are the same numbers with a mutated check digit.
* Personas kods: the checksum ALGORITHM is verified (laacz.lv +
  python-stdnum agreement); the specific vectors are algorithm-DERIVED
  (real codes are personal data and are not published) — they pin the
  implementation against regression, not against an official fixture.

Registration pin (the business-identifiers NOTE discipline): lv_pvn /
lv_regnum are NEW schemes registered lazily by the LV module at import
— neither appears in test_business_identifiers' no-validator loop, so
no move-out is needed; the positive pins below are the deterministic
counterpart (the test_nz_nzbn_validator_registered_by_nz_module shape).
"""
from __future__ import annotations

import pytest

from saebooks.jurisdictions.lv.identifiers import (
    validate_personas_kods,
    validate_pvn_number,
    validate_regnum,
)
from saebooks.services import business_identifiers as bi_svc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Real public registration numbers — checksum holds.
        ("40003245752", True),   # airBaltic
        ("40003074764", True),   # Swedbank
        ("50003050931", True),   # LMT
        ("40003009497", True),   # Latvenergo
        ("90000069281", True),   # VID (public body)
        # Mutated check digits.
        ("40003245753", False),
        ("50003050930", False),
        # Wrong shapes.
        ("4000324575", False),    # 10 digits
        ("400032457521", False),  # 12 digits
        ("01019000006", False),   # personas-kods range (first digit <= 3)
    ],
)
def test_regnum_vectors(value: str, expected: bool) -> None:
    assert validate_regnum(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("LV40003245752", True),
        ("40003245752", True),        # prefix optional
        ("lv 4000 3245 752", True),   # case/space tolerant
        ("LV40003245753", False),     # bad check digit
        ("LV4000324575", False),      # 10 digits
        # Natural-person range (first digit <= 3): FORMAT-ONLY True —
        # the natural-person checksum variant is unverified, so shape
        # acceptance, never a guessed algorithm.
        ("LV01019000006", True),
        ("LV32123456785", True),
        ("LV3212345678", False),      # wrong length still fails
    ],
)
def test_pvn_vectors(value: str, expected: bool) -> None:
    assert validate_pvn_number(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Algorithm-derived (see module docstring): checksum of
        # 0101900000 is 6; 1512850012 is 0; new-format 3212345678 is 5.
        ("010190-00006", True),
        ("01019000006", True),        # hyphen optional
        ("151285-00120", True),
        ("321234-56785", True),       # post-2017 "32" date-free format
        ("010190-00007", False),      # mutated check digit
        ("321234-56784", False),
        ("010190-0006", False),       # wrong length
        ("abcdef-00006", False),
    ],
)
def test_personas_kods_vectors(value: str, expected: bool) -> None:
    assert validate_personas_kods(value) is expected


def test_lv_validators_registered_into_business_identifiers() -> None:
    """Deterministic pin of the LV module's lazy scheme registration —
    importing the identifiers module (idempotent) makes lv_pvn/lv_regnum
    validate through the core service."""
    import saebooks.jurisdictions.lv.identifiers  # noqa: F401

    assert bi_svc.validate("lv_pvn", "LV40003245752") is True
    assert bi_svc.validate("lv_pvn", "LV40003245753") is False
    assert bi_svc.validate("lv_regnum", "40003245752") is True
    assert bi_svc.validate("lv_regnum", "40003245753") is False


def test_lv_schemes_are_known_schemes_with_jurisdiction_default() -> None:
    """The registration commit adds lv_pvn/lv_regnum to KNOWN_SCHEMES +
    _SCHEME_JURISDICTION -> LVA (which must have a _global jurisdictions
    seed row — test_scheme_jurisdiction_defaults... enforces that
    globally)."""
    assert {"lv_pvn", "lv_regnum"} <= bi_svc.KNOWN_SCHEMES
    assert bi_svc._SCHEME_JURISDICTION["lv_pvn"] == "LVA"
    assert bi_svc._SCHEME_JURISDICTION["lv_regnum"] == "LVA"
