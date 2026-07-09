"""Reclassification service — account-to-account classification move of an
already-posted amount, WITHOUT mutating the original posted entry.

Gap 2 (see ``saebooks-0157-builder-prompt.md``). The 0156 ledger-cleanup
re-points ~983 posted expenses into new child accounts. Void+recreate works
(the void bug is fixed) but is heavy and leaves void clutter for a pure
classification change. ``create_and_post_reclassification`` records the move as
a first-class ``Reclassification`` row linked to ONE posted, engine-generated
reclass journal entry — the ORIGINAL posted entry is left untouched
(audit-preserved).

How the move works
------------------
A ``Reclassification`` row records the move; one ``JournalEntry`` (two lines)
records the double-entry. The reclass JE nets the OLD account toward zero (for
``amount``) and lands the amount on the NEW account. The JE is stamped
``origin=RECLASSIFICATION``, ``source_type='reclassification'``,
``source_id=<reclassification.id>``, and the row's ``journal_entry_id`` points
back at it.

Sign convention — direction follows the NATURAL BALANCE SIDE of the pair
-----------------------------------------------------------------------
A pure classification move is a single balanced two-line JE whose net effect
on the report (``balance = debit - credit``) is to remove ``amount`` from
``from_account`` and add it to ``to_account``. That only holds when BOTH
accounts sit on the SAME natural side of the ledger:

* **Debit-natured** pair — ASSET / EXPENSE / COST_OF_SALES / OTHER_EXPENSE.
  Original posting put a Debit on ``from``. The reclass is
  **Dr to_account / Cr from_account**: ``Cr from`` reduces the old
  debit-natured balance toward zero; ``Dr to`` lands the amount on the new
  account. This is the primary case — the ~983 posted expenses moving into
  child expense accounts (``6-1000`` -> ``6-1010``).
* **Credit-natured** pair — LIABILITY / EQUITY / INCOME / OTHER_INCOME.
  Original posting put a Credit on ``from``. The reclass is the mirror,
  **Dr from_account / Cr to_account**, so the old credit-natured balance
  still nets toward zero and the amount lands on the new account.

Reclassifying across natural sides (e.g. an EXPENSE into an INCOME account) is
REJECTED: that is not a classification move — it changes the P&L net rather
than relabelling where an amount sits, and the right tool is a void+recreate
or a proper money-in record (Gap 1 receipt / supplier credit note). Documenting
the allowed set explicitly is the design contract for this record.

Allowed target set
------------------
Both accounts MUST: belong to ``company_id`` / ``tenant_id``; NOT be header
(group) accounts; NOT be system-managed (GST etc. — those are engine-owned and
must never be hand-reclassified); be DIFFERENT from each other; and share the
same natural balance side (debit-natured ↔ debit-natured, or credit-natured ↔
credit-natured). ``amount`` must be positive. Cross-company is rejected at the
app layer AND by the composite ``(account_id, company_id)`` FK. The canonical
use is a same-parent child move or an account correction within a type.

The hard rule: never hand-author the JE; always go through the posting
chokepoint (``journal.post_in_txn``). Period locks are respected via the same
override gate (``override_reason`` / ``actor_role``) the chokepoint enforces.
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
from saebooks.models.reclassification import (
    Reclassification,
    ReclassificationStatus,
)
from saebooks.services import journal as journal_svc
from saebooks.services.journal import PostingError

# Accounts whose natural balance is a DEBIT (increase with a debit). A reclass
# between two of these posts Dr to / Cr from.
_DEBIT_NATURED: frozenset[AccountType] = frozenset(
    {
        AccountType.ASSET,
        AccountType.EXPENSE,
        AccountType.COST_OF_SALES,
        AccountType.OTHER_EXPENSE,
    }
)
# Accounts whose natural balance is a CREDIT (increase with a credit). A
# reclass between two of these posts Dr from / Cr to (the mirror).
_CREDIT_NATURED: frozenset[AccountType] = frozenset(
    {
        AccountType.LIABILITY,
        AccountType.EQUITY,
        AccountType.INCOME,
        AccountType.OTHER_INCOME,
    }
)


class ReclassificationError(Exception):
    """Raised when a reclassification cannot be assembled, posted, or reversed."""


def _natural_side(account_type: AccountType) -> str:
    """'debit' or 'credit' — the side an account's balance naturally sits on."""
    if account_type in _DEBIT_NATURED:
        return "debit"
    if account_type in _CREDIT_NATURED:
        return "credit"
    # Defensive: every AccountType is in exactly one set. If a new type is
    # added without updating these frozensets, fail loud rather than guess.
    raise ReclassificationError(
        f"Account type {account_type} has no defined natural side for "
        "reclassification — refusing to guess the JE direction."
    )


async def _resolve_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    role: str,
) -> Account:
    """Fetch + validate one side of a reclassification.

    The account must belong to this company AND tenant, must not be a header
    (group) account, and must not be system-managed (GST etc.). Opaque error —
    never leak a sister-company id. ``role`` is "source"/"target" for the
    message only.
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
        raise ReclassificationError(
            f"Reclassification {role} account does not belong to this company"
        )
    if acct.is_header:
        raise ReclassificationError(
            f"Reclassification {role} account is a header (group) account — "
            "these are CoA scaffolding and cannot carry journal lines"
        )
    if acct.system_managed:
        raise ReclassificationError(
            f"Reclassification {role} account is system-managed (GST etc.) — "
            "engine-owned accounts must never be hand-reclassified"
        )
    return acct


async def create_and_post_reclassification(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    amount: Decimal,
    reclass_date: date,
    reason: str | None = None,
    source_entry_id: uuid.UUID | None = None,
    created_by: str | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
) -> Reclassification:
    """Create a ``Reclassification`` and post its ONE balanced reclass JE.

    Posts a single two-line journal entry that nets ``from_account`` toward
    zero and lands ``amount`` on ``to_account``, via the posting chokepoint
    ``journal.post_in_txn`` (NEVER a hand-authored JE), stamps it
    ``origin=RECLASSIFICATION``, ``source_type='reclassification'``,
    ``source_id=reclassification.id``, and links
    ``reclassification.journal_entry_id`` -> the posted JE. The ORIGINAL posted
    entry (``source_entry_id``, if supplied) is left completely untouched.

    Direction follows the natural balance side of the pair (both accounts MUST
    share the same side):
      * debit-natured pair -> Dr to / Cr from (the ~983-expense case);
      * credit-natured pair -> Dr from / Cr to (the mirror).

    Atomic: the reclassification row, the JE (+lines), and the linkage all land
    in ONE transaction (single trailing commit). If posting fails for any
    reason (validation, balance, period lock, DB constraint) nothing persists.

    ``override_reason`` / ``actor_role`` are threaded to the chokepoint's
    period-lock override gate so a reclass into a closed period is governed by
    the same rule as every other post.

    Returns the persisted ``Reclassification`` (status POSTED).
    """
    if amount is None or amount <= Decimal("0"):
        raise ReclassificationError("Reclassification amount must be positive")
    if from_account_id == to_account_id:
        raise ReclassificationError(
            "Reclassification source and target accounts must be different"
        )

    # Validate both sides BEFORE building anything.
    from_acct = await _resolve_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=from_account_id,
        role="source",
    )
    to_acct = await _resolve_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=to_account_id,
        role="target",
    )

    # Both accounts MUST share the same natural balance side — otherwise this
    # is not a classification move (it would change the P&L/BS net rather than
    # relabel where the amount sits). Reject loudly with the allowed set.
    from_side = _natural_side(from_acct.account_type)
    to_side = _natural_side(to_acct.account_type)
    if from_side != to_side:
        raise ReclassificationError(
            "Reclassification target account must be on the same natural "
            f"balance side as the source. Source {from_acct.code} "
            f"({from_acct.account_type}) is {from_side}-natured; target "
            f"{to_acct.code} ({to_acct.account_type}) is {to_side}-natured. "
            "A reclassification relabels where an amount sits within one side "
            "of the ledger (expense->expense, asset->asset, income->income, "
            "liability->liability); moving across sides changes the net and "
            "is not a classification move — use a void+recreate or a money-in "
            "record (receipt / supplier credit note) instead."
        )

    # The durable reclassification record. Created first so its id stamps the
    # JE. status POSTED — set after a successful post below would be cleaner,
    # but the row id must exist to stamp source_id, and the single commit makes
    # the whole thing atomic, so a failed post rolls the row back too.
    reclass = Reclassification(
        tenant_id=tenant_id,
        company_id=company_id,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount=amount,
        reclass_date=reclass_date,
        reason=reason,
        source_entry_id=source_entry_id,
        status=ReclassificationStatus.POSTED,
        created_by=created_by,
    )
    session.add(reclass)
    await session.flush()

    # Resolve the Dr/Cr legs from the natural side. company_id on each line is
    # left unset; the 0152 BEFORE-INSERT trigger fills it from the parent entry
    # and the composite FK guards the lines.
    if from_side == "debit":
        # Dr to / Cr from — reduce the old debit-natured account, grow the new.
        debit_account_id, credit_account_id = to_account_id, from_account_id
    else:
        # Dr from / Cr to — reduce the old credit-natured account, grow the new.
        debit_account_id, credit_account_id = from_account_id, to_account_id

    description = reason or (
        f"Reclassify {amount} {from_acct.code} -> {to_acct.code}"
    )

    ref = await journal_svc.next_ref(session)
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=reclass_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=1,
            account_id=debit_account_id,
            description=description,
            debit=amount,
            credit=Decimal("0"),
        )
    )
    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=2,
            account_id=credit_account_id,
            description=description,
            debit=Decimal("0"),
            credit=amount,
        )
    )
    await session.flush()

    # Post via the chokepoint (no commit — caller-owned txn). No GST: a
    # classification move carries no tax_code, so auto_post_gst_lines is a
    # no-op. The period-lock override gate runs here.
    try:
        await journal_svc.post_in_txn(
            session,
            entry.id,
            posted_by=created_by,
            override_reason=override_reason,
            actor_role=actor_role,
            tenant_id=tenant_id,
            origin=JournalOrigin.RECLASSIFICATION,
            source_type="reclassification",
            source_id=reclass.id,
        )
    except PostingError as exc:  # surface as a reclass-level failure
        raise ReclassificationError(
            f"Could not post reclassification: {exc}"
        ) from exc

    # Link the reclassification to its posted JE.
    reclass.journal_entry_id = entry.id

    # Single commit — reclass row, JE (+lines), and linkage land together.
    await session.commit()
    await session.refresh(reclass)
    return reclass


async def get_reclassification(
    session: AsyncSession,
    reclassification_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Reclassification:
    """Fetch a reclassification scoped to tenant + company. Raises
    ``ReclassificationError`` (treated by the API as 404) if it does not exist
    for this scope."""
    reclass = (
        await session.execute(
            select(Reclassification).where(
                Reclassification.id == reclassification_id,
                Reclassification.tenant_id == tenant_id,
                Reclassification.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if reclass is None:
        raise ReclassificationError("Reclassification not found")
    return reclass


async def list_reclassifications(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Reclassification]:
    """List reclassifications for the active company, newest first.

    ``account_id`` matches EITHER side (from or to). Tenant + company scoping
    is explicit here as belt-and-braces over FORCE RLS.
    """
    stmt = select(Reclassification).where(
        Reclassification.tenant_id == tenant_id,
        Reclassification.company_id == company_id,
    )
    if account_id is not None:
        stmt = stmt.where(
            (Reclassification.from_account_id == account_id)
            | (Reclassification.to_account_id == account_id)
        )
    if date_from is not None:
        stmt = stmt.where(Reclassification.reclass_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Reclassification.reclass_date <= date_to)
    stmt = (
        stmt.order_by(
            Reclassification.reclass_date.desc(),
            Reclassification.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def reverse_reclassification(
    session: AsyncSession,
    reclassification_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    reversal_date: date | None = None,
    posted_by: str | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
) -> Reclassification:
    """Reverse a reclassification by reversing its linked reclass JE.

    Posts a swapped mirror JE via ``journal.reverse`` (which flips the reclass
    JE to REVERSED and audit-snapshots first) and flips the
    ``Reclassification`` to REVERSED. Idempotent: reversing an
    already-reversed reclassification raises.

    ``journal.reverse`` commits internally; the status flip is committed in the
    same call sequence. A REVERSED-status guard makes the operation safe to
    retry. ``override_reason`` / ``actor_role`` thread to the period-lock gate
    so a reversal landing in a closed period is governed consistently.
    """
    reclass = await get_reclassification(
        session,
        reclassification_id,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    if reclass.status == ReclassificationStatus.REVERSED:
        raise ReclassificationError("Reclassification is already reversed")
    if reclass.journal_entry_id is None:
        raise ReclassificationError(
            "Reclassification has no linked journal entry to reverse"
        )

    try:
        await journal_svc.reverse(
            session,
            reclass.journal_entry_id,
            reversal_date=reversal_date,
            posted_by=posted_by,
            override_reason=override_reason,
            actor_role=actor_role,
            tenant_id=tenant_id,
        )
    except PostingError as exc:
        raise ReclassificationError(
            f"Could not reverse reclassification: {exc}"
        ) from exc

    reclass.status = ReclassificationStatus.REVERSED
    await session.commit()
    await session.refresh(reclass)
    return reclass
