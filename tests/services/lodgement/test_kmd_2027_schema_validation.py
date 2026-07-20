"""REAL full-XSD validation of the 2027 data-based KMD (XBRL GL, EE0203001)
serializer output — the strongest conformance asset for ``kmd_2027``.

Ported from the parallel ``feat/kmd3-2027`` build, whose critic loop fetched the
four generic xbrl.org base schemas the EMTA taxonomy imports by absolute URL and
committed a local catalog resolver (``_xbrl_gl_validation``). That closes a gap
the canonical build believed unclosable: ``kmd_2027/mapping.py`` documented the
test story as STRUCTURAL-only ("full XSD validation is NOT performed offline …
lxml raises XMLSchemaParseError on gl-plt-2026-03-31.xsd standalone"). It raises
that error only WITHOUT the base schemas; with them committed + catalog-resolved,
a generated instance validates against the real ``case-c-b-e`` taxonomy.

Pure — no DB. Inputs are hand-built ``Kmd2027Row``/``Kmd2027ReportingContext``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.kmd_2027 import mapping as m
from saebooks.services.lodgement.kmd_2027.serializer import (
    Kmd2027Listing,
    Kmd2027ReportingContext,
    Kmd2027Row,
    build_kmd_2027_xml_document,
)
from tests.services.lodgement._xbrl_gl_validation import (
    FIXTURES_DIR,
    load_gl_plt_schema,
    validate_against_xsd,
)

_SAMPLE_PATH = FIXTURES_DIR / "sample.xml"

_GL_LEAF_NAMESPACES = {
    m.NS_GL_COR,
    m.NS_GL_BUS,
    m.NS_GL_EXT,
}


def _ctx(**overrides) -> Kmd2027ReportingContext:
    base = dict(
        regcode="10001234",
        period_start=date(2027, 1, 1),
        period_end=date(2027, 1, 31),
        creator_name="Schema Validation Test OÜ",
        creation_datetime=datetime(2027, 2, 8, 9, 36, 51, 554000),
    )
    base.update(overrides)
    return Kmd2027ReportingContext(**base)


def _listing(rows: list[Kmd2027Row]) -> Kmd2027Listing:
    return Kmd2027Listing(
        regcode="10001234",
        period_start=date(2027, 1, 1),
        period_end=date(2027, 1, 31),
        rows=rows,
    )


def _representative_rows() -> list[Kmd2027Row]:
    """A row of each shape the serializer can emit: a standard domestic sale
    (partner + invoice_total + rate), an exempt sale (no rate, no partner), a
    deductible input-VAT row, an intra-Community supply with the country
    subaccount + KMKR partner, and a signed credit-note row."""
    return [
        Kmd2027Row(
            line_number=1, kmdtyyp_code="M_101", amount=Decimal("2400.00"),
            tax_rate=Decimal("0.24"), partner_code="11111111",
            partner_code_type=m.IDENT_DESC_REGCODE, identifier_category=m.IDENT_CAT_STANDARD,
            document_number="MA10001", document_date=date(2027, 1, 1),
            invoice_total=Decimal("2400.00"),
        ),
        Kmd2027Row(
            line_number=2, kmdtyyp_code="M_301", amount=Decimal("875.00"),
        ),
        Kmd2027Row(
            line_number=3, kmdtyyp_code="O_101", amount=Decimal("240.00"),
            tax_rate=Decimal("0.24"), partner_code="13000001",
            partner_code_type=m.IDENT_DESC_REGCODE, identifier_category=m.IDENT_CAT_STANDARD,
            document_number="OA27001", document_date=date(2027, 1, 5),
        ),
        Kmd2027Row(
            line_number=4, kmdtyyp_code="M_201", amount=Decimal("1200.00"),
            tax_rate=Decimal("0.00"), partner_code="FI08611111",
            partner_code_type=m.IDENT_DESC_VAT_NUMBER, identifier_category=m.IDENT_CAT_STANDARD,
            country_code="FI",
        ),
        Kmd2027Row(
            line_number=5, kmdtyyp_code="M_101", amount=Decimal("-1000.00"),
            tax_rate=Decimal("0.24"), partner_code="11111131",
            partner_code_type=m.IDENT_DESC_REGCODE, identifier_category=m.IDENT_CAT_STANDARD,
            document_number="MK27001", document_date=date(2027, 1, 31),
        ),
    ]


def test_generated_instance_validates_against_real_xsd() -> None:
    xml = build_kmd_2027_xml_document(_listing(_representative_rows()), _ctx())
    errors = validate_against_xsd(xml)
    assert errors == [], errors


def test_zero_row_period_validates_against_real_xsd() -> None:
    xml = build_kmd_2027_xml_document(_listing([]), _ctx())
    errors = validate_against_xsd(xml)
    assert errors == [], errors
    root = etree.fromstring(xml)
    ns = f"{{{m.NS_GL_COR}}}"
    assert root.findall(f".//{ns}entryDetail") == []


def test_document_apply_to_number_row_validates_and_carries_context_ref() -> None:
    """The ``documentApplyToNumber`` element (SAMPLE Example 2, prepayment
    invoice) is the exact spot EMTA's own sample is XSD-invalid (line 154,
    missing ``contextRef`` — see fixtures SOURCES.md). The canonical serializer
    originally reproduced that defect (``context=False``). This asserts our
    output for that element is XSD-VALID and carries ``contextRef="now"`` — the
    element the sample gets wrong."""
    row = Kmd2027Row(
        line_number=1, kmdtyyp_code="M_101", amount=Decimal("1500.00"),
        tax_rate=Decimal("0.24"), partner_code="11111111",
        partner_code_type=m.IDENT_DESC_REGCODE, identifier_category=m.IDENT_CAT_STANDARD,
        document_number="MA10002", document_apply_to_number="EA10001",
        document_date=date(2027, 1, 2),
    )
    xml = build_kmd_2027_xml_document(_listing([row]), _ctx())
    errors = validate_against_xsd(xml)
    assert errors == [], errors

    root = etree.fromstring(xml)
    apply_to = root.find(f".//{{{m.NS_GL_COR}}}documentApplyToNumber")
    assert apply_to is not None
    assert apply_to.get("contextRef") == m.CONTEXT_ID


def test_every_gl_leaf_element_carries_context_ref() -> None:
    """Regression guard for the ``documentApplyToNumber`` fix: EVERY leaf
    gl-cor/gl-bus/gl-ext data element must carry ``contextRef`` (the XSD
    requirement EMTA's sample violates on exactly one element)."""
    rows = _representative_rows() + [
        Kmd2027Row(
            line_number=6, kmdtyyp_code="M_101", amount=Decimal("500.00"),
            tax_rate=Decimal("0.24"), document_number="MA10003",
            document_apply_to_number="EA10002", document_date=date(2027, 1, 3),
        )
    ]
    root = etree.fromstring(build_kmd_2027_xml_document(_listing(rows), _ctx()))
    for el in root.iter():
        if not isinstance(el.tag, str) or len(el) > 0:
            continue  # containers only-carry no contextRef
        if etree.QName(el).namespace in _GL_LEAF_NAMESPACES:
            assert el.get("contextRef") == m.CONTEXT_ID, (
                f"{el.tag} missing contextRef — reproduces EMTA's line-154 defect"
            )


def test_official_sample_validates_except_documented_line154_defect() -> None:
    """The committed schema set is REAL: EMTA's own official sample validates
    against it with EXACTLY ONE error — the documented ``documentApplyToNumber``
    defect at line 154 (missing ``contextRef``). This both proves the harness
    validates against the genuine taxonomy AND empirically confirms the sample
    defect recorded in SOURCES.md (rather than trusting the note)."""
    schema = load_gl_plt_schema()
    doc = etree.parse(str(_SAMPLE_PATH))
    valid = schema.validate(doc)
    errors = list(schema.error_log)
    assert not valid, "expected the official sample to carry its known defect"
    assert len(errors) == 1, f"expected exactly one error, got: {[str(e) for e in errors]}"
    (only,) = errors
    assert only.line == 154, f"defect expected at line 154, got line {only.line}: {only.message}"
    assert "documentApplyToNumber" in only.message or "contextRef" in only.message, only.message
