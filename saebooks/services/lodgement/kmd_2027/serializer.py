"""2027 data-based KMD exporter — XBRL GL (COR+BUS+EXT) section EE0203001.

Renders a period's VAT-relevant transactions as one ``gl-cor:entryDetail`` per
transaction inside a single ``gl-cor:entryHeader`` (``entryNumber`` EE0203001),
following the official package sample ``XBRL_GL_sample_20260617.xml`` element by
element. A zero period ships the header with no detail rows (GUIDE p.25).

This module is PURE — it holds the row / listing / context dataclasses and the
builder, and imports NO database models, so the serializer + golden tests run
with no DB (the generator, which reads the ledger, imports these dataclasses).

Element names/namespaces/enum tokens are pinned in ``mapping.py``. Amounts and
rates are rendered in the sample's canonical XBRL-decimal style (no forced
trailing zeros: ``2400`` not ``2400.00``, ``362.9`` not ``362.90``), signed for
credit invoices / corrections (sample Examples 13/19/20).

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

from saebooks.services.lodgement.kmd_2027 import mapping as m

_TWO_PLACES = Decimal("0.01")


def _amount_str(value: Decimal) -> str:
    """Canonical XBRL-decimal for a monetary amount — quantize to the cent
    (ROUND_HALF_UP) then drop trailing zeros, matching the sample (``2400``,
    ``458.72``, ``362.9``, ``-1000``)."""
    q = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _rate_str(rate: Decimal) -> str:
    """VAT rate as the sample renders it — 2-decimal fraction (``0.24``,
    ``0.09``, ``0.00``). All EE statutory rates are whole percents, so 2 dp is
    exact; ``decimals="3"`` remains on the element per the taxonomy."""
    return format(rate.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True)
class Kmd2027Row:
    """One VAT-relevant transaction = one ``gl-cor:entryDetail``.

    ``amount`` is SIGNED (negative for a credit invoice / downward correction —
    sample Examples 13/19/20) and carries the taxable value for M_/S_ leaves,
    the input-VAT amount for O_ leaves (``kmdtyyp.KmdTyypLeaf.amount_basis``).
    ``line_number`` is the 1-based sequence within the header."""

    line_number: int
    kmdtyyp_code: str
    amount: Decimal
    tax_rate: Decimal | None = None
    partner_code: str | None = None
    partner_code_type: str | None = None      # mapping.IDENT_DESC_*
    identifier_category: str | None = None     # mapping.IDENT_CAT_*
    document_number: str | None = None
    document_apply_to_number: str | None = None
    document_date: date | None = None
    invoice_total: Decimal | None = None       # gl-bus measurable ARVE_KOGUSUMMA
    original_invoice_dates: tuple[date, ...] = ()  # credit-invoice ALGSE_ARVE_KP
    country_code: str | None = None            # RTK2T2013ap (intra-Community rows)
    country_role: str = m.COUNTRY_ROLE_BUYER   # RIIGIROLL2022ap parent role


@dataclass(frozen=True)
class Kmd2027ReportingContext:
    """Filer identity + period + document metadata for the XBRL GL envelope."""

    regcode: str
    period_start: date
    period_end: date
    creator_name: str
    creation_datetime: datetime | None = None  # None -> midnight of period_end
    unique_id: str | None = None               # None -> regcode + creation timestamp
    source_application: str = "SAE Books"
    language: str = m.DEFAULT_LANGUAGE
    entries_comment: str = m.DEFAULT_ENTRIES_COMMENT
    entry_version: str = m.DEFAULT_ENTRY_VERSION
    entry_source_id: str | None = None
    entry_source_count: int | None = None
    period_extra_identifier: str | None = None  # mapping.PERIOD_EXTRA_BANKRUPTCY
    org_description: str = m.ORG_DESC_REGCODE

    def resolved_creation_datetime(self) -> datetime:
        if self.creation_datetime is not None:
            return self.creation_datetime
        return datetime(self.period_end.year, self.period_end.month, self.period_end.day)

    def resolved_creation_date(self) -> date:
        return self.resolved_creation_datetime().date()

    def resolved_unique_id(self) -> str:
        if self.unique_id is not None:
            return self.unique_id
        # GUIDE §3.2: "reporting entity code+report date+time".
        stamp = self.resolved_creation_datetime().isoformat(timespec="milliseconds")
        return f"{self.regcode}-{stamp}"


@dataclass(frozen=True)
class Kmd2027DataQualityError:
    """A posted transaction the exporter could not classify — surfaced, never
    guessed (build-plan §4.5). Carries the engine tag + role that had no
    confident KMDTYYP leaf so the user can seed a finer tag or fix the coding."""

    document_number: str | None
    partner_name: str
    reporting_type: str
    role: str
    message: str


@dataclass(frozen=True)
class Kmd2027Listing:
    """The serializer's stable input contract — the transaction rows for a
    period, one step removed from the ledger (as ``KmdInfListing`` is)."""

    regcode: str
    period_start: date
    period_end: date
    rows: list[Kmd2027Row] = field(default_factory=list)
    errors: list[Kmd2027DataQualityError] = field(default_factory=list)


def _fact(
    parent: etree._Element,
    tag: str,
    text: str | None,
    *,
    context: bool = True,
    unit: str | None = None,
    decimals: str | None = None,
) -> etree._Element:
    """Create a gl-* fact element: contextRef="now" (unless suppressed),
    optional unitRef/decimals, and text."""
    el = etree.SubElement(parent, tag)
    if context:
        el.set(m.ATTR_CONTEXT_REF, m.CONTEXT_ID)
    # Attribute order matches the sample (decimals before unitRef) so our output
    # reads identically to the official instance in a side-by-side diff.
    if decimals is not None:
        el.set(m.ATTR_DECIMALS, decimals)
    if unit is not None:
        el.set(m.ATTR_UNIT_REF, unit)
    if text is not None:
        el.text = text
    return el


def _context_element(root: etree._Element, ctx: Kmd2027ReportingContext) -> None:
    context = etree.SubElement(root, m.EL_CONTEXT, {m.ATTR_ID: m.CONTEXT_ID})
    entity = etree.SubElement(context, m.EL_ENTITY)
    ident = etree.SubElement(entity, m.EL_IDENTIFIER, {m.ATTR_SCHEME: m.ORG_DESC_REGCODE})
    ident.text = ctx.regcode
    period = etree.SubElement(context, m.EL_PERIOD)
    etree.SubElement(period, m.EL_INSTANT).text = ctx.resolved_creation_date().isoformat()


def _units(root: etree._Element) -> None:
    for unit_id, measure in (
        (m.UNIT_EUR, m.MEASURE_EUR),
        (m.UNIT_PURE, m.MEASURE_PURE),
        (m.UNIT_NOT_USED, m.MEASURE_PURE),
    ):
        unit = etree.SubElement(root, m.EL_UNIT, {m.ATTR_ID: unit_id})
        etree.SubElement(unit, m.EL_MEASURE).text = measure


def _document_info(parent: etree._Element, ctx: Kmd2027ReportingContext) -> None:
    info = etree.SubElement(parent, m.EL_DOCUMENT_INFO)
    _fact(info, m.EL_ENTRIES_TYPE, m.ENTRIES_TYPE)
    _fact(info, m.EL_UNIQUE_ID, ctx.resolved_unique_id())
    _fact(info, m.EL_LANGUAGE, ctx.language)
    _fact(info, m.EL_CREATION_DATE, ctx.resolved_creation_date().isoformat())
    _fact(info, m.EL_BUS_CREATOR, ctx.creator_name)
    _fact(info, m.EL_ENTRIES_COMMENT, ctx.entries_comment)
    _fact(info, m.EL_PERIOD_COVERED_START, ctx.period_start.isoformat())
    _fact(info, m.EL_PERIOD_COVERED_END, ctx.period_end.isoformat())
    if ctx.period_extra_identifier:
        _fact(info, m.EL_EXT_PERIOD_EXTRA_ID, ctx.period_extra_identifier)
    _fact(info, m.EL_BUS_SOURCE_APPLICATION, ctx.source_application)


def _entity_information(parent: etree._Element, ctx: Kmd2027ReportingContext) -> None:
    ent = etree.SubElement(parent, m.EL_ENTITY_INFORMATION)
    ids = etree.SubElement(ent, m.EL_BUS_ORG_IDENTIFIERS)
    _fact(ids, m.EL_BUS_ORG_IDENTIFIER, ctx.regcode)
    _fact(ids, m.EL_BUS_ORG_DESCRIPTION, ctx.org_description)


def _account_block(detail: etree._Element, row: Kmd2027Row) -> None:
    account = etree.SubElement(detail, m.EL_ACCOUNT)
    sub = etree.SubElement(account, m.EL_ACCOUNT_SUB)
    _fact(sub, m.EL_ACCOUNT_SUB_ID, row.kmdtyyp_code)
    _fact(sub, m.EL_ACCOUNT_SUB_TYPE, m.ACCOUNT_SUB_TYPE_KMDTYYP)
    if row.country_code:
        # Intra-Community rows carry a second accountSub: the partner country
        # (RTK2T2013ap) under a RIIGIROLL2022ap parent role (SAMPLE Example 8).
        csub = etree.SubElement(account, m.EL_ACCOUNT_SUB)
        _fact(csub, m.EL_ACCOUNT_SUB_ID, row.country_code)
        _fact(csub, m.EL_ACCOUNT_SUB_TYPE, m.COUNTRY_CLASSIFIER)
        tup = etree.SubElement(csub, m.EL_SEGMENT_PARENT_TUPLE)
        _fact(tup, m.EL_PARENT_SUBACCOUNT_CODE, row.country_role)
        _fact(tup, m.EL_PARENT_SUBACCOUNT_TYPE, m.COUNTRY_ROLE_CLASSIFIER)


def _identifier_reference(detail: etree._Element, row: Kmd2027Row) -> None:
    if not (row.partner_code or row.identifier_category):
        return
    ref = etree.SubElement(detail, m.EL_IDENTIFIER_REFERENCE)
    if row.partner_code:
        _fact(ref, m.EL_IDENTIFIER_CODE, row.partner_code)
        if row.partner_code_type:
            _fact(ref, m.EL_IDENTIFIER_DESCRIPTION, row.partner_code_type)
    if row.identifier_category:
        _fact(ref, m.EL_IDENTIFIER_CATEGORY, row.identifier_category)


def _measurable_blocks(detail: etree._Element, row: Kmd2027Row) -> None:
    if row.invoice_total is not None:
        meas = etree.SubElement(detail, m.EL_BUS_MEASURABLE)
        _fact(meas, m.EL_BUS_MEASURABLE_ID, m.MEASURABLE_INVOICE_TOTAL)
        _fact(meas, m.EL_BUS_MEASURABLE_ID_SCHEMA, m.MEASURABLE_QUANTITY_SCHEMA)
        _fact(
            meas, m.EL_BUS_MEASURABLE_QUANTITY, _amount_str(row.invoice_total),
            unit=m.UNIT_EUR, decimals=m.DECIMALS_AMOUNT,
        )
    for orig in row.original_invoice_dates:
        meas = etree.SubElement(detail, m.EL_BUS_MEASURABLE)
        _fact(meas, m.EL_BUS_MEASURABLE_ID, m.MEASURABLE_ORIGINAL_INVOICE_DATE)
        _fact(meas, m.EL_BUS_MEASURABLE_ID_SCHEMA, m.MEASURABLE_EVENT_SCHEMA)
        _fact(meas, m.EL_BUS_MEASURABLE_START_DATETIME, orig.isoformat())


def _entry_detail(header: etree._Element, row: Kmd2027Row) -> None:
    detail = etree.SubElement(header, m.EL_ENTRY_DETAIL)
    _fact(
        detail, m.EL_LINE_NUMBER_COUNTER, str(row.line_number),
        unit=m.UNIT_NOT_USED,
    )
    _account_block(detail, row)
    _fact(detail, m.EL_AMOUNT, _amount_str(row.amount), unit=m.UNIT_EUR, decimals=m.DECIMALS_AMOUNT)
    _identifier_reference(detail, row)
    if row.document_number:
        _fact(detail, m.EL_DOCUMENT_NUMBER, row.document_number)
    if row.document_apply_to_number:
        # EMTA's own sample (Example 2, line 154) emits documentApplyToNumber
        # with NO contextRef — but the taxonomy's documentApplyToNumberComplexType
        # REQUIRES contextRef (SCHEMAV_CVC_COMPLEX_TYPE_4 under full XSD
        # validation; the sample is the ONLY line that fails — see
        # tests/fixtures/xbrl_gl_ee_2027/SOURCES.md). The XSD wins over the
        # defective sample: carry contextRef="now" like every other data element.
        _fact(detail, m.EL_DOCUMENT_APPLY_TO_NUMBER, row.document_apply_to_number)
    if row.document_date:
        _fact(detail, m.EL_DOCUMENT_DATE, row.document_date.isoformat())
    _measurable_blocks(detail, row)
    if row.tax_rate is not None:
        taxes = etree.SubElement(detail, m.EL_TAXES)
        _fact(
            taxes, m.EL_TAX_PERCENTAGE_RATE, _rate_str(row.tax_rate),
            unit=m.UNIT_PURE, decimals=m.DECIMALS_RATE,
        )


def build_kmd_2027_xml_document(
    listing: Kmd2027Listing, ctx: Kmd2027ReportingContext
) -> bytes:
    """Render the period's transactions as an XBRL GL EE0203001 instance.

    ``listing.rows`` order is preserved (the generator assigns
    ``line_number``); a zero-row listing emits the header only (GUIDE p.25)."""
    root = etree.Element(m.EL_XBRL, nsmap=m.NSMAP)
    root.set(m.ATTR_SCHEMA_LOCATION, m.SCHEMA_LOCATION)
    schema_ref = etree.SubElement(root, m.EL_SCHEMA_REF)
    schema_ref.set(m.ATTR_XLINK_HREF, m.SCHEMA_LOCATION_HREF)
    schema_ref.set(m.ATTR_XLINK_ARCROLE, m.SCHEMA_REF_ARCROLE)
    schema_ref.set(m.ATTR_XLINK_TYPE, m.SCHEMA_REF_TYPE)

    _context_element(root, ctx)
    _units(root)

    entries = etree.SubElement(root, m.EL_ACCOUNTING_ENTRIES)
    _document_info(entries, ctx)
    _entity_information(entries, ctx)
    header = etree.SubElement(entries, m.EL_ENTRY_HEADER)
    _fact(header, m.EL_ENTRY_NUMBER, m.ENTRY_NUMBER)
    _fact(header, m.EL_EXT_ENTRY_VERSION, ctx.entry_version)
    if ctx.entry_source_id is not None:
        _fact(header, m.EL_EXT_ENTRY_SOURCE_ID, ctx.entry_source_id)
    if ctx.entry_source_count is not None:
        _fact(header, m.EL_EXT_ENTRY_SOURCE_COUNT, str(ctx.entry_source_count))
    for row in listing.rows:
        _entry_detail(header, row)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
