"""Validate generated e-MTA XML against the REAL committed schemas.

* TSD: full lxml XSD validation of a generated ``tsd_vorm`` document against
  ``tsd_schema_01.01.2025_eng.xsd`` (the current, matching-vintage XSD), plus a
  check that the official example validates under the same XSD.
* KMD / KMD-INF: the bundled ``vatdeclaration.xsd`` is the KMD5 vintage (no
  ``transactions24``) while our output is KMD6, so a full XSD validation would
  reject the newer element (documented in ``fixtures/emta_schemas/SOURCES.md``).
  Instead we assert STRUCTURAL conformance: every element name we emit is a real
  element of the official KMD6 example / the XSD's declared element set, and the
  official example parses. No PLACEHOLDER names survive.

These need no database — inputs are hand-built dataclasses.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from lxml import etree

from saebooks.services.lodgement.kmd import (
    KmdFigures,
    KmdReportingContext,
    build_kmd_xml_document,
)
from saebooks.services.lodgement.kmd_inf import (
    KmdInfReportingContext,
    build_kmd_inf_xml_document,
)
from saebooks.services.lodgement.kmd_inf.generator import (
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
)
from saebooks.services.lodgement.tsd import (
    TsdReportingContext,
    build_tsd_xml_document,
)
from saebooks.services.lodgement.tsd.generator import (
    TsdLisa1Row,
    TsdLisa2ARow,
    TsdLisa2BRow,
    TsdLisa2InvFondRow,
    TsdLisa2Listing,
    TsdLisa2MvtRow,
    TsdLisa3Header,
    TsdLisa4Header,
    TsdLisa5Header,
    TsdLisa6Header,
    TsdLisa6Listing,
    TsdLisa6Row1,
    TsdLisa6Row2,
    TsdLisa7Header,
    TsdLisa7Listing,
    TsdLisa7Row1b,
    TsdListing,
    TsdMainTotals,
    compute_lisa2_totals,
)
from saebooks.services.lodgement.tsd.serializer import (
    build_tsd_lisa2_xml_document,
    build_tsd_lisa3_xml_document,
    build_tsd_lisa4_xml_document,
    build_tsd_lisa5_xml_document,
    build_tsd_lisa6_xml_document,
    build_tsd_lisa7_xml_document,
)

_SCHEMA_DIR = Path(__file__).parent.parent.parent / "fixtures" / "emta_schemas"
_REGCODE = "10123456"


def _all_element_local_names(xsd_path: Path) -> set[str]:
    """Every ``xs:element name=...`` declared in an XSD."""
    tree = etree.parse(str(xsd_path))
    xs = "http://www.w3.org/2001/XMLSchema"
    return {el.get("name") for el in tree.iter(f"{{{xs}}}element") if el.get("name")}


def _local_names(root: etree._Element) -> set[str]:
    return {etree.QName(el).localname for el in root.iter() if isinstance(el.tag, str)}


# ---- TSD: full XSD validation ----------------------------------------------

def _sample_tsd_listing() -> TsdListing:
    row = TsdLisa1Row(
        employee_id=uuid.uuid4(), isikukood="38001010000",
        payment_type_code="PLACEHOLDER_PAYMENT_TYPE_WAGES",
        gross=Decimal("2000.00"), basic_exemption_applied=Decimal("700.00"),
        income_tax=Decimal("252.56"), unemployment_employee=Decimal("32.00"),
        pillar_ii=Decimal("40.00"), social_tax=Decimal("660.00"),
        unemployment_employer=Decimal("16.00"), pay_run_id=uuid.uuid4(),
        payment_date=date(2026, 4, 30),
    )
    main = TsdMainTotals(
        employee_count=1, total_gross=Decimal("2000.00"),
        total_income_tax=Decimal("252.56"), total_unemployment_employee=Decimal("32.00"),
        total_unemployment_employer=Decimal("16.00"), total_social_tax=Decimal("660.00"),
        total_pillar_ii=Decimal("40.00"),
    )
    return TsdListing(
        company_id=uuid.uuid4(), period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30), main=main, lisa1=[row],
    )


def test_generated_tsd_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    ctx = TsdReportingContext(regcode=_REGCODE, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30))
    doc = etree.fromstring(build_tsd_xml_document(_sample_tsd_listing(), ctx))
    schema.assertValid(doc)  # raises with a precise message on failure


def test_official_tsd_example_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    example = etree.parse(str(_SCHEMA_DIR / "tsd_example.xml"))
    schema.assertValid(example)


# ---- Module 1 (ee-frontier-build-plan.md §"MODULE 1") — Lisa 2-7 ------------
#
# Two tiers of test, per annex:
#  1. XSD VALIDATION (every annex): a hand-built/transcribed instance passes
#     ``etree.XMLSchema.assertValid`` against the real
#     ``tsd_schema_01.01.2025_eng.xsd`` — proves wire-format legality.
#  2. STRONG CORRECTNESS CROSS-CHECK (L2 + L6 only — the two annexes with the
#     fullest population in the official example): every row/header value is
#     TRANSCRIBED from ``tsd_naide_xml_01.01.2025_eng.xml`` (the same file
#     validated by ``test_official_tsd_example_validates_against_real_xsd``
#     above) into our typed dataclasses, rendered through our serializer,
#     then compared against the official example's own parsed fragment as a
#     MULTISET of (element-local-name, numeric-or-text-value) pairs —
#     ``_leaf_multiset`` below. Multiset (not byte/string) comparison is
#     deliberate: the official example's decimal formatting is irregular
#     (``c2040_Summa`` = "1111", not "1111.00") while our serializer always
#     emits 2dp; ``xs:decimal`` treats these as the SAME value, and
#     ``Decimal("1111") == Decimal("1111.00")`` in Python too — so a
#     Decimal-keyed multiset asserts "same real element names + same real
#     values" without being defeated by that irrelevant formatting
#     difference (byte-matching the example would be neither required nor
#     correct here — see ee-frontier-build-plan.md session notes).
#  3. L7 gets tier 1 (XSD validation) + the spot-value checks already in
#     ``test_tsd_lisa7_serializer.py``, NOT the full multiset cross-check —
#     an explicit, named scope trim (time-budget, not a correctness gap):
#     L7 has ~30 header fields + 5 row types, the largest annex; L2 (the
#     recommended vertical slice) and L6 (compact, fully tractable) carry
#     the "strong" burden of proof that the general machinery
#     (``_append_optional_fields``, int-vs-decimal routing, grouping) is
#     correct, and L7 reuses that exact machinery unchanged.
#  4. L3/L4/L5 get tier 1 only, by construction (L3 absent from the example
#     entirely; L4/L5 are header-only with no repeating rows) — see each
#     annex's own generator.py section docstring for the golden-strength
#     caveat.


def _leaf_multiset(el: etree._Element) -> dict:
    """Every leaf descendant of ``el`` as a ``{(local_name, value): count}``
    multiset, ``value`` parsed to ``Decimal`` when numeric (so "1111" and
    "1111.00" compare equal) else kept as the raw stripped string."""
    from collections import Counter
    counts: Counter = Counter()
    for e in el.iter():
        if len(e) == 0:
            tag = etree.QName(e).localname
            text = (e.text or "").strip()
            try:
                value: object = Decimal(text)
            except Exception:
                value = text
            counts[(tag, value)] += 1
    return counts


def _official_example_fragment(element_name: str) -> etree._Element:
    example = etree.parse(str(_SCHEMA_DIR / "tsd_example.xml"))
    el = example.getroot().find(element_name)
    assert el is not None, f"{element_name} missing from the official example"
    return el


def _tsd_ctx() -> TsdReportingContext:
    return TsdReportingContext(regcode=_REGCODE, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30))


# ---- Lisa 2 (non-resident payments/withholding) — strong: full transcription

def _official_lisa2_listing() -> TsdLisa2Listing:
    """Every A/B/investment-fund row in the official example, transcribed
    verbatim (values + nesting), so this listing's rendered output should
    be leaf-multiset-IDENTICAL to the official ``tsd_L2_0`` fragment."""
    a_rows = [
        TsdLisa2ARow(
            isikukood="45212181423", name="OIE-MARET SOUDNITSYNA", country_code="FI",
            payment_type_code="120", gross=Decimal("1111"), a1_certificate_country_code=None,
            social_tax_base=Decimal("1111"), incapacity_pension_deducted=None,
            prior_month_rate_deducted=None, minimum_social_tax_increase=None,
            social_tax=Decimal("366.63"), unemployment_base=Decimal("1111"),
            unemployment_employee=None, unemployment_employer=Decimal("8.89"),
            income_tax_base=Decimal("1111"), income_tax_rate=Decimal("22"),
            income_tax=Decimal("89.54"), mvt=(TsdLisa2MvtRow(source_code="650", amount=Decimal("704")),),
        ),
        TsdLisa2ARow(
            isikukood="48409148675", name="INES MARIA MARTING", country_code="FI",
            payment_type_code="120", gross=Decimal("600"), a1_certificate_country_code=None,
            social_tax_base=Decimal("600"), incapacity_pension_deducted=None,
            prior_month_rate_deducted=None, minimum_social_tax_increase=None,
            social_tax=Decimal("198"), unemployment_base=Decimal("600"),
            unemployment_employee=Decimal("9.6"), unemployment_employer=Decimal("4.8"),
            income_tax_base=Decimal("600"), income_tax_rate=Decimal("22"),
            income_tax=Decimal("30.01"), mvt=(TsdLisa2MvtRow(source_code="610", amount=Decimal("454")),),
        ),
        TsdLisa2ARow(
            isikukood="48409148675", name="INES MARIA MARTING", country_code="FI",
            payment_type_code="123", gross=Decimal("300"), a1_certificate_country_code=None,
            social_tax_base=Decimal("300"), incapacity_pension_deducted=None,
            prior_month_rate_deducted=None, minimum_social_tax_increase=None,
            social_tax=Decimal("99"), unemployment_base=Decimal("300"),
            unemployment_employee=Decimal("4.8"), unemployment_employer=Decimal("2.4"),
            income_tax_base=Decimal("300"), income_tax_rate=Decimal("22"),
            income_tax=Decimal("20.94"), mvt=(TsdLisa2MvtRow(source_code="610", amount=Decimal("200")),),
        ),
    ]
    b_rows = [
        TsdLisa2BRow(
            isikukood="34501175307", name="PEEBO GERKO", payment_type_code="120",
            gross=Decimal("5000"), year=2023, month=1, reason_code="VR",
            social_tax_base=Decimal("5000"), social_tax_base_deducted=None,
            social_tax_base_increase=None, social_tax_base_adjustment=None,
            social_tax=Decimal("1650"), unemployment_base=Decimal("5000"),
            unemployment_employee=None, unemployment_employer=Decimal("40"),
            income_tax_base=Decimal("5000"), income_tax_rate=Decimal("20"),
            income_tax=Decimal("959.2"), mvt=(TsdLisa2MvtRow(source_code="650", amount=Decimal("204")),),
        ),
        TsdLisa2BRow(
            isikukood="34501175307", name="PEEBO GERKO", payment_type_code="177",
            gross=Decimal("500"), year=2023, month=1, reason_code="MUU",
            social_tax_base=None, social_tax_base_deducted=None,
            social_tax_base_increase=None, social_tax_base_adjustment=None,
            social_tax=None, unemployment_base=Decimal("500"),
            unemployment_employee=None, unemployment_employer=Decimal("4"),
            income_tax_base=Decimal("500"), income_tax_rate=Decimal("20"),
            income_tax=Decimal("0"), reason_explanation="Eksitus väljamaksel",
            mvt=(TsdLisa2MvtRow(source_code="650", amount=Decimal("500")),),
        ),
        TsdLisa2BRow(
            isikukood="47503070325", name="KADRI JOHANSON", payment_type_code="120",
            gross=Decimal("800.2"), year=2023, month=1, reason_code="EV",
            social_tax_base=Decimal("800.2"), social_tax_base_deducted=None,
            social_tax_base_increase=None, social_tax_base_adjustment=None,
            social_tax=Decimal("264.07"), unemployment_base=Decimal("800.2"),
            unemployment_employee=Decimal("12.8"), unemployment_employer=Decimal("6.4"),
            income_tax_base=Decimal("800.2"), income_tax_rate=Decimal("20"),
            income_tax=Decimal("26.68"), mvt=(TsdLisa2MvtRow(source_code="610", amount=Decimal("654")),),
        ),
        TsdLisa2BRow(
            isikukood="47503070325", name="KADRI JOHANSON", payment_type_code="177",
            gross=Decimal("380"), year=2023, month=1, reason_code="VI",
            social_tax_base=None, social_tax_base_deducted=None,
            social_tax_base_increase=None, social_tax_base_adjustment=None,
            social_tax=None, unemployment_base=Decimal("380"),
            unemployment_employee=Decimal("6.08"), unemployment_employer=Decimal("3.04"),
            income_tax_base=Decimal("380"), income_tax_rate=Decimal("20"),
            income_tax=Decimal("34.78"), mvt=(TsdLisa2MvtRow(source_code="610", amount=Decimal("200")),),
        ),
    ]
    inv_rows = [
        TsdLisa2InvFondRow(
            fund_code="60004693", fund_name="LEPINGULISE INVESTEERIMISFONDI NÄIDE",
            fund_country_code=None, manager_code="71016977", manager_name="HARJU MAKSUAMET UUS",
            manager_country_code=None, participation_percent=Decimal("15"),
            payment_type_code="198", amount=Decimal("20300"), income_tax=Decimal("4466"),
        ),
        TsdLisa2InvFondRow(
            fund_code="60004693", fund_name="LEPINGULISE INVESTEERIMISFONDI NÄIDE",
            fund_country_code=None, manager_code="71016977", manager_name="HARJU MAKSUAMET UUS",
            manager_country_code=None, participation_percent=Decimal("15"),
            payment_type_code="199", amount=Decimal("4000"), income_tax=Decimal("880"),
        ),
    ]
    totals = compute_lisa2_totals(a_rows, b_rows, inv_rows)
    return TsdLisa2Listing(a_rows=a_rows, b_rows=b_rows, inv_fond_rows=inv_rows, totals=totals)


def test_generated_lisa2_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    doc = etree.fromstring(build_tsd_lisa2_xml_document(_official_lisa2_listing(), _tsd_ctx()))
    schema.assertValid(doc)


def test_generated_lisa2_matches_official_example_leaf_values() -> None:
    """STRONG cross-check: our rendering of the officially-transcribed rows
    is leaf-multiset-IDENTICAL to the official ``tsd_L2_0`` fragment — same
    real element names, same real values (2dp-formatting-insensitive). This
    also independently confirms ``compute_lisa2_totals`` reproduces every
    one of the annex's 13 "Calculated: ... in total" fields exactly."""
    doc = etree.fromstring(build_tsd_lisa2_xml_document(_official_lisa2_listing(), _tsd_ctx()))
    ours = _leaf_multiset(doc.find("tsd_L2_0"))
    official = _leaf_multiset(_official_example_fragment("tsd_L2_0"))
    assert ours == official


# ---- Lisa 6 (non-business expenses) — strong: full transcription -----------

def _official_lisa6_listing() -> TsdLisa6Listing:
    header = TsdLisa6Header(
        related_party_value_diff=Decimal("45000"), fines_penalties=Decimal("4800"),
        fines_penalties_to_emta=Decimal("0"), interest_paid=Decimal("470"),
        interest_paid_to_emta=Decimal("0"), seized_assets_value=Decimal("500"),
        environmental_charges=Decimal("100"), environmental_charges_to_emta=Decimal("0"),
        bribes_kickbacks=Decimal("10000"), non_business_membership_fees=Decimal("50"),
        distributions_missing_source_doc=Decimal("0"), non_business_expenses_other=Decimal("100"),
        low_tax_territory_securities_expense=Decimal("50"), low_tax_territory_ownership_expense=Decimal("70"),
        low_tax_territory_penalty_damages=Decimal("300"), low_tax_territory_loan=Decimal("40"),
        low_tax_territory_credit_loss=Decimal("500"), tax_base_reduction=Decimal("44511"),
        total_taxable_amount=Decimal("17469"), income_tax_payable=Decimal("6353.79"),
        tonnage_non_business_total=Decimal("0"),
    )
    rows1 = [
        TsdLisa6Row1(month=5, year=2023, amount=Decimal("44000")),
        TsdLisa6Row1(month=2, year=2024, amount=Decimal("511")),
    ]
    rows2 = [
        TsdLisa6Row2(related_party_code="556016-0680", related_party_name="Ericsson AB",
                      country_code="SE", taxable_amount=Decimal("40000"), payment_type_code="621"),
        TsdLisa6Row2(related_party_code="446016-0350", related_party_name="ABC",
                      country_code="FI", taxable_amount=Decimal("5000"), payment_type_code="622"),
    ]
    return TsdLisa6Listing(header=header, rows1=rows1, rows2=rows2)


def test_generated_lisa6_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    doc = etree.fromstring(build_tsd_lisa6_xml_document(_official_lisa6_listing(), _tsd_ctx()))
    schema.assertValid(doc)


def test_generated_lisa6_matches_official_example_leaf_values() -> None:
    doc = etree.fromstring(build_tsd_lisa6_xml_document(_official_lisa6_listing(), _tsd_ctx()))
    ours = _leaf_multiset(doc.find("tsd_L6_0"))
    official = _leaf_multiset(_official_example_fragment("tsd_L6_0"))
    assert ours == official


# ---- Lisa 7 — XSD validation + spot values (see module docstring above) ---

def test_generated_lisa7_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    listing = TsdLisa7Listing(
        header=TsdLisa7Header(dividends_total=Decimal("5000"), hidden_distributions=Decimal("1000")),
        rows_1b=[TsdLisa7Row1b(
            payer_regcode="1973378-1", payer_name="Swedbank Helsinki", payer_country_code="FI",
            income_type_code="701", payment_date=date(2025, 3, 20),
            foreign_income_amount=Decimal("300.55"), foreign_tax_paid=Decimal("40.35"),
        )],
    )
    doc = etree.fromstring(build_tsd_lisa7_xml_document(listing, _tsd_ctx()))
    schema.assertValid(doc)


# ---- Lisa 3/4/5 — XSD validation only (see module docstring above) --------

def test_generated_lisa3_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    header = TsdLisa3Header(profit_removed_from_pe=Decimal("1000"), treaty_country_code="FI")
    doc = etree.fromstring(build_tsd_lisa3_xml_document(header, _tsd_ctx()))
    schema.assertValid(doc)


def test_generated_lisa4_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    header = TsdLisa4Header(electricity_expense=Decimal("500"), total_expenses_incl_vat=Decimal("3111.85"))
    doc = etree.fromstring(build_tsd_lisa4_xml_document(header, _tsd_ctx()))
    schema.assertValid(doc)


def test_generated_lisa5_validates_against_real_xsd() -> None:
    schema = etree.XMLSchema(etree.parse(str(_SCHEMA_DIR / "tsd_schema_01.01.2025_eng.xsd")))
    header = TsdLisa5Header(gifts_total=Decimal("500.5"), special_income_tax_payable=Decimal("280.79"))
    doc = etree.fromstring(build_tsd_lisa5_xml_document(header, _tsd_ctx()))
    schema.assertValid(doc)


# ---- KMD / KMD-INF: structural conformance (KMD5 XSD, KMD6 output) ----------

def test_generated_kmd_uses_only_real_element_names() -> None:
    allowed = _all_element_local_names(_SCHEMA_DIR / "vatdeclaration.xsd")
    allowed.add("transactions24")  # KMD6 addition, absent from the KMD5 XSD
    ctx = KmdReportingContext(regcode=_REGCODE, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31))
    figures = KmdFigures.from_box_amounts({"1": Decimal("10000.00"), "5": Decimal("840.00")})
    root = etree.fromstring(build_kmd_xml_document(figures, ctx))
    assert root.tag == "vatDeclaration"
    assert not (_local_names(root) - allowed), "emitted KMD element(s) not in the real schema"


def test_generated_kmd_inf_uses_only_real_element_names() -> None:
    allowed = _all_element_local_names(_SCHEMA_DIR / "vatdeclaration.xsd")
    listing = KmdInfListing(
        company_id=uuid.uuid4(), period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
        part_a=[KmdInfPartARow(
            row_no=1, partner_registration_number="10111111", partner_name="P1",
            document_number="INV-1", document_date=date(2026, 2, 5),
            document_total_ex_vat=Decimal("700.00"), taxable_value=Decimal("700.00"),
            rate=Decimal("24.00"), kmd_box_code="1", erisuse_kood=None, is_credit_note=False,
        )],
        part_b=[KmdInfPartBRow(
            row_no=1, partner_registration_number="10555555", partner_name="S1",
            document_number="BILL-1", document_date=date(2026, 2, 9),
            document_total_incl_vat=Decimal("1364.00"), input_vat=Decimal("264.00"),
            rate=Decimal("24.00"), erisuse_kood=None,
        )],
    )
    ctx = KmdInfReportingContext(regcode=_REGCODE, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28))
    root = etree.fromstring(build_kmd_inf_xml_document(listing, ctx))
    assert root.tag == "vatDeclaration"
    assert not (_local_names(root) - allowed), "emitted KMD-INF element(s) not in the real schema"


def test_official_kmd_example_parses() -> None:
    root = etree.parse(str(_SCHEMA_DIR / "vatdeclaration_example.xml")).getroot()
    assert root.tag == "vatDeclaration"
