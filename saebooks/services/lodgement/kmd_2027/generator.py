"""2027 data-based KMD transaction-listing generator.

The KMD-INF invoice-listing data path is the SPINE of the transaction exporter
(andmepohine read §3: "that generator with the threshold removed and a richer
code set"). This module reuses ``kmd_inf.generator``'s document loading +
per-``reporting_type`` line grouping verbatim, then diverges in two ways the
2027 regime requires:

1. **€1,000 threshold OFF.** KMD-INF lists only partners crossing €1,000; the
   data-based KMD reports EVERY VAT-relevant transaction (KMDTYYP M_101 covers
   supply "including if the invoice per transaction partner is less than
   €1,000"). No partner-grouping / crossing test here.
2. **KMDTYYP2026ap code ON per row.** Each row carries a ~50-leaf
   ``KMDTYYP2026ap`` code resolved from ``(reporting_type, role)`` via
   ``kmdtyyp.resolve_kmdtyyp`` — NEVER guessed. A transaction whose engine tag
   has no confident leaf is surfaced as a ``Kmd2027DataQualityError``, not
   dropped and not coded to a wrong leaf.

A reverse-charge EU acquisition emits TWO rows (mirroring the box engine's
two-component fan-out): an S_* acquisition row (self-assessed taxable value) and
an O_4xx input-VAT row — exactly the sample's Example 14 (S_101) + Example 17
(O_401) pairing. Credit notes are SIGNED rows on the base leaf (sample
Examples 13/19), inheriting KMD-INF's signing.

Output is a ``Kmd2027Listing`` (pure serializer input) — one step removed from
the ledger, the same relationship ``KmdInfListing`` has to its serializer.
DB-bound: tests are ``postgres_only``.

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law).

Design decisions where the source data is coarser than the taxonomy (flagged,
not papered over):

* **Identifier category defaults to 100 (full partner code), or 200 for a
  no-registry-code partner (natural person).** The sample's category 103
  (< €1,000 per partner → partner code MAY be omitted) is an OPTIONAL omission,
  not a requirement — transmitting the real code is always valid — so the
  per-partner-period €1,000 test that would let us switch to 103 is deferred; we
  always transmit the code when we have one.
* **Reverse-charge input VAT is computed as taxable_value × rate** (the
  self-assessed figure), because a reverse-charge purchase invoice carries no
  VAT on its face (``BillLine.line_tax`` is 0) — the VAT is self-assessed. Only
  the two EU-acquisition tags the box engine actually fans out
  (``rc_eu_acq_goods``/``rc_eu_acq_services``) get an O_ input row; the
  single-component domestic-RC / exempt-IC tags emit the S_ base row only (their
  input deduction, where it exists, rides an ordinary ``standard`` input line →
  O_101, exactly as in the box world).
* **Optional dimensions left unpopulated (serializer supports them; golden
  exercises them):** the intra-Community partner-country ``accountSub``
  (RTK2T2013ap) needs a contact country field; the credit-invoice
  ``ALGSE_ARVE_KP`` original-invoice date needs the CreditNote→Invoice link; the
  bill-side ARVE_KOGUSUMMA total needs a confirmed bill ex-VAT column. Each is
  an optional block per the guide, so omission is valid, and each is named here
  rather than guessed.
* **Prepayment ``documentApplyToNumber`` (sample Example 2) is derived, not
  invented — a field-swap on the invoice's OWN row, not a second row.** A
  prepayment is represented with the tools the engine already has: an
  ``INCOMING`` ``Payment`` (POSTED) allocated to this invoice
  (``PaymentAllocation.invoice_id``) whose ``payment_date`` precedes the
  invoice's own ``issue_date`` — cash received before the invoice existed. That
  can only happen via a deliberate backdated ``payment_date``, since
  ``payments.allocate`` requires the invoice already POSTED (no earlier hook to
  attach to) — so the trigger cannot misfire on ordinary late-paid invoices,
  whose payments postdate the invoice. A SEPARATE payment-derived row was
  rejected: the invoice's own M_101/S_* row already reports this taxable value,
  so a second row would double the turnover. Instead, on a qualifying invoice,
  ``document_number`` moves to ``document_apply_to_number`` — the row still
  reports the SAME (code, amount, rate); only which document element carries
  the invoice's number changes, exactly as EMTA's Example 2 shows (no
  ``documentNumber`` at all, just ``documentApplyToNumber``).
* **Purchase-side credit notes (sample Example 19) mirror the sale side's
  signing, not a new leaf.** ``SupplierCreditNote`` (purchase-side mirror of
  ``CreditNote``) posts through the SAME ``(reporting_type, role)`` resolution
  as an ordinary bill, at ``sign=-1`` — matching ``kmdtyyp_mapping.yaml``'s own
  ``O_601`` comment: "ordinary credit adjustments ride the base input leaf
  (O_101) as a SIGNED row ... not a functional gap for the common case."
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.payment import Payment, PaymentAllocation, PaymentDirection, PaymentStatus
from saebooks.models.supplier_credit_note import SupplierCreditNote, SupplierCreditNoteStatus
from saebooks.services import business_identifiers
from saebooks.services.lodgement.kmd_2027 import kmdtyyp
from saebooks.services.lodgement.kmd_2027 import mapping as m
from saebooks.services.lodgement.kmd_2027.serializer import (
    Kmd2027DataQualityError,
    Kmd2027Listing,
    Kmd2027Row,
)
from saebooks.services.lodgement.kmd_inf.generator import (
    _group_lines_by_reporting_type,
    _load_contacts,
    _load_tax_codes,
    _partner_from_contact,
)

_ZERO = Decimal("0")
_TWO_PLACES = Decimal("0.01")
_HUNDRED = Decimal("100")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


# Reverse-charge ACQUISITION tags → an S_* self-assessed base row.
_RC_ACQUISITION_TAGS = frozenset({
    "rc_eu_acq_goods", "rc_eu_acq_services", "rc_domestic_acq",
    "ic_acq_exempt", "ee_acq_foreign",
})
# …of which only these two are fanned out by the box engine into an input-VAT
# component (EETaxEngine), so only these also emit an O_4xx row.
_RC_FANOUT_INPUT_TAGS = frozenset({"rc_eu_acq_goods", "rc_eu_acq_services"})
# Ordinary deductible-input tags → an O_* input-VAT row (box-5 feeders minus RC).
_ORDINARY_INPUT_TAGS = frozenset({
    "standard", "standard_legacy_20", "standard_legacy_22",
    "reduced_13", "reduced_9", "capital", "input_import",
})
# Sale-side tags whose partner code is an intra-EU VAT number, not a reg code.
_IC_TAGS = frozenset({
    "zero_ic_goods", "zero_ic_services",
    "rc_eu_acq_goods", "rc_eu_acq_services", "ic_acq_exempt",
})
_NO_RATE_TAGS = frozenset({"exempt"})


def _rate_fraction(rate_percent: Decimal) -> Decimal:
    """Percent (24.00) → fraction (0.24) for gl-cor:taxPercentageRate."""
    return (rate_percent / _HUNDRED)


def _partner_fields(reg_no: str | None, reporting_type: str) -> tuple[str | None, str | None, str]:
    """(partner_code, partner_code_type, identifier_category)."""
    if reg_no:
        code_type = m.IDENT_DESC_VAT_NUMBER if reporting_type in _IC_TAGS else m.IDENT_DESC_REGCODE
        return reg_no, code_type, m.IDENT_CAT_STANDARD
    # No registry code on file → treated as a natural person (category 200).
    return None, None, m.IDENT_CAT_NATURAL_PERSON


async def generate_kmd_2027(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
    regcode: str | None = None,
) -> Kmd2027Listing:
    """Assemble the EE0203001 transaction rows for one period.

    Period basis mirrors the KMD box engine / KMD-INF: invoices use
    ``settlement_date`` else ``issue_date``; bills and credit notes post at
    ``issue_date``.

    ``regcode`` — the filer's Estonian äriregistri kood for the listing. The
    ``Company`` model has NO jurisdiction-neutral registry-code column today
    (only the AU-shaped ``abn`` — the global-reference-audit AU-noun gap), so
    when ``regcode`` is not supplied it falls back to ``Company.abn`` (the one
    registry-code-shaped field) or "". The authoritative regcode for the filed
    envelope is set on the serializer's ``Kmd2027ReportingContext``; the value
    here only stamps the listing.
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")
    if company.base_currency != "EUR":
        raise ValueError(
            f"Company {company_id} has base_currency={company.base_currency!r}, not "
            "'EUR' — every KMDTYYP amount is a base-currency EUR figure. Set "
            "Company.base_currency='EUR' before generating the data-based KMD."
        )

    # The Estonian registry code (äriregistri kood), read explicitly from its
    # own ``ee_regcode`` business identifier when the caller did not pass one.
    # The legacy overloaded ``companies.abn`` column was dropped in 0198.
    if regcode:
        resolved_regcode = regcode
    else:
        _ident = await business_identifiers.get(session, company.id, "ee_regcode")
        resolved_regcode = (_ident.value if _ident is not None else "") or ""

    rows: list[Kmd2027Row] = []
    errors: list[Kmd2027DataQualityError] = []
    counter = _Counter()

    # ---- Sales: posted invoices + credit notes (role = sale) --------------
    period_basis_date = func.coalesce(Invoice.settlement_date, Invoice.issue_date)
    inv_result = await session.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            period_basis_date >= period_start,
            period_basis_date <= period_end,
        )
        .order_by(period_basis_date, Invoice.number)
    )
    invoices = list(inv_result.scalars().all())

    cn_result = await session.execute(
        select(CreditNote)
        .options(selectinload(CreditNote.lines))
        .where(
            CreditNote.company_id == company_id,
            CreditNote.status == CreditNoteStatus.POSTED,
            CreditNote.issue_date >= period_start,
            CreditNote.issue_date <= period_end,
        )
        .order_by(CreditNote.issue_date, CreditNote.number)
    )
    credit_notes = list(cn_result.scalars().all())

    tax_code_ids: set[uuid.UUID] = set()
    contact_ids: set[uuid.UUID] = set()
    for inv in invoices:
        tax_code_ids |= {ln.tax_code_id for ln in inv.lines if ln.tax_code_id}
        if inv.contact_id:
            contact_ids.add(inv.contact_id)
    for cn in credit_notes:
        tax_code_ids |= {ln.tax_code_id for ln in cn.lines if ln.tax_code_id}
        if cn.contact_id:
            contact_ids.add(cn.contact_id)

    tax_codes = await _load_tax_codes(session, tax_code_ids)
    contacts = await _load_contacts(session, contact_ids)
    prepayment_invoice_ids = await _prepayment_invoice_ids(session, company_id, invoices)

    for inv in invoices:
        _, partner_name, reg_no = _partner_from_contact(inv.contact_id, contacts)
        fx = Decimal(str(inv.fx_rate or Decimal("1")))
        groups = _group_lines_by_reporting_type(inv.lines, tax_codes, fx_rate=fx)
        for g in groups:
            _emit_sale(
                rows, errors, counter, reporting_type=g.reporting_type, rate=g.rate,
                taxable_value=g.taxable_value, sign=Decimal("1"),
                document_number=inv.number, doc_date=inv.settlement_date or inv.issue_date,
                doc_total_ex_vat=inv.base_subtotal, partner_name=partner_name, reg_no=reg_no,
                is_prepayment=inv.id in prepayment_invoice_ids,
            )
    for cn in credit_notes:
        _, partner_name, reg_no = _partner_from_contact(cn.contact_id, contacts)
        groups = _group_lines_by_reporting_type(cn.lines, tax_codes)
        for g in groups:
            _emit_sale(
                rows, errors, counter, reporting_type=g.reporting_type, rate=g.rate,
                taxable_value=g.taxable_value, sign=Decimal("-1"),
                document_number=cn.number, doc_date=cn.issue_date,
                doc_total_ex_vat=cn.subtotal, partner_name=partner_name, reg_no=reg_no,
            )

    # ---- Purchases: posted bills (role = input or acquisition) ------------
    bill_result = await session.execute(
        select(Bill)
        .options(selectinload(Bill.lines))
        .where(
            Bill.company_id == company_id,
            Bill.status == BillStatus.POSTED,
            Bill.issue_date >= period_start,
            Bill.issue_date <= period_end,
        )
        .order_by(Bill.issue_date, Bill.number)
    )
    bills = list(bill_result.scalars().all())

    b_tax_code_ids: set[uuid.UUID] = set()
    b_contact_ids: set[uuid.UUID] = set()
    for b in bills:
        b_tax_code_ids |= {ln.tax_code_id for ln in b.lines if ln.tax_code_id}
        if b.contact_id:
            b_contact_ids.add(b.contact_id)
    b_tax_codes = await _load_tax_codes(session, b_tax_code_ids)
    b_contacts = await _load_contacts(session, b_contact_ids)

    for b in bills:
        _, partner_name, reg_no = _partner_from_contact(b.contact_id, b_contacts)
        fx = Decimal(str(b.fx_rate or Decimal("1")))
        groups = _group_lines_by_reporting_type(b.lines, b_tax_codes, fx_rate=fx)
        for g in groups:
            _emit_purchase(
                rows, errors, counter, reporting_type=g.reporting_type, rate=g.rate,
                taxable_value=g.taxable_value, input_vat=g.tax_amount,
                document_number=b.number, doc_date=b.issue_date,
                partner_name=partner_name, reg_no=reg_no,
            )

    # ---- Purchases: posted supplier credit notes (role = input, signed) ---
    # Purchase-side mirror of the sale-side credit-note loop above — SAME
    # (reporting_type, role) resolution as an ordinary bill, negated (sample
    # Example 19 = O_101 at −240; see this module's docstring + O_601's
    # kmdtyyp_mapping.yaml comment).
    scn_result = await session.execute(
        select(SupplierCreditNote)
        .options(selectinload(SupplierCreditNote.lines))
        .where(
            SupplierCreditNote.company_id == company_id,
            SupplierCreditNote.status == SupplierCreditNoteStatus.POSTED,
            SupplierCreditNote.issue_date >= period_start,
            SupplierCreditNote.issue_date <= period_end,
        )
        .order_by(SupplierCreditNote.issue_date, SupplierCreditNote.number)
    )
    supplier_credit_notes = list(scn_result.scalars().all())

    s_tax_code_ids: set[uuid.UUID] = set()
    s_contact_ids: set[uuid.UUID] = set()
    for scn in supplier_credit_notes:
        s_tax_code_ids |= {ln.tax_code_id for ln in scn.lines if ln.tax_code_id}
        if scn.contact_id:
            s_contact_ids.add(scn.contact_id)
    s_tax_codes = await _load_tax_codes(session, s_tax_code_ids)
    s_contacts = await _load_contacts(session, s_contact_ids)

    for scn in supplier_credit_notes:
        _, partner_name, reg_no = _partner_from_contact(scn.contact_id, s_contacts)
        groups = _group_lines_by_reporting_type(scn.lines, s_tax_codes)
        for g in groups:
            _emit_purchase(
                rows, errors, counter, reporting_type=g.reporting_type, rate=g.rate,
                taxable_value=g.taxable_value, input_vat=g.tax_amount, sign=Decimal("-1"),
                document_number=scn.number, doc_date=scn.issue_date,
                partner_name=partner_name, reg_no=reg_no,
            )

    return Kmd2027Listing(
        regcode=resolved_regcode, period_start=period_start, period_end=period_end,
        rows=rows, errors=errors,
    )


async def _prepayment_invoice_ids(
    session: AsyncSession, company_id: uuid.UUID, invoices: list[Invoice],
) -> set[uuid.UUID]:
    """Invoice ids that qualify as a prepayment-invoice row (sample Example 2):
    an ``INCOMING`` ``Payment`` (POSTED), allocated to the invoice, whose
    ``payment_date`` precedes the invoice's own ``issue_date`` — cash received
    before the invoice existed. See this module's docstring for why this is
    the minimal, non-double-counting, no-schema-change representation.

    Fixer round 1 (F2 fix): only the pre-issue-date allocations count, and
    they must sum to the invoice's FULL total (``PaymentAllocation.amount``
    is in the invoice's own currency, same as ``Invoice.total`` — matches
    how ``_post_prepaid_invoice`` in the golden test constructs a
    prepayment: ``amount=invoice_total``). A MIXED invoice — part paid
    before issue_date, the remainder paid/invoiced normally — is NOT a
    full prepayment: reporting its whole taxable value under
    ``documentApplyToNumber`` would misrepresent the normally-invoiced
    portion as settled via an advance payment. Such an invoice falls
    through to the ordinary (non-prepayment) row, which is always valid
    (module docstring: "transmitting the real code is always valid").
    Splitting the row between the prepaid and ordinary portions would
    need EMTA semantics beyond what's verified here, so it is not
    attempted."""
    if not invoices:
        return set()
    inv_by_id = {inv.id: inv for inv in invoices}
    result = await session.execute(
        select(PaymentAllocation.invoice_id, Payment.payment_date, PaymentAllocation.amount)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            Payment.company_id == company_id,
            Payment.status == PaymentStatus.POSTED,
            Payment.direction == PaymentDirection.INCOMING,
            PaymentAllocation.invoice_id.in_(inv_by_id.keys()),
        )
    )
    prepaid_total: dict[uuid.UUID, Decimal] = {}
    for invoice_id, payment_date, amount in result.all():
        inv = inv_by_id.get(invoice_id)
        if inv is not None and payment_date < inv.issue_date:
            prepaid_total[invoice_id] = prepaid_total.get(invoice_id, Decimal("0")) + amount
    prepaid: set[uuid.UUID] = set()
    for invoice_id, total in prepaid_total.items():
        inv = inv_by_id[invoice_id]
        if total >= inv.total - Decimal("0.01"):
            prepaid.add(invoice_id)
    return prepaid


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _flag(errors, *, document_number, partner_name, reporting_type, role) -> None:
    known = kmdtyyp.is_unmapped_engine_tag(reporting_type, role)
    if known:
        reason = (
            "engine tag is a KNOWN-unmapped KMDTYYP gap (ambiguous across leaves) — "
            "seed a finer engine tag before it can be classified"
        )
    else:
        reason = "no KMDTYYP2026ap leaf maps to this (reporting_type, role) pair"
    errors.append(Kmd2027DataQualityError(
        document_number=document_number, partner_name=partner_name,
        reporting_type=reporting_type, role=role,
        message=(
            f"Transaction on document {document_number!r} (partner {partner_name!r}) "
            f"tagged reporting_type={reporting_type!r} role={role!r}: {reason}. "
            "Row omitted from the export (not coded to a guessed leaf)."
        ),
    ))


def _emit_sale(
    rows, errors, counter, *, reporting_type, rate, taxable_value, sign,
    document_number, doc_date, doc_total_ex_vat, partner_name, reg_no,
    is_prepayment: bool = False,
) -> None:
    code = kmdtyyp.resolve_kmdtyyp(reporting_type, "sale")
    if code is None:
        _flag(errors, document_number=document_number, partner_name=partner_name,
              reporting_type=reporting_type, role="sale")
        return
    partner_code, code_type, ident_cat = _partner_fields(reg_no, reporting_type)
    tax_rate = None if reporting_type in _NO_RATE_TAGS else _rate_fraction(rate)
    rows.append(Kmd2027Row(
        line_number=counter.next(), kmdtyyp_code=code,
        amount=_q2(taxable_value * sign), tax_rate=tax_rate,
        partner_code=partner_code, partner_code_type=code_type,
        identifier_category=ident_cat,
        # Prepayment field-swap (sample Example 2) — see module docstring.
        # documentNumber and documentApplyToNumber are mutually exclusive on
        # this row: a prepayment names itself via documentApplyToNumber only.
        document_number=None if is_prepayment else document_number,
        document_apply_to_number=document_number if is_prepayment else None,
        document_date=doc_date,
        invoice_total=_q2(doc_total_ex_vat * sign) if doc_total_ex_vat is not None else None,
    ))


def _emit_purchase(
    rows, errors, counter, *, reporting_type, rate, taxable_value, input_vat,
    document_number, doc_date, partner_name, reg_no, sign: Decimal = Decimal("1"),
) -> None:
    partner_code, code_type, ident_cat = _partner_fields(reg_no, reporting_type)

    if reporting_type in _RC_ACQUISITION_TAGS:
        # S_* self-assessed acquisition base row (taxable value).
        acq_code = kmdtyyp.resolve_kmdtyyp(reporting_type, "acquisition")
        if acq_code is None:
            _flag(errors, document_number=document_number, partner_name=partner_name,
                  reporting_type=reporting_type, role="acquisition")
        else:
            rows.append(Kmd2027Row(
                line_number=counter.next(), kmdtyyp_code=acq_code,
                amount=_q2(taxable_value * sign), tax_rate=_rate_fraction(rate),
                partner_code=partner_code, partner_code_type=code_type,
                identifier_category=ident_cat, document_number=document_number,
                document_date=doc_date,
            ))
        # O_4xx input-VAT row for the two EU-acquisition tags the engine fans out.
        if reporting_type in _RC_FANOUT_INPUT_TAGS:
            in_code = kmdtyyp.resolve_kmdtyyp(reporting_type, "input")
            if in_code is None:
                _flag(errors, document_number=document_number, partner_name=partner_name,
                      reporting_type=reporting_type, role="input")
            else:
                rows.append(Kmd2027Row(
                    line_number=counter.next(), kmdtyyp_code=in_code,
                    amount=_q2(taxable_value * _rate_fraction(rate) * sign),
                    tax_rate=_rate_fraction(rate),
                    partner_code=partner_code, partner_code_type=code_type,
                    identifier_category=ident_cat, document_number=document_number,
                    document_date=doc_date,
                ))
        return

    if reporting_type in _ORDINARY_INPUT_TAGS:
        in_code = kmdtyyp.resolve_kmdtyyp(reporting_type, "input")
        if in_code is None:
            _flag(errors, document_number=document_number, partner_name=partner_name,
                  reporting_type=reporting_type, role="input")
            return
        rows.append(Kmd2027Row(
            line_number=counter.next(), kmdtyyp_code=in_code,
            amount=_q2(input_vat * sign), tax_rate=_rate_fraction(rate),
            partner_code=partner_code, partner_code_type=code_type,
            identifier_category=ident_cat, document_number=document_number,
            document_date=doc_date,
        ))
        return

    # Any other purchase tag falls here and is intentionally NOT exported and
    # NOT flagged. This is deliberate for two documented classes (build-plan
    # §4.5 producibility boundary):
    #   * NON-VAT-RETURN inputs — exempt-input (INPUT_EXEMPT), NTR: correctly
    #     produce no KMDTYYP row and no data-quality flag (there is nothing to
    #     report; flagging them would be noise).
    #   * SEEDED-BUT-INPUT-UNMAPPED legacy tags — e.g. `reduced_5_legacy` (5%
    #     press rate, superseded 2024-12-31) has an M_101 SALE mapping but no
    #     input leaf (O_101's engine sources are the 9/13/24 rates only). Such a
    #     tag cannot occur in a 2027 reporting period (the rate predates it), so
    #     dropping it here is out of scope, not a silent data-loss bug.
    # NOTE the asymmetry (surfaced by the ported critic-loop finding): the SALE
    # and ACQUISITION paths FLAG an unmapped tag (resolve→None→_flag); this input
    # `else` does not. That is correct ONLY while every seeded input-role tag
    # either maps to an O_ leaf OR is one of the two classes above —
    # tests/services/lodgement/test_kmd_2027_coverage_guard.py guards the mapped
    # half; a NEW seeded input tag that should export must be added to
    # `_ORDINARY_INPUT_TAGS`, not left to fall silently through here.
