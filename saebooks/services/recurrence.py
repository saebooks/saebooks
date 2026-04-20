"""Recurring-invoice engine.

A ``RecurringInvoice`` is a template + schedule. On its ``next_run``
date, :func:`materialise_one` forks a fresh DRAFT :class:`Invoice`
using the template's lines and advances ``next_run`` to the next
anchor in the series. The ``generate-recurring`` CLI entry point walks
every due template once per day (driven by cron).

Month-end safety:
    Monthly/quarterly/yearly advances snap to ``anchor_day`` and cap
    at the target month's last day. So a 31-Jan MONTHLY yields
    28-Feb (or 29 in a leap year) and then climbs back to 31-Mar
    without the "28th drift" bug that plagues naive day-add code.

Idempotence:
    :func:`due_today` filters to ACTIVE templates with
    ``next_run <= as_of``. After :func:`materialise_one` advances the
    anchor, a second call on the same day yields no new work — so a
    cron run that accidentally fires twice can't double-mint invoices.
"""
from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.invoice import Invoice
from saebooks.models.recurring_invoice import (
    RecurrenceFrequency,
    RecurrenceStatus,
    RecurringInvoice,
    RecurringInvoiceLine,
)
from saebooks.services import invoices as invoice_svc


class RecurrenceError(ValueError):
    """Raised on template validation or state-transition failure."""


# ---------------------------------------------------------------------- #
# Date arithmetic                                                         #
# ---------------------------------------------------------------------- #


def _add_months(src: date, months: int, anchor_day: int | None) -> date:
    """Advance ``src`` by ``months`` calendar months, snapping to anchor.

    The "anchor-day + cap" rule keeps 31-Jan → 28-Feb → 31-Mar from
    drifting down to the 28th on every roll. If ``anchor_day`` is
    ``None`` we use ``src.day`` as the anchor (first run seeds it).
    """
    anchor = anchor_day or src.day
    total = src.month - 1 + months
    year = src.year + total // 12
    month = total % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor, last))


def advance(
    current: date, frequency: RecurrenceFrequency, anchor_day: int | None
) -> date:
    """Return the next run date for a template with the given cadence."""
    if frequency == RecurrenceFrequency.WEEKLY:
        return current + timedelta(days=7)
    if frequency == RecurrenceFrequency.FORTNIGHTLY:
        return current + timedelta(days=14)
    if frequency == RecurrenceFrequency.MONTHLY:
        return _add_months(current, 1, anchor_day)
    if frequency == RecurrenceFrequency.QUARTERLY:
        return _add_months(current, 3, anchor_day)
    if frequency == RecurrenceFrequency.YEARLY:
        return _add_months(current, 12, anchor_day)
    raise RecurrenceError(f"Unknown frequency {frequency!r}")


def _default_anchor_day(
    frequency: RecurrenceFrequency, next_run: date
) -> int | None:
    """Seed anchor_day from the first next_run for calendar cadences."""
    if frequency in (
        RecurrenceFrequency.MONTHLY,
        RecurrenceFrequency.QUARTERLY,
        RecurrenceFrequency.YEARLY,
    ):
        return next_run.day
    return None


# ---------------------------------------------------------------------- #
# CRUD                                                                    #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal


def _normalise_line(raw: dict[str, object], line_no: int) -> RecurringInvoiceLine:
    tax_code_id = raw.get("tax_code_id")
    if isinstance(tax_code_id, str) and tax_code_id:
        tax_code_id = uuid.UUID(tax_code_id)
    elif not tax_code_id:
        tax_code_id = None

    account_id = raw["account_id"]
    if not isinstance(account_id, uuid.UUID):
        account_id = uuid.UUID(str(account_id))

    return RecurringInvoiceLine(
        line_no=line_no,
        description=str(raw["description"]),
        account_id=account_id,
        tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
        quantity=Decimal(str(raw.get("quantity", 1))),
        unit_price=Decimal(str(raw.get("unit_price", 0))),
        discount_pct=Decimal(str(raw.get("discount_pct", 0))),
    )


async def create(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    name: str,
    frequency: RecurrenceFrequency,
    next_run: date,
    lines: list[dict[str, object]],
    anchor_day: int | None = None,
    end_date: date | None = None,
    due_days: int = 30,
    payment_terms: str | None = None,
    notes: str | None = None,
    auto_post: bool = False,
) -> RecurringInvoice:
    if not lines:
        raise RecurrenceError("At least one line is required")
    if due_days < 0:
        raise RecurrenceError("due_days must be non-negative")
    if end_date is not None and end_date < next_run:
        raise RecurrenceError("end_date must be on or after next_run")

    effective_anchor = anchor_day or _default_anchor_day(frequency, next_run)

    template = RecurringInvoice(
        company_id=company_id,
        contact_id=contact_id,
        name=name.strip(),
        frequency=frequency,
        status=RecurrenceStatus.ACTIVE,
        anchor_day=effective_anchor,
        next_run=next_run,
        end_date=end_date,
        due_days=due_days,
        payment_terms=payment_terms,
        notes=notes,
        auto_post=auto_post,
    )
    for idx, raw in enumerate(lines, 1):
        template.lines.append(_normalise_line(raw, idx))
    session.add(template)
    await session.flush()
    await session.commit()
    return await get(session, template.id)


async def get(
    session: AsyncSession, template_id: uuid.UUID
) -> RecurringInvoice:
    result = await session.execute(
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(RecurringInvoice.id == template_id)
    )
    tpl = result.scalar_one_or_none()
    if tpl is None:
        raise RecurrenceError(f"Recurring invoice {template_id} not found")
    return tpl


async def list_templates(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: RecurrenceStatus | None = None,
    include_archived: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[RecurringInvoice]:
    stmt = (
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(RecurringInvoice.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(RecurringInvoice.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(RecurringInvoice.status == status)
    stmt = stmt.order_by(RecurringInvoice.next_run, RecurringInvoice.created_at)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update(
    session: AsyncSession,
    template_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    name: str | None = None,
    frequency: RecurrenceFrequency | None = None,
    next_run: date | None = None,
    anchor_day: int | None = None,
    end_date: date | None = None,
    due_days: int | None = None,
    payment_terms: str | None = None,
    notes: str | None = None,
    auto_post: bool | None = None,
    lines: list[dict[str, object]] | None = None,
) -> RecurringInvoice:
    tpl = await get(session, template_id)
    if contact_id is not None:
        tpl.contact_id = contact_id
    if name is not None:
        tpl.name = name.strip()
    if frequency is not None:
        tpl.frequency = frequency
    if next_run is not None:
        tpl.next_run = next_run
    if anchor_day is not None:
        tpl.anchor_day = anchor_day
    if end_date is not None:
        tpl.end_date = end_date
    if due_days is not None:
        if due_days < 0:
            raise RecurrenceError("due_days must be non-negative")
        tpl.due_days = due_days
    if payment_terms is not None:
        tpl.payment_terms = payment_terms
    if notes is not None:
        tpl.notes = notes
    if auto_post is not None:
        tpl.auto_post = auto_post
    if lines is not None:
        if not lines:
            raise RecurrenceError("At least one line is required")
        from sqlalchemy import delete as sa_delete

        await session.execute(
            sa_delete(RecurringInvoiceLine).where(
                RecurringInvoiceLine.recurring_invoice_id == tpl.id
            )
        )
        await session.flush()
        session.expire(tpl, ["lines"])
        for idx, raw in enumerate(lines, 1):
            new_line = _normalise_line(raw, idx)
            new_line.recurring_invoice_id = tpl.id
            session.add(new_line)
        await session.flush()
    await session.commit()
    return await get(session, tpl.id)


# ---------------------------------------------------------------------- #
# Lifecycle transitions                                                   #
# ---------------------------------------------------------------------- #


async def pause(
    session: AsyncSession, template_id: uuid.UUID
) -> RecurringInvoice:
    tpl = await get(session, template_id)
    if tpl.status == RecurrenceStatus.ENDED:
        raise RecurrenceError("Cannot pause an ENDED template")
    tpl.status = RecurrenceStatus.PAUSED
    await session.commit()
    return tpl


async def resume(
    session: AsyncSession, template_id: uuid.UUID
) -> RecurringInvoice:
    tpl = await get(session, template_id)
    if tpl.status == RecurrenceStatus.ENDED:
        raise RecurrenceError("Cannot resume an ENDED template")
    tpl.status = RecurrenceStatus.ACTIVE
    await session.commit()
    return tpl


async def end(
    session: AsyncSession, template_id: uuid.UUID
) -> RecurringInvoice:
    tpl = await get(session, template_id)
    tpl.status = RecurrenceStatus.ENDED
    await session.commit()
    return tpl


async def archive(
    session: AsyncSession, template_id: uuid.UUID
) -> RecurringInvoice:
    tpl = await get(session, template_id)
    tpl.archived_at = datetime.now(UTC)
    await session.commit()
    return tpl


# ---------------------------------------------------------------------- #
# Runtime                                                                 #
# ---------------------------------------------------------------------- #


async def due_today(
    session: AsyncSession,
    *,
    as_of: date | None = None,
    company_id: uuid.UUID | None = None,
) -> list[RecurringInvoice]:
    """Return ACTIVE templates whose ``next_run`` is due on or before ``as_of``."""
    today = as_of or date.today()
    stmt = (
        select(RecurringInvoice)
        .options(selectinload(RecurringInvoice.lines))
        .where(
            RecurringInvoice.status == RecurrenceStatus.ACTIVE,
            RecurringInvoice.archived_at.is_(None),
            RecurringInvoice.next_run <= today,
        )
    )
    if company_id is not None:
        stmt = stmt.where(RecurringInvoice.company_id == company_id)
    stmt = stmt.order_by(RecurringInvoice.next_run, RecurringInvoice.created_at)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def materialise_one(
    session: AsyncSession,
    template: RecurringInvoice,
    *,
    as_of: date | None = None,
) -> Invoice:
    """Fork one DRAFT invoice from a template and advance its cadence.

    ``auto_post=True`` flips the freshly-minted invoice to POSTED (and
    mints its number) in the same transaction. The template's
    ``last_run`` + ``invoices_generated`` + ``next_run`` are always
    updated, even for auto-posted runs.

    If the new ``next_run`` lands past ``end_date``, the template is
    moved to ``ENDED`` so it stops firing.
    """
    today = as_of or date.today()
    if template.status != RecurrenceStatus.ACTIVE:
        raise RecurrenceError(
            f"Template {template.id} is not ACTIVE "
            f"(status={template.status.value})"
        )
    if template.next_run > today:
        raise RecurrenceError(
            f"Template {template.id} is not yet due "
            f"(next_run={template.next_run.isoformat()}, as_of={today.isoformat()})"
        )

    issue_date = template.next_run
    due_date = issue_date + timedelta(days=template.due_days)

    lines_payload: list[dict[str, object]] = [
        {
            "description": ln.description,
            "account_id": ln.account_id,
            "tax_code_id": ln.tax_code_id,
            "quantity": ln.quantity,
            "unit_price": ln.unit_price,
            "discount_pct": ln.discount_pct,
        }
        for ln in sorted(template.lines, key=lambda ln: ln.line_no)
    ]

    invoice = await invoice_svc.create_draft(
        session,
        company_id=template.company_id,
        contact_id=template.contact_id,
        issue_date=issue_date,
        due_date=due_date,
        lines=lines_payload,
        notes=template.notes,
        payment_terms=template.payment_terms,
    )
    if template.auto_post:
        invoice = await invoice_svc.post_invoice(
            session, invoice.id, posted_by="recurring"
        )

    # Advance the template.
    template.last_run = issue_date
    template.invoices_generated += 1
    new_next = advance(template.next_run, template.frequency, template.anchor_day)
    template.next_run = new_next
    if template.end_date is not None and new_next > template.end_date:
        template.status = RecurrenceStatus.ENDED
    await session.commit()
    return invoice


async def run_due(
    session: AsyncSession,
    *,
    as_of: date | None = None,
    company_id: uuid.UUID | None = None,
) -> list[Invoice]:
    """Materialise every due template. Loops until no more are due.

    A single MONTHLY template that's been paused for 3 months catches
    up in three passes through :func:`materialise_one`. The inner loop
    stops when the template's advance pushes ``next_run`` past
    ``as_of``.
    """
    today = as_of or date.today()
    created: list[Invoice] = []
    # Re-query each iteration because materialise_one commits.
    while True:
        due = await due_today(session, as_of=today, company_id=company_id)
        if not due:
            break
        progress = False
        for tpl in due:
            inv = await materialise_one(session, tpl, as_of=today)
            created.append(inv)
            progress = True
        if not progress:
            break
    return created
