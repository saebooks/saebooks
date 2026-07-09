"""Intercompany posting service — LOCAL (same-tenant) Phase 1.

Posts a **linked reciprocal pair** of journal entries for an economic event
between two companies co-resident in ONE tenant DB. Both legs post inside a
**single transaction** via the Phase-0 ``journal.post_in_txn(commit=False)``
primitive and a single trailing commit: if either leg fails to post, neither
posts (no half-pair, no orphan).

What a pair is
--------------
An ``IcTxn`` row records the shared economic event. Two ``JournalEntry`` rows —
one per company — record the local double-entry; each is stamped
``origin=INTERCOMPANY``, ``source_type='ic_txn'``, ``source_id=<ic_txn.id>``.
Two ``IcLeg`` rows link each JE to the ``IcTxn`` (``side`` ORIGINATOR /
COUNTERPARTY). The reciprocal "Due to/from" control accounts come from the two
``IcEdge`` rows (one per company), NOT from caller free-text, so the balance
hits the declared control account on each side.

Directors-loan-style example (the §3 personal -> SAE funding edge, modelled as
a same-tenant pair for the LOCAL primitive — the REAL personal<->business edge is
cross-DB / REMOTE and awaits Phase 3, see ``TODO(remote-relay)`` below)::

    # Originator = personal company; counterparty = SAE company.
    # Edge (originator side): control_account = personal "Loan to SAE" (ASSET).
    # Edge (counterparty side): control_account = SAE "Directors Loan" (LIABILITY).
    await post_local_pair(
        session,
        tenant_id=tenant_id,
        originator_company_id=personal_id,
        counterparty_company_id=sae_id,
        amount=Decimal("5000.00"),
        entry_date=date(2026, 6, 6),
        description="Director funds SAE working capital",
        # Originator: Dr Loan-to-SAE (control), Cr Bank (contra).
        originator_contra_account_id=personal_bank_id,
        # Counterparty: Dr Bank (contra), Cr Directors-Loan (control).
        counterparty_contra_account_id=sae_bank_id,
    )

The sign convention is fixed by ``DIRECTION``: on the ORIGINATOR side the
control account is **debited** (a right / receivable / "due from") and the
contra credited; on the COUNTERPARTY side the control account is **credited** (an
obligation / "due to") and the contra debited. A caller wanting the opposite
economic direction swaps which company is the originator.

No GST is posted: the legs carry no ``gst_amount`` and the control accounts are
balance-sheet accounts (ASSET / LIABILITY), so ``auto_post_gst_lines`` is a
no-op (balance-sheet movement, no GST — design §3).

REMOTE relay
------------
Cross-DB partners (a company in a *different* tenant stack — e.g. a real
personal-tenant <-> business-tenant directors-loan edge) CANNOT use this primitive:
there is no shared transaction across two Postgres servers. That is the Phase-3
broker relay (outbox/inbox, Ed25519 signing, per-edge tokens). The seam is
marked with ``TODO(remote-relay)`` where a REMOTE edge would branch.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcLeg,
    IcLegSide,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, JournalOrigin
from saebooks.services import journal as journal_svc
from saebooks.services.journal import PostingError


class IntercompanyError(Exception):
    """Raised when an intercompany pair cannot be assembled or posted."""


async def _resolve_edge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    partner_company_id: uuid.UUID,
    direction: IcEdgeDirection,
) -> IcEdge:
    """Fetch the IcEdge for (company -> partner) in the given direction.

    The edge is the capability: an economic event between two companies is only
    allowed along a pre-declared edge whose ``control_account_id`` bounds where
    the balance lands. A missing edge is a hard ``IntercompanyError`` (an
    opaque message — never leak the partner/account ids of a row the caller is
    not entitled to enumerate).
    """
    row = (
        await session.execute(
            select(IcEdge).where(
                IcEdge.tenant_id == tenant_id,
                IcEdge.company_id == company_id,
                IcEdge.partner_company_id == partner_company_id,
                IcEdge.direction == direction,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise IntercompanyError(
            f"No {direction} intercompany edge declared for this company pair"
        )
    return row


async def _assert_account_owned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Refuse a contra account that is not a postable account OF this company.

    Belt-and-braces over the DB composite FK / coherence trigger: a leg's
    contra account must belong to the leg's own company and tenant and must not
    be a header (group) account. Opaque error — no sister-company id leak.
    """
    acct = (
        await session.execute(
            select(Account.id, Account.is_header).where(
                Account.id == account_id,
                Account.company_id == company_id,
                Account.tenant_id == tenant_id,
            )
        )
    ).first()
    if acct is None:
        raise IntercompanyError("Contra account does not belong to this company")
    if acct.is_header:
        raise IntercompanyError(
            "Cannot post to a header (group) account — these are CoA scaffolding"
        )


async def _build_leg_draft(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    entry_date: date,
    description: str | None,
    control_account_id: uuid.UUID,
    contra_account_id: uuid.UUID,
    amount: Decimal,
    debit_control: bool,
) -> JournalEntry:
    """Construct (but do NOT commit) one balanced two-line draft JE.

    ``debit_control`` True  => Dr control / Cr contra (ORIGINATOR: due-from).
    ``debit_control`` False => Cr control / Dr contra (COUNTERPARTY: due-to).

    Mirrors ``journal.create_draft`` line-building but stages on the caller's
    session WITHOUT committing, so the pair can post atomically. ``company_id``
    on each line is left unset — the 0152 BEFORE-INSERT trigger fills it from
    the parent entry and the composite FK guards cross-company lines.
    """
    ref = await journal_svc.next_ref(session)
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=entry_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    if debit_control:
        control_debit, control_credit = amount, Decimal("0")
        contra_debit, contra_credit = Decimal("0"), amount
    else:
        control_debit, control_credit = Decimal("0"), amount
        contra_debit, contra_credit = amount, Decimal("0")

    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=1,
            account_id=control_account_id,
            description=description,
            debit=control_debit,
            credit=control_credit,
        )
    )
    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=2,
            account_id=contra_account_id,
            description=description,
            debit=contra_debit,
            credit=contra_credit,
        )
    )
    await session.flush()
    return entry


async def post_local_pair(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    originator_company_id: uuid.UUID,
    counterparty_company_id: uuid.UUID,
    amount: Decimal,
    entry_date: date,
    description: str | None = None,
    originator_contra_account_id: uuid.UUID,
    counterparty_contra_account_id: uuid.UUID,
    posted_by: str | None = None,
) -> IcTxn:
    """Post a linked reciprocal pair of JEs for a same-tenant (LOCAL) IC event.

    Atomic: both legs post inside ONE transaction via
    ``journal.post_in_txn(commit=False)`` and a single commit. If either leg
    fails (validation, balance, period lock, DB constraint) the whole pair
    rolls back — no half-pair, no orphan ``ic_legs`` / ``ic_txn``.

    Returns the persisted ``IcTxn`` (status ACTIVE). Both JEs are stamped
    ``origin=INTERCOMPANY``, ``source_type='ic_txn'``, ``source_id=ic_txn.id``;
    two ``IcLeg`` rows link them.

    Both companies MUST be in ``tenant_id`` (LOCAL). A reciprocal pair of
    ``IcEdge`` rows must already exist: an ORIGINATOR edge on
    ``originator_company_id`` -> ``counterparty_company_id`` and a COUNTERPARTY
    edge on ``counterparty_company_id`` -> ``originator_company_id``. The
    control accounts come from those edges (the balance-sheet allowlist), not
    from the caller.
    """
    if amount <= Decimal("0"):
        raise IntercompanyError("Intercompany amount must be positive")
    if originator_company_id == counterparty_company_id:
        raise IntercompanyError(
            "Originator and counterparty must be different companies"
        )

    # Resolve both ends of the reciprocal edge. Each side declares its own
    # control account; both must exist or nothing posts.
    orig_edge = await _resolve_edge(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        partner_company_id=counterparty_company_id,
        direction=IcEdgeDirection.ORIGINATOR,
    )
    cpty_edge = await _resolve_edge(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        partner_company_id=originator_company_id,
        direction=IcEdgeDirection.COUNTERPARTY,
    )

    # Validate caller-supplied contra accounts belong to the right company.
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        account_id=originator_contra_account_id,
    )
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        account_id=counterparty_contra_account_id,
    )

    # TODO(remote-relay): if either edge were a REMOTE partner (partner in a
    # different tenant stack / DB — e.g. the real personal<->business directors-
    # loan edge), this single-transaction primitive CANNOT be used. Phase 3
    # branches here into the broker outbox path: post ONLY the originator leg +
    # an ic_outbox row in this local txn, then a dispatcher relays a signed
    # payload to the partner stack's /ic/accept. There is no cross-DB shared
    # transaction. Phase 1 covers same-tenant pairs only.

    # The shared economic event.
    ic_txn = IcTxn(
        tenant_id=tenant_id,
        company_id=originator_company_id,
        description=description,
        status=IcTxnStatus.ACTIVE,
    )
    session.add(ic_txn)
    await session.flush()

    # Originator leg: Dr control (due-from) / Cr contra.
    orig_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=orig_edge.control_account_id,
        contra_account_id=originator_contra_account_id,
        amount=amount,
        debit_control=True,
    )
    # Counterparty leg: Cr control (due-to) / Dr contra.
    cpty_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=cpty_edge.control_account_id,
        contra_account_id=counterparty_contra_account_id,
        amount=amount,
        debit_control=False,
    )

    # Post BOTH legs without committing — atomicity is purely local.
    await journal_svc.post_in_txn(
        session,
        orig_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=ic_txn.id,
    )
    await journal_svc.post_in_txn(
        session,
        cpty_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=ic_txn.id,
    )

    # Link each JE to the shared txn.
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=originator_company_id,
            ic_txn_id=ic_txn.id,
            journal_entry_id=orig_entry.id,
            side=IcLegSide.ORIGINATOR,
        )
    )
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=counterparty_company_id,
            ic_txn_id=ic_txn.id,
            journal_entry_id=cpty_entry.id,
            side=IcLegSide.COUNTERPARTY,
        )
    )

    # Single commit — all six objects (ic_txn, 2 JEs+lines, 2 ic_legs) land
    # together or not at all.
    await session.commit()
    return ic_txn


async def reverse_local_pair(
    session: AsyncSession,
    ic_txn_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    reversal_date: date | None = None,
    posted_by: str | None = None,
) -> IcTxn:
    """Settle/reverse an intercompany txn by reversing BOTH legs, linked.

    Reverses each leg's JE via ``journal.reverse`` (which posts a swapped
    mirror, flips the original to REVERSED, and audit-snapshots first), records
    each reversal JE as a new ``IcLeg`` against the SAME ``ic_txn`` (so the
    unwind stays linked and traceable both directions), and flips the
    ``IcTxn`` to ``REVERSED``.

    Note ``journal.reverse`` commits internally per leg; the two reversals are
    therefore not a single DB transaction, but each reversal is individually
    atomic and idempotency is protected by the ``IcTxn`` status guard (a
    REVERSED txn cannot be reversed again).
    """
    ic_txn = (
        await session.execute(
            select(IcTxn).where(
                IcTxn.id == ic_txn_id,
                IcTxn.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if ic_txn is None:
        raise IntercompanyError("Intercompany transaction not found")
    if ic_txn.status == IcTxnStatus.REVERSED:
        raise IntercompanyError("Intercompany transaction is already reversed")

    legs = (
        await session.execute(
            select(IcLeg).where(
                IcLeg.ic_txn_id == ic_txn_id,
                IcLeg.tenant_id == tenant_id,
            )
        )
    ).scalars().all()
    if not legs:
        raise IntercompanyError("Intercompany transaction has no legs to reverse")

    for leg in legs:
        try:
            reversal = await journal_svc.reverse(
                session,
                leg.journal_entry_id,
                reversal_date=reversal_date,
                posted_by=posted_by,
                tenant_id=tenant_id,
            )
        except PostingError as exc:  # surface as an IC-level failure
            raise IntercompanyError(
                f"Could not reverse intercompany leg: {exc}"
            ) from exc
        # Link the reversal JE to the same ic_txn (mirror side).
        mirror_side = (
            IcLegSide.COUNTERPARTY
            if leg.side == IcLegSide.ORIGINATOR
            else IcLegSide.ORIGINATOR
        )
        session.add(
            IcLeg(
                tenant_id=tenant_id,
                company_id=leg.company_id,
                ic_txn_id=ic_txn_id,
                journal_entry_id=reversal.id,
                # Distinct (company, txn, side) from the original leg: the
                # original leg already holds (company, txn, leg.side), so the
                # reversal records the mirror side for that company.
                side=mirror_side,
            )
        )

    ic_txn.status = IcTxnStatus.REVERSED
    await session.commit()
    return ic_txn
