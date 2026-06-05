"""Transfer service — account-to-account money movement.

The missing engine primitive from DB-rebuild handover #2:
``create_and_post_transfer`` records a money movement between two
balance-sheet accounts of ONE company as a first-class ``Transfer`` row
linked to ONE posted balance-sheet journal entry.

What a transfer is
-------------------
A ``Transfer`` row records the money movement; one ``JournalEntry`` (two
lines) records the double-entry. The JE is stamped ``origin=TRANSFER``,
``source_type='transfer'``, ``source_id=<transfer.id>``, and the transfer's
``journal_entry_id`` points back at it.

Sign convention (FIXED): money LEAVES ``from_account`` and ARRIVES at
``to_account``. The JE is **Dr to_account / Cr from_account**. For the three
canonical uses:

* **Credit-card paydown** — ``from`` = bank (ASSET), ``to`` = the credit-card
  liability (``2-1115``). Dr 2-1115 (liability down) / Cr bank (asset down).
* **Director-loan repayment** — ``from`` = bank (ASSET), ``to`` = directors-loan
  liability (``2-2200``). Dr 2-2200 (loan down) / Cr bank (asset down).
* **Bank/loan transfer** — ``from`` = bank A, ``to`` = bank B. Dr B / Cr A.

No GST: both accounts are balance-sheet accounts, so this is a pure
balance-sheet movement. The lines carry no ``gst_amount`` and
``auto_post_gst_lines`` is a no-op (design: balance-sheet movement, no GST).

Both accounts MUST be balance-sheet accounts (ASSET / LIABILITY / EQUITY),
MUST belong to ``company_id`` / ``tenant_id``, MUST NOT be header (group)
accounts, and MUST be different from each other — validated before any JE is
built. The hard rule from the handover: never hand-author the JE; always go
through the posting chokepoint (``journal.post_in_txn``).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.transfer import Transfer, TransferStatus
from saebooks.services import journal as journal_svc
from saebooks.services.journal import PostingError

# Accounts whose movement is purely balance-sheet (no P&L, no GST).
_BALANCE_SHEET_TYPES: frozenset[AccountType] = frozenset(
    {AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY}
)


class TransferError(Exception):
    """Raised when a transfer cannot be assembled, posted, or reversed."""


async def _resolve_balance_sheet_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    role: str,
) -> Account:
    """Fetch + validate one side of a transfer.

    The account must belong to this company AND tenant, must be a
    balance-sheet account (ASSET / LIABILITY / EQUITY), and must not be a
    header (group) account. Opaque error — never leak a sister-company id.
    ``role`` is "source"/"destination" for the message only.
    """
    acct = (
        await session.execute(
            select(Account).where(
                Account.id == account_id,
                Account.company_id == company_id,
                Account.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise TransferError(
            f"Transfer {role} account does not belong to this company"
        )
    if acct.is_header:
        raise TransferError(
            f"Transfer {role} account is a header (group) account — these are "
            "CoA scaffolding and cannot carry journal lines"
        )
    if acct.account_type not in _BALANCE_SHEET_TYPES:
        raise TransferError(
            f"Transfer {role} account must be a balance-sheet account "
            f"(ASSET / LIABILITY / EQUITY), got {acct.account_type}. A transfer "
            "moves money between balance-sheet accounts only — use an "
            "invoice/bill/expense for P&L postings."
        )
    return acct


async def create_and_post_transfer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    amount: Decimal,
    transfer_date: date,
    description: str | None = None,
    reference: str | None = None,
    posted_by: str | None = None,
) -> Transfer:
    """Create a ``Transfer`` and post its ONE balance-sheet JE atomically.

    Posts a single two-line balance-sheet journal entry **Dr to_account /
    Cr from_account** (no GST) via the posting chokepoint
    ``journal.post_in_txn`` (NEVER a hand-authored JE), stamps it
    ``origin=TRANSFER``, ``source_type='transfer'``, ``source_id=transfer.id``,
    and links ``transfer.journal_entry_id`` -> the posted JE.

    Atomic: the transfer row, the JE (+lines), and the linkage all land in ONE
    transaction (single trailing commit). If posting fails for any reason
    (validation, balance, period lock, DB constraint) nothing persists.

    Both accounts must be balance-sheet accounts (ASSET / LIABILITY / EQUITY)
    of this company; they must differ; ``amount`` must be positive.

    Returns the persisted ``Transfer`` (status POSTED).
    """
    if amount is None or amount <= Decimal("0"):
        raise TransferError("Transfer amount must be positive")
    if from_account_id == to_account_id:
        raise TransferError(
            "Transfer source and destination accounts must be different"
        )

    # Validate both sides BEFORE building anything.
    await _resolve_balance_sheet_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=from_account_id,
        role="source",
    )
    await _resolve_balance_sheet_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=to_account_id,
        role="destination",
    )

    # The durable transfer record. Created first so its id stamps the JE.
    transfer = Transfer(
        tenant_id=tenant_id,
        company_id=company_id,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount=amount,
        transfer_date=transfer_date,
        description=description,
        reference=reference,
        status=TransferStatus.POSTED,
    )
    session.add(transfer)
    await session.flush()

    # Build the balanced two-line draft on THIS session WITHOUT committing —
    # so the transfer row + JE + linkage commit together. Dr to / Cr from.
    # company_id on each line is left unset; the 0152 BEFORE-INSERT trigger
    # fills it from the parent entry and the composite FK guards the lines.
    ref = await journal_svc.next_ref(session)
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=transfer_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=1,
            account_id=to_account_id,
            description=description,
            debit=amount,
            credit=Decimal("0"),
        )
    )
    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=2,
            account_id=from_account_id,
            description=description,
            debit=Decimal("0"),
            credit=amount,
        )
    )
    await session.flush()

    # Post via the chokepoint (no commit — caller-owned txn). No GST: both
    # accounts are balance-sheet, so auto_post_gst_lines is a no-op.
    try:
        await journal_svc.post_in_txn(
            session,
            entry.id,
            posted_by=posted_by,
            tenant_id=tenant_id,
            origin=JournalOrigin.TRANSFER,
            source_type="transfer",
            source_id=transfer.id,
        )
    except PostingError as exc:  # surface as a transfer-level failure
        raise TransferError(f"Could not post transfer: {exc}") from exc

    # Link the transfer to its posted JE.
    transfer.journal_entry_id = entry.id

    # Single commit — transfer row, JE (+lines), and linkage land together.
    await session.commit()
    await session.refresh(transfer)
    return transfer


async def get_transfer(
    session: AsyncSession,
    transfer_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Transfer:
    """Fetch a transfer scoped to tenant + company. Raises ``TransferError``
    (treated by the API as 404) if it does not exist for this scope."""
    transfer = (
        await session.execute(
            select(Transfer).where(
                Transfer.id == transfer_id,
                Transfer.tenant_id == tenant_id,
                Transfer.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise TransferError("Transfer not found")
    return transfer


async def list_transfers(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Transfer]:
    """List transfers for the active company, newest first.

    ``account_id`` matches EITHER side (from or to). Tenant + company scoping
    is explicit here as belt-and-braces over FORCE RLS.
    """
    stmt = select(Transfer).where(
        Transfer.tenant_id == tenant_id,
        Transfer.company_id == company_id,
    )
    if account_id is not None:
        stmt = stmt.where(
            (Transfer.from_account_id == account_id)
            | (Transfer.to_account_id == account_id)
        )
    if date_from is not None:
        stmt = stmt.where(Transfer.transfer_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Transfer.transfer_date <= date_to)
    stmt = stmt.order_by(
        Transfer.transfer_date.desc(), Transfer.created_at.desc()
    ).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def reverse_transfer(
    session: AsyncSession,
    transfer_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    reversal_date: date | None = None,
    posted_by: str | None = None,
) -> Transfer:
    """Void/reverse a transfer by reversing its linked JE.

    Posts a swapped mirror JE via ``journal.reverse`` (which flips the original
    JE to REVERSED and audit-snapshots first) and flips the ``Transfer`` to
    REVERSED. Idempotent: reversing an already-reversed transfer raises.

    ``journal.reverse`` commits internally; the status flip is committed in the
    same call sequence. A REVERSED-status guard makes the operation safe to
    retry.
    """
    transfer = await get_transfer(
        session, transfer_id, tenant_id=tenant_id, company_id=company_id
    )
    if transfer.status == TransferStatus.REVERSED:
        raise TransferError("Transfer is already reversed")
    if transfer.journal_entry_id is None:
        raise TransferError("Transfer has no linked journal entry to reverse")

    try:
        await journal_svc.reverse(
            session,
            transfer.journal_entry_id,
            reversal_date=reversal_date,
            posted_by=posted_by,
            tenant_id=tenant_id,
        )
    except PostingError as exc:
        raise TransferError(f"Could not reverse transfer: {exc}") from exc

    transfer.status = TransferStatus.REVERSED
    await session.commit()
    await session.refresh(transfer)
    return transfer
