"""Pure unit tests for ``services.einvoice.mapping`` — no DB."""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from saebooks.services.einvoice import mapping as m

_UNCL5305_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "peppol_bis3" / "codelist" / "UNCL5305.xml"
)

# The full sale-side reachable set (module docstring's SCOPE section) —
# every key must resolve, and resolving must never raise.
_REACHABLE_REPORTING_TYPES = [
    "standard", "standard_legacy_20", "standard_legacy_22",
    "reduced_9", "reduced_13", "reduced_5_legacy", "capital",
    "exempt", "zero_ic_goods", "zero_ic_services", "zero_export",
    "zero_traveller", "rc_domestic_supply", "install_other_ms",
    "no_tax", "NTR",
]

# Purchase-side-only tags (see tax_engine/ee.py + tax_codes.yaml) — MUST NOT
# be reachable from this sale-side map; a line carrying one of these is a
# data-integrity bug upstream, not a mapping the generator should paper over.
_PURCHASE_ONLY_REPORTING_TYPES = [
    "rc_eu_acq_goods", "rc_eu_acq_services", "ic_acq_exempt",
    "rc_domestic_acq", "ee_acq_foreign", "input_import",
    "input_std", "input_cap", "input_exempt",
]


@pytest.mark.parametrize("reporting_type", _REACHABLE_REPORTING_TYPES)
def test_every_reachable_reporting_type_resolves(reporting_type: str) -> None:
    cat = m.resolve_tax_category(reporting_type)
    assert cat.tax_category_id in {
        m.CAT_STANDARD, m.CAT_ZERO_GOODS, m.CAT_EXEMPT, m.CAT_REVERSE_CHARGE,
        m.CAT_INTRA_COMMUNITY, m.CAT_EXPORT, m.CAT_OUTSIDE_SCOPE,
    }


@pytest.mark.parametrize("reporting_type", _PURCHASE_ONLY_REPORTING_TYPES)
def test_purchase_side_reporting_types_are_unreachable(reporting_type: str) -> None:
    with pytest.raises(KeyError):
        m.resolve_tax_category(reporting_type)


def test_all_positive_rates_map_to_category_s_not_distinct_codes() -> None:
    """UNCL5305's own 'Standard rate' description is singular — 24%/13%/9%/
    capital all carry category S, differentiated by cbc:Percent, not by a
    per-rate code (there is no such code in the list)."""
    positive_rate_types = [
        "standard", "standard_legacy_20", "standard_legacy_22",
        "reduced_9", "reduced_13", "reduced_5_legacy", "capital",
    ]
    for rt in positive_rate_types:
        cat = m.resolve_tax_category(rt)
        assert cat.tax_category_id == m.CAT_STANDARD, rt
        assert cat.rate_carries_percent is True, rt


def test_no_aa_code_in_the_real_unc15305_subset() -> None:
    """The task brief's own guess ('S/AA/E') was wrong — assert against the
    REAL fetched UNCL5305 subset (tests/fixtures/peppol_bis3/codelist/
    UNCL5305.xml) that AA is not a valid BT-118 code."""
    doc = etree.parse(str(_UNCL5305_FIXTURE))
    ns = {"cl": "urn:fdc:difi.no:2017:vefa:structure:CodeList-1"}
    ids = {el.text for el in doc.findall(".//cl:Code/cl:Id", ns)}
    assert "AA" not in ids
    assert ids == {"AE", "E", "S", "Z", "G", "O", "K", "L", "M", "B"}
    # And every code REPORTING_TYPE_TO_TAX_CATEGORY actually emits is in the
    # real list.
    used_codes = {cat.tax_category_id for cat in m.REPORTING_TYPE_TO_TAX_CATEGORY.values()}
    assert used_codes <= ids


def test_exempt_reason_code_left_none_for_generator_text_fallback() -> None:
    cat = m.resolve_tax_category("exempt")
    assert cat.tax_category_id == m.CAT_EXEMPT
    assert cat.exemption_reason_code is None


def test_zero_ic_goods_vs_zero_ic_services_distinct_categories() -> None:
    """Documented, flagged-as-verify cell (advisor review): goods -> K,
    services -> AE. Assert they are NOT the same category (a regression here
    would silently collapse the distinction this file's docstring argues for)."""
    goods = m.resolve_tax_category("zero_ic_goods")
    services = m.resolve_tax_category("zero_ic_services")
    assert goods.tax_category_id == m.CAT_INTRA_COMMUNITY
    assert services.tax_category_id == m.CAT_REVERSE_CHARGE
    assert goods.tax_category_id != services.tax_category_id
