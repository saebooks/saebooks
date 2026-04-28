"""Journal entry service — create, update, post, reverse, delete.

Business rules:
- Drafts may be unbalanced; posts must balance (sum debits == sum credits).
- Period-lock check: posting into a locked period requires override_reason.
- Immutable mode (Community default): posted entries can only be reversed,
  not edited. Hybrid/Open modes allow edit with full audit trail.
- Auto-ref: JE-NNNNNN, from a Postgres sequence. User may override.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, PeriodLock
from saebooks.services import audit as audit_svc
from saebooks.services import gst as gst_svc
from saebooks.services import settings as settings_svc


class PostingError(Exception):
    pass


async def _validate_line_accounts(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[dict[str, object]],
    *,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Raise PostingError if any line account belongs to a different company or tenant."""
    if not lines:
        return
    ids = [uuid.UUID(str(ln["account_id"])) for ln in lines]
    result = await session.execute(
        select(Account.id, Account.company_id, Account.tenant_id).where(Account.id.in_(ids))
    )
    rows = {r.id: r for r in result.all()}
    bad = []
    for i in ids:
        row = rows.get(i)
        if row is None or row.company_id != company_id:
            bad.append(i)
        elif tenant_id is not None and row.tenant_id != tenant_id:
            bad.append(i)
    if bad:
        raise PostingError(
            "Account(s) do not belong to this company: "
            + ", ".join(str(i) for i in bad)
        )


async def next_ref(session: AsyncSession) -> str:
    result = await session.execute(text("SELECT nextval('journal_ref_seq')"))
    seq = result.scalar_one()
    return f"JE-{seq:06d}"


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entry_date: date,
    description: str | None = None,
    ref: str | None = None,
    lines: list[dict[str, object]] | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    if not ref:
        ref = await next_ref(session)

    entry = JournalEntry(
        company_id=company_id,
        ref=ref,
        entry_date=entry_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    if lines:
        await _validate_line_accounts(session, company_id, lines, tenant_id=tenant_id)
        for i, line_data in enumerate(lines, 1):
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    line_no=i,
                    account_id=line_data["account_id"],
                    description=line_data.get("description"),
                    debit=Decimal(str(line_data.get("debit", 0))),
                    credit=Decimal(str(line_data.get("credit", 0))),
                    tax_code_id=line_data.get("tax_code_id"),
                    gst_amount=line_data.get("gst_amount"),
                    project_id=line_data.get("project_id"),
                )
            )

    await session.commit()
    return await get(session, entry.id)


async def get(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Fetch a journal entry by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id raises ``ValueError`` (treated as not found),
    so cross-tenant probes 404 even if the underlying row exists.
    Belt-and-braces complement to FORCE RLS at the DB layer.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.id == entry_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(JournalEntry.tenant_id == tenant_id)
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ValueError(f"Journal entry {entry_id} not found")
    return entry


async def list_entries(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: EntryStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[JournalEntry]:
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.company_id == company_id)
    )
    if status is not None:
        stmt = stmt.where(JournalEntry.status == status)
    stmt = stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.ref.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    entry_date: date | None = None,
    description: str | None = None,
    ref: str | None = None,
    lines: list[dict[str, object]] | None = None,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if entry.status != EntryStatus.DRAFT:
        audit_mode = await settings_svc.get(session, "audit_mode", "immutable")
        if audit_mode == "immutable":
            raise PostingError("Cannot edit a posted entry in immutable mode — reverse instead")

    before = audit_svc.capture(entry)

    if entry_date is not None:
        entry.entry_date = entry_date
    if description is not None:
        entry.description = description
    if ref is not None:
        entry.ref = ref

    if lines is not None:
        await _validate_line_accounts(session, entry.company_id, lines, tenant_id=entry.tenant_id)
        # Replace all lines
        for old_line in entry.lines:
            await session.delete(old_line)
        await session.flush()
        for i, line_data in enumerate(lines, 1):
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    line_no=i,
                    account_id=line_data["account_id"],
                    description=line_data.get("description"),
                    debit=Decimal(str(line_data.get("debit", 0))),
                    credit=Decimal(str(line_data.get("credit", 0))),
                    tax_code_id=line_data.get("tax_code_id"),
                    gst_amount=line_data.get("gst_amount"),
                    project_id=line_data.get("project_id"),
                )
            )

    await audit_svc.snapshot_row(
        session, entry,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    return await get(session, entry_id, tenant_id=tenant_id)


async def _check_balance(entry: JournalEntry) -> None:
    total_debit = sum(line.debit for line in entry.lines)
    total_credit = sum(line.credit for line in entry.lines)
    if total_debit != total_credit:
        raise PostingError(
            f"Entry {entry.ref} is unbalanced: "
            f"debits={total_debit}, credits={total_credit}"
        )
    if not entry.lines:
        raise PostingError(f"Entry {entry.ref} has no lines")


async def _check_period_lock(
    session: AsyncSession,
    company_id: uuid.UUID,
    entry_date: date,
    override_reason: str | None,
) -> None:
    result = await session.execute(
        select(func.max(PeriodLock.locked_through)).where(
            PeriodLock.company_id == company_id
        )
    )
    locked_through = result.scalar_one_or_none()
    if (
        locked_through is not None
        and entry_date <= locked_through
        and not override_reason
    ):
        raise PostingError(
            f"Period is locked through {locked_through}. "
            f"Provide an override reason to post into a locked period."
        )


async def post(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if entry.status == EntryStatus.POSTED:
        raise PostingError(f"Entry {entry.ref} is already posted")
    if entry.status == EntryStatus.REVERSED:
        raise PostingError(f"Entry {entry.ref} has been reversed")

    await _check_period_lock(session, entry.company_id, entry.entry_date, override_reason)

    # Auto-generate GST account lines BEFORE balancing.
    # Lines may carry `gst_amount` as the net/gross split metadata —
    # the auto-poster adds the matching DR GST Paid / CR GST Collected
    # line so the entry balances. Pre-checking balance here would reject
    # legitimate net+gst entries (e.g. DR Telephone 100 [+gst 10] / CR Bank 110).
    gst_lines = await gst_svc.auto_post_gst_lines(session, entry)
    if gst_lines:
        await session.flush()
        # auto_post_gst_lines appends to entry.lines in-place, so no re-fetch needed.

    # Final balance check — the entry must balance after GST has been posted.
    await _check_balance(entry)

    entry.status = EntryStatus.POSTED
    entry.posted_at = datetime.now(UTC)
    entry.posted_by = posted_by
    if override_reason:
        entry.override_reason = override_reason

    await session.commit()
    return entry


async def reverse(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    reversal_date: date | None = None,
    posted_by: str | None = None,
    override_reason: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Create and post a reversal of a posted entry."""
    original = await get(session, entry_id, tenant_id=tenant_id)
    if original.status != EntryStatus.POSTED:
        raise PostingError(f"Can only reverse posted entries (current: {original.status})")

    # Snapshot the original before we flip its status to REVERSED.
    await audit_svc.snapshot_row(
        session, original,
        action="reverse",
        reason=f"Reversed by new entry (date={reversal_date or original.entry_date})",
        performed_by=posted_by,
    )

    rev_date = reversal_date or original.entry_date
    rev_ref = await next_ref(session)

    reversal = JournalEntry(
        company_id=original.company_id,
        ref=rev_ref,
        entry_date=rev_date,
        description=f"Reversal of {original.ref}: {original.description or ''}".strip(),
        status=EntryStatus.DRAFT,
        reversal_of_id=original.id,
    )
    session.add(reversal)
    await session.flush()

    for line in original.lines:
        session.add(
            JournalLine(
                entry_id=reversal.id,
                line_no=line.line_no,
                account_id=line.account_id,
                description=line.description,
                debit=line.credit,
                credit=line.debit,
                tax_code_id=line.tax_code_id,
                # Do NOT carry gst_amount onto the reversal — the original
                # GST Collected/Paid line has already been copied (with
                # debit/credit swapped). Passing gst_amount would cause
                # auto_post_gst_lines to add a duplicate GST line on post().
                gst_amount=None,
                project_id=line.project_id,
            )
        )

    await session.commit()

    # Auto-post the reversal
    reversal = await post(
        session, reversal.id, posted_by=posted_by, override_reason=override_reason
    )

    # Mark original as reversed
    original.status = EntryStatus.REVERSED
    await session.commit()

    return reversal


async def delete(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Delete a journal entry and its lines. Any status — MYOB-style."""
    entry = await get(session, entry_id, tenant_id=tenant_id)
    await audit_svc.snapshot_row(
        session, entry,
        action="delete",
        performed_by=performed_by,
    )
    await session.delete(entry)
    await session.commit()


async def lock_period(
    session: AsyncSession,
    company_id: uuid.UUID,
    locked_through: date,
    *,
    locked_by: str | None = None,
    reason: str | None = None,
) -> PeriodLock:
    lock = PeriodLock(
        company_id=company_id,
        locked_through=locked_through,
        locked_by=locked_by,
        reason=reason,
    )
    session.add(lock)
    await session.commit()
    return lock
