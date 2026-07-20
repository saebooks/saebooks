"""Pure serializer tests — no DB. Builds ``EInvoiceDocument`` dataclasses by
hand and validates the output against the REAL UBL 2.1 XSD
(``_ubl_validation.py``) plus structural comparison against the real
OpenPeppol BIS 3.0 example instances (``tests/fixtures/peppol_bis3/``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree

from saebooks.services.einvoice import mapping as m
from saebooks.services.einvoice.serializer import (
    EInvoiceDocument,
    EInvoiceLine,
    EInvoiceParty,
    EInvoiceTaxSubtotal,
    build_einvoice_xml_document,
    to_bytes,
)
from tests.services.einvoice._ubl_validation import validate_ubl_invoice

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "peppol_bis3" / "examples"


def _naidis_seller() -> EInvoiceParty:
    return EInvoiceParty(
        name="Näidis OÜ",
        country_code="EE",
        registration_number="10137025",
        vat_number="EE101370251",
        street_name="Näidise tn 1",
        city_name="Tallinn",
        postal_zone="10111",
        endpoint_scheme_id=m.EAS_EE_REGISTRIKOOD,
        endpoint_id="10137025",
    )


def _ostja_buyer() -> EInvoiceParty:
    return EInvoiceParty(
        name="Ostja AS",
        country_code="EE",
        registration_number="12345678",
        street_name="Ostja tee 2",
        city_name="Tartu",
        postal_zone="50001",
        endpoint_scheme_id=m.EAS_EE_REGISTRIKOOD,
        endpoint_id="12345678",
    )


def _standard_rate_document() -> EInvoiceDocument:
    line = EInvoiceLine(
        line_id="1", description="Konsultatsioon", quantity=Decimal("10"),
        unit_code=m.DEFAULT_UNIT_CODE, unit_price=Decimal("100.00"),
        line_extension_amount=Decimal("1000.00"), tax_category_id=m.CAT_STANDARD,
        tax_percent=Decimal("24"),
    )
    sub = EInvoiceTaxSubtotal(
        taxable_amount=Decimal("1000.00"), tax_amount=Decimal("240.00"),
        tax_category_id=m.CAT_STANDARD, tax_percent=Decimal("24"),
    )
    return EInvoiceDocument(
        invoice_id="INV-2026-0001", issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25),
        seller=_naidis_seller(), buyer=_ostja_buyer(), lines=[line], tax_subtotals=[sub],
        line_extension_amount=Decimal("1000.00"), tax_exclusive_amount=Decimal("1000.00"),
        tax_inclusive_amount=Decimal("1240.00"), tax_amount=Decimal("240.00"),
        payable_amount=Decimal("1240.00"),
    )


def test_standard_rate_document_is_xsd_valid() -> None:
    root = build_einvoice_xml_document(_standard_rate_document())
    validate_ubl_invoice(to_bytes(root))


def test_carries_load_bearing_bis3_magic_strings() -> None:
    """CustomizationID/ProfileID/InvoiceTypeCode — copied verbatim from
    base-example.xml (see mapping.py's own sourcing docstring). A wrong
    character here fails every real Peppol validator even though lxml's
    XSD check alone would not catch it (these are plain strings to the
    XSD)."""
    root = build_einvoice_xml_document(_standard_rate_document())
    ns = {"cbc": m.NS_CBC}
    assert root.findtext("cbc:CustomizationID", namespaces=ns) == m.CUSTOMIZATION_ID
    assert root.findtext("cbc:ProfileID", namespaces=ns) == m.PROFILE_ID
    assert root.findtext("cbc:InvoiceTypeCode", namespaces=ns) == "380"
    example = etree.parse(str(_EXAMPLES_DIR / "base-example.xml")).getroot()
    assert root.findtext("cbc:CustomizationID", namespaces=ns) == example.findtext(
        "cbc:CustomizationID", namespaces=ns
    )
    assert root.findtext("cbc:ProfileID", namespaces=ns) == example.findtext(
        "cbc:ProfileID", namespaces=ns
    )


def test_endpoint_id_uses_estonian_registrikood_eas_scheme() -> None:
    root = build_einvoice_xml_document(_standard_rate_document())
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}
    supplier_endpoint = root.find(
        "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", namespaces=ns
    )
    assert supplier_endpoint is not None
    assert supplier_endpoint.get("schemeID") == "0191"
    assert supplier_endpoint.text == "10137025"


def test_amounts_reconcile_br_co_style() -> None:
    """BR-CO-10/BR-CO-13/BR-CO-14/BR-CO-15-shaped cross-checks: sum of line
    LineExtensionAmount == LegalMonetaryTotal/LineExtensionAmount; sum of
    TaxSubtotal/TaxAmount == TaxTotal/TaxAmount; TaxExclusive+Tax ==
    TaxInclusive == PayableAmount (no allowances/charges/prepaid in this
    generator's scope)."""
    doc = _standard_rate_document()
    root = build_einvoice_xml_document(doc)
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}

    line_sum = sum(
        Decimal(el.text)
        for el in root.findall("cac:InvoiceLine/cbc:LineExtensionAmount", namespaces=ns)
    )
    header_line_extension = Decimal(
        root.findtext("cac:LegalMonetaryTotal/cbc:LineExtensionAmount", namespaces=ns)
    )
    assert line_sum == header_line_extension

    subtotal_sum = sum(
        Decimal(el.text)
        for el in root.findall("cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount", namespaces=ns)
    )
    header_tax = Decimal(root.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=ns))
    assert subtotal_sum == header_tax

    tax_exclusive = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=ns))
    tax_inclusive = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=ns))
    payable = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=ns))
    assert tax_exclusive + header_tax == tax_inclusive == payable


def _category_document(
    category_id: str, exemption_code: str | None, exemption_text: str | None,
    tax_percent: Decimal | None = Decimal("0"),
) -> EInvoiceDocument:
    """``tax_percent`` defaults to ``Decimal("0")`` — every zero/exempt
    category except "O" (outside scope) carries an explicit
    ``cbc:Percent>0`` in the real Peppol BIS 3.0 examples (critic round 3
    finding); callers testing "O" pass ``tax_percent=None`` explicitly."""
    line = EInvoiceLine(
        line_id="1", description="Zero/exempt-category test line", quantity=Decimal("1"),
        unit_code=m.DEFAULT_UNIT_CODE, unit_price=Decimal("500.00"),
        line_extension_amount=Decimal("500.00"), tax_category_id=category_id,
        tax_percent=tax_percent, exemption_reason_code=exemption_code, exemption_reason_text=exemption_text,
    )
    sub = EInvoiceTaxSubtotal(
        taxable_amount=Decimal("500.00"), tax_amount=Decimal("0.00"),
        tax_category_id=category_id, tax_percent=tax_percent,
        exemption_reason_code=exemption_code, exemption_reason_text=exemption_text,
    )
    return EInvoiceDocument(
        invoice_id="INV-2026-0002", issue_date=date(2026, 7, 11),
        seller=_naidis_seller(), buyer=_ostja_buyer(), lines=[line], tax_subtotals=[sub],
        line_extension_amount=Decimal("500.00"), tax_exclusive_amount=Decimal("500.00"),
        tax_inclusive_amount=Decimal("500.00"), tax_amount=Decimal("0.00"),
        payable_amount=Decimal("500.00"),
    )


def test_exempt_category_e_with_free_text_reason_is_xsd_valid() -> None:
    """Mirrors vat-category-E.xml's OWN choice in the O example (text-only,
    no code) — BT-120/BT-121 are alternatives, not both-required."""
    doc = _category_document(m.CAT_EXEMPT, None, "Maksuvaba käive (KMS §16)")
    root = build_einvoice_xml_document(doc)
    validate_ubl_invoice(to_bytes(root))
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}
    reason = root.find(
        "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:TaxExemptionReason", namespaces=ns
    )
    assert reason is not None and reason.text == "Maksuvaba käive (KMS §16)"


def test_reverse_charge_category_ae_with_vatex_code_is_xsd_valid() -> None:
    doc = _category_document(m.CAT_REVERSE_CHARGE, m.VATEX_REVERSE_CHARGE, None)
    root = build_einvoice_xml_document(doc)
    validate_ubl_invoice(to_bytes(root))
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}
    code = root.find(
        "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:TaxExemptionReasonCode", namespaces=ns
    )
    assert code is not None and code.text == "VATEX-EU-AE"


def test_intra_community_category_k_is_xsd_valid() -> None:
    doc = _category_document(m.CAT_INTRA_COMMUNITY, m.VATEX_INTRA_COMMUNITY_SUPPLY, None)
    root = build_einvoice_xml_document(doc)
    validate_ubl_invoice(to_bytes(root))


def test_export_category_g_is_xsd_valid() -> None:
    doc = _category_document(m.CAT_EXPORT, m.VATEX_EXPORT, None)
    root = build_einvoice_xml_document(doc)
    validate_ubl_invoice(to_bytes(root))


def test_outside_scope_category_o_is_xsd_valid() -> None:
    doc = _category_document(m.CAT_OUTSIDE_SCOPE, None, "Not subject to VAT", tax_percent=None)
    root = build_einvoice_xml_document(doc)
    validate_ubl_invoice(to_bytes(root))


@pytest.mark.parametrize(
    "category_id,exemption_code,exemption_text",
    [
        (m.CAT_EXEMPT, None, "Maksuvaba käive (KMS §16)"),
        (m.CAT_REVERSE_CHARGE, m.VATEX_REVERSE_CHARGE, None),
        (m.CAT_INTRA_COMMUNITY, m.VATEX_INTRA_COMMUNITY_SUPPLY, None),
        (m.CAT_EXPORT, m.VATEX_EXPORT, None),
    ],
)
def test_zero_categories_carry_explicit_percent_zero(
    category_id: str, exemption_code: str | None, exemption_text: str | None,
) -> None:
    """critic round 3 finding: every real Peppol BIS 3.0 vat-category-{E,Z}
    example carries an explicit ``<cbc:Percent>0</cbc:Percent>`` on E/AE/K/G
    — omitting it (as this generator previously did for every non-standard
    category) fails BR-E-2/BR-AE-2/BR-IC-2/BR-G-2-shaped Schematron rules
    even though it is XSD-valid either way."""
    doc = _category_document(category_id, exemption_code, exemption_text)
    root = build_einvoice_xml_document(doc)
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}
    percent = root.find(
        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", namespaces=ns
    )
    assert percent is not None and Decimal(percent.text) == Decimal("0")


def test_outside_scope_category_o_omits_percent() -> None:
    """"O" (outside scope) is the one category where the real Peppol BIS
    3.0 example (vat-category-O.xml) omits Percent entirely."""
    doc = _category_document(m.CAT_OUTSIDE_SCOPE, None, "Not subject to VAT", tax_percent=None)
    root = build_einvoice_xml_document(doc)
    ns = {"cac": m.NS_CAC, "cbc": m.NS_CBC}
    percent = root.find(
        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", namespaces=ns
    )
    assert percent is None


def test_all_real_peppol_examples_still_validate_against_committed_xsd() -> None:
    """Sanity check that the fixture pairing itself hasn't drifted: every
    committed OpenPeppol example must validate against the committed UBL
    2.1 XSD (proves the two fixture sets are mutually consistent, not just
    independently well-formed)."""
    for path in sorted(_EXAMPLES_DIR.glob("*.xml")):
        validate_ubl_invoice(path.read_bytes())
