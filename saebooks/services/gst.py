"""GST auto-posting and BAS settlement service.

When gst_auto_post is enabled, posting a journal entry automatically
generates GST account lines:
  - Lines with a tax code on income accounts → CR GST Collected
  - Lines with a tax code on expense/asset accounts → DR GST Paid

BAS settlement creates a clearing journal:
  - DR GST Collected (zero it)
  - CR GST Paid (zero it)
  - Net to GST Clearing
  - User then pays ATO from bank → DR GST Clearing, CR Bank

Users can toggle gst_auto_post OFF for manual control.
System-managed accounts are marked but not hard-locked — manual posting
is allowed when gst_auto_post is OFF.
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.services import settings as settings_svc

# Account types where GST goes to "GST Paid" (input tax credit)
_INPUT_TYPES = {
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    AccountType.ASSET,
}

# Account types where GST goes to "GST Collected" (output tax)
_OUTPUT_TYPES = {
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
}


async def is_auto_post_enabled(session: AsyncSession) -> bool:
    """Check if GST auto-posting is enabled."""
    val = await settings_svc.get(session, "gst_auto_post", "true")
    return str(val).lower() in ("true", "1", "yes")


async def _get_gst_account(
    session: AsyncSession, company_id: uuid.UUID, setting_key: str
) -> Account | None:
    """Look up a GST system account by its code stored in settings.

    Accounts are stored with hyphenated codes (e.g. "2-1310") after migration
    0010, but settings may have been written with flat codes (e.g. "21310").
    We try both forms so installs set up before the code-hyphenation migration
    keep working without requiring a manual settings update.
    """
    raw = await settings_svc.get(session, setting_key, "")
    if not raw:
        return None
    code = str(raw)
    # Derive the hyphenated form: "21310" -> "2-1310" (insert dash after first char).
    if "-" not in code and len(code) >= 2 and code[0].isdigit():
        hyphenated = code[0] + "-" + code[1:]
    else:
        hyphenated = code
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_([code, hyphenated]),
            Account.archived_at.is_(None),
        )
    )
    return result.scalars().first()


async def auto_post_gst_lines(
    session: AsyncSession,
    entry: JournalEntry,
) -> list[JournalLine]:
    """Generate GST account lines for a journal entry being posted.

    For each line that has a gst_amount, creates a corresponding line
    on the appropriate GST account (Collected or Paid).

    Returns the list of new GST lines added. The caller is responsible
    for flushing/committing.
    """
    if not await is_auto_post_enabled(session):
        return []

    collected_acct = await _get_gst_account(
        session, entry.company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, entry.company_id, "gst_paid_account_code"
    )

    if not collected_acct or not paid_acct:
        return []

    # Build a map of account_id → account_type for the entry's lines
    acct_ids = {line.account_id for line in entry.lines}
    acct_types: dict[uuid.UUID, AccountType] = {}
    if acct_ids:
        result = await session.execute(
            select(Account.id, Account.account_type).where(Account.id.in_(acct_ids))
        )
        for row in result.all():
            acct_types[row[0]] = row[1]

    new_lines: list[JournalLine] = []
    max_line_no = max((line.line_no for line in entry.lines), default=0)

    for line in entry.lines:
        gst = line.gst_amount
        if not gst or gst == Decimal("0"):
            continue

        # Skip lines that are already on GST accounts (avoid infinite recursion)
        if line.account_id in (collected_acct.id, paid_acct.id):
            continue

        acct_type = acct_types.get(line.account_id)
        if acct_type is None:
            continue

        max_line_no += 1

        if acct_type in _OUTPUT_TYPES:
            # Income line with GST → CR GST Collected
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=collected_acct.id,
                description=f"GST on {line.description or 'sale'}",
                debit=Decimal("0"),
                credit=abs(gst),
            )
        elif acct_type in _INPUT_TYPES:
            # Expense/asset line with GST → DR GST Paid
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=paid_acct.id,
                description=f"GST on {line.description or 'purchase'}",
                debit=abs(gst),
                credit=Decimal("0"),
            )
        else:
            continue

        session.add(gst_line)
        # Also append to the entry's loaded relationship so callers iterating
        # entry.lines see the new line without a re-fetch (avoids stale
        # identity-map state after flush).
        entry.lines.append(gst_line)
        new_lines.append(gst_line)

    return new_lines


async def settle_bas(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    settlement_date: date,
    from_date: date | None = None,
    to_date: date | None = None,
) -> JournalEntry | None:
    """Create a BAS settlement journal entry.

    Zeroes out GST Collected and GST Paid, with the net going to
    GST Clearing. Returns the created (draft) journal entry, or None
    if there's nothing to settle.

    The caller should post this entry after review.
    """
    from saebooks.services import journal as journal_svc

    collected_acct = await _get_gst_account(
        session, company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, company_id, "gst_paid_account_code"
    )
    clearing_acct = await _get_gst_account(
        session, company_id, "gst_clearing_account_code"
    )

    if not collected_acct or not paid_acct or not clearing_acct:
        return None

    # Calculate current balances on the GST accounts
    from saebooks.services.reports import _account_balances

    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=to_date
    )

    collected_bal = Decimal("0")
    paid_bal = Decimal("0")

    for bal in balances:
        if bal.account_id == collected_acct.id:
            collected_bal = bal.balance  # credit-normal → negative
        elif bal.account_id == paid_acct.id:
            paid_bal = bal.balance  # debit-normal → positive

    # If both are zero, nothing to settle
    if collected_bal == Decimal("0") and paid_bal == Decimal("0"):
        return None

    lines: list[dict[str, object]] = []

    # DR GST Collected (to zero it — it has a credit balance, so debit it)
    if collected_bal != Decimal("0"):
        lines.append({
            "account_id": collected_acct.id,
            "description": "Clear GST Collected for BAS",
            "debit": abs(collected_bal),
            "credit": Decimal("0"),
        })

    # CR GST Paid (to zero it — it has a debit balance, so credit it)
    if paid_bal != Decimal("0"):
        lines.append({
            "account_id": paid_acct.id,
            "description": "Clear GST Paid for BAS",
            "debit": Decimal("0"),
            "credit": abs(paid_bal),
        })

    # Net to GST Clearing
    # Net GST payable = collected (credit, negative balance) - paid (debit, positive balance)
    # If net is positive (owe ATO), CR Clearing; if negative (refund), DR Clearing
    net = abs(collected_bal) - paid_bal  # both are absolute-ish
    if net > Decimal("0"):
        # Owe ATO — credit clearing (liability)
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST payable to ATO",
            "debit": Decimal("0"),
            "credit": net,
        })
    elif net < Decimal("0"):
        # ATO refund — debit clearing
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST refund from ATO",
            "debit": abs(net),
            "credit": Decimal("0"),
        })

    if not lines:
        return None

    period_label = ""
    if from_date and to_date:
        period_label = f" ({from_date} to {to_date})"

    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        entry_date=settlement_date,
        description=f"BAS settlement{period_label}",
        lines=lines,
    )

    return entry
