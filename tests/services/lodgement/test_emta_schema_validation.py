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
    TsdListing,
    TsdMainTotals,
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
