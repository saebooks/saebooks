"""Gap 3 — "flag for review" on transactions / invoices / expenses.

A lightweight, non-posting metadata toggle: ``flagged_for_review`` (bool) +
``review_note`` (optional text) on ``journal_entries`` (transactions/JEs),
``invoices`` and ``expenses`` (migration 0157). Lets a reviewer mark an item for
follow-up during a books review WITHOUT touching its posting state or version
(the flag is metadata, not a financial mutation — bumping ``version`` would
collide with optimistic-locking on posts/edits).

One generic setter keyed by entity name so the three API routers share exactly
one code path. The change is recorded in ``change_log`` (op="update") for
attribution, but it does NOT bump the entity ``version`` and does NOT write a JE
— flagging never changes the ledger.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.expense import Expense
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry
from saebooks.services import change_log as change_log_svc

# entity-name -> (ORM class, change_log entity string)
_REGISTRY: dict[str, tuple[type, str]] = {
    "journal_entry": (JournalEntry, "journal_entry"),
    "invoice": (Invoice, "invoice"),
    "expense": (Expense, "expense"),
}


class ReviewFlagError(ValueError):
    """Raised when the target row is missing or the entity name is unknown."""


def _to_jsonable(val: object) -> object:
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if hasattr(val, "value"):
        return val.value
    return val


async def set_review_flag(
    session: AsyncSession,
    entity: str,
    row_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: str,
    flagged: bool,
    review_note: str | None = None,
) -> object:
    """Set/clear the review flag on one row of ``entity``.

    Scoped by tenant + company (belt-and-braces over FORCE RLS). ``review_note``
    is set when provided; clearing the flag (``flagged=False``) also clears the
    note. Records a change_log row but does NOT bump the entity version.

    Returns the refreshed ORM row. Raises ``ReviewFlagError`` if the entity name
    is unknown or the row does not exist for this scope.
    """
    reg = _REGISTRY.get(entity)
    if reg is None:
        raise ReviewFlagError(f"Unknown review-flag entity {entity!r}")
    model, cl_entity = reg

    row = (
        await session.execute(
            select(model).where(
                model.id == row_id,
                model.tenant_id == tenant_id,
                model.company_id == company_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ReviewFlagError(f"{entity} {row_id} not found")

    row.flagged_for_review = flagged
    if flagged:
        # Only overwrite the note when explicitly provided on a set; preserve
        # an existing note if the caller flips the flag without sending one.
        if review_note is not None:
            row.review_note = review_note
    else:
        # Clearing the flag clears the note too.
        row.review_note = None
    await session.flush()

    await change_log_svc.append(
        session,
        entity=cl_entity,
        entity_id=row.id,
        op="update",
        actor=actor,
        payload={
            "id": str(row.id),
            "flagged_for_review": row.flagged_for_review,
            "review_note": row.review_note,
        },
        version=getattr(row, "version", 0),
    )
    await session.commit()
    await session.refresh(row)
    return row


__all__ = ["ReviewFlagError", "set_review_flag"]
