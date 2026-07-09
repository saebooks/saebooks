"""Credit-note service — create, post, void, allocate.

Two posting modes depending on line account types:

Income reversal (customer credit note):
    Dr Income ........................ line_subtotal (per line)
    Dr GST Collected ................. line_tax
    Cr AR Control (Trade Debtors) .... total

Contra-COGS / hold-back (manufacturer rebate):
    Cr COGS .......................... line_subtotal (per line)
    Cr GST Collected ................. line_tax
    Dr AR Control (Trade Debtors) .... total  ← BAS G8 adjustment

The mode is selected per-posting based on account_type of the line accounts.
Mixing INCOME and COST_OF_SALES lines on a single credit note is rejected —
create two separate credit notes.

Allocation path: a posted credit note carries ``amount_allocated``
which is bumped as ``PaymentAllocation`` rows with
``credit_note_id=...`` are written against it. Allocating against an
invoice reduces the invoice's ``amount_paid`` (because the credit is
"paying" it, not receiving cash).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.credit_note import CreditNote, CreditNoteLine, CreditNoteStatus
from saebooks.models.journal import JournalOrigin
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import numbering

_TWOPLACES = Decimal("0.01")
_AR_CODE = "1-1200"


class CreditNoteError(ValueError):
    """Raised on credit-note validation or state-transition failure."""


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal


def _compute_line_totals(
    line: _LineInput, tax_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor)
    tax = _q2(subtotal * tax_rate / Decimal("100"))
    total = subtotal + tax
    return subtotal, tax, total


async def _resolve_tax_rate(
    session: AsyncSession,
    tax_code_id: uuid.UUID | None,
    company_id: uuid.UUID | None = None,
) -> Decimal:
    if tax_code_id is None:
        return Decimal("0")
    if company_id is not None:
        result = await session.execute(
            select(TaxCode).where(
                TaxCode.id == tax_code_id, TaxCode.company_id == company_id
            )
        )
        tc = result.scalars().first()
    else:
        tc = await session.get(TaxCode, tax_code_id)
    if tc is None:
        raise CreditNoteError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _replace_lines(
    session: AsyncSession,
    cn: CreditNote,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    from sqlalchemy import delete as sa_delete
    await session.execute(
        sa_delete(CreditNoteLine).where(CreditNoteLine.credit_note_id == cn.id)
    )
    await session.flush()
    session.expire(cn, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=_as_uuid(raw["account_id"]),
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
        )
        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id, company_id)
        subtotal, tax, total = _compute_line_totals(line_input, tax_rate)
        session.add(
            CreditNoteLine(
                credit_note_id=cn.id,
                line_no=i,
                description=line_input.description,
                account_id=line_input.account_id,
                tax_code_id=line_input.tax_code_id,
                quantity=line_input.quantity,
                unit_price=line_input.unit_price,
                discount_pct=line_input.discount_pct,
                line_subtotal=subtotal,
                line_tax=tax,
                line_total=total,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, cn: CreditNote) -> None:
    lines = (
        await session.execute(
            select(CreditNoteLine).where(CreditNoteLine.credit_note_id == cn.id)
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    cn.subtotal = _q2(Decimal(subtotal))
    cn.tax_total = _q2(Decimal(tax))
    cn.total = cn.subtotal + cn.tax_total


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    issue_date: date,
    lines: list[dict[str, object]] | None = None,
    original_invoice_id: uuid.UUID | None = None,
    reason: str | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
) -> CreditNote:
    # Company default payment terms (0171) — per-document override wins.
    if payment_terms is None:
        company = await session.get(Company, company_id)
        if company is not None and company.default_payment_terms:
            payment_terms = company.default_payment_terms
    cn = CreditNote(
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        original_invoice_id=original_invoice_id,
        reason=reason,
        notes=notes,
        payment_terms=payment_terms,
        status=CreditNoteStatus.DRAFT,
    )
    session.add(cn)
    await session.flush()
    if lines:
        await _replace_lines(session, cn, lines, company_id=company_id)
    await _recalc(session, cn)
    await session.commit()
    return await get(session, cn.id)


async def get(session: AsyncSession, credit_note_id: uuid.UUID) -> CreditNote:
    result = await session.execute(
        select(CreditNote)
        .options(selectinload(CreditNote.lines), selectinload(CreditNote.one_off_customer))
        .where(CreditNote.id == credit_note_id)
    )
    cn = result.scalar_one_or_none()
    if cn is None:
        raise CreditNoteError(f"Credit note {credit_note_id} not found")
    return cn


async def list_credit_notes(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: CreditNoteStatus | None = None,
    contact_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[CreditNote]:
    stmt = (
        select(CreditNote)
        .options(selectinload(CreditNote.lines), selectinload(CreditNote.one_off_customer))
        .where(CreditNote.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(CreditNote.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(CreditNote.status == status)
    if contact_id is not None:
        stmt = stmt.where(CreditNote.contact_id == contact_id)
    stmt = stmt.order_by(CreditNote.issue_date.desc(), CreditNote.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    lines: list[dict[str, object]] | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> CreditNote:
    cn = await get(session, credit_note_id)
    if cn.status != CreditNoteStatus.DRAFT:
        raise CreditNoteError(
            f"Cannot edit credit note in status {cn.status.value}"
        )
    if contact_id is not None:
        cn.contact_id = contact_id
    if issue_date is not None:
        cn.issue_date = issue_date
    if reason is not None:
        cn.reason = reason
    if notes is not None:
        cn.notes = notes
    if lines is not None:
        await _replace_lines(session, cn, lines)
    await _recalc(session, cn)
    await session.commit()
    return await get(session, cn.id)


async def _get_ar_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == _AR_CODE,
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise CreditNoteError(
            "AR control account 1-1200 Trade Debtors is missing"
        )
    return acct


async def _get_gst_collected_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    from saebooks.services import settings as settings_svc
    code = await settings_svc.get(session, "gst_collected_account_code", "")
    if not code:
        return None
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == str(code),
            Account.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def post_credit_note(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> CreditNote:
    cn = await get(session, credit_note_id)
    if cn.status == CreditNoteStatus.POSTED:
        raise CreditNoteError("Credit note is already posted")
    if cn.status == CreditNoteStatus.VOIDED:
        raise CreditNoteError("Credit note is voided")
    if not cn.lines:
        raise CreditNoteError("Cannot post credit note with no lines")
    if cn.total <= Decimal("0"):
        raise CreditNoteError("Cannot post credit note with non-positive total")

    if not cn.number:
        cn.number = await numbering.next_number(
            session, cn.company_id, "credit_note"
        )

    ar_account = await _get_ar_account(session, cn.company_id)
    gst_account = await _get_gst_collected_account(session, cn.company_id)

    # Determine line account types to choose posting mode.
    _INCOME_TYPES = frozenset({AccountType.INCOME, AccountType.OTHER_INCOME})
    _COGS_TYPES = frozenset({AccountType.COST_OF_SALES})

    line_account_ids = list({ln.account_id for ln in cn.lines})
    acct_rows = (
        await session.execute(select(Account).where(Account.id.in_(line_account_ids)))
    ).scalars().all()
    acct_type_map: dict[uuid.UUID, AccountType] = {a.id: a.account_type for a in acct_rows}

    has_income = any(acct_type_map.get(ln.account_id) in _INCOME_TYPES for ln in cn.lines)
    has_cogs = any(acct_type_map.get(ln.account_id) in _COGS_TYPES for ln in cn.lines)

    if has_income and has_cogs:
        raise CreditNoteError(
            "Cannot mix income and COGS accounts on a single credit note; "
            "create separate credit notes for income reversal and cost adjustments"
        )

    # Income reversal: Dr Income / Dr GST Collected / Cr AR
    # Contra-COGS (hold-back): Cr COGS / Cr GST Collected / Dr AR  ← BAS G8
    #
    # tax_code_id MUST be carried onto the income/COGS reversal lines —
    # the BAS aggregator filters journal_lines by their tax_code
    # reporting_type, and a NULL tax_code_id makes the reversal invisible
    # to G1/G8, leaving the BAS still showing the original sale (Round-2
    # critic 13). The GST adjustment line itself doesn't need a tax_code
    # because its account already IS the GST control account and the BAS
    # builder picks it up via account-side aggregation.
    lines: list[dict[str, object]] = []
    for line in cn.lines:
        is_cogs = acct_type_map.get(line.account_id) in _COGS_TYPES
        lines.append(
            {
                "account_id": line.account_id,
                "description": f"{cn.number}: {line.description}",
                "debit": Decimal("0") if is_cogs else line.line_subtotal,
                "credit": line.line_subtotal if is_cogs else Decimal("0"),
                "tax_code_id": line.tax_code_id,
            }
        )
    if cn.tax_total > Decimal("0") and gst_account is not None:
        lines.append(
            {
                "account_id": gst_account.id,
                "description": f"{cn.number}: GST adjustment",
                "debit": Decimal("0") if has_cogs else cn.tax_total,
                "credit": cn.tax_total if has_cogs else Decimal("0"),
            }
        )
    lines.append(
        {
            "account_id": ar_account.id,
            "description": f"Credit note {cn.number}",
            "debit": cn.total if has_cogs else Decimal("0"),
            "credit": Decimal("0") if has_cogs else cn.total,
        }
    )

    entry = await journal_svc.create_draft(
        session,
        company_id=cn.company_id,
        tenant_id=cn.tenant_id,
        entry_date=cn.issue_date,
        description=f"Credit note {cn.number}",
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.CREDIT_NOTE,
        source_type="credit_note",
        source_id=cn.id,
    )

    cn.status = CreditNoteStatus.POSTED
    cn.journal_entry_id = posted.id
    cn.posted_at = datetime.now(UTC)
    cn.posted_by = posted_by
    await session.commit()

    # A posted credit note linked to an invoice relieves that invoice's
    # outstanding balance (e.g. a bad-debt write-off). Recompute the
    # invoice's amount_paid so it drops out of aged receivables.
    if cn.original_invoice_id is not None:
        from saebooks.services.payments import _refresh_invoice_amount_paid
        await _refresh_invoice_amount_paid(session, cn.original_invoice_id)
        await session.commit()

    return await get(session, cn.id)


async def void_credit_note(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> CreditNote:
    cn = await get(session, credit_note_id)
    if cn.status == CreditNoteStatus.VOIDED:
        return cn
    if cn.status == CreditNoteStatus.DRAFT:
        cn.status = CreditNoteStatus.VOIDED
        await session.commit()
        return cn
    if cn.amount_allocated > Decimal("0"):
        raise CreditNoteError(
            "Credit note has allocations — unallocate before voiding"
        )
    if cn.journal_entry_id is None:
        raise CreditNoteError("Posted credit note has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        cn.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void credit note {cn.number}",
        tenant_id=cn.tenant_id,
    )
    cn.status = CreditNoteStatus.VOIDED
    cn.void_journal_entry_id = reversal.id
    await session.commit()

    # Voiding the credit note removes its relief of the linked invoice, so
    # the invoice reverts to open and reappears in aged receivables.
    if cn.original_invoice_id is not None:
        from saebooks.services.payments import _refresh_invoice_amount_paid
        await _refresh_invoice_amount_paid(session, cn.original_invoice_id)
        await session.commit()

    return cn


async def archive(
    session: AsyncSession, credit_note_id: uuid.UUID
) -> CreditNote:
    cn = await get(session, credit_note_id)
    cn.archived_at = datetime.now(UTC)
    await session.commit()
    return cn


# ==========================================================================
# API-oriented service (cycle 10) — optimistic locking + change_log
#
# These functions are the API surface for /api/v1/credit_notes.  They are
# intentionally separate from the legacy posting pipeline above so the
# two surfaces can evolve independently.
# ==========================================================================

from saebooks.services import audit_log as audit_log_svc  # noqa: E402
from saebooks.services import change_log as change_log_svc  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: CreditNote) -> None:
        super().__init__(
            f"CreditNote {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Columns serialised into change_log.payload
# ---------------------------------------------------------------------------

_CN_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "number",
    "issue_date",
    "status",
    "original_invoice_id",
    "subtotal",
    "tax_total",
    "total",
    "amount_allocated",
    "reason",
    "notes",
    "payment_terms",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise_cn(cn: CreditNote) -> dict:
    data: dict = {}
    for key in _CN_COLUMNS:
        val = getattr(cn, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _get_api(
    session: AsyncSession, credit_note_id: uuid.UUID
) -> CreditNote | None:
    """Fetch a single credit note with lines. Returns None if not found."""
    result = await session.execute(
        select(CreditNote)
        .options(selectinload(CreditNote.lines), selectinload(CreditNote.one_off_customer))
        .where(CreditNote.id == credit_note_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Read operations (API layer)
# ---------------------------------------------------------------------------


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    status: CreditNoteStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CreditNote], int]:
    """Return (credit_notes, total_count) — excludes archived credit notes."""
    from sqlalchemy import func as sa_func
    base_where = [
        CreditNote.company_id == company_id,
        CreditNote.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(CreditNote.contact_id == contact_id)
    if status is not None:
        base_where.append(CreditNote.status == status)
    if date_from is not None:
        base_where.append(CreditNote.issue_date >= date_from)
    if date_to is not None:
        base_where.append(CreditNote.issue_date <= date_to)

    count_stmt = select(sa_func.count()).select_from(CreditNote).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(CreditNote)
        .options(selectinload(CreditNote.lines), selectinload(CreditNote.one_off_customer))
        .where(*base_where)
        .order_by(CreditNote.issue_date.desc(), CreditNote.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    notes = list((await session.execute(stmt)).scalars().unique().all())
    return notes, total


async def api_get(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> CreditNote | None:
    """Fetch a single credit note with its lines. Returns None if not found.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    """
    if tenant_id is None and company_id is None:
        return await _get_api(session, credit_note_id)
    clauses = [CreditNote.id == credit_note_id]
    if tenant_id is not None:
        clauses.append(CreditNote.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(CreditNote.company_id == company_id)
    result = await session.execute(
        select(CreditNote)
        .options(selectinload(CreditNote.lines), selectinload(CreditNote.one_off_customer))
        .where(*clauses)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations (API layer)
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    contact_id: uuid.UUID,
    issue_date: date,
    lines: list[dict],
    original_invoice_id: uuid.UUID | None = None,
    reference: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
    commit: bool = True,
) -> CreditNote:
    """Create a credit note draft with version=1 and a change_log row.

    ``payment_terms`` omitted/None is defaulted from the company's
    ``default_payment_terms`` (0171) — an explicit per-document value wins.
    """
    if payment_terms is None:
        company = await session.get(Company, company_id)
        if company is not None and company.default_payment_terms:
            payment_terms = company.default_payment_terms
    cn = CreditNote(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        issue_date=issue_date,
        status=CreditNoteStatus.DRAFT,
        original_invoice_id=original_invoice_id,
        reason=reason,
        notes=notes,
        payment_terms=payment_terms,
        subtotal=Decimal("0"),
        tax_total=Decimal("0"),
        total=Decimal("0"),
        amount_allocated=Decimal("0"),
        version=1,
    )
    session.add(cn)
    await session.flush()
    await session.refresh(cn)

    # Assign a credit note number
    cn.number = await numbering.next_number(session, company_id, "credit_note")

    # Write lines
    if lines:
        await _replace_lines(session, cn, lines)
    await _recalc(session, cn)
    await session.flush()

    cn_loaded = await _get_api(session, cn.id)
    assert cn_loaded is not None

    await change_log_svc.append(
        session,
        entity="credit_note",
        entity_id=cn_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise_cn(cn_loaded),
        version=cn_loaded.version,
    )
    if commit:
        await session.commit()
    return await _get_api(session, cn_loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    lines: list[dict] | None = None,
    original_invoice_id: uuid.UUID | None = None,
    reason: str | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
) -> CreditNote:
    """Update a credit note draft with optimistic locking + change_log."""
    cn = await _get_api(session, credit_note_id)
    if cn is None:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if cn.version != expected_version:
        raise VersionConflict(cn)
    if cn.status != CreditNoteStatus.DRAFT:
        # Non-financial metadata (reason, notes, payment_terms) may be
        # corrected on POSTED / VOIDED credit notes — mirrors the invoice and
        # bill allowlists; none of it feeds totals, GST or the posted journal
        # entry. Anything financial stays DRAFT-only: void the credit note
        # and raise a new one. issue_date is the JE gl_entry_date (GST period
        # anchor); original_invoice_id drives invoice settlement; a lines
        # replacement would recalc document totals away from the posted JE.
        financial_change = (
            contact_id is not None
            or issue_date is not None
            or original_invoice_id is not None
            or lines is not None
        )
        if financial_change:
            raise CreditNoteError(
                f"credit_note_not_draft: cannot edit credit note {cn.id} in state "
                f"{cn.status.value}; void the existing credit note and raise a new one instead."
            )

    if contact_id is not None:
        cn.contact_id = contact_id
    if issue_date is not None:
        cn.issue_date = issue_date
    if original_invoice_id is not None:
        cn.original_invoice_id = original_invoice_id
    if reason is not None:
        cn.reason = reason
    if notes is not None:
        cn.notes = notes
    if payment_terms is not None:
        cn.payment_terms = payment_terms

    if lines is not None:
        await _replace_lines(session, cn, lines)
        await _recalc(session, cn)

    cn.version = cn.version + 1
    await session.flush()
    await session.refresh(cn)

    cn_loaded = await _get_api(session, credit_note_id)
    assert cn_loaded is not None

    await change_log_svc.append(
        session,
        entity="credit_note",
        entity_id=cn_loaded.id,
        op="update",
        actor=actor,
        payload=_serialise_cn(cn_loaded),
        version=cn_loaded.version,
    )
    await session.commit()
    return await _get_api(session, credit_note_id)  # type: ignore[return-value]


async def api_void(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> CreditNote:
    """Soft-delete (archive/void) a credit note with optimistic locking + change_log."""
    cn = await _get_api(session, credit_note_id)
    if cn is None:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if cn.version != expected_version:
        raise VersionConflict(cn)

    cn.archived_at = datetime.now(UTC)
    cn.status = CreditNoteStatus.VOIDED
    cn.version = cn.version + 1
    await session.flush()
    await session.refresh(cn)

    cn_loaded = await _get_api(session, credit_note_id)
    assert cn_loaded is not None

    await change_log_svc.append(
        session,
        entity="credit_note",
        entity_id=cn_loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise_cn(cn_loaded),
        version=cn_loaded.version,
    )
    await session.commit()
    return await _get_api(session, credit_note_id)  # type: ignore[return-value]


async def api_post_credit_note(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> CreditNote:
    """Transition DRAFT → POSTED with JE generation, optimistic locking + change_log.

    Wraps the legacy ``post_credit_note()`` pipeline which mints the credit note
    number, builds journal lines (Dr Income / Dr GST / Cr AR), calls
    ``journal_svc.post()``, and stamps ``journal_entry_id`` + ``posted_at``.
    After that legacy call completes and commits, we bump ``version`` and
    append a change_log row in a second transaction.
    """
    cn = await _get_api(session, credit_note_id)
    if cn is None:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if tenant_id is not None and cn.tenant_id != tenant_id:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if cn.version != expected_version:
        raise VersionConflict(cn)
    if cn.status == CreditNoteStatus.VOIDED:
        raise CreditNoteError(
            f"Credit note {cn.id} is VOIDED and cannot be posted"
        )
    if cn.status == CreditNoteStatus.POSTED:
        raise CreditNoteError(
            f"Credit note {cn.id} is already POSTED"
        )

    # Delegate to the legacy pipeline (mints number, builds JE, posts it,
    # commits internally). After this call the session is in a fresh state.
    cn = await post_credit_note(
        session,
        credit_note_id,
        posted_by=actor,
    )

    # Bump version + append change_log in the same transaction.
    cn.version = cn.version + 1
    await session.flush()
    await session.refresh(cn)

    cn_loaded = await _get_api(session, credit_note_id)
    assert cn_loaded is not None

    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=cn_loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.CREDIT_NOTE_POST,
            table_name="credit_notes",
            row_id=str(cn_loaded.id),
            row_snapshot=_serialise_cn(cn_loaded),
        )
    await change_log_svc.append(
        session,
        entity="credit_note",
        entity_id=cn_loaded.id,
        op="post",
        actor=actor,
        payload=_serialise_cn(cn_loaded),
        version=cn_loaded.version,
    )
    await session.commit()
    return await _get_api(session, credit_note_id)  # type: ignore[return-value]


async def api_void_credit_note(
    session: AsyncSession,
    credit_note_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> CreditNote:
    """Transition POSTED → VOIDED with JE reversal, optimistic locking + change_log.

    Wraps the legacy ``void_credit_note()`` pipeline which handles the
    POSTED case (reversal JE via ``journal_svc.reverse()``).

    Only POSTED credit notes may be voided via this endpoint; DRAFT credit
    notes must be archived via DELETE, and already-VOIDED notes raise 422.
    """
    cn = await _get_api(session, credit_note_id)
    if cn is None:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if tenant_id is not None and cn.tenant_id != tenant_id:
        raise CreditNoteError(f"CreditNote {credit_note_id} not found")
    if cn.version != expected_version:
        raise VersionConflict(cn)
    if cn.status == CreditNoteStatus.VOIDED:
        raise CreditNoteError(f"Credit note {cn.id} is already VOIDED")
    if cn.status == CreditNoteStatus.DRAFT:
        raise CreditNoteError(
            f"Credit note {cn.id} is DRAFT — use DELETE to archive a draft"
        )

    # Delegate to legacy pipeline (handles JE reversal, commits).
    cn = await void_credit_note(
        session,
        credit_note_id,
        posted_by=actor,
        override_reason=f"API void by {actor}",
    )

    # Bump version + append change_log.
    cn.version = cn.version + 1
    await session.flush()
    await session.refresh(cn)

    cn_loaded = await _get_api(session, credit_note_id)
    assert cn_loaded is not None

    await change_log_svc.append(
        session,
        entity="credit_note",
        entity_id=cn_loaded.id,
        op="void",
        actor=actor,
        payload=_serialise_cn(cn_loaded),
        version=cn_loaded.version,
    )
    await session.commit()
    return await _get_api(session, credit_note_id)  # type: ignore[return-value]


__all__ = [
    "CreditNoteError",
    "CreditNoteStatus",
    "VersionConflict",
    "api_create",
    "api_get",
    "api_post_credit_note",
    "api_update",
    "api_void",
    "api_void_credit_note",
    "archive",
    "create_draft",
    "get",
    "list_active",
    "list_credit_notes",
    "post_credit_note",
    "update_draft",
    "void_credit_note",
]
