"""KMD-INF (VAT-return invoice annex) listing generator.

Packet 1 of the kmd-inf-tsd scope
(``~/.claude/plans/kmd-inf-tsd-scope.md`` §1.1/§2.1/§7). Assembles the
Part A (issued sales) / Part B (received purchase) row sets for a
period from POSTED invoices/credit-notes/bills.

**Parallel to, not built on, the KMD box engine** (scope §0): KMD-INF
is a repeating-row listing (one row per invoice line-group), not a box
vector — ``tax_return_generator.py`` aggregates ``JournalLine`` rows
into a fixed 28-box vector and is untouched by this module. This module
reads ``Invoice``/``CreditNote``/``Bill`` rows + their lines directly,
using the SAME period-basis and ``TaxCode.reporting_type`` conventions
the box engine relies on, so the two reconcile (see the golden test).

**Not implemented here (packet 2):** the e-MTA XML/CSV serializer
(``services/lodgement/kmd_inf/{mapping,serializer}.py``). This module's
``KmdInfListing`` is that serializer's future input contract — one step
removed from the ledger, the same relationship ``KmdFigures`` has to
``tax_return_generator.TaxReturnResult``.

---

## Design decisions this module had to make where the scope was silent
or self-contradictory (flagged prominently per the build's "fix forward
minimally and flag it" instruction):

1. **Credit-note threshold aggregation — scope §2.1 vs scope §6
   contradiction, RESOLVED IN FAVOUR OF §6 + the seed.** Scope §2.1 says
   the generator should "default to net-with-credit-notes" for the
   threshold crossing test. But [SEED-EE] (quoted in the same section)
   says the threshold is assessed *"separately for invoices vs credit
   notes"*, and the scope's OWN golden-period test (§6) requires partner
   P1 (€1,200 ex-VAT invoices + a −€300 credit note = €900 **net**) to
   be **listed** — which only happens if credit notes are excluded from
   the crossing test. A €900 net-of-credit-notes total would NOT cross
   €1,000 and P1 would silently vanish from the golden's own expected
   output. §2.1's "net" default cannot be right simultaneously with §6
   and the seed; this module trusts the seed + the golden and makes
   the DEFAULT strategy ``"separate"``: **only invoice (or bill) totals
   count toward the €1,000 crossing test; credit notes never count
   toward crossing on their own.** Once a partner crosses via their
   invoice/bill total, ALL of that partner's rows for the period —
   invoices AND credit notes — are listed (per the base rule, scope
   §2.1 "Threshold mechanics"). The alternative ``"net"`` strategy
   (netting credit notes into the crossing sum) is kept as a single
   swappable predicate (``_CROSSING_STRATEGIES``) exactly as the scope
   asked, but is NOT the default — confirm the real KMD-INF täitmise
   juhend before ever flipping it.
2. **Part B is bills-only — engine gap, scope deviation.** The scope's
   §1.1 problem statement says Part B lists "received (purchase)
   invoices + credit notes". This engine has **no purchase-side
   credit-note / vendor-credit model** — only the sales-side
   ``CreditNote`` (``models/credit_note.py``, FK'd to
   ``invoices``/``one_off_customers`` only, no bill equivalent exists
   anywhere in the tree). Part B is therefore generated from posted
   ``Bill`` rows only; a purchase credit/debit note model is a
   prerequisite for the "+ credit notes" half of Part B and is flagged
   here as a gap for a future milestone, not silently ignored.
3. **Part B deductible-input filter — UPDATED, critic round 2 fix.**
   Originally used ``TaxCode.input_credit_recoverable is True`` ALONE.
   That let a line tagged ``reporting_type="rc_domestic_acq"`` (a real
   seeded code, ``RC_DOMESTIC_ACQ`` in ``tax_codes.yaml``, which reports
   to KMD boxes 7/7.1 — NOT box 5) reach Part B carrying an input-VAT
   figure with nothing to reconcile against in that period's KMD box 5,
   the moment a company provisioned that code with the model's
   ``input_credit_recoverable`` default of ``True`` — breaking INF's
   defining "row values reconcile to the KMD boxes" property (scope
   §1.1). Now requires BOTH ``input_credit_recoverable is True`` AND
   ``reporting_type`` be one of box 5's actual feeder types
   (``_KMD_BOX_5_FEEDER_REPORTING_TYPES``, mirroring
   ``tax_return_box_definitions.yaml``'s "5_DOMESTIC"/"5_RC"
   ``feeder_tax_codes`` — kept in sync by inspection, same discipline
   as ``services.payroll_ee``'s embedded-rate fallback, see that
   constant's own comment for why runtime box-definition lookup isn't
   used here). Applied PER LINE, before both row assembly and the
   threshold test (scope §2.1 "Part B filter (important)"), so a bill
   with a mix of deductible/non-deductible/wrong-reporting-type lines
   contributes only its box-5-feeding deductible lines to Part B and to
   the crossing sum. erisuse-kood ``12`` ("reverse-charge acquisition")
   can still be DERIVED for an excluded ``rc_domestic_acq`` line (see
   ``_erisuse_b``) — that mapping is a pure function of
   ``reporting_type`` and is unaffected; only ROW INCLUSION in Part B
   changed.
4. **Erisuse-kood 01 and 11 are never derived.** The scope's own text
   flags both as needing "finer seed leaves" (01, §41/§42 special
   scheme) with no current ``reporting_type`` tag to key off, and 11
   (§30 partial deduction) has no partial-fraction field on ``TaxCode``
   at all (``input_credit_recoverable`` is boolean, all-or-nothing).
   Both erisuse codes are therefore always ``None`` in this module's
   output — never guessed.
5. **Row granularity = one row per (document, TaxCode.reporting_type
   group)**, not one row per document. A mixed-rate invoice/bill emits
   one row per distinct reporting_type present on its lines, each
   carrying its OWN taxable value / input-VAT so the per-rate columns
   reconcile to KMD boxes 1/2/2-2/5 exactly (scope §2.1's stated
   reconciliation requirement) — and every row from a mixed-rate
   document is stamped erisuse-kood ``"03"`` (scope: "a mixed-rate
   invoice ⇒ erisuse-kood 03").
6. **Critic round 5 fixes.** (a) ``REPORTING_TYPE_TO_KMD_BOX`` was
   missing box "9" (KMS §41^1 seller-side RC/installation supply,
   ``rc_domestic_supply``/``install_other_ms``) — added, see the
   constant's own comment. (b) Threshold/row grouping now keys off
   ``partner_group_key`` (registration_number when present, else a
   per-Contact fallback — ``_partner_group_key``), not ``Contact.id``,
   so two Contact rows sharing one registrikood are merged into a
   single counterparty before the €1,000 test, per scope §2.1's
   "grouping key = the counterparty registry code".
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.bill import Bill, BillLine, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNote, CreditNoteLine, CreditNoteStatus
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.tax_code import TaxCode

# NOTE: ``Invoice.contact_id`` / ``CreditNote.contact_id`` / ``Bill.contact_id``
# are all ``nullable=False`` at the DB level (verified against
# ``models/{invoice,credit_note,bill}.py`` — the ``Mapped[uuid.UUID | None]``
# type hints are misleading; every posted document has a REAL ``Contact``
# row). The ``one_off_customer_id``/``one_off_vendor_id`` columns + their
# ``OneOffCustomer``/``OneOffVendor`` models are a separate, apparently
# vestigial mechanism — ``services/invoices.py``'s ``create_draft`` takes
# a required (non-Optional) ``contact_id`` and never writes
# ``one_off_customer_id``, and no test in the tree exercises it. The
# scope's §2.1 "one-off customer with no registry code" scenario is
# therefore just an ordinary ``Contact`` row (``is_one_off=True`` or not)
# with ``registration_number IS NULL`` — handled by the same code path
# as any other under-documented contact, not a separate branch.

_ZERO = Decimal("0")
_DEFAULT_THRESHOLD = Decimal("1000.00")
_TWOPLACES = Decimal("0.01")


def _q2(value: Decimal) -> Decimal:
    """Quantize to the cent, half-up — same convention as
    ``services.invoices._q2`` / ``kmd/serializer.py``'s ``_money``."""
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)

# scope §2.1 Part A table: standard→box1, reduced_9→box2, reduced_13→box2-2
# (plus the legacy-rate siblings, same box family). Only the MAIN box
# codes a human reconciles against the printed form — never the seed's
# internal "_DOMESTIC"/"_RC" helper boxes (mapping.py's own convention).
#
# Critic round 5 fix: box "9" (KMS §41^1 seller-side reverse-charge /
# installation supply) was missing entirely — its feeder_tax_codes
# (tax_return_box_definitions.yaml box "9", display_order 24) are
# ``rc_domestic_supply`` + ``install_other_ms``. Without this, a Part A
# row for either reporting_type serialized with ``kmd_box_code=None``
# even though ``_erisuse_a`` correctly stamps erisuse-kood "02" for
# ``rc_domestic_supply`` — the row-to-KMD-box reconciliation broke for
# exactly the category this module's own erisuse-kood branch targets.
REPORTING_TYPE_TO_KMD_BOX: dict[str, str] = {
    "standard": "1",
    "standard_legacy_20": "1-1",
    "standard_legacy_22": "1-2",
    "reduced_9": "2",
    "reduced_5_legacy": "2-1",
    "reduced_13": "2-2",
    "rc_domestic_supply": "9",
    "install_other_ms": "9",
}

# Critic round 2 fix: the SET of ``TaxCode.reporting_type`` values that
# actually feed KMD box 5 (deductible input VAT) — mirrors
# ``tax_return_box_definitions.yaml``'s "5_DOMESTIC" (display_order 141)
# + "5_RC" (display_order 142) ``feeder_tax_codes`` lists EXACTLY, kept
# in sync by inspection (same discipline as ``services.payroll_ee``'s
# embedded-rate fallback — cannot be resolved at runtime via
# ``tax_return_generator._fetch_box_definitions`` here: that helper's
# embedded fallback only covers ``("AU", "BAS")``, so calling it for
# ``("EE", "KMD")`` in the standard REFERENCE_DATABASE_URL-unset test/CI
# harness would silently return an EMPTY feeder set and exclude every
# Part B line). Previously the Part B filter (below) keyed off
# ``TaxCode.input_credit_recoverable`` ALONE — a company-provisioned
# code tagged ``reporting_type="rc_domestic_acq"`` (a real seeded code,
# ``tax_codes.yaml`` code ``RC_DOMESTIC_ACQ``, which reports to KMD
# boxes 7/7.1, NOT box 5) with the model's ``input_credit_recoverable``
# default of ``True`` would be listed on the INF annex carrying an
# input-VAT figure that reconciled to nothing in that period's KMD box
# 5, breaking INF's defining "row values reconcile to the KMD boxes"
# property (scope §1.1). Design decision #3 below is updated to match.
_KMD_BOX_5_FEEDER_REPORTING_TYPES: frozenset[str] = frozenset({
    # "5_DOMESTIC" feeder_tax_codes (tax_return_box_definitions.yaml ~L403)
    "standard", "standard_legacy_20", "standard_legacy_22",
    "reduced_13", "reduced_9", "capital", "input_import",
    # "5_RC" feeder_tax_codes (tax_return_box_definitions.yaml ~L417)
    "rc_eu_acq_goods", "rc_eu_acq_services",
})

# scope §2.1 Part A erisuse-kood 02: buyer self-assess (§41¹).
_ERISUSE_A_RC_DOMESTIC_SUPPLY = "02"
_ERISUSE_A_MIXED_RATE = "03"

# scope §2.1 Part B erisuse-kood 12: reverse-charge acquisition.
_ERISUSE_B_RC_TYPES = frozenset(
    {"rc_eu_acq_goods", "rc_eu_acq_services", "rc_domestic_acq"}
)
_ERISUSE_B_RC = "12"

CreditNoteAggregation = Literal["separate", "net"]


class KmdInfCompanyConfigError(ValueError):
    """Raised when the company is not configured to file KMD-INF at all
    — critic round 3 fix. ``Company.base_currency`` is a free-form
    column fully independent of ``Company.jurisdiction`` (no cross-field
    constraint anywhere in the model/service/API layer); every taxable
    value and the €1,000 threshold below are treated as EUR amounts —
    Invoice/Bill per-line figures are converted via the document's own
    ``fx_rate`` (critic round 4 fix, see
    ``_group_lines_by_reporting_type``), but only on the assumption
    that ``base_currency`` (the currency ``fx_rate`` converts INTO) is
    itself EUR. A company provisioned with a non-EUR ``base_currency``
    would have every converted figure land in the WRONG target
    currency and still get silently emitted as EMTA-bound EUR figures.
    Refusing loudly beats filing a wrong-currency listing. (CreditNote
    has no ``fx_rate``/FX support at all — a separate, pre-existing
    gap this guard does not cover.)"""


@dataclass(frozen=True)
class KmdInfPartARow:
    """One Part A (issued sales) row — one document × reporting_type
    group. Amounts are SIGNED: negative for a credit-note row (scope
    §2.1 "Include credit notes ... as signed rows")."""

    row_no: int
    partner_registration_number: str
    partner_name: str
    document_number: str | None
    document_date: date
    document_total_ex_vat: Decimal
    taxable_value: Decimal
    rate: Decimal
    kmd_box_code: str | None
    erisuse_kood: str | None
    is_credit_note: bool


@dataclass(frozen=True)
class KmdInfPartBRow:
    """One Part B (received purchase) row — one bill × reporting_type
    deductible group. See module docstring point 2: bills only, no
    purchase-side credit note exists in this engine."""

    row_no: int
    partner_registration_number: str
    partner_name: str
    document_number: str | None
    document_date: date
    document_total_incl_vat: Decimal
    input_vat: Decimal
    rate: Decimal
    erisuse_kood: str | None
    is_credit_note: bool = False


@dataclass(frozen=True)
class KmdInfDataQualityError:
    """A counterparty crossed the €1,000 threshold but has no
    registration_number on file — scope §2.1: "a partner with no code
    but ≥€1,000 is a data-quality error to surface, not a silent
    drop." """

    part: Literal["A", "B"]
    partner_name: str
    partner_contact_id: uuid.UUID
    period_total_ex_vat: Decimal
    message: str


@dataclass(frozen=True)
class KmdInfListing:
    company_id: uuid.UUID
    period_start: date
    period_end: date
    part_a: list[KmdInfPartARow] = field(default_factory=list)
    part_b: list[KmdInfPartBRow] = field(default_factory=list)
    errors: list[KmdInfDataQualityError] = field(default_factory=list)


@dataclass
class _LineGroup:
    reporting_type: str
    rate: Decimal
    taxable_value: Decimal
    tax_amount: Decimal


@dataclass
class _Doc:
    is_credit_note: bool
    id: uuid.UUID
    number: str | None
    doc_date: date
    partner_key: uuid.UUID  # Contact.id — always present (see module docstring)
    partner_name: str
    partner_reg_no: str | None
    # ex-VAT amount that counts toward the €1,000 crossing test — for
    # Part B this is the SUM OF DEDUCTIBLE LINES ONLY (filter-before-
    # threshold, scope §2.1).
    threshold_amount: Decimal
    # document-level display total (Part A: ex-VAT subtotal; Part B:
    # incl-VAT total) — repeated on every row this document emits.
    display_total: Decimal
    line_groups: list[_LineGroup]
    # Sum of line_subtotal for lines whose tax_code_id does not resolve
    # to a TaxCode (NULL or a stale/deleted id) — critic round 1 fix.
    # These lines are silently excluded from line_groups (so they can
    # never produce a row), yet their value is still counted in
    # threshold_amount (the whole-document total). A partner can cross
    # the €1,000 threshold entirely on unresolved-line value and be
    # selected for listing while emitting zero (or partial) rows. Kept
    # here so ``_assemble_part`` can surface it as a data-quality error
    # instead of a silent drop.
    unresolved_amount: Decimal = _ZERO
    # Critic round 5 fix: the grouping/threshold key — registration_number
    # when present (merges duplicate Contact rows for one registrikood),
    # else a per-contact fallback. See ``_partner_group_key``. Defaulted
    # (like ``unresolved_amount`` above) purely for dataclass field-order
    # reasons — always explicitly set at every construction site.
    partner_group_key: str = ""


def _crossing_sum_separate(invoice_totals: list[Decimal], credit_note_totals: list[Decimal]) -> Decimal:
    """Default strategy (see module docstring point 1): only
    invoice/bill totals count toward the crossing test."""
    return sum((abs(t) for t in invoice_totals), _ZERO)


def _crossing_sum_net(invoice_totals: list[Decimal], credit_note_totals: list[Decimal]) -> Decimal:
    """Alternate strategy: net invoices against credit notes before
    testing. UNVERIFIED against the real juhend — see module docstring
    point 1. NOT the default."""
    return abs(sum(invoice_totals, _ZERO) - sum(credit_note_totals, _ZERO))


_CROSSING_STRATEGIES = {
    "separate": _crossing_sum_separate,
    "net": _crossing_sum_net,
}


def _erisuse_a(reporting_types: set[str]) -> str | None:
    if len(reporting_types) > 1:
        return _ERISUSE_A_MIXED_RATE
    if reporting_types == {"rc_domestic_supply"}:
        return _ERISUSE_A_RC_DOMESTIC_SUPPLY
    return None


def _erisuse_b(reporting_type: str) -> str | None:
    if reporting_type in _ERISUSE_B_RC_TYPES:
        return _ERISUSE_B_RC
    return None


async def _load_tax_codes(session: AsyncSession, tax_code_ids: set[uuid.UUID]) -> dict[uuid.UUID, TaxCode]:
    if not tax_code_ids:
        return {}
    result = await session.execute(select(TaxCode).where(TaxCode.id.in_(tax_code_ids)))
    return {tc.id: tc for tc in result.scalars().all()}


async def _load_contacts(session: AsyncSession, contact_ids: set[uuid.UUID]) -> dict[uuid.UUID, Contact]:
    if not contact_ids:
        return {}
    result = await session.execute(select(Contact).where(Contact.id.in_(contact_ids)))
    return {c.id: c for c in result.scalars().all()}


def _partner_from_contact(contact_id: uuid.UUID, contacts: dict[uuid.UUID, Contact]) -> tuple[uuid.UUID, str, str | None]:
    c = contacts.get(contact_id)
    if c is None:
        # Contact row not found (shouldn't happen for a posted document —
        # FK is RESTRICT — but degrade gracefully rather than raise).
        return contact_id, "Unknown", None
    return contact_id, c.name, c.registration_number


def _partner_group_key(contact_id: uuid.UUID, reg_no: str | None) -> str:
    """Critic round 5 fix: the €1,000 threshold + row assembly must
    group by the counterparty REGISTRY CODE (scope §2.1 "Counterparty
    grouping key = the counterparty registry code"), not by
    ``Contact.id``. Two distinct ``Contact`` rows sharing one real-world
    ``registration_number`` (a duplicate re-entered contact, or separate
    AR/AP records for the same entity — nothing in the schema prevents
    this) must be merged into a single KMD-INF partner so their period
    totals combine before the threshold test; otherwise two €600
    invoices split across two Contact rows for the same registrikood
    each stay under €1,000 and the partner is silently omitted even
    though the combined €1,200 should list. Contacts with NO
    registration_number are kept separately grouped BY CONTACT (never
    merged with each other) so each can still independently trigger the
    no-code data-quality error (scope §2.1 "a partner with no code ...
    is a data-quality error to surface")."""
    if reg_no:
        return f"reg:{reg_no}"
    return f"contact:{contact_id}"


def _unresolved_amount(
    lines: list[InvoiceLine] | list[CreditNoteLine] | list[BillLine],
    tax_codes: dict[uuid.UUID, TaxCode],
    *,
    fx_rate: Decimal = Decimal("1"),
) -> Decimal:
    """Sum of ``line_subtotal`` for lines whose ``tax_code_id`` does not
    resolve to a loaded ``TaxCode`` (NULL or unresolved) — critic round
    1 fix, see ``_Doc.unresolved_amount``.

    ``fx_rate`` converts document-currency ``line_subtotal`` to
    base-currency EUR — critic round 4 fix, see
    ``_group_lines_by_reporting_type``'s docstring for why this
    matters (``InvoiceLine``/``BillLine`` carry no per-line base
    amount; only the document header does)."""
    total = _ZERO
    for line in lines:
        tc = tax_codes.get(line.tax_code_id) if line.tax_code_id else None
        if tc is None:
            total += _q2(line.line_subtotal * fx_rate)
    return total


def _group_lines_by_reporting_type(
    lines: list[InvoiceLine] | list[CreditNoteLine] | list[BillLine],
    tax_codes: dict[uuid.UUID, TaxCode],
    *,
    deductible_only: bool = False,
    fx_rate: Decimal = Decimal("1"),
) -> list[_LineGroup]:
    """Group lines by ``TaxCode.reporting_type``, summing taxable value
    and tax.

    ``fx_rate`` — critic round 4 fix. ``InvoiceLine.line_subtotal``/
    ``line_tax`` (and their ``BillLine``/``CreditNoteLine`` siblings)
    are DOCUMENT-currency amounts (``services/invoices.py``'s
    ``_replace_lines`` writes them straight from ``qty * unit_price``
    with no fx conversion; only the document-level ``base_subtotal``/
    ``base_total`` columns are converted, via
    ``_q2(line.line_subtotal * fx_rate)`` per
    ``services/invoices.py:690``). There is no per-line base-currency
    column to read instead, so this function replicates that exact
    per-line conversion — same rounding convention — before grouping,
    matching the KMD-INF ``KmdInfCompanyConfigError`` guard's claim
    that every taxable value is a base-currency (EUR) figure. Callers
    for credit notes pass the default ``fx_rate=1`` — ``CreditNote``
    has no ``fx_rate``/``base_*`` columns at all (no FX support exists
    for that model yet; a separate, pre-existing gap, not introduced
    or fixed here).

    Critic round 5 fix: group key is ``(reporting_type, rate)``, not
    ``reporting_type`` alone. Nothing in the schema stops a company
    provisioning two ``TaxCode`` rows with the same ``reporting_type``
    but different ``rate`` (``tax_code.py`` -- the two fields are
    independent, no cross-check). Keying on ``reporting_type`` alone
    silently took ``rate`` from whichever line was grouped first and
    accumulated a second rate's value under it -- a wrong-for-half-its-
    value rate column with no error surfaced. Keying on the pair keeps
    every distinct rate its own row (still one row per document x
    reporting_type in the ordinary, non-colliding case -- this is a
    no-op there)."""
    groups: dict[tuple[str, Decimal], _LineGroup] = {}
    for line in lines:
        tc = tax_codes.get(line.tax_code_id) if line.tax_code_id else None
        if tc is None:
            continue
        if deductible_only and (
            not tc.input_credit_recoverable
            or tc.reporting_type not in _KMD_BOX_5_FEEDER_REPORTING_TYPES
        ):
            continue
        rt = tc.reporting_type
        key = (rt, tc.rate)
        base_subtotal = _q2(line.line_subtotal * fx_rate)
        base_tax = _q2(line.line_tax * fx_rate)
        g = groups.get(key)
        if g is None:
            groups[key] = _LineGroup(reporting_type=rt, rate=tc.rate, taxable_value=base_subtotal, tax_amount=base_tax)
        else:
            g.taxable_value += base_subtotal
            g.tax_amount += base_tax
    return list(groups.values())


async def generate_kmd_inf(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
    credit_note_aggregation: CreditNoteAggregation = "separate",
    threshold: Decimal = _DEFAULT_THRESHOLD,
) -> KmdInfListing:
    """Assemble the KMD-INF Part A / Part B row sets for one period.

    Period basis mirrors the KMD box engine's own posting-date rule
    (scope §2.1): invoices use ``settlement_date`` when set else
    ``issue_date`` (``services/invoices.py``'s ``gl_entry_date``
    convention); bills and credit notes post at ``issue_date`` (neither
    model has a settlement_date column — verified against
    ``services/bills.py``/``services/credit_notes.py``'s own
    ``entry_date=...`` posting calls).
    """
    strategy = _CROSSING_STRATEGIES[credit_note_aggregation]

    # Critic round 3 fix: every amount below (the €1,000 threshold and
    # every displayed taxable-value/input-VAT figure) is read straight
    # off Invoice/CreditNote/Bill base_* columns and treated as EUR —
    # refuse up front if the company's base_currency is not actually
    # EUR, rather than silently mislabel a wrong-currency figure as an
    # EMTA-bound EUR one. See KmdInfCompanyConfigError.
    company = await session.get(Company, company_id)
    if company is None:
        raise KmdInfCompanyConfigError(f"Company {company_id} not found")
    if company.base_currency != "EUR":
        raise KmdInfCompanyConfigError(
            f"Company {company_id} has base_currency={company.base_currency!r}, "
            "not 'EUR' — KMD-INF's €1,000 threshold and every taxable-value/"
            "input-VAT column are computed directly from ledger base-currency "
            "amounts with no conversion. Set Company.base_currency='EUR' "
            "before generating KMD-INF for this company."
        )

    # ---- Part A: posted invoices + posted credit notes ----------------
    # Period-basis pushed into the WHERE clause (not filtered in Python
    # after loading every posted document ever) — against a real
    # multi-year ledger, loading all history per call does not scale.
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

    docs_a: list[_Doc] = []
    for inv in invoices:
        partner_key, partner_name, reg_no = _partner_from_contact(inv.contact_id, contacts)
        group_key = _partner_group_key(partner_key, reg_no)
        # critic round 4 fix: convert document-currency line amounts to
        # base-currency (EUR) via the invoice's own fx_rate — see
        # ``_group_lines_by_reporting_type``'s docstring. Without this,
        # a non-1 fx_rate invoice emits a per-rate ``taxable_value`` in
        # the WRONG currency on the same row as the correctly-converted
        # ``document_total_ex_vat`` (which already reads ``base_subtotal``).
        inv_fx_rate = Decimal(str(inv.fx_rate or Decimal("1")))
        line_groups = _group_lines_by_reporting_type(inv.lines, tax_codes, fx_rate=inv_fx_rate)
        docs_a.append(_Doc(
            is_credit_note=False, id=inv.id, number=inv.number, doc_date=inv.settlement_date or inv.issue_date,
            partner_key=partner_key, partner_name=partner_name, partner_reg_no=reg_no,
            partner_group_key=group_key,
            threshold_amount=inv.base_subtotal, display_total=inv.base_subtotal,
            line_groups=line_groups, unresolved_amount=_unresolved_amount(inv.lines, tax_codes, fx_rate=inv_fx_rate),
        ))
    for cn in credit_notes:
        partner_key, partner_name, reg_no = _partner_from_contact(cn.contact_id, contacts)
        group_key = _partner_group_key(partner_key, reg_no)
        # CreditNote has no fx_rate/base_* columns (no FX support for
        # this model at all — pre-existing gap, not this finding's to
        # fix); fx_rate stays the default 1.
        line_groups = _group_lines_by_reporting_type(cn.lines, tax_codes)
        docs_a.append(_Doc(
            is_credit_note=True, id=cn.id, number=cn.number, doc_date=cn.issue_date,
            partner_key=partner_key, partner_name=partner_name, partner_reg_no=reg_no,
            partner_group_key=group_key,
            threshold_amount=cn.subtotal, display_total=cn.subtotal,
            line_groups=line_groups, unresolved_amount=_unresolved_amount(cn.lines, tax_codes),
        ))

    part_a, errors_a = _assemble_part(
        docs_a, part="A", threshold=threshold, strategy=strategy,
        row_factory=_make_part_a_row,
    )

    # ---- Part B: posted bills only (see module docstring point 2) -----
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

    docs_b: list[_Doc] = []
    for b in bills:
        partner_key, partner_name, reg_no = _partner_from_contact(b.contact_id, b_contacts)
        group_key = _partner_group_key(partner_key, reg_no)
        # Filter BEFORE threshold (scope §2.1 "Part B filter (important)").
        # critic round 4 fix: convert via the bill's own fx_rate — same
        # reasoning as the Part A invoice branch above. Without this,
        # ``threshold_amount`` (built from these groups) compared a
        # document-currency figure against the EUR 1,000 threshold.
        b_fx_rate = Decimal(str(b.fx_rate or Decimal("1")))
        deductible_groups = _group_lines_by_reporting_type(
            b.lines, b_tax_codes, deductible_only=True, fx_rate=b_fx_rate,
        )
        if not deductible_groups:
            continue  # no deductible input VAT -> excluded even if partner otherwise crosses
        threshold_amount = sum((g.taxable_value for g in deductible_groups), _ZERO)
        docs_b.append(_Doc(
            is_credit_note=False, id=b.id, number=b.number, doc_date=b.issue_date,
            partner_key=partner_key, partner_name=partner_name, partner_reg_no=reg_no,
            partner_group_key=group_key,
            threshold_amount=threshold_amount, display_total=b.base_total,
            line_groups=deductible_groups, unresolved_amount=_unresolved_amount(b.lines, b_tax_codes, fx_rate=b_fx_rate),
        ))

    part_b, errors_b = _assemble_part(
        docs_b, part="B", threshold=threshold, strategy=strategy,
        row_factory=_make_part_b_row,
    )

    return KmdInfListing(
        company_id=company_id, period_start=period_start, period_end=period_end,
        part_a=part_a, part_b=part_b, errors=errors_a + errors_b,
    )


def _assemble_part(docs: list[_Doc], *, part: Literal["A", "B"], threshold: Decimal, strategy, row_factory):
    # Critic round 5 fix: group by ``partner_group_key`` (registration_number
    # when present, else per-Contact fallback — see ``_partner_group_key``),
    # NOT ``Contact.id``, so two Contact rows sharing one registrikood are
    # merged into a single counterparty before the €1,000 threshold test.
    by_partner: dict[str, list[_Doc]] = {}
    for d in docs:
        by_partner.setdefault(d.partner_group_key, []).append(d)

    rows: list = []
    errors: list[KmdInfDataQualityError] = []

    for group_key, partner_docs in by_partner.items():
        # Diagnostic-only contact id (KmdInfDataQualityError.partner_contact_id
        # stays a real Contact.id, not the merged group_key string) — the
        # first contributing document's contact, representative enough for
        # an error message naming the partner by name anyway.
        diag_contact_id = partner_docs[0].partner_key
        invoice_totals = [d.threshold_amount for d in partner_docs if not d.is_credit_note]
        cn_totals = [d.threshold_amount for d in partner_docs if d.is_credit_note]
        crossing = strategy(invoice_totals, cn_totals)
        if crossing < threshold:
            # Critic round 1 fix: a partner whose ONLY period activity is
            # credit note(s) can never cross via the default "separate"
            # strategy (invoice_totals is empty, crossing==0) no matter
            # how large the credit note is — the module's own "surface,
            # don't silently drop" posture (used for the no-reg-no case
            # above) applies here too. Flag rather than silently vanish.
            cn_abs_sum = sum((abs(t) for t in cn_totals), _ZERO)
            if not invoice_totals and cn_totals and cn_abs_sum >= threshold:
                partner_name = partner_docs[0].partner_name
                errors.append(KmdInfDataQualityError(
                    part=part, partner_name=partner_name,
                    partner_contact_id=diag_contact_id, period_total_ex_vat=cn_abs_sum,
                    message=(
                        f"Partner '{partner_name}' has no invoices/bills this "
                        f"period, only credit note(s) totalling €{cn_abs_sum} "
                        f"ex-VAT — this exceeds the €1,000 KMD-INF Part {part} "
                        "threshold but the default credit-note-aggregation "
                        "strategy ('separate') never counts credit notes "
                        "toward crossing, so this partner is excluded. "
                        "Confirm against the KMD-INF täitmise juhend "
                        "(module docstring point 1, UNVERIFIED) — flip "
                        "credit_note_aggregation='net' if credit notes "
                        "should count."
                    ),
                ))
            continue

        reg_no = next((d.partner_reg_no for d in partner_docs if d.partner_reg_no), None)
        partner_name = partner_docs[0].partner_name

        if reg_no is None:
            errors.append(KmdInfDataQualityError(
                part=part, partner_name=partner_name,
                partner_contact_id=diag_contact_id, period_total_ex_vat=crossing,
                message=(
                    f"Partner '{partner_name}' has no registration_number on file "
                    f"but crosses the €1,000 KMD-INF Part {part} threshold "
                    f"(€{crossing}) — cannot be listed as a KMD-INF counterparty. "
                    "Set Contact.registration_number to resolve."
                ),
            ))
            continue

        # Critic round 1 fix: a line with no resolvable tax_code_id is
        # excluded from line_groups (so it can never produce a row), but
        # its value is still counted in threshold_amount (the whole-
        # document total) — a partner can cross the threshold and be
        # listed here yet a document contributes zero/partial rows.
        # Surface it instead of letting the value vanish silently.
        unresolved_total = sum((d.unresolved_amount for d in partner_docs), _ZERO)
        if unresolved_total:
            errors.append(KmdInfDataQualityError(
                part=part, partner_name=partner_name,
                partner_contact_id=diag_contact_id, period_total_ex_vat=unresolved_total,
                message=(
                    f"Partner '{partner_name}' has €{unresolved_total} of line "
                    f"value on Part {part} documents with no resolvable "
                    "tax_code_id — those lines are excluded from the listed "
                    "rows even though their amount counts toward the €1,000 "
                    "threshold. Set InvoiceLine/CreditNoteLine/BillLine."
                    "tax_code_id to resolve."
                ),
            ))

        row_no = 0
        for d in sorted(partner_docs, key=lambda x: (x.doc_date, x.number or "")):
            reporting_types = {g.reporting_type for g in d.line_groups}
            for g in d.line_groups:
                row_no += 1
                rows.append(row_factory(row_no, reg_no, partner_name, d, g, reporting_types))

    return rows, errors


def _make_part_a_row(row_no: int, reg_no: str, partner_name: str, d: _Doc, g: _LineGroup, reporting_types: set[str]) -> KmdInfPartARow:
    sign = Decimal("-1") if d.is_credit_note else Decimal("1")
    erisuse = _erisuse_a(reporting_types)
    return KmdInfPartARow(
        row_no=row_no, partner_registration_number=reg_no, partner_name=partner_name,
        document_number=d.number, document_date=d.doc_date,
        document_total_ex_vat=d.display_total * sign,
        taxable_value=g.taxable_value * sign,
        rate=g.rate, kmd_box_code=REPORTING_TYPE_TO_KMD_BOX.get(g.reporting_type),
        erisuse_kood=erisuse, is_credit_note=d.is_credit_note,
    )


def _make_part_b_row(row_no: int, reg_no: str, partner_name: str, d: _Doc, g: _LineGroup, reporting_types: set[str]) -> KmdInfPartBRow:
    return KmdInfPartBRow(
        row_no=row_no, partner_registration_number=reg_no, partner_name=partner_name,
        document_number=d.number, document_date=d.doc_date,
        document_total_incl_vat=d.display_total,
        input_vat=g.tax_amount,
        rate=g.rate,
        erisuse_kood=_erisuse_b(g.reporting_type), is_credit_note=False,
    )
