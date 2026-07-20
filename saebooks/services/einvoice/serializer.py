"""EN 16931 / Peppol BIS Billing 3.0 UBL Invoice serializer.

Renders one posted (sale-side, ``InvoiceTypeCode`` 380) engine invoice as a
standalone UBL 2.1 ``Invoice`` document. Pure — no DB/session imports; the row
dataclasses below are this module's stable input contract, mirroring
``lodgement/kmd_2027/serializer.py``'s split (a DB-aware ``generator.py``
assembles these from the ledger and calls this builder).

Element ORDER matters — UBL's complex types are XSD ``sequence``s, not
``all``/``choice``; every ``_cac``/``_cbc`` call below follows the sequence
order read directly off ``tests/fixtures/ubl21/xsd/{maindoc,common}/*.xsd``
(``InvoiceType`` / ``PartyType`` / ``AddressType`` / ``PartyTaxSchemeType`` /
``PartyLegalEntityType`` / ``TaxTotalType`` / ``TaxSubtotalType`` /
``TaxCategoryType`` / ``MonetaryTotalType`` / ``InvoiceLineType`` /
``ItemType`` / ``PriceType``) — not guessed, not copied from prose. See
``mapping.py``'s module docstring for the full sourcing trail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

from saebooks.money import money_quantum
from saebooks.services.einvoice import mapping as m

_TWO_PLACES = money_quantum(2)
_FOUR_PLACES = Decimal("0.0001")


def _money_str(value: Decimal) -> str:
    """Fixed 2-decimal-place monetary text (EUR's minor unit) — always
    ``"1656.25"`` / ``"2800.00"``, never trailing-zero-stripped (contrast
    ``kmd_2027``'s XBRL-decimal style) — this is a plain UBL amount, not an
    XBRL fact, and EN 16931 BR-CO business rules operate on the numeric
    value, not the text form, so a fixed, unambiguous rendering is
    preferable for a document meant to be read by a human as well as
    machine-validated."""
    return format(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")


def _price_str(value: Decimal) -> str:
    """``cac:Price/cbc:PriceAmount`` (BT-146, item net price) text —
    UNLIKE every other monetary amount this is not fixed to 2 decimal
    places. ``generator._line_unit_price`` derives it as an unrounded
    quotient (``line_subtotal / quantity``), which is exact to 2dp only
    when quantity divides evenly; forcing it through ``_money_str`` would
    quantize away that precision and make quantity x printed-price drift
    from the printed LineExtensionAmount (critic round 3 finding). Every
    real Peppol BIS 3.0 example (base-example.xml's own "400"/"500") also
    renders Price trimmed, not zero-padded, so this quantizes to 4dp
    (matching InvoiceLine.quantity's own DB precision) and strips trailing
    zeros, same convention as ``_qty_str`` below. Note this narrows but
    cannot eliminate the drift for a quotient with no finite decimal
    representation (e.g. 100.00/7) — LineExtensionAmount (BT-131), not
    Price x Quantity, remains the document's authoritative line total."""
    q = value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _qty_str(value: Decimal) -> str:
    """Quantity text — strip trailing zeros beyond what's meaningful
    (``"10"`` not ``"10.0000"``, matching every Peppol BIS 3.0 example's own
    style), but keep up to 4 dp (``InvoiceLine.quantity``'s DB precision)."""
    q = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --------------------------------------------------------------------------- #
# Input contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EInvoiceParty:
    """One ``cac:Party`` — seller (``AccountingSupplierParty``) or buyer
    (``AccountingCustomerParty``). ``registration_number`` is BT-30/BT-47
    (legal registration identifier — Estonian registrikood); ``vat_number``
    is BT-31/BT-48 (VAT identifier, e.g. ``"EE101137025"``); either may be
    ``None`` (a non-Estonian counterparty may carry only one of the two).
    ``endpoint_id``/``endpoint_scheme_id`` address the Peppol network
    routing identifier (``cbc:EndpointID/@schemeID``) — when ``None`` no
    ``EndpointID`` element is emitted (a document can still be XSD-valid
    without one; only actual network transmission needs it, and that is the
    operator adapter's concern, not the serializer's)."""

    name: str
    country_code: str
    registration_number: str | None = None
    vat_number: str | None = None
    street_name: str | None = None
    city_name: str | None = None
    postal_zone: str | None = None
    endpoint_scheme_id: str | None = None
    endpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class EInvoiceLine:
    """One ``cac:InvoiceLine``. ``tax_category_id``/``tax_percent``/
    ``exemption_reason_*`` come from ``mapping.resolve_tax_category`` — the
    generator resolves them per-line before constructing this row; the
    serializer itself makes no VAT-category decision."""

    line_id: str
    description: str
    quantity: Decimal
    unit_code: str
    unit_price: Decimal
    line_extension_amount: Decimal
    tax_category_id: str
    tax_percent: Decimal | None
    exemption_reason_code: str | None = None
    exemption_reason_text: str | None = None


@dataclass(frozen=True, slots=True)
class EInvoiceTaxSubtotal:
    """One ``cac:TaxSubtotal`` — one row per distinct (category, percent)
    pair present on the invoice's lines (BR-CO-17/18/19-style grouping)."""

    taxable_amount: Decimal
    tax_amount: Decimal
    tax_category_id: str
    tax_percent: Decimal | None
    exemption_reason_code: str | None = None
    exemption_reason_text: str | None = None


@dataclass(frozen=True, slots=True)
class EInvoiceDocument:
    """The serializer's stable input contract — one posted sale-side
    invoice, fully resolved (no DB lookups happen past this point)."""

    invoice_id: str
    issue_date: date
    seller: EInvoiceParty
    buyer: EInvoiceParty
    lines: list[EInvoiceLine]
    tax_subtotals: list[EInvoiceTaxSubtotal]
    line_extension_amount: Decimal
    tax_exclusive_amount: Decimal
    tax_inclusive_amount: Decimal
    tax_amount: Decimal
    payable_amount: Decimal
    # BT-113/cbc:PrepaidAmount (critic round 4 finding) — amount already
    # settled against this invoice (payments and/or posted credit notes)
    # BEFORE the e-invoice was generated. None/0 means "nothing prepaid",
    # in which case no <cbc:PrepaidAmount> element is emitted at all (BR-CO-16
    # still holds: payable_amount already reflects the subtraction — see
    # generator.py). Only present so the emitted document mirrors what the
    # generator already computed; the serializer performs no arithmetic here.
    prepaid_amount: Decimal | None = None
    due_date: date | None = None
    currency: str = "EUR"
    buyer_reference: str | None = None
    notes: str | None = None


# --------------------------------------------------------------------------- #
# Element builders
# --------------------------------------------------------------------------- #


def _cbc(parent: etree._Element, local: str, text: str, **attrib: str) -> etree._Element:
    el = etree.SubElement(parent, f"{{{m.NS_CBC}}}{local}")
    for k, v in attrib.items():
        el.set(k, v)
    el.text = text
    return el


def _cac(parent: etree._Element, local: str) -> etree._Element:
    return etree.SubElement(parent, f"{{{m.NS_CAC}}}{local}")


def _amount(parent: etree._Element, local: str, value: Decimal, currency: str) -> etree._Element:
    return _cbc(parent, local, _money_str(value), currencyID=currency)


def _postal_address(party_el: etree._Element, party: EInvoiceParty) -> None:
    addr = _cac(party_el, "PostalAddress")
    # AddressType sequence: StreetName, ..., CityName, PostalZone, ..., cac:Country
    if party.street_name:
        _cbc(addr, "StreetName", party.street_name)
    if party.city_name:
        _cbc(addr, "CityName", party.city_name)
    if party.postal_zone:
        _cbc(addr, "PostalZone", party.postal_zone)
    country = _cac(addr, "Country")
    _cbc(country, "IdentificationCode", party.country_code)


def _party(parent: etree._Element, wrapper_local: str, party: EInvoiceParty) -> None:
    wrapper = _cac(parent, wrapper_local)
    p = _cac(wrapper, "Party")
    # PartyType sequence: EndpointID, PartyIdentification*, PartyName*,
    # ..., PostalAddress, ..., PartyTaxScheme*, PartyLegalEntity*, ...
    if party.endpoint_id:
        attrib = {"schemeID": party.endpoint_scheme_id} if party.endpoint_scheme_id else {}
        _cbc(p, "EndpointID", party.endpoint_id, **attrib)
    if party.registration_number:
        # No schemeID here: party.endpoint_scheme_id identifies the party's
        # PEPPOL NETWORK ROUTING address scheme (cbc:EndpointID above) — a
        # separate concept from what kind of number registration_number is.
        # Reusing it would mislabel the registrikood under an unrelated
        # scheme whenever the two differ (critic round 3 finding: e.g. a
        # buyer routed via EAS_EE_VAT "9931" would tag its EE registrikood
        # as if it were a VAT number under that scheme). base-example.xml's
        # own PartyIdentification/ID carries no schemeID at all — matched
        # here rather than guessing one.
        ident = _cac(p, "PartyIdentification")
        _cbc(ident, "ID", party.registration_number)
    name_el = _cac(p, "PartyName")
    _cbc(name_el, "Name", party.name)
    _postal_address(p, party)
    if party.vat_number:
        pts = _cac(p, "PartyTaxScheme")
        _cbc(pts, "CompanyID", party.vat_number)
        scheme = _cac(pts, "TaxScheme")
        _cbc(scheme, "ID", m.TAX_SCHEME_ID)
    legal = _cac(p, "PartyLegalEntity")
    _cbc(legal, "RegistrationName", party.name)
    if party.registration_number:
        _cbc(legal, "CompanyID", party.registration_number)


def _tax_category(parent_local_wrapper: etree._Element, wrapper_local: str,
                   category_id: str, percent: Decimal | None,
                   exemption_reason_code: str | None,
                   exemption_reason_text: str | None) -> None:
    cat_wrapper = _cac(parent_local_wrapper, wrapper_local)
    _cbc(cat_wrapper, "ID", category_id)
    if percent is not None:
        _cbc(cat_wrapper, "Percent", _money_str(percent))
    if exemption_reason_code:
        _cbc(cat_wrapper, "TaxExemptionReasonCode", exemption_reason_code)
    if exemption_reason_text:
        _cbc(cat_wrapper, "TaxExemptionReason", exemption_reason_text)
    scheme = _cac(cat_wrapper, "TaxScheme")
    _cbc(scheme, "ID", m.TAX_SCHEME_ID)


def _tax_total(root: etree._Element, doc: EInvoiceDocument) -> None:
    total = _cac(root, "TaxTotal")
    _amount(total, "TaxAmount", doc.tax_amount, doc.currency)
    for sub in doc.tax_subtotals:
        subtotal = _cac(total, "TaxSubtotal")
        _amount(subtotal, "TaxableAmount", sub.taxable_amount, doc.currency)
        _amount(subtotal, "TaxAmount", sub.tax_amount, doc.currency)
        _tax_category(
            subtotal, "TaxCategory", sub.tax_category_id, sub.tax_percent,
            sub.exemption_reason_code, sub.exemption_reason_text,
        )


def _legal_monetary_total(root: etree._Element, doc: EInvoiceDocument) -> None:
    total = _cac(root, "LegalMonetaryTotal")
    _amount(total, "LineExtensionAmount", doc.line_extension_amount, doc.currency)
    _amount(total, "TaxExclusiveAmount", doc.tax_exclusive_amount, doc.currency)
    _amount(total, "TaxInclusiveAmount", doc.tax_inclusive_amount, doc.currency)
    # MonetaryTotalType sequence: ...TaxInclusiveAmount, AllowanceTotalAmount,
    # ChargeTotalAmount, PrepaidAmount, PayableRoundingAmount, PayableAmount
    # (verified against UBL-CommonAggregateComponents-2.1.xsd) — this
    # generator has no allowance/charge/rounding scope (see
    # test_amounts_reconcile_br_co_style), so PrepaidAmount is the only
    # optional element between TaxInclusiveAmount and PayableAmount that can
    # appear here (critic round 4 finding: BR-CO-16 payable_amount already
    # nets prepaid out — see generator.py — this just surfaces the figure).
    if doc.prepaid_amount:
        _amount(total, "PrepaidAmount", doc.prepaid_amount, doc.currency)
    _amount(total, "PayableAmount", doc.payable_amount, doc.currency)


def _invoice_line(root: etree._Element, line: EInvoiceLine, currency: str) -> None:
    line_el = _cac(root, "InvoiceLine")
    _cbc(line_el, "ID", line.line_id)
    _cbc(line_el, "InvoicedQuantity", _qty_str(line.quantity), unitCode=line.unit_code)
    _amount(line_el, "LineExtensionAmount", line.line_extension_amount, currency)
    item = _cac(line_el, "Item")
    _cbc(item, "Description", line.description)
    _cbc(item, "Name", line.description)
    _tax_category(
        item, "ClassifiedTaxCategory", line.tax_category_id, line.tax_percent,
        line.exemption_reason_code, line.exemption_reason_text,
    )
    price = _cac(line_el, "Price")
    _cbc(price, "PriceAmount", _price_str(line.unit_price), currencyID=currency)


def build_einvoice_xml_document(doc: EInvoiceDocument) -> etree._Element:
    """Build the ``Invoice`` root element. Caller serializes with
    ``etree.tostring(root, xml_declaration=True, encoding="UTF-8",
    pretty_print=True)`` (mirrors every other serializer in this codebase —
    kept out of this function so tests can inspect the element tree
    directly, same convention as ``kmd_2027.serializer``)."""
    root = etree.Element(f"{{{m.NS_INVOICE}}}Invoice", nsmap=m.NSMAP)
    _cbc(root, "CustomizationID", m.CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", m.PROFILE_ID)
    _cbc(root, "ID", doc.invoice_id)
    _cbc(root, "IssueDate", doc.issue_date.isoformat())
    if doc.due_date is not None:
        _cbc(root, "DueDate", doc.due_date.isoformat())
    _cbc(root, "InvoiceTypeCode", m.INVOICE_TYPE_CODE)
    if doc.notes:
        _cbc(root, "Note", doc.notes)
    _cbc(root, "DocumentCurrencyCode", doc.currency)
    # PEPPOL-EN16931-R003: BuyerReference or OrderReference is required —
    # this generator carries no purchase-order model, so BuyerReference is
    # always emitted, defaulting to the invoice's own ID when the caller
    # supplies none (documented fallback, not a silent guess).
    _cbc(root, "BuyerReference", doc.buyer_reference or doc.invoice_id)

    _party(root, "AccountingSupplierParty", doc.seller)
    _party(root, "AccountingCustomerParty", doc.buyer)

    _tax_total(root, doc)
    _legal_monetary_total(root, doc)

    for line in doc.lines:
        _invoice_line(root, line, doc.currency)

    return root


def to_bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
