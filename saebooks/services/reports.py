"""Reporting service — trial balance, P&L, balance sheet, aged AR/AP.

All reports operate on POSTED journal lines only (except aged AR/AP,
which walks the `invoices`/`bills` tables directly so it can show
per-document line items and balance-due drilldown).
"""
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

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
) -> tuple[list[ReportSection], Decimal]:
    """Balance sheet: assets, liabilities, equity. Returns (sections, net_assets)."""
    balances = await _account_balances(session, company_id, as_of=as_of)
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
    net_assets = assets + liabilities + equity  # liabilities are credit-normal (negative)
    return sections, net_assets


async def _account_balances(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    as_of: date | None = None,
) -> list[AccountBalance]:
    """Aggregate posted journal lines into per-account debit/credit totals."""
    # Build filters
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status == EntryStatus.POSTED,
    ]
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
    """The full aged report — group per contact plus grand totals."""

    as_at: date
    groups: list[AgedContactGroup] = field(default_factory=list)
    grand_totals: dict[str, Decimal] = field(
        default_factory=lambda: {k: Decimal("0") for k in BUCKET_KEYS}
    )

    @property
    def grand_total(self) -> Decimal:
        return sum(self.grand_totals.values(), Decimal("0"))


async def aged_ar(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_at: date | None = None,
) -> AgedReport:
    """Return the aged-debtors report as at ``as_at`` (default today).

    Walks POSTED, non-archived invoices with a balance_due > 0 (i.e.
    ``total > amount_paid``). Issued-date filter is ``issue_date <=
    as_at`` so future-dated invoices don't appear. Voided and archived
    invoices are excluded.
    """
    cutoff = as_at or date.today()
    stmt = (
        select(Invoice, Contact.name)
        .join(Contact, Invoice.contact_id == Contact.id)
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.archived_at.is_(None),
            Invoice.issue_date <= cutoff,
            Invoice.total > Invoice.amount_paid,
        )
        .order_by(Contact.name, Invoice.due_date)
    )
    rows = (await session.execute(stmt)).all()

    groups: dict[uuid.UUID, AgedContactGroup] = {}
    for inv, contact_name in rows:
        days_overdue = (cutoff - inv.due_date).days
        row = AgedInvoiceRow(
            invoice_id=inv.id,
            number=inv.number or "(draft)",
            issue_date=inv.issue_date,
            due_date=inv.due_date,
            total=inv.total,
            amount_paid=inv.amount_paid,
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
        group.buckets[row.bucket] += row.balance_due

    report = AgedReport(as_at=cutoff)
    # Sort groups by descending total so the biggest debtors are on top.
    report.groups = sorted(
        groups.values(), key=lambda g: g.total, reverse=True
    )
    for group in report.groups:
        for key in BUCKET_KEYS:
            report.grand_totals[key] += group.buckets[key]
    return report


def aged_ar_csv(report: AgedReport) -> str:
    """Render an aged-AR report as RFC 4180 CSV (one row per invoice).

    Columns: ``contact,invoice_number,issue_date,due_date,total,paid,
    balance_due,days_overdue,bucket``. A footer row per contact + a
    grand-total footer row would break spreadsheet pivots, so we emit
    only detail rows; users can pivot in their tool of choice.
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
                    group.contact_name,
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
) -> AgedReport:
    """Return the aged-creditors report as at ``as_at`` (default today)."""
    cutoff = as_at or date.today()
    stmt = (
        select(Bill, Contact.name)
        .join(Contact, Bill.contact_id == Contact.id)
        .where(
            Bill.company_id == company_id,
            Bill.status == BillStatus.POSTED,
            Bill.archived_at.is_(None),
            Bill.issue_date <= cutoff,
            Bill.total > Bill.amount_paid,
        )
        .order_by(Contact.name, Bill.due_date)
    )
    rows = (await session.execute(stmt)).all()

    groups: dict[uuid.UUID, AgedContactGroup] = {}
    for bill, contact_name in rows:
        days_overdue = (cutoff - bill.due_date).days
        row = AgedInvoiceRow(
            invoice_id=bill.id,
            number=bill.number or "(draft)",
            issue_date=bill.issue_date,
            due_date=bill.due_date,
            total=bill.total,
            amount_paid=bill.amount_paid,
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
                    group.contact_name,
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
