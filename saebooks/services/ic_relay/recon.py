"""Intercompany reconciliation — read-only position view (Phase 3d, SAFE).

A read-only report over the intercompany tables that lists, for the active
company/tenant, every intercompany ``IcTxn`` together with its legs, and flags
**unmatched** legs — a txn that has not landed both halves. This is the human
surface for the "half-pair stuck" failure mode (relay plan §4.5 / §5.5): a
REMOTE event whose reciprocal leg never posted shows up here as unmatched for
human action; the engine NEVER auto-reverses a local leg on a delivery failure.

Read-only and reversible-by-construction: this module issues SELECTs only. It
writes nothing, posts nothing, and touches no shared state.

Scope note (deliberate, INERT-phase honest)
--------------------------------------------
The *full* cross-tenant elimination view (joining the two tenant DBs on the
shared ``ic_txn_id`` to net due-to against due-from on consolidation) requires
the broker registry + the live relay data and the accountant principal's
cross-tenant grants (relay plan §5.5 / §6 Phase 3d). Those land with the broker.
Until then this view operates entirely WITHIN one tenant, under that tenant's
own FORCE-RLS — there is no BYPASSRLS or cross-tenant data path here. What it
delivers now:

* every IC ``IcTxn`` for the active company, with its posted legs;
* a ``matched`` flag per txn — a LOCAL pair has both ORIGINATOR + COUNTERPARTY
  legs; a REMOTE-originated txn whose partner hasn't accepted has only one;
* the in-flight relay state from ``ic_outbox`` (PENDING/SENT/FAILED/DEAD) and
  ``ic_inbox`` (RECEIVED/POSTED/REJECTED) for the same txn ids, so once the
  live relay writes those rows the operator can see exactly which legs are stuck
  and why — without any of it being able to mutate the books.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.ic import (
    IcInbox,
    IcLeg,
    IcLegSide,
    IcOutbox,
    IcTxn,
)


@dataclass(frozen=True)
class ReconLeg:
    """One posted leg of an intercompany txn (read-only projection)."""

    id: uuid.UUID
    company_id: uuid.UUID
    journal_entry_id: uuid.UUID
    side: str


@dataclass(frozen=True)
class ReconRow:
    """One intercompany txn in the reconciliation view (read-only projection).

    ``matched`` is True when both the ORIGINATOR and COUNTERPARTY legs are
    present (a complete pair). ``outbox_status`` / ``inbox_status`` carry the
    in-flight relay state when present (None for a pure LOCAL pair, which never
    rides the relay).
    """

    ic_txn_id: uuid.UUID
    company_id: uuid.UUID
    status: str
    description: str | None
    matched: bool
    legs: list[ReconLeg] = field(default_factory=list)
    outbox_status: str | None = None
    inbox_status: str | None = None


async def intercompany_position(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> list[ReconRow]:
    """Return the read-only intercompany position for ``company_id``.

    Lists every ``IcTxn`` the active company participates in (owns OR has a leg
    in), each with its legs and a ``matched`` flag, plus any in-flight relay
    state from ``ic_outbox`` / ``ic_inbox`` for the same txn ids. SELECT-only;
    writes nothing. Every query is tenant- and company-scoped so it runs under
    the caller's own FORCE-RLS with no cross-tenant data path.
    """
    # Txn ids the active company has a leg in (it may be the counterparty of a
    # txn owned by the sister company).
    leg_txn_ids = (
        select(IcLeg.ic_txn_id)
        .where(
            IcLeg.tenant_id == tenant_id,
            IcLeg.company_id == company_id,
        )
        .scalar_subquery()
    )
    txns = (
        (
            await session.execute(
                select(IcTxn)
                .where(
                    IcTxn.tenant_id == tenant_id,
                    (IcTxn.company_id == company_id)
                    | (IcTxn.id.in_(leg_txn_ids)),
                )
                .order_by(IcTxn.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not txns:
        return []

    txn_ids = [t.id for t in txns]

    # All legs for those txns (tenant-scoped). Group in Python — small N.
    legs = (
        (
            await session.execute(
                select(IcLeg).where(
                    IcLeg.tenant_id == tenant_id,
                    IcLeg.ic_txn_id.in_(txn_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    legs_by_txn: dict[uuid.UUID, list[IcLeg]] = {}
    for leg in legs:
        legs_by_txn.setdefault(leg.ic_txn_id, []).append(leg)

    # In-flight relay state keyed by the shared ic_txn_id. ic_outbox.ic_txn_id
    # is the local txn id (FK); ic_inbox.ic_txn_id is the carried external id —
    # both are the same shared value by design, so keying on it links them.
    outbox = (
        (
            await session.execute(
                select(IcOutbox.ic_txn_id, IcOutbox.status).where(
                    IcOutbox.tenant_id == tenant_id,
                    IcOutbox.ic_txn_id.in_(txn_ids),
                )
            )
        )
        .all()
    )
    outbox_by_txn = {row.ic_txn_id: str(row.status) for row in outbox}
    inbox = (
        (
            await session.execute(
                select(IcInbox.ic_txn_id, IcInbox.status).where(
                    IcInbox.tenant_id == tenant_id,
                    IcInbox.ic_txn_id.in_(txn_ids),
                )
            )
        )
        .all()
    )
    inbox_by_txn = {row.ic_txn_id: str(row.status) for row in inbox}

    rows: list[ReconRow] = []
    for txn in txns:
        txn_legs = legs_by_txn.get(txn.id, [])
        sides = {leg.side for leg in txn_legs}
        matched = (
            IcLegSide.ORIGINATOR in sides and IcLegSide.COUNTERPARTY in sides
        )
        rows.append(
            ReconRow(
                ic_txn_id=txn.id,
                company_id=txn.company_id,
                status=str(txn.status),
                description=txn.description,
                matched=matched,
                legs=[
                    ReconLeg(
                        id=leg.id,
                        company_id=leg.company_id,
                        journal_entry_id=leg.journal_entry_id,
                        side=str(leg.side),
                    )
                    for leg in txn_legs
                ],
                outbox_status=outbox_by_txn.get(txn.id),
                inbox_status=inbox_by_txn.get(txn.id),
            )
        )
    return rows
