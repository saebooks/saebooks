"""Supplier (purchase) credit-note service — create, post, void.

The purchase-side mirror of ``services/credit_notes.py``. A supplier credit note
reverses (all or part of) a purchase: the supplier credits us for returned
materials, a rebate, or a cashback against a bill. In GL terms it is the mirror
of a bill — it credits the expense and reverses the input GST credit, debiting
the AP control account (reducing what we owe the supplier):

    Dr Trade Creditors (AP control 2-1200) .. total
    Cr Expense .............................. line_subtotal (per line)
    Cr GST Paid ............................. tax_total

GST handling
------------
The GST line is built EXPLICITLY (never via ``gst_amount``/auto-posting). The
auto-poster keys GST direction off account type, and an EXPENSE line always
yields a *debit* to GST Paid (the normal purchase direction). A credit note must
*credit* GST Paid to reverse the input credit, so we cannot delegate to the
auto-poster — we resolve ``gst_paid_account_code`` and add the Cr GST Paid line
ourselves, exactly as the customer credit note builds its GST adjustment line
explicitly. ``tax_code_id`` IS carried onto the expense reversal lines so the
BAS aggregator (which filters journal_lines by tax_code reporting_type) sees the
G11/1B decreasing adjustment.

Only expense-type accounts (EXPENSE / COST_OF_SALES / OTHER_EXPENSE) are allowed
on a supplier credit note line — a credit note is a purchase reversal. Income or
balance-sheet lines are rejected (use a generic ``Receipt`` for money-in that
isn't a purchase reversal).

The ledger is derived from app records: the JE is always built and posted
through the chokepoint ``journal.post`` (origin=SUPPLIER_CREDIT_NOTE), never
hand-authored.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import JournalOrigin
from saebooks.models.supplier_credit_note import (
    SupplierCreditNote,
    SupplierCreditNoteLine,
    SupplierCreditNoteStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import audit_log as audit_log_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import journal as journal_svc
from saebooks.services import numbering
from saebooks.services import settings as settings_svc

_TWOPLACES = Decimal("0.01")
_AP_CODE = "2-1200"

# Expense-side account types permitted on a supplier credit-note line.
_EXPENSE_TYPES = frozenset(
    {AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE}
)


class SupplierCreditNoteError(ValueError):
    """Raised on supplier-credit-note validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: SupplierCreditNote) -> None:
        super().__init__(
            f"SupplierCreditNote {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


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
    company_id: uuid.UUID,
) -> Decimal:
    if tax_code_id is None:
        return Decimal("0")
    tc = (
        await session.execute(
            select(TaxCode).where(
                TaxCode.id == tax_code_id, TaxCode.company_id == company_id
            )
        )
    ).scalars().first()
    if tc is None:
        raise SupplierCreditNoteError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _replace_lines(
    session: AsyncSession,
    scn: SupplierCreditNote,
    lines: list[dict[str, object]],
) -> None:
    from sqlalchemy import delete as sa_delete

    await session.execute(
        sa_delete(SupplierCreditNoteLine).where(
            SupplierCreditNoteLine.supplier_credit_note_id == scn.id
        )
    )
    await session.flush()
    session.expire(scn, ["lines"])

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
        tax_rate = await _resolve_tax_rate(
            session, line_input.tax_code_id, scn.company_id
        )
        subtotal, tax, total = _compute_line_totals(line_input, tax_rate)
        session.add(
            SupplierCreditNoteLine(
                supplier_credit_note_id=scn.id,
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


async def _recalc(session: AsyncSession, scn: SupplierCreditNote) -> None:
    lines = (
        await session.execute(
            select(SupplierCreditNoteLine).where(
                SupplierCreditNoteLine.supplier_credit_note_id == scn.id
            )
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    scn.subtotal = _q2(Decimal(subtotal))
    scn.tax_total = _q2(Decimal(tax))
    scn.total = scn.subtotal + scn.tax_total


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def _get_ap_account(session: AsyncSession, company_id: uuid.UUID) -> Account:
    acct = (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id, Account.code == _AP_CODE
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        raise SupplierCreditNoteError(
            "AP control account 2-1200 Trade Creditors is missing — "
            "re-run the CoA seed."
        )
    return acct


async def _get_gst_paid_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    code = await settings_svc.get(session, "gst_paid_account_code", "")
    if not code:
        return None
    code = str(code)
    if "-" not in code and len(code) >= 2 and code[0].isdigit():
        hyphenated = code[0] + "-" + code[1:]
    else:
        hyphenated = code
    return (
        await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code.in_([code, hyphenated]),
                Account.archived_at.is_(None),
            )
        )
    ).scalars().first()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def _get(
    session: AsyncSession,
    scn_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> SupplierCreditNote | None:
    clauses = [SupplierCreditNote.id == scn_id]
    if tenant_id is not None:
        clauses.append(SupplierCreditNote.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(SupplierCreditNote.company_id == company_id)
    return (
        await session.execute(
            select(SupplierCreditNote)
            .options(selectinload(SupplierCreditNote.lines))
            .where(*clauses)
        )
    ).scalar_one_or_none()


async def api_get(
    session: AsyncSession,
    scn_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> SupplierCreditNote | None:
    return await _get(session, scn_id, tenant_id=tenant_id, company_id=company_id)


async def list_active(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    status: SupplierCreditNoteStatus | None = None,
    flagged: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SupplierCreditNote], int]:
    base_where = [
        SupplierCreditNote.tenant_id == tenant_id,
        SupplierCreditNote.company_id == company_id,
        SupplierCreditNote.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(SupplierCreditNote.contact_id == contact_id)
    if status is not None:
        base_where.append(SupplierCreditNote.status == status)
    if date_from is not None:
        base_where.append(SupplierCreditNote.issue_date >= date_from)
    if date_to is not None:
        base_where.append(SupplierCreditNote.issue_date <= date_to)

    count_stmt = (
        select(sa_func.count()).select_from(SupplierCreditNote).where(*base_where)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(SupplierCreditNote)
        .options(selectinload(SupplierCreditNote.lines))
        .where(*base_where)
        .order_by(
            SupplierCreditNote.issue_date.desc(),
            SupplierCreditNote.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().unique().all())
    return rows, total


# ---------------------------------------------------------------------------
# change_log serialisation
# ---------------------------------------------------------------------------

_SCN_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "number",
    "issue_date",
    "status",
    "original_bill_id",
    "supplier_reference",
    "subtotal",
    "tax_total",
    "total",
    "reason",
    "notes",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise(scn: SupplierCreditNote) -> dict:
    data: dict = {}
    for key in _SCN_COLUMNS:
        val = getattr(scn, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    contact_id: uuid.UUID,
    issue_date: date,
    lines: list[dict],
    original_bill_id: uuid.UUID | None = None,
    supplier_reference: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> SupplierCreditNote:
    scn = SupplierCreditNote(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        issue_date=issue_date,
        status=SupplierCreditNoteStatus.DRAFT,
        original_bill_id=original_bill_id,
        supplier_reference=supplier_reference,
        reason=reason,
        notes=notes,
        subtotal=Decimal("0"),
        tax_total=Decimal("0"),
        total=Decimal("0"),
        version=1,
    )
    session.add(scn)
    await session.flush()
    scn.number = await numbering.next_number(
        session, company_id, "supplier_credit_note"
    )
    if lines:
        await _replace_lines(session, scn, lines)
    await _recalc(session, scn)
    await session.flush()

    loaded = await _get(session, scn.id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="supplier_credit_note",
        entity_id=loaded.id,
        op="create",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, scn.id)
    assert result is not None
    return result


async def api_update(
    session: AsyncSession,
    scn_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    lines: list[dict] | None = None,
    original_bill_id: uuid.UUID | None = None,
    supplier_reference: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> SupplierCreditNote:
    scn = await _get(session, scn_id, tenant_id=tenant_id, company_id=company_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if scn.status != SupplierCreditNoteStatus.DRAFT:
        raise SupplierCreditNoteError(
            f"Cannot edit supplier credit note in status {scn.status.value}"
        )
    if scn.version != expected_version:
        raise VersionConflict(scn)

    if contact_id is not None:
        scn.contact_id = contact_id
    if issue_date is not None:
        scn.issue_date = issue_date
    if original_bill_id is not None:
        scn.original_bill_id = original_bill_id
    if supplier_reference is not None:
        scn.supplier_reference = supplier_reference
    if reason is not None:
        scn.reason = reason
    if notes is not None:
        scn.notes = notes
    if lines is not None:
        await _replace_lines(session, scn, lines)
        await _recalc(session, scn)

    scn.version = scn.version + 1
    await session.flush()

    loaded = await _get(session, scn_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="supplier_credit_note",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, scn_id)
    assert result is not None
    return result


async def post_supplier_credit_note(
    session: AsyncSession,
    scn_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> SupplierCreditNote:
    """Transition DRAFT -> POSTED, building + posting the reversal JE.

    Dr AP control (total) / Cr expense (per line) / Cr GST Paid (tax_total).
    The JE is built and posted via ``journal.post`` (origin=SUPPLIER_CREDIT_NOTE),
    never hand-authored.
    """
    scn = await _get(session, scn_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if scn.status == SupplierCreditNoteStatus.POSTED:
        raise SupplierCreditNoteError("Supplier credit note is already posted")
    if scn.status == SupplierCreditNoteStatus.VOIDED:
        raise SupplierCreditNoteError("Supplier credit note is voided")
    if not scn.lines:
        raise SupplierCreditNoteError(
            "Cannot post supplier credit note with no lines"
        )
    if scn.total <= Decimal("0"):
        raise SupplierCreditNoteError(
            "Cannot post supplier credit note with non-positive total"
        )

    if not scn.number:
        scn.number = await numbering.next_number(
            session, scn.company_id, "supplier_credit_note"
        )

    ap_account = await _get_ap_account(session, scn.company_id)
    gst_account = await _get_gst_paid_account(session, scn.company_id)

    # All line accounts must be expense-type — a supplier credit note is a
    # purchase reversal. Validate up front; reject income / balance-sheet.
    line_account_ids = list({ln.account_id for ln in scn.lines})
    acct_rows = (
        await session.execute(
            select(Account).where(Account.id.in_(line_account_ids))
        )
    ).scalars().all()
    acct_type_map: dict[uuid.UUID, AccountType] = {
        a.id: a.account_type for a in acct_rows
    }
    bad = [
        ln.account_id
        for ln in scn.lines
        if acct_type_map.get(ln.account_id) not in _EXPENSE_TYPES
    ]
    if bad:
        raise SupplierCreditNoteError(
            "Supplier credit note lines must use expense-type accounts "
            "(EXPENSE / COST_OF_SALES / OTHER_EXPENSE) — a supplier credit "
            "note reverses a purchase. Use a generic Receipt for money-in "
            "against an income account."
        )

    # Dr AP control (total). Reduces what we owe the supplier.
    journal_lines: list[dict[str, object]] = [
        {
            "account_id": ap_account.id,
            "description": f"Supplier credit note {scn.number}",
            "debit": scn.total,
            "credit": Decimal("0"),
        }
    ]
    # Cr expense per line (reverses the original purchase expense). tax_code_id
    # carried so the BAS aggregator sees the G11/1B decreasing adjustment.
    for line in scn.lines:
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{scn.number}: {line.description}",
                "debit": Decimal("0"),
                "credit": line.line_subtotal,
                "tax_code_id": line.tax_code_id,
            }
        )
    # Cr GST Paid (reverse the input credit). Built explicitly — the
    # auto-poster would DEBIT GST Paid for an expense line, the wrong sign.
    if scn.tax_total > Decimal("0"):
        if gst_account is None:
            raise SupplierCreditNoteError(
                "Supplier credit note has GST but gst_paid_account_code is "
                "not configured — set it in settings before posting."
            )
        journal_lines.append(
            {
                "account_id": gst_account.id,
                "description": f"{scn.number}: GST adjustment (input credit reversed)",
                "debit": Decimal("0"),
                "credit": scn.tax_total,
            }
        )

    entry = await journal_svc.create_draft(
        session,
        company_id=scn.company_id,
        tenant_id=scn.tenant_id,
        entry_date=scn.issue_date,
        description=f"Supplier credit note {scn.number}",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.SUPPLIER_CREDIT_NOTE,
        source_type="supplier_credit_note",
        source_id=scn.id,
    )

    scn.status = SupplierCreditNoteStatus.POSTED
    scn.journal_entry_id = posted.id
    scn.posted_at = datetime.now(UTC)
    scn.posted_by = posted_by
    await session.commit()
    result = await _get(session, scn.id)
    assert result is not None
    return result


async def api_post(
    session: AsyncSession,
    scn_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierCreditNote:
    scn = await _get(session, scn_id, tenant_id=tenant_id, company_id=company_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if scn.version != expected_version:
        raise VersionConflict(scn)
    if scn.status == SupplierCreditNoteStatus.VOIDED:
        raise SupplierCreditNoteError(
            f"Supplier credit note {scn.id} is VOIDED and cannot be posted"
        )
    if scn.status == SupplierCreditNoteStatus.POSTED:
        raise SupplierCreditNoteError(
            f"Supplier credit note {scn.id} is already POSTED"
        )

    scn = await post_supplier_credit_note(session, scn_id, posted_by=actor)

    scn.version = scn.version + 1
    await session.flush()

    loaded = await _get(session, scn_id)
    assert loaded is not None
    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.SUPPLIER_CREDIT_NOTE_POST,
            table_name="supplier_credit_notes",
            row_id=str(loaded.id),
            row_snapshot=_serialise(loaded),
        )
    await change_log_svc.append(
        session,
        entity="supplier_credit_note",
        entity_id=loaded.id,
        op="post",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, scn_id)
    assert result is not None
    return result


async def void_supplier_credit_note(
    session: AsyncSession,
    scn_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> SupplierCreditNote:
    scn = await _get(session, scn_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if scn.status == SupplierCreditNoteStatus.VOIDED:
        return scn
    if scn.status == SupplierCreditNoteStatus.DRAFT:
        scn.status = SupplierCreditNoteStatus.VOIDED
        await session.commit()
        return scn
    if scn.journal_entry_id is None:
        raise SupplierCreditNoteError(
            "Posted supplier credit note has no journal entry id"
        )
    reversal = await journal_svc.reverse(
        session,
        scn.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void supplier credit note {scn.number}",
        tenant_id=scn.tenant_id,
    )
    scn.status = SupplierCreditNoteStatus.VOIDED
    scn.void_journal_entry_id = reversal.id
    await session.commit()
    result = await _get(session, scn.id)
    assert result is not None
    return result


async def api_void(
    session: AsyncSession,
    scn_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> SupplierCreditNote:
    scn = await _get(session, scn_id, tenant_id=tenant_id, company_id=company_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if scn.version != expected_version:
        raise VersionConflict(scn)
    if scn.status == SupplierCreditNoteStatus.VOIDED:
        raise SupplierCreditNoteError(
            f"Supplier credit note {scn.id} is already VOIDED"
        )
    if scn.status == SupplierCreditNoteStatus.DRAFT:
        raise SupplierCreditNoteError(
            f"Supplier credit note {scn.id} is DRAFT — use DELETE to archive"
        )

    scn = await void_supplier_credit_note(
        session, scn_id, posted_by=actor, override_reason=f"API void by {actor}"
    )
    scn.version = scn.version + 1
    await session.flush()

    loaded = await _get(session, scn_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="supplier_credit_note",
        entity_id=loaded.id,
        op="void",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, scn_id)
    assert result is not None
    return result


async def set_flag(
    session: AsyncSession,
    scn_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    archive: bool = False,
) -> SupplierCreditNote:
    """Archive a DRAFT supplier credit note (soft-delete)."""
    scn = await _get(session, scn_id, tenant_id=tenant_id, company_id=company_id)
    if scn is None:
        raise SupplierCreditNoteError(f"SupplierCreditNote {scn_id} not found")
    if archive:
        scn.archived_at = datetime.now(UTC)
    await session.commit()
    result = await _get(session, scn_id)
    assert result is not None
    return result


__all__ = [
    "SupplierCreditNoteError",
    "SupplierCreditNoteStatus",
    "VersionConflict",
    "api_create",
    "api_get",
    "api_post",
    "api_update",
    "api_void",
    "list_active",
    "post_supplier_credit_note",
    "set_flag",
    "void_supplier_credit_note",
]
