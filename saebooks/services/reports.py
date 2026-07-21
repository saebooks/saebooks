"""Reporting service — trial balance, P&L, balance sheet, aged AR/AP,
P&L by segment, budget vs actual, cashflow forecast.

All reports operate on POSTED journal lines only (except aged AR/AP
+ cashflow forecast, which walk ``invoices``/``bills`` tables directly
so they can show per-document line items).
"""
import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, and_, cast, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.budget import Budget
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNote, CreditNoteStatus
from saebooks.models.department import CostCentre, Department
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.payment import Payment, PaymentAllocation, PaymentStatus
from saebooks.models.project import Project
from saebooks.models.recurring_invoice import (
    RecurrenceStatus,
    RecurringInvoice,
    RecurringInvoiceLine,
)
from saebooks.models.supplier_credit_note import (
    SupplierCreditNote,
    SupplierCreditNoteStatus,
)

# Journal-entry statuses that contribute to GL balances in reports.
#
# A reversal ("void") posts a mirror entry AND flips the original to
# REVERSED. If reports summed only POSTED, the REVERSED original would be
# dropped while its POSTED reversal still counted -- so the reversal would
# subtract a SECOND time (verified live 2026-06-06: voiding a $1,580 invoice
# wrongly moved Trade Debtors 5790.40 -> 4210.40 and understated income).
#
# Including REVERSED makes the original and its POSTED reversal cancel
# exactly. This is also correct ACROSS period boundaries: a reversal dated
# in a later period is still filtered out of a balance taken before its date
# (the entry_date <= to_date predicate), so the original correctly remains
# live in the intervening period and only nets to zero once the reversal's
# date is in scope. (Excluding both sides instead would erase the original
# retroactively and misstate that intervening period.)
#
# DRAFT entries stay excluded -- they have never hit the ledger.
REPORTABLE_STATUSES = (EntryStatus.POSTED, EntryStatus.REVERSED)

# Account types that go on the balance sheet (permanent accounts)
BALANCE_SHEET_TYPES = {
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
}

# Account types that go on the P&L (temporary accounts)
PNL_TYPES = {
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
}


# ---------------------------------------------------------------------------
# R7 — comparative/prior-period shared helpers
#
# ``?compare=previous_period|previous_year`` on the JSON /profit_loss,
# /balance_sheet, /trial_balance routes (api/v1/reports.py) ports the
# prior-period math that already existed ONLY inline in the statement-pack
# PDF route (``statement_pack_pdf`` / its private ``_subtract_one_year_pack``
# + ``_merge_lines``). These two helpers are the ONE shared implementation
# for the JSON path -- the PDF route's own copy predates this and is left
# untouched (out of scope for R7; flagged as pre-existing duplication).
# ---------------------------------------------------------------------------


def subtract_one_year(d: date) -> date:
    """Return the date exactly one year before ``d``, guarding 29 Feb.

    Leap-safe: 29 Feb in a leap year has no equivalent in a non-leap prior
    year, so falls back to 28 Feb (matches the statement pack's identical
    ``_subtract_one_year_pack`` semantics).
    """
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def previous_period_range(from_date: date, to_date: date) -> tuple[date, date]:
    """Return the immediately-preceding period of identical length.

    E.g. 2028-01-01..2028-01-31 (31 days inclusive) -> 2027-12-01..2027-12-31.
    ``prev_to = from_date - 1 day``; ``prev_from`` is shifted back by the
    same day-count as ``[from_date, to_date]`` so the comparative period
    covers exactly as many days as the current one.
    """
    prev_to = from_date - timedelta(days=1)
    prev_from = prev_to - (to_date - from_date)
    return prev_from, prev_to


def fy_bounds_for_company(
    as_of: date, fin_year_start_month: int, fin_year_start_day: int
) -> tuple[date, date]:
    """Return ``(fy_start, fy_end)`` for the financial year containing ``as_of``,
    anchored at an arbitrary ``(fin_year_start_month, fin_year_start_day)``.

    Jurisdiction-neutral generalisation of ``api/v1/reports.py``'s
    AU-hardcoded ``_current_fy_bounds`` (1 July), which cannot be reused
    for a company whose ``Company.fin_year_start_month``/
    ``fin_year_start_day`` (services/companies.py, validated 1-12 / 1-31;
    migration 0214) anchor its year elsewhere -- e.g. the UK's 6 April.

    A ``fin_year_start_day`` that does not exist in the anchor month for a
    given calendar year (e.g. day 31 anchored in February, or day 29-31 in
    any short month) clamps to that month's actual last day via
    ``calendar.monthrange`` -- there is no other established clamp
    convention anywhere in the codebase to follow (migration 0214 is
    purely additive/storage-only; nothing does date arithmetic with
    ``fin_year_start_day`` yet), so this keeps the anchor a real date
    every year rather than overflowing into the next month.

    ``fy_end`` is always the day before the following year's anchor date,
    so consecutive financial years never overlap or gap.
    """

    def _anchor(year: int) -> date:
        last_day_of_month = calendar.monthrange(year, fin_year_start_month)[1]
        day = min(fin_year_start_day, last_day_of_month)
        return date(year, fin_year_start_month, day)

    this_year_anchor = _anchor(as_of.year)
    if as_of >= this_year_anchor:
        fy_start = this_year_anchor
        fy_end = _anchor(as_of.year + 1) - timedelta(days=1)
    else:
        fy_start = _anchor(as_of.year - 1)
        fy_end = this_year_anchor - timedelta(days=1)
    return fy_start, fy_end


def merge_comparative_lines(
    current_lines: list[dict[str, Any]],
    comparative_lines: list[dict[str, Any]],
    *,
    value_key: str,
    zero_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Attach a ``comparative`` value onto each current-period line.

    Lines are matched by ``account_id``. ``value_key`` names the numeric
    field to pull the comparative figure from (``"amount"`` for P&L lines,
    ``"balance"`` for balance-sheet/trial-balance lines) -- every other key
    on the current line is passed through unchanged.

    An account with activity ONLY in the comparative period (no current-period
    line) still needs to surface, or the comparison silently drops it -- so
    those are appended with their current-period value defaulted to 0. This
    mirrors the account-alignment approach in the statement pack's private
    ``_merge_lines`` helper (api/v1/reports.py) without reusing it directly:
    that helper collapses both periods into a ``current_amount``/
    ``prior_amount`` pair, a different response shape than the JSON reports'
    existing ``amount``/``balance`` field plus an additive ``comparative``
    key -- the shape that keeps the opt-in JSON response backward compatible
    when ``compare`` is omitted.

    ``zero_keys`` (default ``[value_key]``) lists every numeric field that
    must be zeroed to a genuine current-period 0 on a comparative-only
    synthetic line. The trial balance has THREE numeric fields per line
    (``debit_total``/``credit_total``/``balance``) -- zeroing only
    ``balance`` would leave the comparative period's debit/credit totals
    misrepresented as the current period's, so its caller passes all three.
    P&L/BS lines have a single numeric field and can rely on the default.

    Comparative-only synthetic lines are appended after every current-period
    line, which would otherwise break the ``Account.code`` ordering the raw
    queries already produce (``ORDER BY Account.code``) -- so the merged
    result is re-sorted by ``code`` before returning, restoring that
    invariant for callers/consumers that rely on code order (e.g. CSV
    export, statement rendering).
    """
    zero_keys = zero_keys if zero_keys is not None else [value_key]
    comparative_by_id: dict[Any, float] = {
        ln["account_id"]: float(ln[value_key])
        for ln in comparative_lines
        if ln.get("account_id") is not None
    }
    merged: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    for line in current_lines:
        aid = line.get("account_id")
        seen_ids.add(aid)
        merged.append({**line, "comparative": comparative_by_id.get(aid, 0.0)})
    for ln in comparative_lines:
        aid = ln.get("account_id")
        if aid is None or aid in seen_ids:
            continue
        synthetic = {k: v for k, v in ln.items() if k != value_key}
        synthetic.update({k: 0.0 for k in zero_keys if k != value_key})
        synthetic[value_key] = 0.0
        synthetic["comparative"] = float(ln[value_key])
        merged.append(synthetic)
    merged.sort(key=lambda ln: ln.get("code") or "")
    return merged


@dataclass
class AccountBalance:
    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")

    @property
    def balance(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class ReportSection:
    label: str
    account_type: AccountType
    rows: list[AccountBalance] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((r.debit for r in self.rows), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), Decimal("0"))

    @property
    def total_balance(self) -> Decimal:
        return sum((r.balance for r in self.rows), Decimal("0"))


async def trial_balance(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> list[ReportSection]:
    """Trial balance: sum of debits and credits per account for posted entries."""
    balances = await _account_balances(session, company_id, as_of=as_of)
    return _group_balances(balances)


async def profit_and_loss(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[ReportSection], Decimal]:
    """P&L: income - expenses for a period. Returns (sections, net_profit)."""
    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=to_date
    )
    pnl = [b for b in balances if b.account_type in PNL_TYPES]
    sections = _group_balances(pnl)

    income = sum(
        (s.total_balance for s in sections
         if s.account_type in {AccountType.INCOME, AccountType.OTHER_INCOME}),
        Decimal("0"),
    )
    expenses = sum(
        (s.total_balance for s in sections
         if s.account_type in {
             AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE
         }),
        Decimal("0"),
    )
    # Income is credit-normal (negative balance), expenses debit-normal (positive)
    net_profit = -income - expenses
    return sections, net_profit


async def balance_sheet(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_of: date | None = None,
    tenant_id: uuid.UUID | None = None,
) -> tuple[list[ReportSection], Decimal]:
    """Balance sheet: assets, liabilities, equity. Returns (sections, net_assets).

    Synthesises a "Current Year Earnings" line under Equity for any
    un-closed P&L balances, matching Xero/MYOB/QBO behaviour for open
    periods.  The synthetic line is never zero-suppressed — it is always
    present so accountants know the period has not been formally closed.
    """
    balances = await _account_balances(session, company_id, as_of=as_of, tenant_id=tenant_id)
    bs = [b for b in balances if b.account_type in BALANCE_SHEET_TYPES]
    sections = _group_balances(bs)

    assets = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.ASSET),
        Decimal("0"),
    )
    liabilities = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.LIABILITY),
        Decimal("0"),
    )
    equity = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.EQUITY),
        Decimal("0"),
    )

    # --- Current Year Earnings (synthetic) -----------------------------------
    # Sum all INCOME/OTHER_INCOME and EXPENSE/COST_OF_SALES/OTHER_EXPENSE
    # balances up to as_of.  Income accounts are credit-normal (negative
    # balance in our debit-minus-credit model); expenses are debit-normal
    # (positive).  Net = -income_balance - expense_balance → positive when
    # income exceeds expenses.
    pnl_balances = [b for b in balances if b.account_type in PNL_TYPES]
    income_sum = sum(
        b.balance for b in pnl_balances
        if b.account_type in {AccountType.INCOME, AccountType.OTHER_INCOME}
    )
    expense_sum = sum(
        b.balance for b in pnl_balances
        if b.account_type in {
            AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE
        }
    )
    # net_income > 0 means profitable; in equity terms it is credit-normal so
    # it REDUCES the debit-minus-credit result (balance is negative on the BS).
    net_income = -income_sum - expense_sum

    # Inject a synthetic AccountBalance into (or append to) the EQUITY section.
    cye_row = AccountBalance(
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        code="CYE",
        name="Current Year Earnings",
        account_type=AccountType.EQUITY,
        debit=Decimal("0") if net_income >= 0 else -net_income,
        credit=net_income if net_income >= 0 else Decimal("0"),
    )
    # Find or create the EQUITY section and append the synthetic row.
    equity_section = next(
        (s for s in sections if s.account_type == AccountType.EQUITY), None
    )
    if equity_section is None:
        equity_section = ReportSection(
            label="Equity", account_type=AccountType.EQUITY, rows=[]
        )
        sections.append(equity_section)
    equity_section.rows.append(cye_row)

    # Recompute totals including CYE.
    equity = sum(
        (s.total_balance for s in sections if s.account_type == AccountType.EQUITY),
        Decimal("0"),
    )

    net_assets = assets + liabilities + equity  # liabilities are credit-normal (negative)
    return sections, net_assets


async def _account_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    as_of: date | None = None,
    tenant_id: uuid.UUID | None = None,
) -> list[AccountBalance]:
    """Aggregate posted journal lines into per-account debit/credit totals.

    ``tenant_id`` scopes to a single tenant when provided.  Without it
    the query spans all tenants (appropriate for single-tenant dev; the
    HTML balance-sheet route does not yet have a tenant in its request
    context, but the JSON API route always passes one).
    """
    # Build filters
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status.in_(REPORTABLE_STATUSES),
    ]
    if tenant_id is not None:
        conditions.append(JournalEntry.tenant_id == tenant_id)
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date or as_of:
        conditions.append(JournalEntry.entry_date <= (to_date or as_of))

    stmt = (
        select(
            JournalLine.account_id,
            Account.code,
            Account.name,
            Account.account_type,
            JournalLine.debit,
            JournalLine.credit,
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(and_(*conditions))
    )

    result = await session.execute(stmt)

    totals: dict[uuid.UUID, AccountBalance] = {}
    for row in result.all():
        acct_id = row[0]
        if acct_id not in totals:
            totals[acct_id] = AccountBalance(
                account_id=acct_id,
                code=row[1],
                name=row[2],
                account_type=row[3],
            )
        totals[acct_id].debit += row[4]
        totals[acct_id].credit += row[5]

    return sorted(totals.values(), key=lambda b: b.code)


TYPE_ORDER = [
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
    AccountType.COST_OF_SALES,
    AccountType.EXPENSE,
    AccountType.OTHER_EXPENSE,
]

TYPE_LABELS = {
    AccountType.ASSET: "Assets",
    AccountType.LIABILITY: "Liabilities",
    AccountType.EQUITY: "Equity",
    AccountType.INCOME: "Income",
    AccountType.OTHER_INCOME: "Other income",
    AccountType.COST_OF_SALES: "Cost of sales",
    AccountType.EXPENSE: "Expenses",
    AccountType.OTHER_EXPENSE: "Other expense",
}


def _group_balances(balances: list[AccountBalance]) -> list[ReportSection]:
    by_type: dict[AccountType, list[AccountBalance]] = defaultdict(list)
    for b in balances:
        if b.debit != Decimal("0") or b.credit != Decimal("0"):
            by_type[b.account_type].append(b)

    sections = []
    for t in TYPE_ORDER:
        if rows := by_type.get(t):
            sections.append(ReportSection(
                label=TYPE_LABELS.get(t, t.value),
                account_type=t,
                rows=rows,
            ))
    return sections


# ---------------------------------------------------------------------- #
# Aged AR (debtors)                                                       #
# ---------------------------------------------------------------------- #

# Day boundaries used for bucketing.  A balance with age==0 (due today)
# sits in ``current``; age 1..30 sits in ``d1_30``; 31..60 in
# ``d31_60``; 61..90 in ``d61_90``; 91+ in ``d90_plus``.  Negative ages
# (due in the future) also go in ``current`` because they haven't
# broken terms yet.
BUCKET_KEYS = ("current", "d1_30", "d31_60", "d61_90", "d90_plus")
BUCKET_LABELS = {
    "current": "Current",
    "d1_30": "1-30",
    "d31_60": "31-60",
    "d61_90": "61-90",
    "d90_plus": "90+",
}


def _bucket_for_age(days_overdue: int) -> str:
    """Return the bucket key for a given days-overdue integer.

    Boundaries are inclusive on the upper edge so exactly 30 days
    overdue lands in ``d1_30`` and exactly 60 in ``d31_60`` — matches
    Xero/QBO convention.
    """
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "d1_30"
    if days_overdue <= 60:
        return "d31_60"
    if days_overdue <= 90:
        return "d61_90"
    return "d90_plus"


@dataclass
class AgedInvoiceRow:
    """One POSTED, unpaid (or partially paid) invoice on the aged report."""

    invoice_id: uuid.UUID
    number: str
    issue_date: date
    due_date: date
    total: Decimal
    amount_paid: Decimal
    days_overdue: int

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.amount_paid

    @property
    def bucket(self) -> str:
        return _bucket_for_age(self.days_overdue)


@dataclass
class AgedContactGroup:
    """All aged invoices for one contact, pre-summed into buckets."""

    contact_id: uuid.UUID
    contact_name: str
    invoices: list[AgedInvoiceRow] = field(default_factory=list)
    buckets: dict[str, Decimal] = field(
        default_factory=lambda: {k: Decimal("0") for k in BUCKET_KEYS}
    )

    @property
    def total(self) -> Decimal:
        return sum(self.buckets.values(), Decimal("0"))


@dataclass
class AgedReport:
    """The full aged report — group per contact plus grand totals.

    ``grand_totals`` reflects Trade Debtors only (excluding retentions).
    ``retentions_grand_totals`` carries the Retentions Receivable balance
    split by bucket; all zeros when the report has no retention lines.
    """

    as_at: date
    groups: list[AgedContactGroup] = field(default_factory=list)
    grand_totals: dict[str, Decimal] = field(
        default_factory=lambda: {k: Decimal("0") for k in BUCKET_KEYS}
    )
    retentions_grand_totals: dict[str, Decimal] = field(
        default_factory=lambda: {k: Decimal("0") for k in BUCKET_KEYS}
    )

    @property
    def grand_total(self) -> Decimal:
        return sum(self.grand_totals.values(), Decimal("0"))

    @property
    def retentions_grand_total(self) -> Decimal:
        return sum(self.retentions_grand_totals.values(), Decimal("0"))


async def _invoice_settled_asof(
    session: AsyncSession,
    invoice_ids: list[uuid.UUID],
    cutoff: date,
) -> dict[uuid.UUID, Decimal]:
    """Per-invoice settled amount AS AT ``cutoff`` (point-in-time).

    Unlike the scalar ``Invoice.amount_paid`` (which reflects the *current*
    settled total regardless of date), this sums only the relief that had
    actually occurred on or before ``cutoff``:

    * POSTED payment allocations whose parent ``Payment.payment_date <=
      cutoff``; plus
    * the unallocated portion of POSTED credit notes linked via
      ``original_invoice_id`` whose ``issue_date <= cutoff``.

    A payment or credit note dated *after* the report's as-of date does not
    reduce the outstanding balance — matching the date-aware AR control on
    the balance sheet (GL ``entry_date <= as_of``).
    """
    settled: dict[uuid.UUID, Decimal] = {iid: Decimal("0") for iid in invoice_ids}
    if not invoice_ids:
        return settled

    pay_rows = await session.execute(
        select(
            PaymentAllocation.invoice_id,
            func.coalesce(func.sum(PaymentAllocation.amount), 0),
        )
        .join(Payment, PaymentAllocation.payment_id == Payment.id)
        .where(
            PaymentAllocation.invoice_id.in_(invoice_ids),
            Payment.status == PaymentStatus.POSTED,
            Payment.payment_date <= cutoff,
        )
        .group_by(PaymentAllocation.invoice_id)
    )
    for inv_id, amt in pay_rows.all():
        settled[inv_id] += Decimal(str(amt or 0))

    cn_rows = await session.execute(
        select(
            CreditNote.original_invoice_id,
            func.coalesce(
                func.sum(CreditNote.total - CreditNote.amount_allocated), 0
            ),
        )
        .where(
            CreditNote.original_invoice_id.in_(invoice_ids),
            CreditNote.status == CreditNoteStatus.POSTED,
            CreditNote.issue_date <= cutoff,
        )
        .group_by(CreditNote.original_invoice_id)
    )
    for inv_id, amt in cn_rows.all():
        settled[inv_id] += Decimal(str(amt or 0))

    return settled


async def _bill_settled_asof(
    session: AsyncSession,
    bill_ids: list[uuid.UUID],
    cutoff: date,
) -> dict[uuid.UUID, Decimal]:
    """Per-bill settled amount AS AT ``cutoff`` (point-in-time).

    Sums:

    * POSTED payment allocations whose parent ``Payment.payment_date <=
      cutoff``; plus
    * the FULL total of POSTED supplier credit notes linked via
      ``original_bill_id`` whose ``issue_date <= cutoff``. Unlike the
      AR-side ``CreditNote`` (which nets ``total - amount_allocated``
      because it may be cash-refunded), ``SupplierCreditNote`` has no
      ``amount_allocated``/cash-refund path in this codebase (no
      ``payment_allocations.supplier_credit_note_id`` column), so its
      whole total always relieves the linked bill.

    A payment or credit note dated *after* the report's as-of date does
    not reduce the outstanding balance — matching the date-aware AP
    control on the balance sheet (GL ``entry_date <= as_of``).
    """
    settled: dict[uuid.UUID, Decimal] = {bid: Decimal("0") for bid in bill_ids}
    if not bill_ids:
        return settled

    pay_rows = await session.execute(
        select(
            PaymentAllocation.bill_id,
            func.coalesce(func.sum(PaymentAllocation.amount), 0),
        )
        .join(Payment, PaymentAllocation.payment_id == Payment.id)
        .where(
            PaymentAllocation.bill_id.in_(bill_ids),
            Payment.status == PaymentStatus.POSTED,
            Payment.payment_date <= cutoff,
        )
        .group_by(PaymentAllocation.bill_id)
    )
    for bill_id, amt in pay_rows.all():
        settled[bill_id] += Decimal(str(amt or 0))

    cn_rows = await session.execute(
        select(
            SupplierCreditNote.original_bill_id,
            func.coalesce(func.sum(SupplierCreditNote.total), 0),
        )
        .where(
            SupplierCreditNote.original_bill_id.in_(bill_ids),
            SupplierCreditNote.status == SupplierCreditNoteStatus.POSTED,
            SupplierCreditNote.issue_date <= cutoff,
        )
        .group_by(SupplierCreditNote.original_bill_id)
    )
    for bill_id, amt in cn_rows.all():
        settled[bill_id] += Decimal(str(amt or 0))

    return settled


async def _writeoff_date_by_invoice(
    session: AsyncSession,
    invoice_ids: list[uuid.UUID],
) -> dict[uuid.UUID, date]:
    """Effective write-off date per WRITTEN_OFF invoice (point-in-time).

    A bad-debt write-off relieves the invoice via its
    ``write_off_journal_entry_id``; the write-off's *effective date* is that
    journal entry's ``entry_date``. Aged AR uses this so a write-off dated
    in the future (e.g. a year-end 30-Jun write-off booked early) does NOT
    drop the invoice from receivables until that date arrives — matching the
    date-aware Trade Debtors control on the balance sheet. Invoices whose
    ``write_off_journal_entry_id`` is NULL (legacy status-only write-offs
    with no backing JE) are absent from the result and treated as already
    off-ledger, preserving prior behaviour.
    """
    out: dict[uuid.UUID, date] = {}
    if not invoice_ids:
        return out
    rows = await session.execute(
        select(Invoice.id, JournalEntry.entry_date)
        .join(JournalEntry, Invoice.write_off_journal_entry_id == JournalEntry.id)
        .where(
            Invoice.id.in_(invoice_ids),
            Invoice.write_off_journal_entry_id.is_not(None),
        )
    )
    for inv_id, wod in rows.all():
        out[inv_id] = wod
    return out


async def aged_ar(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_at: date | None = None,
    tenant_id: uuid.UUID | None = None,
) -> AgedReport:
    """Return the aged-debtors report as at ``as_at`` (default today).

    POINT-IN-TIME: walks POSTED (and future-dated-write-off WRITTEN_OFF),
    non-archived invoices with ``issue_date <= as_at`` and keeps those still
    outstanding *as at the cutoff*. Settlement is computed date-aware (see
    ``_invoice_settled_asof``) rather than from the scalar
    ``Invoice.amount_paid``, so a payment or credit note dated after the
    report date does not reduce the outstanding balance. A WRITTEN_OFF
    invoice whose bad-debt write-off is dated AFTER ``as_at`` (e.g. a
    year-end 30-Jun write-off booked early) is still receivable as at the
    cutoff and IS included until the write-off date; once the write-off
    date is reached it drops out — matching the date-aware Trade Debtors
    control on the balance sheet. Voided and archived invoices are excluded.

    ``tenant_id`` is defense-in-depth, optional (mirrors
    ``_account_balances``): ``company_id`` is already tenant-scoped by
    callers going through ``get_active_company_id``, so this narrows the
    query further rather than being load-bearing on its own.
    """
    cutoff = as_at or date.today()
    # Do NOT pre-filter on the scalar ``total > amount_paid`` — that would
    # wrongly drop an invoice that is only settled by a *future* credit note
    # or payment. Include WRITTEN_OFF invoices too, then exclude below the
    # ones whose write-off is already effective as at the cutoff. Fetch all
    # candidates, compute point-in-time outstanding, keep those > 0.
    conditions = [
        Invoice.company_id == company_id,
        Invoice.status.in_(
            (InvoiceStatus.POSTED, InvoiceStatus.WRITTEN_OFF)
        ),
        Invoice.archived_at.is_(None),
        Invoice.issue_date <= cutoff,
    ]
    if tenant_id is not None:
        conditions.append(Invoice.tenant_id == tenant_id)
    stmt = (
        select(Invoice, Contact.name)
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(*conditions)
        .order_by(Contact.name, Invoice.due_date)
    )
    candidate_rows = (await session.execute(stmt)).all()
    candidate_ids = [inv.id for inv, _ in candidate_rows]

    settled_by_invoice = await _invoice_settled_asof(
        session, candidate_ids, cutoff
    )
    # Effective write-off date per WRITTEN_OFF invoice: a write-off dated
    # > cutoff has not happened yet, so the invoice is still receivable.
    writeoff_date = await _writeoff_date_by_invoice(session, candidate_ids)
    outstanding_by_invoice: dict[uuid.UUID, Decimal] = {}
    rows: list[tuple[Invoice, str]] = []
    for inv, contact_name in candidate_rows:
        if inv.status == InvoiceStatus.WRITTEN_OFF:
            wod = writeoff_date.get(inv.id)
            # No backing write-off JE (legacy) or already written off as at
            # the cutoff → off-ledger, exclude. Only a future write-off keeps
            # the invoice open.
            if wod is None or wod <= cutoff:
                continue
        settled = settled_by_invoice.get(inv.id, Decimal("0"))
        outstanding = inv.total - settled
        if outstanding < Decimal("0"):
            outstanding = Decimal("0")
        if outstanding > Decimal("0"):
            outstanding_by_invoice[inv.id] = outstanding
            rows.append((inv, contact_name))

    # Fetch per-invoice retention amounts so Trade Debtors and
    # Retentions Receivable can be reported as separate lines.
    invoice_ids = [inv.id for inv, _ in rows]
    retention_by_invoice: dict[uuid.UUID, Decimal] = {}
    if invoice_ids:
        ret_stmt = (
            select(
                InvoiceLine.invoice_id,
                func.sum(
                    InvoiceLine.line_subtotal * InvoiceLine.retention_pct / Decimal("100")
                ).label("retention_amount"),
            )
            .where(
                InvoiceLine.invoice_id.in_(invoice_ids),
                InvoiceLine.retention_pct > Decimal("0"),
            )
            .group_by(InvoiceLine.invoice_id)
        )
        for inv_id, amt in (await session.execute(ret_stmt)).all():
            if amt and amt > Decimal("0"):
                retention_by_invoice[inv_id] = amt

    groups: dict[uuid.UUID, AgedContactGroup] = {}
    report = AgedReport(as_at=cutoff)

    for inv, contact_name in rows:
        days_overdue = (cutoff - inv.due_date).days
        outstanding = outstanding_by_invoice[inv.id]
        ret_amt = retention_by_invoice.get(inv.id, Decimal("0"))
        # Payments reduce Trade Debtors first; retentions are last to clear.
        ret_outstanding = min(ret_amt, outstanding)
        trade_outstanding = outstanding - ret_outstanding

        row = AgedInvoiceRow(
            invoice_id=inv.id,
            number=inv.number or "(draft)",
            issue_date=inv.issue_date,
            due_date=inv.due_date,
            total=inv.total,
            # Point-in-time settled amount (total - outstanding), NOT the
            # scalar inv.amount_paid which may include future settlements.
            amount_paid=inv.total - outstanding,
            days_overdue=days_overdue,
        )
        group = groups.get(inv.contact_id)
        if group is None:
            group = AgedContactGroup(
                contact_id=inv.contact_id,
                contact_name=contact_name,
            )
            groups[inv.contact_id] = group
        group.invoices.append(row)
        # Buckets show trade-debtor portion only; retentions go to grand totals.
        group.buckets[row.bucket] += trade_outstanding
        if ret_outstanding > Decimal("0"):
            report.retentions_grand_totals[row.bucket] += ret_outstanding

    # Sort groups by descending total so the biggest debtors are on top.
    report.groups = sorted(
        groups.values(), key=lambda g: g.total, reverse=True
    )
    for group in report.groups:
        for key in BUCKET_KEYS:
            report.grand_totals[key] += group.buckets[key]
    return report


# Leading characters that a spreadsheet (Excel/Sheets/LibreOffice) treats
# as a formula/function trigger when opening a CSV -- ``=``, ``+``, and
# ``@`` always; a leading ``-`` is ambiguous (a legitimate negative number
# vs. e.g. ``-2+3+cmd|...``) but Excel also treats it as a formula trigger
# on TEXT cells, so it's included here too. Applied ONLY to free-text
# cells (contact/account names) below -- never to numeric columns, whose
# negatives legitimately start with "-" and must NOT be quote-prefixed.
_CSV_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def _csv_sanitize_text(value: str) -> str:
    """Prefix a leading single quote onto a free-text CSV cell that starts
    with a spreadsheet formula-trigger character, so opening the export in
    Excel/Sheets/LibreOffice renders it as literal text rather than
    evaluating it as a formula (CSV/CWE-1236 formula-injection guard).
    """
    if value and value[0] in _CSV_FORMULA_TRIGGER_CHARS:
        return f"'{value}"
    return value


def aged_ar_csv(report: AgedReport) -> str:
    """Render an aged-AR report as RFC 4180 CSV (one row per invoice).

    Columns: ``contact,invoice_number,issue_date,due_date,total,paid,
    balance_due,days_overdue,bucket``. A footer row per contact + a
    grand-total footer row would break spreadsheet pivots, so we emit
    only detail rows; users can pivot in their tool of choice.

    ``balance_due`` is the invoice's FULL outstanding balance, i.e.
    trade-debtor portion + any retention (see ``aged_ar``'s per-invoice
    ``AgedInvoiceRow``, which does not split retention out the way the
    JSON ``/aged_receivables`` route's ``_build_report`` does). Summing
    this column therefore exceeds the JSON route's
    ``totals.total``/trade-debtors-only figure by the retention amount
    whenever any invoice carries ``retention_pct > 0`` -- this is not a
    bug, just a different (document-level, not ledger-line-level) view.
    """
    import csv
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "contact",
            "invoice_number",
            "issue_date",
            "due_date",
            "total",
            "paid",
            "balance_due",
            "days_overdue",
            "bucket",
        ]
    )
    for group in report.groups:
        for inv in group.invoices:
            writer.writerow(
                [
                    _csv_sanitize_text(group.contact_name),
                    inv.number,
                    inv.issue_date.isoformat(),
                    inv.due_date.isoformat(),
                    f"{inv.total:.2f}",
                    f"{inv.amount_paid:.2f}",
                    f"{inv.balance_due:.2f}",
                    inv.days_overdue,
                    BUCKET_LABELS[inv.bucket],
                ]
            )
    return buf.getvalue()


# ---------------------------------------------------------------------- #
# Aged AP (creditors)                                                     #
# ---------------------------------------------------------------------- #
#
# Symmetric to Aged AR. Walks POSTED, non-archived bills with a
# balance_due > 0 (i.e. ``total > amount_paid``). Issued-date filter is
# ``issue_date <= as_at`` so future-dated bills don't appear. Voided
# and archived bills are excluded. Bucketing code
# (``BUCKET_KEYS``/``_bucket_for_age``) and the ``AgedInvoiceRow``/
# ``AgedContactGroup``/``AgedReport`` dataclasses are re-used verbatim
# — an "invoice number" on an AP report is just the bill number.


async def aged_ap(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_at: date | None = None,
    tenant_id: uuid.UUID | None = None,
) -> AgedReport:
    """Return the aged-creditors report as at ``as_at`` (default today).

    POINT-IN-TIME: settlement is computed date-aware (see
    ``_bill_settled_asof``) so a payment or supplier credit note dated
    after the report date does not reduce the outstanding balance.

    ``tenant_id`` is defense-in-depth, optional (mirrors ``aged_ar`` /
    ``_account_balances``) — see ``aged_ar``'s docstring.
    """
    cutoff = as_at or date.today()
    # As with AR, do NOT pre-filter on the scalar ``total > amount_paid`` —
    # a future-dated payment must not drop a bill from the as-of report.
    conditions = [
        Bill.company_id == company_id,
        Bill.status == BillStatus.POSTED,
        Bill.archived_at.is_(None),
        Bill.issue_date <= cutoff,
    ]
    if tenant_id is not None:
        conditions.append(Bill.tenant_id == tenant_id)
    stmt = (
        select(Bill, Contact.name)
        .join(Contact, Bill.contact_id == Contact.id)
        .where(*conditions)
        .order_by(Contact.name, Bill.due_date)
    )
    candidate_rows = (await session.execute(stmt)).all()

    settled_by_bill = await _bill_settled_asof(
        session, [bill.id for bill, _ in candidate_rows], cutoff
    )

    groups: dict[uuid.UUID, AgedContactGroup] = {}
    for bill, contact_name in candidate_rows:
        settled = settled_by_bill.get(bill.id, Decimal("0"))
        outstanding = bill.total - settled
        if outstanding < Decimal("0"):
            outstanding = Decimal("0")
        if outstanding <= Decimal("0"):
            continue
        days_overdue = (cutoff - bill.due_date).days
        row = AgedInvoiceRow(
            invoice_id=bill.id,
            number=bill.number or "(draft)",
            issue_date=bill.issue_date,
            due_date=bill.due_date,
            total=bill.total,
            # Point-in-time settled amount, NOT the scalar bill.amount_paid.
            amount_paid=bill.total - outstanding,
            days_overdue=days_overdue,
        )
        group = groups.get(bill.contact_id)
        if group is None:
            group = AgedContactGroup(
                contact_id=bill.contact_id,
                contact_name=contact_name,
            )
            groups[bill.contact_id] = group
        group.invoices.append(row)
        group.buckets[row.bucket] += row.balance_due

    report = AgedReport(as_at=cutoff)
    # Sort groups by descending total so the biggest creditors are on top.
    report.groups = sorted(
        groups.values(), key=lambda g: g.total, reverse=True
    )
    for group in report.groups:
        for key in BUCKET_KEYS:
            report.grand_totals[key] += group.buckets[key]
    return report


def aged_ap_csv(report: AgedReport) -> str:
    """Render an aged-AP report as RFC 4180 CSV (one row per bill).

    Columns: ``contact,bill_number,issue_date,due_date,total,paid,
    balance_due,days_overdue,bucket``.

    ``balance_due`` is the bill's FULL outstanding balance, i.e.
    ``bill.total - amount_paid``, including any retention held (see
    ``services/bills.py``'s Retentions Payable posting, which moves the
    retained portion off Trade Creditors onto a separate liability
    account at post time). Unlike ``aged_ar_csv``, there is no separate
    JSON breakout to diverge from here -- the JSON ``/aged_payables``
    route sums the same full document balance (no retention split), so
    this column agrees with the JSON totals either way. Informational
    only.
    """
    import csv
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "contact",
            "bill_number",
            "issue_date",
            "due_date",
            "total",
            "paid",
            "balance_due",
            "days_overdue",
            "bucket",
        ]
    )
    for group in report.groups:
        for bill in group.invoices:
            writer.writerow(
                [
                    _csv_sanitize_text(group.contact_name),
                    bill.number,
                    bill.issue_date.isoformat(),
                    bill.due_date.isoformat(),
                    f"{bill.total:.2f}",
                    f"{bill.amount_paid:.2f}",
                    f"{bill.balance_due:.2f}",
                    bill.days_overdue,
                    BUCKET_LABELS[bill.bucket],
                ]
            )
    return buf.getvalue()


# ---------------------------------------------------------------------- #
# P&L by segment (project for v1; contact segment needs                   #
# JournalEntry.contact_id which lands in a later batch)                   #
# ---------------------------------------------------------------------- #


@dataclass
class SegmentRow:
    """One segment's slice of the P&L (e.g. one project's P&L)."""

    segment_id: uuid.UUID | None  # None == "Unassigned"
    segment_label: str
    sections: list[ReportSection] = field(default_factory=list)
    net_profit: Decimal = Decimal("0")


_VALID_SEGMENTS = frozenset({"project", "department", "cost_centre"})


async def pl_by_segment(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    segment: str = "project",
) -> list[SegmentRow]:
    """P&L grouped by segment tag.

    Supported values for ``segment``: ``"project"``, ``"department"``,
    ``"cost_centre"``.  Lines without the relevant tag land in an
    "Unassigned" bucket so the grand-total reconciles with
    :func:`profit_and_loss` for the same window.
    """
    if segment not in _VALID_SEGMENTS:
        raise ValueError(
            f"Unsupported segment {segment!r}; valid values: "
            f"{sorted(_VALID_SEGMENTS)}"
        )

    # Map segment name → the JournalLine column to group by.
    seg_col = {
        "project": JournalLine.project_id,
        "department": JournalLine.department_id,
        "cost_centre": JournalLine.cost_centre_id,
    }[segment]

    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status.in_(REPORTABLE_STATUSES),
    ]
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date:
        conditions.append(JournalEntry.entry_date <= to_date)

    stmt = (
        select(
            seg_col,
            JournalLine.account_id,
            Account.code,
            Account.name,
            Account.account_type,
            JournalLine.debit,
            JournalLine.credit,
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(and_(*conditions), Account.account_type.in_(PNL_TYPES))
    )
    result = await session.execute(stmt)

    # segment_id -> account_id -> AccountBalance
    per_segment: dict[uuid.UUID | None, dict[uuid.UUID, AccountBalance]] = (
        defaultdict(dict)
    )
    for row in result.all():
        seg_id, acct_id, code, name, acct_type, debit, credit = row
        bucket = per_segment[seg_id]
        if acct_id not in bucket:
            bucket[acct_id] = AccountBalance(
                account_id=acct_id,
                code=code,
                name=name,
                account_type=acct_type,
            )
        bucket[acct_id].debit += debit
        bucket[acct_id].credit += credit

    # Resolve dimension labels up front. ``None`` stays "Unassigned".
    dim_ids = {sid for sid in per_segment if sid is not None}
    labels: dict[uuid.UUID, str] = {}
    if dim_ids:
        if segment == "project":
            lbl_stmt = select(Project.id, Project.code, Project.name).where(
                Project.id.in_(dim_ids)
            )
            for pid, pcode, pname in (await session.execute(lbl_stmt)).all():
                labels[pid] = f"{pcode} — {pname}"
        elif segment == "department":
            lbl_stmt = select(Department.id, Department.code, Department.name).where(
                Department.id.in_(dim_ids)
            )
            for did, dcode, dname in (await session.execute(lbl_stmt)).all():
                labels[did] = f"{dcode} — {dname}"
        else:  # cost_centre
            lbl_stmt = select(CostCentre.id, CostCentre.code, CostCentre.name).where(
                CostCentre.id.in_(dim_ids)
            )
            for cid, ccode, cname in (await session.execute(lbl_stmt)).all():
                labels[cid] = f"{ccode} — {cname}"

    rows: list[SegmentRow] = []
    for seg_id, bucket in per_segment.items():
        sorted_balances = sorted(bucket.values(), key=lambda b: b.code)
        sections = _group_balances(sorted_balances)
        income = sum(
            (s.total_balance for s in sections
             if s.account_type in {
                 AccountType.INCOME, AccountType.OTHER_INCOME,
             }),
            Decimal("0"),
        )
        expenses = sum(
            (s.total_balance for s in sections
             if s.account_type in {
                 AccountType.EXPENSE,
                 AccountType.COST_OF_SALES,
                 AccountType.OTHER_EXPENSE,
             }),
            Decimal("0"),
        )
        net_profit = -income - expenses
        label = labels.get(seg_id, "Unassigned") if seg_id else "Unassigned"
        rows.append(
            SegmentRow(
                segment_id=seg_id,
                segment_label=label,
                sections=sections,
                net_profit=net_profit,
            )
        )

    # "Unassigned" last; otherwise alphabetical by label.
    rows.sort(key=lambda r: (r.segment_id is None, r.segment_label))
    return rows


# ---------------------------------------------------------------------- #
# Budget vs actual                                                        #
# ---------------------------------------------------------------------- #


@dataclass
class BudgetVsActualRow:
    """One account's 12-month budget-vs-actual comparison for a year.

    Amounts are stored as the account's **natural positive sign** —
    income reads as ``credit - debit`` so budgeted $1,000 sales and
    actual $1,000 sales both come out as ``+1000``; expenses read as
    ``debit - credit`` for the same reason. A positive ``variance``
    means actual exceeded budget (good for income, bad for expenses —
    the UI colours accordingly).
    """

    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: AccountType
    budget_monthly: list[Decimal] = field(
        default_factory=lambda: [Decimal("0")] * 12
    )
    actual_monthly: list[Decimal] = field(
        default_factory=lambda: [Decimal("0")] * 12
    )

    @property
    def budget_ytd(self) -> Decimal:
        return sum(self.budget_monthly, Decimal("0"))

    @property
    def actual_ytd(self) -> Decimal:
        return sum(self.actual_monthly, Decimal("0"))

    @property
    def variance_ytd(self) -> Decimal:
        return self.actual_ytd - self.budget_ytd

    @property
    def variance_monthly(self) -> list[Decimal]:
        return [
            self.actual_monthly[i] - self.budget_monthly[i]
            for i in range(12)
        ]


@dataclass
class BudgetVsActualReport:
    year: int
    rows: list[BudgetVsActualRow] = field(default_factory=list)

    def _sum_column(self, key: str) -> list[Decimal]:
        out = [Decimal("0")] * 12
        for row in self.rows:
            monthly = getattr(row, key)
            for i in range(12):
                out[i] += monthly[i]
        return out

    @property
    def budget_totals(self) -> list[Decimal]:
        return self._sum_column("budget_monthly")

    @property
    def actual_totals(self) -> list[Decimal]:
        return self._sum_column("actual_monthly")

    @property
    def variance_totals(self) -> list[Decimal]:
        return [
            self.actual_totals[i] - self.budget_totals[i]
            for i in range(12)
        ]


async def budget_vs_actual(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    year: int,
) -> BudgetVsActualReport:
    """Compare budgeted amounts to POSTED actuals per P&L account.

    Returns one row per account that has either a budget or an actual
    in ``year``. The whole thing is a single-pass aggregation — the
    UI can layer on its own sort order.
    """
    # Actuals — aggregate POSTED journal lines per (account, month).
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status.in_(REPORTABLE_STATUSES),
        extract("year", JournalEntry.entry_date) == year,
    ]
    month_expr = cast(extract("month", JournalEntry.entry_date), Integer)
    stmt = (
        select(
            JournalLine.account_id,
            Account.code,
            Account.name,
            Account.account_type,
            month_expr.label("month"),
            func.coalesce(func.sum(JournalLine.debit), 0).label("debit"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("credit"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(and_(*conditions), Account.account_type.in_(PNL_TYPES))
        .group_by(
            JournalLine.account_id,
            Account.code,
            Account.name,
            Account.account_type,
            month_expr,
        )
    )
    actual_rows = (await session.execute(stmt)).all()

    # Budgets — already per (account, year, month).
    budget_rows = (
        await session.execute(
            select(Budget).where(
                Budget.company_id == company_id,
                Budget.year == year,
            )
        )
    ).scalars().all()

    # Resolve (code, name, type) from whichever side provides the account.
    meta: dict[uuid.UUID, tuple[str, str, AccountType]] = {}
    for ar in actual_rows:
        meta[ar.account_id] = (ar.code, ar.name, ar.account_type)
    missing = {b.account_id for b in budget_rows} - set(meta)
    if missing:
        q = select(
            Account.id, Account.code, Account.name, Account.account_type
        ).where(Account.id.in_(missing))
        for aid, code, name, atype in (await session.execute(q)).all():
            meta[aid] = (code, name, atype)

    per_account_actuals: dict[uuid.UUID, list[Decimal]] = defaultdict(
        lambda: [Decimal("0")] * 12
    )
    per_account_budgets: dict[uuid.UUID, list[Decimal]] = defaultdict(
        lambda: [Decimal("0")] * 12
    )
    for ar in actual_rows:
        debit = Decimal(str(ar.debit or 0))
        credit = Decimal(str(ar.credit or 0))
        if ar.account_type in (
            AccountType.INCOME, AccountType.OTHER_INCOME,
        ):
            value = credit - debit  # credit-normal → positive
        else:
            value = debit - credit  # debit-normal → positive
        per_account_actuals[ar.account_id][int(ar.month) - 1] = value
    for b in budget_rows:
        per_account_budgets[b.account_id][b.month - 1] = b.amount

    account_ids = sorted(
        set(per_account_actuals) | set(per_account_budgets),
        key=lambda aid: meta.get(aid, ("", "", AccountType.EXPENSE))[0],
    )
    rows = [
        BudgetVsActualRow(
            account_id=aid,
            account_code=meta[aid][0],
            account_name=meta[aid][1],
            account_type=meta[aid][2],
            budget_monthly=per_account_budgets[aid],
            actual_monthly=per_account_actuals[aid],
        )
        for aid in account_ids
    ]
    return BudgetVsActualReport(year=year, rows=rows)


# ---------------------------------------------------------------------- #
# Cashflow forecast                                                       #
# ---------------------------------------------------------------------- #


@dataclass
class ForecastItem:
    """One projected cash event. ``amount`` is signed: +=inflow, -=outflow."""

    expected_date: date
    description: str
    source: str  # "invoice" | "bill" | "recurring"
    source_id: uuid.UUID
    amount: Decimal


@dataclass
class WeekBucket:
    """One 7-day slice of the horizon for the weekly roll-up."""

    start: date
    inflows: Decimal = Decimal("0")
    outflows: Decimal = Decimal("0")
    running_balance: Decimal = Decimal("0")

    @property
    def net(self) -> Decimal:
        return self.inflows - self.outflows


@dataclass
class CashflowForecast:
    """Full cash-flow forecast: items, weekly buckets, grand totals."""

    from_date: date
    to_date: date
    opening_balance: Decimal
    items: list[ForecastItem] = field(default_factory=list)
    weeks: list[WeekBucket] = field(default_factory=list)

    @property
    def total_inflows(self) -> Decimal:
        return sum(
            (i.amount for i in self.items if i.amount > 0), Decimal("0")
        )

    @property
    def total_outflows(self) -> Decimal:
        return -sum(
            (i.amount for i in self.items if i.amount < 0), Decimal("0")
        )

    @property
    def projected_closing(self) -> Decimal:
        return self.opening_balance + self.total_inflows - self.total_outflows


def _advance_by_frequency(
    current: date, frequency: str, anchor_day: int | None
) -> date:
    """Thin wrapper around ``services.recurrence.advance``.

    Imports lazily to avoid a circular import at module load (reports
    is imported by the dashboard, which is imported from main; the
    recurrence service imports invoices which imports journal which
    is a heavy leaf).
    """
    # `advance` wants a RecurrenceFrequency enum — look it up.
    from saebooks.models.recurring_invoice import RecurrenceFrequency
    from saebooks.services.recurrence import advance

    return advance(current, RecurrenceFrequency(frequency), anchor_day)


async def _recurring_line_total(
    session: AsyncSession,
    company_id: uuid.UUID,
    ln: RecurringInvoiceLine,
) -> Decimal:
    """One recurring-invoice line's GST-inclusive total.

    Delegates to ``services.invoices._resolve_tax_code`` /
    ``_compute_line_totals`` -- the exact same functions
    ``services.recurrence.materialise_one`` uses (via ``invoices.create_draft``
    -> ``_add_line``) to build the real invoice -- so a forecast item for a
    GST-registered recurring template agrees with what actually gets
    invoiced. Previously this projection summed
    ``qty * unit_price * (1 - discount%)`` only (ex-GST), understating a
    10%-GST recurring line by 10% versus the invoice/bill branches above
    (which use ``inv.total`` / ``bill.total`` -- both GST-inclusive).
    Imports lazily; see ``_advance_by_frequency`` for why reports.py avoids
    a module-level import of services.invoices.
    """
    from saebooks.services.invoices import (
        _compute_line_totals,
        _LineInput,
        _resolve_tax_code,
    )

    tax_code = await _resolve_tax_code(session, ln.tax_code_id, company_id)
    line_input = _LineInput(
        description=ln.description,
        account_id=ln.account_id,
        tax_code_id=ln.tax_code_id,
        quantity=ln.quantity,
        unit_price=ln.unit_price,
        discount_pct=ln.discount_pct,
        project_id=None,
        item_id=None,
    )
    _subtotal, _tax, total = _compute_line_totals(line_input, tax_code)
    return total


async def cashflow_forecast(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    horizon_days: int = 90,
    as_of: date | None = None,
    tenant_id: uuid.UUID | None = None,
) -> CashflowForecast:
    """Project cash in/out over the next ``horizon_days`` days.

    Three sources of projected movement:

    * Open POSTED invoices (``total > amount_paid``) → inflow on
      ``due_date``. Overdue invoices land on ``as_of`` so they show up
      in week-0 rather than vanishing into the past.
    * Open POSTED bills (same rule) → outflow on ``due_date``.
    * ACTIVE recurring-invoice templates → one inflow per materialisation
      at each ``next_run``, walking forward through the horizon. Totals
      are the GST-inclusive line totals (see ``_recurring_line_total``),
      matching what ``materialise_one`` would actually invoice.

    Opening balance = GL balance (debit - credit) of all ASSET accounts
    flagged ``reconcile=True`` through ``as_of``. This is the same sum
    the dashboard uses so the two agree.

    ``tenant_id`` is defense-in-depth, optional (mirrors ``aged_ar`` /
    ``_account_balances``) — see ``aged_ar``'s docstring.
    """
    as_of = as_of or date.today()
    horizon_end = as_of + timedelta(days=horizon_days)

    # Opening bank balance — same shape as dashboard.bank_balances total
    open_conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status.in_(REPORTABLE_STATUSES),
        JournalEntry.entry_date <= as_of,
        Account.account_type == AccountType.ASSET,
        Account.reconcile.is_(True),
    ]
    if tenant_id is not None:
        open_conditions.append(JournalEntry.tenant_id == tenant_id)
    open_stmt = (
        select(
            func.coalesce(
                func.sum(JournalLine.debit - JournalLine.credit),
                Decimal("0"),
            )
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(*open_conditions)
    )
    opening = (await session.execute(open_stmt)).scalar() or Decimal("0")
    opening = Decimal(str(opening))

    items: list[ForecastItem] = []

    # Open invoices → inflows
    inv_conditions = [
        Invoice.company_id == company_id,
        Invoice.status == InvoiceStatus.POSTED,
        Invoice.archived_at.is_(None),
        Invoice.total > Invoice.amount_paid,
        Invoice.due_date <= horizon_end,
    ]
    if tenant_id is not None:
        inv_conditions.append(Invoice.tenant_id == tenant_id)
    inv_stmt = (
        select(Invoice, Contact.name)
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(*inv_conditions)
    )
    for inv, cname in (await session.execute(inv_stmt)).all():
        due = max(inv.due_date, as_of)  # overdue → land on today
        items.append(
            ForecastItem(
                expected_date=due,
                description=f"Invoice {inv.number or '(draft)'} — {cname}",
                source="invoice",
                source_id=inv.id,
                amount=inv.total - inv.amount_paid,
            )
        )

    # Open bills → outflows
    bill_conditions = [
        Bill.company_id == company_id,
        Bill.status == BillStatus.POSTED,
        Bill.archived_at.is_(None),
        Bill.total > Bill.amount_paid,
        Bill.due_date <= horizon_end,
    ]
    if tenant_id is not None:
        bill_conditions.append(Bill.tenant_id == tenant_id)
    bill_stmt = (
        select(Bill, Contact.name)
        .join(Contact, Bill.contact_id == Contact.id)
        .where(*bill_conditions)
    )
    for bill, cname in (await session.execute(bill_stmt)).all():
        due = max(bill.due_date, as_of)
        items.append(
            ForecastItem(
                expected_date=due,
                description=f"Bill {bill.number or '(draft)'} — {cname}",
                source="bill",
                source_id=bill.id,
                amount=-(bill.total - bill.amount_paid),
            )
        )

    # Recurring-invoice templates → projected inflows at each
    # materialisation in the horizon window.
    rec_conditions = [
        RecurringInvoice.company_id == company_id,
        RecurringInvoice.status == RecurrenceStatus.ACTIVE,
        RecurringInvoice.archived_at.is_(None),
    ]
    if tenant_id is not None:
        rec_conditions.append(RecurringInvoice.tenant_id == tenant_id)
    rec_stmt = (
        select(RecurringInvoice, Contact.name)
        .join(Contact, RecurringInvoice.contact_id == Contact.id)
        .options(selectinload(RecurringInvoice.lines))
        .where(*rec_conditions)
    )
    for tpl, cname in (await session.execute(rec_stmt)).all():
        total_per_run = Decimal("0")
        for ln in tpl.lines:
            total_per_run += await _recurring_line_total(session, company_id, ln)
        if total_per_run <= 0:
            continue
        run = tpl.next_run
        # Walk forward through the horizon. `advance` is pure so no
        # risk of infinite loop provided it strictly moves forward.
        safety = 0
        while run <= horizon_end and safety < 400:
            safety += 1
            if run >= as_of and (tpl.end_date is None or run <= tpl.end_date):
                items.append(
                    ForecastItem(
                        expected_date=run,
                        description=f"Recurring: {tpl.name} — {cname}",
                        source="recurring",
                        source_id=tpl.id,
                        amount=total_per_run,
                    )
                )
            run = _advance_by_frequency(
                run, tpl.frequency, tpl.anchor_day
            )

    items.sort(key=lambda i: (i.expected_date, i.description))

    # Weekly roll-up — 7-day slices from as_of through horizon_end.
    weeks: list[WeekBucket] = []
    cursor = as_of
    while cursor <= horizon_end:
        weeks.append(WeekBucket(start=cursor))
        cursor += timedelta(days=7)
    for item in items:
        idx = (item.expected_date - as_of).days // 7
        if 0 <= idx < len(weeks):
            if item.amount >= 0:
                weeks[idx].inflows += item.amount
            else:
                weeks[idx].outflows += -item.amount

    running = opening
    for wk in weeks:
        running += wk.net
        wk.running_balance = running

    return CashflowForecast(
        from_date=as_of,
        to_date=horizon_end,
        opening_balance=opening,
        items=items,
        weeks=weeks,
    )


# ---------------------------------------------------------------------- #
# Revenue by customer                                                     #
# ---------------------------------------------------------------------- #


@dataclass
class CustomerRevenueRow:
    """One customer's total invoiced revenue (ex-GST) for a date range."""

    contact_id: uuid.UUID
    contact_name: str
    revenue: Decimal  # sum of invoice subtotals (net of GST)


@dataclass
class RevenueByCustomerResult:
    """Revenue breakdown by customer, with concentration metrics."""

    from_date: date
    to_date: date
    rows: list[CustomerRevenueRow]      # sorted by revenue desc
    total_revenue: Decimal
    top_customer_pct: float | None       # None when total_revenue == 0
    concentration_warning: bool          # True when top customer >= 80 %


async def revenue_by_customer(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
) -> RevenueByCustomerResult:
    """Sum invoiced revenue (subtotal, ex-GST) per customer for a date range.

    Uses POSTED invoices issued within [from_date, to_date].  Voided and
    archived invoices are excluded.  Concentration warning fires when the
    top customer accounts for >= 80 % of total revenue — the ATO's 80/20
    PSI rule threshold.
    """
    from saebooks.models.invoice import Invoice, InvoiceStatus

    stmt = (
        select(
            Invoice.contact_id,
            Contact.name,
            func.sum(Invoice.subtotal).label("revenue"),
        )
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.archived_at.is_(None),
            Invoice.issue_date >= from_date,
            Invoice.issue_date <= to_date,
        )
        .group_by(Invoice.contact_id, Contact.name)
        .order_by(func.sum(Invoice.subtotal).desc())
    )

    result = await session.execute(stmt)
    rows: list[CustomerRevenueRow] = [
        CustomerRevenueRow(
            contact_id=row.contact_id,
            contact_name=row.name,
            revenue=Decimal(str(row.revenue or "0")),
        )
        for row in result.all()
    ]

    total_revenue = sum((r.revenue for r in rows), Decimal("0"))
    top_pct = float(rows[0].revenue / total_revenue * 100) if total_revenue > 0 and rows else None

    return RevenueByCustomerResult(
        from_date=from_date,
        to_date=to_date,
        rows=rows,
        total_revenue=total_revenue,
        top_customer_pct=top_pct,
        concentration_warning=top_pct is not None and top_pct >= 80.0,
    )
