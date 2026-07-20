"""EN 16931 / Peppol BIS Billing 3.0 e-invoice generator.

Reads one POSTED, EUR-denominated engine ``Invoice`` and its ``Company``
(seller) / ``Contact`` (buyer) and produces UBL Invoice XML bytes via
``serializer.build_einvoice_xml_document``. Mirrors
``lodgement/kmd_2027/generator.py``'s split: this module is the only one that
touches the database; ``mapping.py``/``serializer.py`` stay pure.

Data-shape gaps (read before calling) — this engine has no confirmed DB home
yet for several EN 16931-mandatory fields:

* **Seller VAT number (BT-31)** — no column exists anywhere on ``Company``.
  Always caller-supplied via ``SellerIdentity.vat_number``.
* **Seller registration number (BT-30, registrikood)** — no dedicated column
  either; falls back to ``Company.abn`` (the one registry-code-shaped field),
  the SAME convention ``lodgement/kmd_2027/generator.py``'s ``regcode``
  parameter already establishes for this engine. Prefer an explicit
  ``SellerIdentity.registration_number`` where one is known.
* **Seller/buyer street address** — ``Company.address`` is an unstructured
  JSONB blob with no fixed key schema (see ``saebooks/api/v1/invoices.py``'s
  own ``company_addr = (company.address or {})`` pass-through); this
  generator does not attempt to parse it. Supply ``SellerIdentity.street_name``
  /``city_name``/``postal_zone`` explicitly, or omit them (BT-35/BT-37 street/
  city are conditionally required by BR-08/BR-09 only when the fuller address
  is used at all — a country-only address is EN 16931-valid).
* **Buyer country (BT-55)** — ``Contact.country`` is free text (e.g.
  ``"Australia"``, the column default), not an ISO 3166-1 alpha-2 code.
  Resolved via a small explicit name->code table for the handful of
  jurisdictions this engine's seeds actually name; anything else needs
  ``buyer_country_code=`` supplied explicitly. Raises rather than guesses.
* **Buyer VAT number (BT-48)** — ``Contact`` has no VAT-number column, only
  ``registration_number`` (registrikood/business-registry code, reused
  cross-jurisdiction per that column's own docstring). Caller-supplied via
  ``generate_einvoice(..., buyer_vat_number=...)`` (critic round 4 fix,
  mirrors ``SellerIdentity.vat_number``'s own no-DB-column workaround);
  required whenever a line resolves to the Reverse-Charge (AE) or
  Intra-Community-supply (K) tax category — see ``generate_einvoice``'s own
  docstring.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.tax_code import TaxCode
from saebooks.money import money_quantum
from saebooks.services import business_identifiers
from saebooks.services.einvoice import mapping as m
from saebooks.services.einvoice.serializer import (
    EInvoiceDocument,
    EInvoiceLine,
    EInvoiceParty,
    EInvoiceTaxSubtotal,
    build_einvoice_xml_document,
    to_bytes,
)


class EInvoiceError(ValueError):
    """Base for every error this generator raises — never a silently wrong
    e-invoice. Mirrors ``KmdInfDataQualityError``'s "surfaced, never
    guessed" convention."""


class EInvoiceDataError(EInvoiceError):
    """Missing or unresolvable identity data (invoice/company/contact not
    found, no resolvable seller registration number, no resolvable buyer
    country code, ...)."""


class EInvoiceMappingError(EInvoiceError):
    """A line's ``TaxCode.reporting_type`` has no entry in
    ``mapping.REPORTING_TYPE_TO_TAX_CATEGORY`` — see that module's SCOPE
    section for why this is almost always a purchase-side tag on a sale
    line (a data-integrity bug upstream), not a mapping gap to silently
    paper over."""


class EInvoiceStatusError(EInvoiceError):
    """The invoice is not POSTED, or is not EUR-denominated."""


_TWO_PLACES = money_quantum(2)


def _q2(value: Decimal) -> Decimal:
    """Round-half-up to 2dp — same convention as
    ``services.invoices._q2``/``serializer._money_str``, used here to
    recompute a VAT-category subtotal's ``TaxAmount`` ONCE at the group
    level (critic round 4 finding: summing per-line amounts that were each
    already independently rounded at post time produces a BR-CO-17-wrong
    figure whenever multiple lines share a category and their individual
    roundings don't cancel)."""
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# Small, explicit country-name -> ISO 3166-1 alpha-2 lookup for the free-text
# ``Contact.country`` column. Deliberately NOT exhaustive — covers the
# jurisdictions this engine's own seeds name (AU/NZ/UK/EE + EE's near
# neighbours). Anything else needs ``buyer_country_code=`` supplied
# explicitly; raising loud beats guessing a country code on a legal document.
_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "estonia": "EE", "eesti": "EE",
    "australia": "AU",
    "new zealand": "NZ",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "finland": "FI", "soome": "FI",
    "latvia": "LV", "läti": "LV",
    "lithuania": "LT", "leedu": "LT",
    "germany": "DE", "saksamaa": "DE",
    "sweden": "SE", "rootsi": "SE",
}


def _resolve_country_code(raw: str | None, *, override: str | None, field_name: str) -> str:
    if override:
        return override.upper()
    if raw:
        candidate = raw.strip()
        if len(candidate) == 2 and candidate.isalpha():
            return candidate.upper()
        mapped = _COUNTRY_NAME_TO_ISO2.get(candidate.lower())
        if mapped:
            return mapped
    raise EInvoiceDataError(
        f"cannot resolve an ISO 3166-1 alpha-2 country code for {field_name} "
        f"(raw value: {raw!r}) — pass an explicit override"
    )


@dataclass(frozen=True, slots=True)
class SellerIdentity:
    """The seller-side fields this engine has no confirmed DB home for yet
    (see module docstring). Caller-supplied per generation call, mirroring
    ``lodgement/kmd_2027.generate_kmd_2027``'s own ``regcode: str | None``
    parameter convention."""

    registration_number: str | None = None  # falls back to Company.abn
    vat_number: str | None = None
    country_code: str = "EE"
    street_name: str | None = None
    city_name: str | None = None
    postal_zone: str | None = None
    peppol_scheme_id: str = m.EAS_EE_REGISTRIKOOD
    peppol_id: str | None = None  # falls back to the resolved registration_number


def _reason_text_for(cat: m.TaxCategoryMapping, tax_code: TaxCode | None) -> str | None:
    """BT-120 free-text exemption reason — only used where ``mapping.py``
    left ``exemption_reason_code`` deliberately ``None`` (today: only
    ``"exempt"``/category E, per that mapping's own documented ambiguity).
    Falls back to the company's own ``TaxCode.name`` — an EMTA-language
    description of the specific exemption — rather than a generic string."""
    if cat.exemption_reason_code is not None or cat.tax_category_id == m.CAT_STANDARD:
        return None
    return tax_code.name if tax_code is not None else None


def _line_unit_price(ln: InvoiceLine) -> Decimal:
    """UBL ``cac:Price/cbc:PriceAmount`` (BT-146, item net price) — EN 16931
    defines this as the price already net of any line-level discount, so
    that ``quantity x PriceAmount = LineExtensionAmount`` holds without a
    separate ``cac:AllowanceCharge`` (the invariant the committed Peppol
    BIS 3.0 base-example fixture demonstrates for its own, undiscounted,
    line). Derived from the already-posted, already-rounded
    ``InvoiceLine.line_subtotal`` rather than re-deriving
    ``unit_price * (1 - discount_pct/100)`` so it stays byte-identical to
    what ``services.invoices._compute_line_totals`` actually posted."""
    if ln.quantity:
        return ln.line_subtotal / ln.quantity
    return ln.unit_price


def _buyer_endpoint(contact: Contact, buyer_country: str, buyer_registration: str | None) -> tuple[str | None, str | None]:
    """Resolve the buyer's Peppol network routing address
    (``cbc:EndpointID`` scheme + value).

    Prefers ``Contact.peppol_participant_id`` — the dedicated routing-address
    column migration 0197 added specifically so ``buyer_requirement.py``
    could confirm one is on file — over guessing an EndpointID from the
    buyer's legal registration number. Falls back to the EE-registrikood
    convention only when no routing address has been captured yet, so an
    Estonian buyer with a bare registration number still gets a usable
    (if provisional) EndpointID; returns ``(None, None)`` when neither is
    available (no EndpointID is emitted — see ``EInvoiceParty``'s own
    docstring: actual network transmission, not XSD validity, is what
    needs one, and that's the operator adapter's concern)."""
    raw = (contact.peppol_participant_id or "").strip()
    if raw:
        scheme, sep, value = raw.partition(":")
        if sep and scheme and value:
            return scheme, value
        raise EInvoiceDataError(
            f"Contact.peppol_participant_id={raw!r} is not in the expected "
            "'scheme:value' wire form (see "
            "operator.PeppolParticipantId.as_string) — fix the buyer's "
            "on-file routing address rather than guess one"
        )
    if buyer_country == "EE" and buyer_registration:
        return m.EAS_EE_REGISTRIKOOD, buyer_registration
    return None, None


async def generate_einvoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    seller: SellerIdentity | None = None,
    buyer_country_code: str | None = None,
    buyer_vat_number: str | None = None,
) -> bytes:
    """Build EN 16931/Peppol BIS 3.0 UBL Invoice XML for a posted engine
    invoice. Raises an ``EInvoiceError`` subclass on anything that would
    otherwise produce a silently-wrong legal document; never guesses.

    ``buyer_vat_number`` — BT-48, caller-supplied (mirrors
    ``buyer_country_code``/``SellerIdentity.vat_number``'s own workaround for
    a field this engine's ``Contact`` model has no DB column for yet — see
    module docstring). Required when any line resolves to the Reverse-Charge
    (AE) or Intra-Community-supply (K) tax category — the buyer's own VAT
    self-accounting is what substantiates a zero-rated AE/K line, so
    generating one with no buyer VAT identifier on file would be exactly the
    kind of silently-wrong legal document this module exists to refuse
    (critic round 4 finding)."""
    seller = seller or SellerIdentity()

    inv = (
        await session.execute(
            select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice_id)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise EInvoiceDataError(f"invoice {invoice_id} not found")
    if inv.status != InvoiceStatus.POSTED:
        raise EInvoiceStatusError(
            f"invoice {invoice_id} is {inv.status.value}, not POSTED — an e-invoice "
            "represents a legal, issued document; generate it only after posting"
        )
    if inv.currency != "EUR":
        raise EInvoiceStatusError(
            f"invoice {invoice_id} is denominated in {inv.currency}, not EUR — "
            "EN 16931/Peppol e-invoicing under the Estonian Accounting Act applies "
            "to EUR-denominated invoices; a non-EUR invoice is out of scope here"
        )

    company = (
        await session.execute(select(Company).where(Company.id == inv.company_id))
    ).scalar_one_or_none()
    if company is None:
        raise EInvoiceDataError(f"company {inv.company_id} not found")

    contact = None
    if inv.contact_id is not None:
        contact = (
            await session.execute(select(Contact).where(Contact.id == inv.contact_id))
        ).scalar_one_or_none()
    if contact is None:
        raise EInvoiceDataError(
            f"invoice {invoice_id} has no resolvable Contact — e-invoicing needs a "
            "registered buyer identity; a one-off-customer invoice cannot be e-invoiced"
        )

    # Jurisdiction-aware primary registration number — an AU seller's ABN, an
    # EE seller's äriregistri kood, etc. — resolved from the company's
    # business identifiers (the overloaded ``companies.abn`` column was dropped
    # in 0198). Callers can still override with SellerIdentity.registration_number.
    seller_regcode = seller.registration_number or (
        await business_identifiers.primary_registry_identifier(session, company)
    )
    if not seller_regcode:
        raise EInvoiceDataError(
            "seller registration number not resolvable — pass "
            "SellerIdentity.registration_number, or record the company's "
            "primary registry identifier (e.g. its au_abn / ee_regcode "
            "business identifier) for its jurisdiction"
        )
    seller_peppol_id = seller.peppol_id or seller_regcode

    seller_party = EInvoiceParty(
        name=company.legal_name or company.name,
        country_code=seller.country_code.upper(),
        registration_number=seller_regcode,
        vat_number=seller.vat_number,
        street_name=seller.street_name,
        city_name=seller.city_name,
        postal_zone=seller.postal_zone,
        endpoint_scheme_id=seller.peppol_scheme_id,
        endpoint_id=seller_peppol_id,
    )

    buyer_country = _resolve_country_code(
        contact.country, override=buyer_country_code, field_name="buyer (Contact.country)"
    )
    buyer_registration = contact.registration_number
    buyer_endpoint_scheme_id, buyer_endpoint_id = _buyer_endpoint(contact, buyer_country, buyer_registration)
    buyer_party = EInvoiceParty(
        name=contact.name,
        country_code=buyer_country,
        registration_number=buyer_registration,
        vat_number=buyer_vat_number,
        street_name=contact.address_line1,
        city_name=contact.city,
        postal_zone=contact.postcode,
        endpoint_scheme_id=buyer_endpoint_scheme_id,
        endpoint_id=buyer_endpoint_id,
    )

    tax_code_ids = {ln.tax_code_id for ln in inv.lines if ln.tax_code_id is not None}
    tax_codes: dict[uuid.UUID, TaxCode] = {}
    if tax_code_ids:
        rows = (await session.execute(select(TaxCode).where(TaxCode.id.in_(tax_code_ids)))).scalars().all()
        tax_codes = {tc.id: tc for tc in rows}

    # Trade-in lines are excluded from the sale (services/invoices.py's own
    # _recalc convention — they post as a separate AP bill, not part of this
    # sale's header totals) so they are excluded here too, keeping
    # LegalMonetaryTotal consistent with the posted inv.subtotal/tax_total.
    sale_lines: list[InvoiceLine] = [ln for ln in inv.lines if not ln.is_trade_in]
    sale_lines.sort(key=lambda x: x.line_no)

    lines: list[EInvoiceLine] = []
    subtotal_groups: dict[tuple[str, str, str], EInvoiceTaxSubtotal] = {}
    # BR-AE-02/BR-IC-02-shaped requirement (critic round 4 finding): a
    # Reverse-Charge or Intra-Community-supply line needs the buyer
    # identified as the VAT-self-accounting party. Tracked across the loop
    # so the guard below fires once, after every line is seen, rather than
    # rejecting a multi-line invoice on its first qualifying line only.
    needs_buyer_vat_number = False
    # BR-S-02-shaped requirement (verifier finding, symmetric to the buyer
    # guard above): a Standard-rated (S) line requires at least one VAT
    # identifier on the document — the Seller VAT identifier (BT-31,
    # SellerIdentity.vat_number) or, failing that, the Buyer VAT identifier
    # (BT-48, buyer_vat_number). This generator emits neither a BT-32 seller
    # tax-registration id nor derives BT-31 from any DB column, so a
    # standard-rate invoice generated with a default SellerIdentity() would
    # otherwise silently carry zero VAT identification anywhere — exactly the
    # silently-wrong legal document this module refuses to produce.
    needs_seller_vat_id = False

    for i, ln in enumerate(sale_lines, start=1):
        tax_code = tax_codes.get(ln.tax_code_id) if ln.tax_code_id else None
        reporting_type = tax_code.reporting_type if tax_code is not None else "no_tax"
        try:
            cat = m.resolve_tax_category(reporting_type)
        except KeyError as exc:
            raise EInvoiceMappingError(
                f"invoice line {ln.line_no} ({ln.description!r}) has "
                f"TaxCode.reporting_type={reporting_type!r}, which has no "
                "EN16931/Peppol tax-category mapping — see mapping.py's SCOPE "
                "section (this is almost always a purchase-side reporting_type "
                "on a sale line, a data-integrity bug upstream)"
            ) from exc

        # cbc:Percent is required text on every zero/reduced-to-zero VAT
        # category except "O" (outside scope) — BR-E-2/BR-AE-2/BR-IC-2/
        # BR-G-2-shaped rules (critic round 3 finding: every committed
        # Peppol BIS 3.0 vat-category-{E,Z}.xml example carries an explicit
        # <cbc:Percent>0</cbc:Percent>; only vat-category-O.xml omits it).
        if cat.rate_carries_percent:
            percent = tax_code.rate if tax_code is not None else None
        elif cat.tax_category_id == m.CAT_OUTSIDE_SCOPE:
            percent = None
        else:
            percent = Decimal("0")
        reason_text = _reason_text_for(cat, tax_code)

        if cat.tax_category_id in (m.CAT_REVERSE_CHARGE, m.CAT_INTRA_COMMUNITY):
            needs_buyer_vat_number = True
        if cat.tax_category_id == m.CAT_STANDARD:
            needs_seller_vat_id = True

        lines.append(
            EInvoiceLine(
                line_id=str(i),
                description=ln.description,
                quantity=ln.quantity,
                unit_code=m.DEFAULT_UNIT_CODE,
                unit_price=_line_unit_price(ln),
                line_extension_amount=ln.line_subtotal,
                tax_category_id=cat.tax_category_id,
                tax_percent=percent,
                exemption_reason_code=cat.exemption_reason_code,
                exemption_reason_text=reason_text,
            )
        )

        # Group by (category, percent, exemption reason) — not just
        # (category, percent). Category "E" (exempt) alone spans several
        # unrelated Directive 2006/112/EC articles (mapping.py's own
        # docstring), so two exempt lines with different reasons must not
        # be merged into one cac:TaxSubtotal that then only carries the
        # first line's TaxExemptionReason text.
        key = (cat.tax_category_id, str(percent) if percent is not None else "", reason_text or "")
        existing = subtotal_groups.get(key)
        if existing is None:
            subtotal_groups[key] = EInvoiceTaxSubtotal(
                taxable_amount=ln.line_subtotal,
                tax_amount=ln.line_tax,
                tax_category_id=cat.tax_category_id,
                tax_percent=percent,
                exemption_reason_code=cat.exemption_reason_code,
                exemption_reason_text=reason_text,
            )
        else:
            subtotal_groups[key] = EInvoiceTaxSubtotal(
                taxable_amount=existing.taxable_amount + ln.line_subtotal,
                tax_amount=existing.tax_amount + ln.line_tax,
                tax_category_id=existing.tax_category_id,
                tax_percent=existing.tax_percent,
                exemption_reason_code=existing.exemption_reason_code,
                exemption_reason_text=existing.exemption_reason_text,
            )

    if needs_buyer_vat_number and not buyer_vat_number:
        raise EInvoiceDataError(
            f"invoice {invoice_id} has a Reverse-Charge (AE) or "
            "Intra-Community-supply (K) line, which EN 16931 requires a "
            "Buyer VAT identifier (BT-48) to substantiate — pass "
            "generate_einvoice(..., buyer_vat_number=...) rather than emit "
            "a reverse-charge/intra-community e-invoice with no buyer VAT "
            "identifier on it"
        )

    if needs_seller_vat_id and not (seller.vat_number or buyer_vat_number):
        raise EInvoiceDataError(
            f"invoice {invoice_id} has a Standard-rated (S) line, which "
            "EN 16931 (BR-S-02) requires a Seller VAT identifier (BT-31) to "
            "carry — pass SellerIdentity(vat_number=...) (or, where the buyer "
            "self-accounts, generate_einvoice(..., buyer_vat_number=...)) "
            "rather than emit a standard-rate e-invoice with no VAT "
            "identifier on it"
        )

    # BR-CO-17-style recompute (critic round 4 finding): each group's
    # TaxAmount is rounded ONCE here, from the group's own (already-summed)
    # taxable_amount x percent — not by summing the per-line ln.line_tax
    # figures above, each of which was independently rounded to 2dp at post
    # time and can drift from the category-level figure when several lines
    # share a category (their individual roundings don't cancel). Categories
    # with no percent (CAT_OUTSIDE_SCOPE) keep the accumulated line total,
    # which is always 0 for a genuinely out-of-scope line.
    tax_subtotals: list[EInvoiceTaxSubtotal] = []
    for sub in subtotal_groups.values():
        tax_amount = (
            _q2(sub.taxable_amount * sub.tax_percent / Decimal("100"))
            if sub.tax_percent is not None
            else sub.tax_amount
        )
        tax_subtotals.append(
            EInvoiceTaxSubtotal(
                taxable_amount=sub.taxable_amount,
                tax_amount=tax_amount,
                tax_category_id=sub.tax_category_id,
                tax_percent=sub.tax_percent,
                exemption_reason_code=sub.exemption_reason_code,
                exemption_reason_text=sub.exemption_reason_text,
            )
        )

    # Header TaxAmount (BT-110) is the sum of the just-recomputed subtotals
    # — not inv.tax_total — so BT-110 == sum(BT-117) holds (BR-CO-14); and
    # TaxInclusiveAmount is derived from THAT figure, not inv.total, so
    # TaxExclusive + Tax == TaxInclusive keeps holding too (BR-CO-15). This
    # e-invoice VAT total can therefore differ from the posted ledger's own
    # inv.tax_total by the same rounding residual the recompute above fixes
    # — an intentional, EN16931-correct divergence, not a bug.
    header_tax_amount = sum((s.tax_amount for s in tax_subtotals), Decimal("0"))
    tax_exclusive_amount = inv.subtotal
    tax_inclusive_amount = tax_exclusive_amount + header_tax_amount

    # PayableAmount (BT-115) nets out what's already been settled — a
    # partial payment or a posted credit note (both flow through
    # payments._refresh_invoice_amount_paid into inv.amount_paid; neither
    # touches inv.status, which stays POSTED) — per BR-CO-16: PayableAmount
    # = TaxInclusiveAmount - PrepaidAmount (critic round 4 finding: this
    # generator previously always emitted the full original total,
    # overstating a debt that had already been paid down or written off).
    prepaid_amount = inv.amount_paid if inv.amount_paid else None
    payable_amount = tax_inclusive_amount - (inv.amount_paid or Decimal("0"))

    doc = EInvoiceDocument(
        invoice_id=inv.number or str(inv.id),
        issue_date=inv.issue_date,
        due_date=inv.due_date,
        currency=inv.currency,
        seller=seller_party,
        buyer=buyer_party,
        lines=lines,
        tax_subtotals=tax_subtotals,
        line_extension_amount=inv.subtotal,
        tax_exclusive_amount=tax_exclusive_amount,
        tax_inclusive_amount=tax_inclusive_amount,
        tax_amount=header_tax_amount,
        payable_amount=payable_amount,
        prepaid_amount=prepaid_amount,
        notes=inv.notes,
    )
    root = build_einvoice_xml_document(doc)
    return to_bytes(root)
