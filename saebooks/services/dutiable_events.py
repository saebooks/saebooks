"""Dutiable transaction event service — create + post (M1.5 · T5).

The missing engine primitive alongside ``services/transfers.py``:
``create_and_post_event`` records an assessed stamp/transfer/conveyance/
securities/insurance duty as a first-class ``DutiableTransactionEvent``
row linked to ONE posted journal entry.

What a dutiable transaction event is
-------------------------------------
A ``DutiableTransactionEvent`` row records the assessed duty; one
``JournalEntry`` (two lines) records the double-entry. The JE is stamped
``origin=DUTY``, ``source_type='dutiable_transaction_event'``,
``source_id=<event.id>``, and the event's ``journal_entry_id`` points
back at it. No GST: duty is not itself a GST/VAT-bearing supply, so the
lines carry no ``gst_amount`` (same posture as ``transfers.py``).

Sign convention: **Dr debit_account / Cr credit_account** for
``computed_duty``. ``debit_account`` is the duty cost (an EXPENSE
account) or the asset the duty is capitalised into (an ASSET account) —
this service does not force a choice, company policy decides.
``credit_account`` is the payable the duty is owed to, or the bank/cash
account it is paid from directly.

Rate lookup (optional, decoupled)
----------------------------------
``lookup_stamp_duty_rate`` is a standalone helper that reads the
existing reference-DB ``stamp_duty_rate`` table for AU real-property
duty. It is NOT wired into ``create_and_post_event`` — callers resolve
``computed_duty`` themselves (directly, or via this helper) and pass it
in. This keeps the event capability usable with zero reference-DB rows
on file; a missing/absent reference DB never blocks recording an event
(MODULARITY — the two capabilities do not share a failure domain).

Both accounts MUST belong to ``company_id``/``tenant_id`` and MUST NOT be
header (group) accounts — validated before any JE is built. The hard
rule: never hand-author the JE; always go through the posting
chokepoint (``journal.post_in_txn``).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.dutiable_transaction_event import (
    DutiableEventStatus,
    DutiableTransactionEvent,
    DutyType,
)
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.services import journal as journal_svc
from saebooks.services.journal import PostingError

_DUTY_TYPES: frozenset[str] = frozenset(t.value for t in DutyType)


class DutiableEventError(Exception):
    """Raised when a dutiable transaction event cannot be assembled, posted,
    or reversed."""


async def _resolve_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    role: str,
) -> Account:
    """Fetch + validate one side of an event. Must belong to this company AND
    tenant, and must not be a header (group) account. ``role`` is
    "debit"/"credit" for the message only."""
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
        raise DutiableEventError(
            f"Dutiable event {role} account does not belong to this company"
        )
    if acct.is_header:
        raise DutiableEventError(
            f"Dutiable event {role} account is a header (group) account — "
            "these are CoA scaffolding and cannot carry journal lines"
        )
    return acct


async def create_and_post_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    event_date: date,
    duty_type: str,
    jurisdiction: str,
    dutiable_value: Decimal,
    computed_duty: Decimal,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    sub_jurisdiction: str | None = None,
    applied_concession_id: uuid.UUID | None = None,
    description: str | None = None,
    reference: str | None = None,
    posted_by: str | None = None,
) -> DutiableTransactionEvent:
    """Create a ``DutiableTransactionEvent`` and post its ONE JE atomically.

    Posts a single two-line journal entry **Dr debit_account /
    Cr credit_account** (no GST) via the posting chokepoint
    ``journal.post_in_txn`` (NEVER a hand-authored JE), stamps it
    ``origin=DUTY``, ``source_type='dutiable_transaction_event'``,
    ``source_id=event.id``, and links ``event.journal_entry_id`` -> the
    posted JE.

    Atomic: the event row, the JE (+lines), and the linkage all land in
    ONE transaction (single trailing commit). If posting fails for any
    reason (validation, balance, period lock, DB constraint) nothing
    persists.

    ``computed_duty`` must be positive; ``duty_type`` must be a known
    ``DutyType`` value; both accounts must belong to this company.

    Returns the persisted ``DutiableTransactionEvent`` (status POSTED).
    """
    if duty_type not in _DUTY_TYPES:
        raise DutiableEventError(
            f"Unknown duty_type {duty_type!r}; expected one of {sorted(_DUTY_TYPES)}"
        )
    if computed_duty is None or computed_duty <= Decimal("0"):
        raise DutiableEventError("computed_duty must be positive")
    if dutiable_value is None or dutiable_value < Decimal("0"):
        raise DutiableEventError("dutiable_value must not be negative")
    if debit_account_id == credit_account_id:
        raise DutiableEventError(
            "Dutiable event debit and credit accounts must be different"
        )

    # Validate both sides BEFORE building anything.
    await _resolve_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=debit_account_id,
        role="debit",
    )
    await _resolve_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        account_id=credit_account_id,
        role="credit",
    )

    # The durable event record. Created first so its id stamps the JE.
    event = DutiableTransactionEvent(
        tenant_id=tenant_id,
        company_id=company_id,
        event_date=event_date,
        duty_type=duty_type,
        jurisdiction=jurisdiction,
        sub_jurisdiction=sub_jurisdiction,
        dutiable_value=dutiable_value,
        computed_duty=computed_duty,
        applied_concession_id=applied_concession_id,
        description=description,
        reference=reference,
        status=DutiableEventStatus.POSTED,
        debit_account_id=debit_account_id,
        credit_account_id=credit_account_id,
    )
    session.add(event)
    await session.flush()

    # Build the balanced two-line draft on THIS session WITHOUT committing —
    # so the event row + JE + linkage commit together. Dr debit / Cr credit.
    ref = await journal_svc.next_ref(session)
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=event_date,
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
            debit=computed_duty,
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
            credit=computed_duty,
        )
    )
    await session.flush()

    # Post via the chokepoint (no commit — caller-owned txn). No GST: duty
    # is not itself a GST-bearing supply, so auto_post_gst_lines is a no-op.
    try:
        await journal_svc.post_in_txn(
            session,
            entry.id,
            posted_by=posted_by,
            tenant_id=tenant_id,
            origin=JournalOrigin.DUTY,
            source_type="dutiable_transaction_event",
            source_id=event.id,
        )
    except PostingError as exc:  # surface as an event-level failure
        raise DutiableEventError(f"Could not post dutiable event: {exc}") from exc

    # Link the event to its posted JE.
    event.journal_entry_id = entry.id

    # Single commit — event row, JE (+lines), and linkage land together.
    await session.commit()
    await session.refresh(event)
    return event


async def get_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> DutiableTransactionEvent:
    """Fetch an event scoped to tenant + company. Raises
    ``DutiableEventError`` (treated by the API as 404) if it does not
    exist for this scope."""
    event = (
        await session.execute(
            select(DutiableTransactionEvent).where(
                DutiableTransactionEvent.id == event_id,
                DutiableTransactionEvent.tenant_id == tenant_id,
                DutiableTransactionEvent.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise DutiableEventError("Dutiable transaction event not found")
    return event


async def list_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    duty_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DutiableTransactionEvent]:
    """List dutiable transaction events for the active company, newest
    first. Tenant + company scoping is explicit here as belt-and-braces
    over FORCE RLS."""
    stmt = select(DutiableTransactionEvent).where(
        DutiableTransactionEvent.tenant_id == tenant_id,
        DutiableTransactionEvent.company_id == company_id,
    )
    if duty_type is not None:
        stmt = stmt.where(DutiableTransactionEvent.duty_type == duty_type)
    if date_from is not None:
        stmt = stmt.where(DutiableTransactionEvent.event_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(DutiableTransactionEvent.event_date <= date_to)
    stmt = stmt.order_by(
        DutiableTransactionEvent.event_date.desc(),
        DutiableTransactionEvent.created_at.desc(),
    ).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def reverse_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    reversal_date: date | None = None,
    posted_by: str | None = None,
) -> DutiableTransactionEvent:
    """Void/reverse a dutiable transaction event by reversing its linked JE.

    Posts a swapped mirror JE via ``journal.reverse`` and flips the
    ``DutiableTransactionEvent`` to REVERSED. Idempotent: reversing an
    already-reversed event raises.
    """
    event = await get_event(
        session, event_id, tenant_id=tenant_id, company_id=company_id
    )
    if event.status == DutiableEventStatus.REVERSED:
        raise DutiableEventError("Dutiable transaction event is already reversed")
    if event.journal_entry_id is None:
        raise DutiableEventError(
            "Dutiable transaction event has no linked journal entry to reverse"
        )

    try:
        await journal_svc.reverse(
            session,
            event.journal_entry_id,
            reversal_date=reversal_date,
            posted_by=posted_by,
            tenant_id=tenant_id,
        )
    except PostingError as exc:
        raise DutiableEventError(
            f"Could not reverse dutiable transaction event: {exc}"
        ) from exc

    event.status = DutiableEventStatus.REVERSED
    await session.commit()
    await session.refresh(event)
    return event


# ---------------------------------------------------------------------- #
# Reference-DB rate lookup (optional, decoupled — see module docstring)  #
# ---------------------------------------------------------------------- #


async def lookup_stamp_duty_rate(
    reference_session: AsyncSession,
    *,
    jurisdiction: str,
    state: str,
    transaction_type: str,
    dutiable_value: Decimal,
) -> Decimal | None:
    """Compute duty from the existing reference-DB ``stamp_duty_rate``
    bracket table (AU real-property duty and friends) for one dutiable
    value. Returns ``None`` if no bracket row matches — the caller
    supplies ``computed_duty`` itself in that case; this table owning no
    data for a jurisdiction never blocks recording an event.

    ``reference_session`` is a caller-provided reference-DB session
    (``saebooks.db.ReferenceSession`` when configured) — this function
    does not import or require the reference DB itself, keeping the
    event-recording capability decoupled from reference-DB availability.
    """
    from saebooks.models.reference.stamp_duty_rate import StampDutyRate

    stmt = select(StampDutyRate).where(
        StampDutyRate.jurisdiction == jurisdiction,
        StampDutyRate.state == state,
        StampDutyRate.transaction_type == transaction_type,
        StampDutyRate.lower_bound <= dutiable_value,
    ).order_by(StampDutyRate.lower_bound.desc())
    bracket = (await reference_session.execute(stmt)).scalars().first()
    if bracket is None:
        return None
    if bracket.upper_bound is not None and dutiable_value > bracket.upper_bound:
        return None
    excess = dutiable_value - bracket.lower_bound
    return bracket.base_amount + (excess * bracket.rate / Decimal("100"))
