"""Time-entry service — CRUD + approval workflow + convert-to-invoice.

Standalone v1, ahead of the full payroll-grade ``employees`` table.

Workflow (entries move forward only — no DRAFT→APPROVED shortcut):

    DRAFT --submit--> SUBMITTED --approve--> APPROVED --lock--> LOCKED
                                \\--reject--> REJECTED

A REJECTED entry can be edited and re-submitted (goes back to DRAFT
implicitly on the next save). A LOCKED entry belongs to a finalised
pay run (future) or to a posted invoice; no further mutation allowed.

The convert-to-invoice path is the immediate v1 win:
``convert_to_invoice_line(...)`` takes N billable entries that all
share the same contact, bundles them into a single new invoice line
(description = "<hours>h <project>: <description>" or similar), and
writes ``invoice_line_id`` back on every consumed entry. The invoice
is created in DRAFT if not supplied. Idempotent: re-converting an
already-converted entry no-ops.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.time_entry import TimeEntry, TimeEntryApprovalStatus

_TWOPLACES = Decimal("0.01")
_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --- shared helpers --------------------------------------------------------


class TimeEntryError(Exception):
    """Domain-level error — service raises, router translates to HTTPException."""

    def __init__(self, message: str, *, code: str = "time_entry_error") -> None:
        super().__init__(message)
        self.code = code


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _is_editable(entry: TimeEntry) -> bool:
    """DRAFT and REJECTED entries can be edited. Anything else is frozen."""
    return entry.approval_status in (
        TimeEntryApprovalStatus.DRAFT.value,
        TimeEntryApprovalStatus.REJECTED.value,
    )


# --- create / read / update / archive --------------------------------------


async def create(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    work_date: date,
    hours: Decimal,
    description: str = "",
    contact_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    cost_centre_id: uuid.UUID | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    break_minutes: int = 0,
    billable: bool = False,
    rate: Decimal | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> TimeEntry:
    if hours <= Decimal("0"):
        raise TimeEntryError("hours must be > 0", code="invalid_hours")
    if (start_time is None) != (end_time is None):
        raise TimeEntryError(
            "start_time and end_time must be set together",
            code="invalid_clock_pair",
        )
    if break_minutes < 0:
        raise TimeEntryError("break_minutes cannot be negative", code="invalid_break")
    if billable and rate is not None and rate < 0:
        raise TimeEntryError("rate cannot be negative", code="invalid_rate")

    entry = TimeEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        user_id=user_id,
        contact_id=contact_id,
        work_date=work_date,
        hours=hours,
        start_time=start_time,
        end_time=end_time,
        break_minutes=break_minutes,
        description=description,
        project_id=project_id,
        department_id=department_id,
        cost_centre_id=cost_centre_id,
        billable=billable,
        rate=rate,
        approval_status=TimeEntryApprovalStatus.DRAFT.value,
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


async def get(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entry_id: uuid.UUID,
) -> TimeEntry | None:
    stmt = select(TimeEntry).where(
        TimeEntry.company_id == company_id,
        TimeEntry.id == entry_id,
        TimeEntry.archived_at.is_(None),
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


@dataclass
class TimeEntryFilters:
    user_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    approval_status: str | None = None
    billable_only: bool = False
    uninvoiced_only: bool = False
    date_from: date | None = None
    date_to: date | None = None


async def list_entries(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    filters: TimeEntryFilters | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[TimeEntry], int]:
    filters = filters or TimeEntryFilters()

    where = [TimeEntry.company_id == company_id, TimeEntry.archived_at.is_(None)]
    if filters.user_id:
        where.append(TimeEntry.user_id == filters.user_id)
    if filters.contact_id:
        where.append(TimeEntry.contact_id == filters.contact_id)
    if filters.project_id:
        where.append(TimeEntry.project_id == filters.project_id)
    if filters.approval_status:
        where.append(TimeEntry.approval_status == filters.approval_status)
    if filters.billable_only:
        where.append(TimeEntry.billable.is_(True))
    if filters.uninvoiced_only:
        where.append(TimeEntry.invoice_line_id.is_(None))
    if filters.date_from:
        where.append(TimeEntry.work_date >= filters.date_from)
    if filters.date_to:
        where.append(TimeEntry.work_date <= filters.date_to)

    count_stmt = select(func.count()).select_from(TimeEntry).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        select(TimeEntry)
        .where(*where)
        .order_by(TimeEntry.work_date.desc(), TimeEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(items_stmt)).scalars().all())
    return items, int(total)


async def update(
    session: AsyncSession,
    *,
    entry: TimeEntry,
    expected_version: int | None = None,
    force: bool = False,
    **fields: object,
) -> TimeEntry:
    if not force and not _is_editable(entry):
        raise TimeEntryError(
            f"entry is {entry.approval_status} — only DRAFT/REJECTED entries can be edited",
            code="not_editable",
        )
    if expected_version is not None and entry.version != expected_version:
        raise TimeEntryError(
            f"version mismatch: expected {expected_version}, got {entry.version}",
            code="version_mismatch",
        )

    # Whitelist the patchable fields. PATCH callers can pass None to
    # explicitly clear an optional FK.
    ALLOWED = {
        "work_date",
        "hours",
        "description",
        "contact_id",
        "project_id",
        "department_id",
        "cost_centre_id",
        "start_time",
        "end_time",
        "break_minutes",
        "billable",
        "rate",
    }
    for name, value in fields.items():
        if name not in ALLOWED:
            continue
        setattr(entry, name, value)

    # Editing a REJECTED entry moves it back to DRAFT — caller can
    # re-submit when they're happy.
    if entry.approval_status == TimeEntryApprovalStatus.REJECTED.value:
        entry.approval_status = TimeEntryApprovalStatus.DRAFT.value
        entry.rejection_reason = None

    entry.version += 1
    await session.flush()
    await session.refresh(entry)
    return entry


async def archive(
    session: AsyncSession,
    *,
    entry: TimeEntry,
) -> TimeEntry:
    """Soft-delete. Only DRAFT entries can be archived freely.

    APPROVED/LOCKED entries must NOT vanish — they have downstream
    consequences (an invoice line, a future pay run line) that need a
    history trail. Callers can ``void`` an LOCKED entry's downstream
    line, which kicks back to APPROVED, then archive — but that's a
    different operation.
    """
    if entry.approval_status not in (
        TimeEntryApprovalStatus.DRAFT.value,
        TimeEntryApprovalStatus.REJECTED.value,
    ):
        raise TimeEntryError(
            f"cannot archive {entry.approval_status} entry — only DRAFT/REJECTED allowed",
            code="not_archivable",
        )
    entry.archived_at = datetime.now(UTC)
    await session.flush()
    return entry


# --- approval workflow -----------------------------------------------------


async def submit(
    session: AsyncSession,
    *,
    entry: TimeEntry,
) -> TimeEntry:
    if entry.approval_status != TimeEntryApprovalStatus.DRAFT.value:
        raise TimeEntryError(
            f"can only submit DRAFT entries; got {entry.approval_status}",
            code="wrong_state",
        )
    entry.approval_status = TimeEntryApprovalStatus.SUBMITTED.value
    entry.submitted_at = datetime.now(UTC)
    entry.version += 1
    await session.flush()
    await session.refresh(entry)
    return entry


async def approve(
    session: AsyncSession,
    *,
    entry: TimeEntry,
    approver_user_id: uuid.UUID,
) -> TimeEntry:
    if entry.approval_status != TimeEntryApprovalStatus.SUBMITTED.value:
        raise TimeEntryError(
            f"can only approve SUBMITTED entries; got {entry.approval_status}",
            code="wrong_state",
        )
    entry.approval_status = TimeEntryApprovalStatus.APPROVED.value
    entry.approved_at = datetime.now(UTC)
    entry.approved_by = approver_user_id
    entry.rejection_reason = None
    entry.version += 1
    await session.flush()
    await session.refresh(entry)
    return entry


async def revert(
    session: AsyncSession,
    *,
    entry: TimeEntry,
) -> TimeEntry:
    """Revert an APPROVED entry back to DRAFT so it can be edited or archived.

    Only allowed when:
      * approval_status is APPROVED
      * invoice_line_id is null (revert after invoicing would orphan a line)

    Clears approved_at + approved_by and bumps the version. Once back at
    DRAFT the caller can edit via PATCH or archive via DELETE.
    """
    if entry.approval_status != TimeEntryApprovalStatus.APPROVED.value:
        raise TimeEntryError(
            f"can only revert APPROVED entries; got {entry.approval_status}",
            code="wrong_state",
        )
    if entry.invoice_line_id is not None:
        raise TimeEntryError(
            "cannot revert an entry already on an invoice — void the invoice line first",
            code="already_invoiced",
        )
    entry.approval_status = TimeEntryApprovalStatus.DRAFT.value
    entry.approved_at = None
    entry.approved_by = None
    entry.submitted_at = None
    entry.version += 1
    await session.flush()
    await session.refresh(entry)
    return entry


async def reject(
    session: AsyncSession,
    *,
    entry: TimeEntry,
    approver_user_id: uuid.UUID,
    reason: str,
) -> TimeEntry:
    if entry.approval_status != TimeEntryApprovalStatus.SUBMITTED.value:
        raise TimeEntryError(
            f"can only reject SUBMITTED entries; got {entry.approval_status}",
            code="wrong_state",
        )
    if not reason.strip():
        raise TimeEntryError("rejection_reason is required", code="missing_reason")
    entry.approval_status = TimeEntryApprovalStatus.REJECTED.value
    entry.approved_by = approver_user_id  # records WHO rejected too
    entry.rejection_reason = reason.strip()
    entry.version += 1
    await session.flush()
    await session.refresh(entry)
    return entry


# --- convert to invoice line -----------------------------------------------


@dataclass
class ConvertResult:
    invoice_id: uuid.UUID
    invoice_line_id: uuid.UUID
    converted_entry_ids: list[uuid.UUID]
    total_hours: Decimal
    total_amount: Decimal


async def convert_to_invoice_line(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entry_ids: list[uuid.UUID],
    invoice_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> ConvertResult:
    """Bundle the given billable entries into one new invoice line.

    Either supply ``invoice_id`` (append to an existing DRAFT invoice)
    or ``contact_id`` (create a new DRAFT invoice). If neither is
    supplied, the contact is inferred from the entries — they must
    all share one, else we error out.
    """
    if not entry_ids:
        raise TimeEntryError("entry_ids cannot be empty", code="no_entries")

    # Load entries.
    entries = list(
        (
            await session.execute(
                select(TimeEntry).where(
                    TimeEntry.company_id == company_id,
                    TimeEntry.id.in_(entry_ids),
                    TimeEntry.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(entries) != len(entry_ids):
        missing = set(entry_ids) - {e.id for e in entries}
        raise TimeEntryError(
            f"entries not found in this company: {sorted(missing)}",
            code="entries_not_found",
        )

    for e in entries:
        if not e.billable:
            raise TimeEntryError(
                f"entry {e.id} is not billable", code="not_billable"
            )
        if e.invoice_line_id is not None:
            raise TimeEntryError(
                f"entry {e.id} is already on invoice line {e.invoice_line_id}",
                code="already_converted",
            )
        if e.approval_status == TimeEntryApprovalStatus.LOCKED.value:
            raise TimeEntryError(
                f"entry {e.id} is LOCKED", code="locked"
            )

    # Pick target invoice — either passed in, inferred, or new.
    if invoice_id is not None:
        invoice = await session.get(Invoice, invoice_id)
        if invoice is None or invoice.company_id != company_id:
            raise TimeEntryError("invoice not found", code="invoice_not_found")
        if invoice.status != InvoiceStatus.DRAFT.value:
            raise TimeEntryError(
                f"invoice is {invoice.status}; only DRAFT invoices can take new lines",
                code="invoice_not_draft",
            )
    else:
        # Infer contact if not supplied.
        if contact_id is None:
            distinct_contacts = {e.contact_id for e in entries if e.contact_id}
            if len(distinct_contacts) == 0:
                raise TimeEntryError(
                    "entries have no contact; pass contact_id explicitly",
                    code="missing_contact",
                )
            if len(distinct_contacts) > 1:
                raise TimeEntryError(
                    "entries span multiple contacts; pass contact_id explicitly",
                    code="multiple_contacts",
                )
            contact_id = distinct_contacts.pop()

        contact = await session.get(Contact, contact_id)
        if contact is None or contact.company_id != company_id:
            raise TimeEntryError(
                "contact not found", code="contact_not_found"
            )

        # Create a minimal DRAFT invoice. Use today as issue/due — the
        # operator can edit on the invoice screen before posting.
        today = date.today()
        invoice = Invoice(
            company_id=company_id,
            tenant_id=tenant_id,
            contact_id=contact_id,
            number=None,  # numbered at post-time
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT.value,
            currency="AUD",
            fx_rate=Decimal("1"),
        )
        session.add(invoice)
        await session.flush()

    # Compute bundled hours + dollar value. Rate per entry; mixed rates
    # are fine — we sum the per-entry totals.
    total_hours = Decimal("0")
    total_amount = Decimal("0")
    for e in entries:
        total_hours += e.hours
        rate = e.rate or Decimal("0")
        total_amount += rate * e.hours
    total_amount = _q2(total_amount)

    # Build the invoice line description from the entries — concise.
    if len(entries) == 1:
        e = entries[0]
        desc = e.description or f"Time worked {e.work_date.isoformat()} ({e.hours}h)"
    else:
        dates = sorted({e.work_date for e in entries})
        desc = (
            f"Time worked {dates[0].isoformat()} to {dates[-1].isoformat()} "
            f"({total_hours}h, {len(entries)} entries)"
        )

    # Resolve an income account: pick the company's first INCOME account
    # ordered by code as the safe default. The operator can change it
    # on the invoice line edit screen.
    income_acct = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.account_type == AccountType.INCOME,
                Account.archived_at.is_(None),
            )
            .order_by(Account.code)
            .limit(1)
        )
    ).scalar_one_or_none()
    if income_acct is None:
        raise TimeEntryError(
            "no income account configured for company",
            code="no_income_account",
        )

    # Pick next line_no.
    next_line_no_stmt = (
        select(func.coalesce(func.max(InvoiceLine.line_no), 0) + 1).where(
            InvoiceLine.invoice_id == invoice.id
        )
    )
    next_line_no = (
        await session.execute(next_line_no_stmt)
    ).scalar_one()

    # Use the first entry's rate as the line's unit price; quantity = total_hours.
    unit_price = entries[0].rate if entries[0].rate is not None else Decimal("0")
    line_subtotal = _q2(unit_price * total_hours)

    line = InvoiceLine(
        invoice_id=invoice.id,
        line_no=int(next_line_no),
        description=desc,
        account_id=income_acct.id,
        quantity=total_hours,
        unit_price=unit_price,
        discount_pct=Decimal("0"),
        line_subtotal=line_subtotal,
        line_tax=Decimal("0"),
        line_total=line_subtotal,
        project_id=entries[0].project_id,
    )
    session.add(line)
    await session.flush()

    # Write back the FK on every entry.
    for e in entries:
        e.invoice_line_id = line.id
        e.version += 1

    await session.flush()

    return ConvertResult(
        invoice_id=invoice.id,
        invoice_line_id=line.id,
        converted_entry_ids=[e.id for e in entries],
        total_hours=total_hours,
        total_amount=line_subtotal,
    )


# --- weekly grid helper ----------------------------------------------------


async def list_week(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    week_start: date,
) -> list[TimeEntry]:
    """Return all entries (DRAFT, SUBMITTED, APPROVED, REJECTED) for a
    single ISO-week starting on Monday ``week_start``. Hidden:
    ``archived_at IS NOT NULL`` and ``LOCKED`` (we don't show
    pay-locked entries in the editable grid).
    """
    from datetime import timedelta as _td

    week_end = week_start + _td(days=6)
    stmt = (
        select(TimeEntry)
        .where(
            TimeEntry.company_id == company_id,
            TimeEntry.user_id == user_id,
            TimeEntry.work_date >= week_start,
            TimeEntry.work_date <= week_end,
            TimeEntry.archived_at.is_(None),
            TimeEntry.approval_status != TimeEntryApprovalStatus.LOCKED.value,
        )
        .order_by(TimeEntry.work_date.asc(), TimeEntry.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())
