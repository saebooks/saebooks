"""BAS (Business Activity Statement) report service.

Calculates GST collected and GST paid from posted journal lines
that have a tax_code with reporting_type mappings. Uses ATO labels.

BAS labels:
  G1  — Total sales (inc. GST)
  G2  — Export sales
  G3  — Other GST-free sales
  G10 — Capital purchases (inc. GST)
  G11 — Non-capital purchases (inc. GST)
  1A  — GST collected on sales
  1B  — GST paid on purchases

The mapping from tax_code.reporting_type:
  - "taxable"        → income lines go to G1, expense lines to G11
  - "gst_free"       → income lines go to G3
  - "export"         → income lines go to G2
  - "input_taxed"    → excluded from GST calc
  - "capital"        → expense lines go to G10
  - "no_tax"         → excluded
"""
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.tax_code import TaxCode

# Account types considered "income" for BAS purposes
_INCOME_TYPES = {AccountType.INCOME, AccountType.OTHER_INCOME}

# Account types considered "purchases" for BAS purposes
_PURCHASE_TYPES = {
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    AccountType.ASSET,
}


@dataclass
class BASLine:
    label: str
    description: str
    amount: Decimal = Decimal("0")


@dataclass
class BASReport:
    period_from: date | None
    period_to: date | None
    g1: BASLine
    g2: BASLine
    g3: BASLine
    g10: BASLine
    g11: BASLine
    label_1a: BASLine
    label_1b: BASLine

    @property
    def gst_payable(self) -> Decimal:
        """Net GST: collected minus paid. Positive = owe ATO."""
        return self.label_1a.amount - self.label_1b.amount


async def bas_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> BASReport:
    """Build a BAS report for the given period."""
    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status == EntryStatus.POSTED,
    ]
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date:
        conditions.append(JournalEntry.entry_date <= to_date)

    # Query: join lines → entries → accounts → tax_codes
    # Get: account_type, reporting_type, debit, credit, gst_amount
    stmt = (
        select(
            Account.account_type,
            TaxCode.reporting_type,
            JournalLine.debit,
            JournalLine.credit,
            JournalLine.gst_amount,
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .outerjoin(TaxCode, JournalLine.tax_code_id == TaxCode.id)
        .where(and_(*conditions))
    )

    result = await session.execute(stmt)

    g1 = Decimal("0")   # Total sales inc GST
    g2 = Decimal("0")   # Export sales
    g3 = Decimal("0")   # Other GST-free sales
    g10 = Decimal("0")  # Capital purchases inc GST
    g11 = Decimal("0")  # Non-capital purchases inc GST
    gst_collected = Decimal("0")  # 1A
    gst_paid = Decimal("0")       # 1B

    for row in result.all():
        acct_type = row[0]
        reporting_type = row[1] or "no_tax"
        debit = row[2]
        credit = row[3]
        gst = row[4] or Decimal("0")

        # Net amount: for income accounts, credit - debit; for expense, debit - credit
        if acct_type in _INCOME_TYPES:
            net = credit - debit  # income is credit-normal
            if reporting_type == "taxable":
                g1 += net + gst  # total inc GST
                gst_collected += gst
            elif reporting_type == "export":
                g2 += net
            elif reporting_type == "gst_free":
                g3 += net
        elif acct_type in _PURCHASE_TYPES:
            net = debit - credit  # expense is debit-normal
            if reporting_type in ("taxable", "capital"):
                if reporting_type == "capital":
                    g10 += net + gst
                else:
                    g11 += net + gst
                gst_paid += gst
            elif reporting_type == "gst_free":
                pass  # GST-free purchases — no GST component

    return BASReport(
        period_from=from_date,
        period_to=to_date,
        g1=BASLine("G1", "Total sales (including any GST)", g1),
        g2=BASLine("G2", "Export sales", g2),
        g3=BASLine("G3", "Other GST-free sales", g3),
        g10=BASLine("G10", "Capital purchases (including any GST)", g10),
        g11=BASLine("G11", "Non-capital purchases (including any GST)", g11),
        label_1a=BASLine("1A", "GST collected on sales", gst_collected),
        label_1b=BASLine("1B", "GST paid on purchases", gst_paid),
    )
