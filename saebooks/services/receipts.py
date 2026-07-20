"""Generic money-in receipt service — create, post, void.

A ``Receipt`` records money received that is NOT tied to a customer invoice or a
bill: supplier refunds, cashbacks, rebates, an ATO GST refund, an insurance
recovery, interest received. It debits a bank/asset account and credits one or
more income OR expense accounts, with an optional GST line per line:

    Dr Bank/Asset ........................... total
    Cr Income / Cr Expense .................. amount (per line)
    Cr GST Collected (income line) .......... tax  (a sale's GST given back)
    Cr GST Paid      (expense line) ......... tax  (input credit reversed)

GST sign control
----------------
GST lines are built EXPLICITLY, never via ``gst_amount``/auto-posting. The
auto-poster keys GST direction off account type and would *debit* GST Paid for
an expense line — the wrong direction for a refund / negative expense. By
resolving the GST accounts and adding the credit lines ourselves we keep the
sign deterministic per line account type:

  * INCOME / OTHER_INCOME line -> credit ``gst_collected_account_code``.
  * EXPENSE / COST_OF_SALES / OTHER_EXPENSE line -> credit ``gst_paid_account_code``.

``tax_code_id`` is carried onto the income/expense lines so the BAS aggregator
(which filters journal_lines by tax_code reporting_type) attributes the
adjustment to the right BAS label.

The destination must be a balance-sheet ASSET account (bank). Each line account
must be income- or expense-type. Both are validated before any JE is built. The
JE is always posted via the chokepoint ``journal.post`` (origin=RECEIPT), never
hand-authored.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import JournalOrigin
from saebooks.models.receipt import Receipt, ReceiptLine, ReceiptStatus
from saebooks.models.tax_code import TaxCode
from saebooks.money import round_money
from saebooks.services import audit_log as audit_log_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import journal as journal_svc
from saebooks.services import numbering
from saebooks.services import settings as settings_svc

_INCOME_TYPES = frozenset({AccountType.INCOME, AccountType.OTHER_INCOME})
_EXPENSE_TYPES = frozenset(
    {AccountType.EXPENSE, AccountType.COST_OF_SALES, AccountType.OTHER_EXPENSE}
)


class ReceiptError(ValueError):
    """Raised on receipt validation or state-transition failure."""


class VersionConflict(Exception):
    def __init__(self, current: Receipt) -> None:
        super().__init__(
            f"Receipt {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


def _q2(value: Decimal, places: int = 2) -> Decimal:
    """ROUND_HALF_UP to a currency's minor unit (default AUD/base — 2)."""
    return round_money(value, places)


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    amount: Decimal


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
        raise ReceiptError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _replace_lines(
    session: AsyncSession,
    rcpt: Receipt,
    lines: list[dict[str, object]],
) -> None:
    from sqlalchemy import delete as sa_delete

    await session.execute(
        sa_delete(ReceiptLine).where(ReceiptLine.receipt_id == rcpt.id)
    )
    await session.flush()
    session.expire(rcpt, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        li = _LineInput(
            description=str(raw["description"]),
            account_id=_as_uuid(raw["account_id"]),
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            amount=Decimal(str(raw.get("amount", 0))),
        )
        tax_rate = await _resolve_tax_rate(session, li.tax_code_id, rcpt.company_id)
        amount = _q2(li.amount)
        tax = _q2(amount * tax_rate / Decimal("100"))
        session.add(
            ReceiptLine(
                receipt_id=rcpt.id,
                line_no=i,
                description=li.description,
                account_id=li.account_id,
                tax_code_id=li.tax_code_id,
                amount=amount,
                tax_amount=tax,
                line_total=amount + tax,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, rcpt: Receipt) -> None:
    lines = (
        await session.execute(
            select(ReceiptLine).where(ReceiptLine.receipt_id == rcpt.id)
        )
    ).scalars().all()
    subtotal = sum((ln.amount for ln in lines), Decimal("0"))
    tax = sum((ln.tax_amount for ln in lines), Decimal("0"))
    rcpt.subtotal = _q2(Decimal(subtotal))
    rcpt.tax_total = _q2(Decimal(tax))
    rcpt.total = rcpt.subtotal + rcpt.tax_total


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


async def _get_gst_account(
    session: AsyncSession, company_id: uuid.UUID, setting_key: str
) -> Account | None:
    code = await settings_svc.get(session, setting_key, "")
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
    receipt_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Receipt | None:
    clauses = [Receipt.id == receipt_id]
    if tenant_id is not None:
        clauses.append(Receipt.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Receipt.company_id == company_id)
    return (
        await session.execute(
            select(Receipt).options(selectinload(Receipt.lines)).where(*clauses)
        )
    ).scalar_one_or_none()


async def api_get(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Receipt | None:
    return await _get(session, receipt_id, tenant_id=tenant_id, company_id=company_id)


async def list_active(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    status: ReceiptStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Receipt], int]:
    base_where = [
        Receipt.tenant_id == tenant_id,
        Receipt.company_id == company_id,
        Receipt.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(Receipt.contact_id == contact_id)
    if status is not None:
        base_where.append(Receipt.status == status)
    if date_from is not None:
        base_where.append(Receipt.receipt_date >= date_from)
    if date_to is not None:
        base_where.append(Receipt.receipt_date <= date_to)

    count_stmt = select(sa_func.count()).select_from(Receipt).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Receipt)
        .options(selectinload(Receipt.lines))
        .where(*base_where)
        .order_by(Receipt.receipt_date.desc(), Receipt.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().unique().all())
    return rows, total


# ---------------------------------------------------------------------------
# change_log serialisation
# ---------------------------------------------------------------------------

_RCPT_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "bank_account_id",
    "contact_id",
    "number",
    "receipt_date",
    "status",
    "reference",
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


def _serialise(rcpt: Receipt) -> dict:
    data: dict = {}
    for key in _RCPT_COLUMNS:
        val = getattr(rcpt, key, None)
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
    bank_account_id: uuid.UUID,
    receipt_date: date,
    lines: list[dict],
    contact_id: uuid.UUID | None = None,
    reference: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> Receipt:
    rcpt = Receipt(
        company_id=company_id,
        tenant_id=tenant_id,
        bank_account_id=bank_account_id,
        contact_id=contact_id,
        receipt_date=receipt_date,
        status=ReceiptStatus.DRAFT,
        reference=reference,
        reason=reason,
        notes=notes,
        subtotal=Decimal("0"),
        tax_total=Decimal("0"),
        total=Decimal("0"),
        version=1,
    )
    session.add(rcpt)
    await session.flush()
    rcpt.number = await numbering.next_number(session, company_id, "receipt")
    if lines:
        await _replace_lines(session, rcpt, lines)
    await _recalc(session, rcpt)
    await session.flush()

    loaded = await _get(session, rcpt.id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="receipt",
        entity_id=loaded.id,
        op="create",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, rcpt.id)
    assert result is not None
    return result


async def api_update(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    bank_account_id: uuid.UUID | None = None,
    receipt_date: date | None = None,
    lines: list[dict] | None = None,
    contact_id: uuid.UUID | None = None,
    reference: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
) -> Receipt:
    rcpt = await _get(session, receipt_id, tenant_id=tenant_id, company_id=company_id)
    if rcpt is None:
        raise ReceiptError(f"Receipt {receipt_id} not found")
    if rcpt.status != ReceiptStatus.DRAFT:
        raise ReceiptError(f"Cannot edit receipt in status {rcpt.status.value}")
    if rcpt.version != expected_version:
        raise VersionConflict(rcpt)

    if bank_account_id is not None:
        rcpt.bank_account_id = bank_account_id
    if receipt_date is not None:
        rcpt.receipt_date = receipt_date
    if contact_id is not None:
        rcpt.contact_id = contact_id
    if reference is not None:
        rcpt.reference = reference
    if reason is not None:
        rcpt.reason = reason
    if notes is not None:
        rcpt.notes = notes
    if lines is not None:
        await _replace_lines(session, rcpt, lines)
        await _recalc(session, rcpt)

    rcpt.version = rcpt.version + 1
    await session.flush()

    loaded = await _get(session, receipt_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="receipt",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, receipt_id)
    assert result is not None
    return result


async def post_receipt(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Receipt:
    """Transition DRAFT -> POSTED, building + posting the money-in JE.

    Dr bank/asset (total) / Cr income|expense (per line) / Cr GST (per line tax).
    """
    rcpt = await _get(session, receipt_id)
    if rcpt is None:
        raise ReceiptError(f"Receipt {receipt_id} not found")
    if rcpt.status == ReceiptStatus.POSTED:
        raise ReceiptError("Receipt is already posted")
    if rcpt.status == ReceiptStatus.VOIDED:
        raise ReceiptError("Receipt is voided")
    if not rcpt.lines:
        raise ReceiptError("Cannot post receipt with no lines")
    if rcpt.total <= Decimal("0"):
        raise ReceiptError("Cannot post receipt with non-positive total")

    if not rcpt.number:
        rcpt.number = await numbering.next_number(
            session, rcpt.company_id, "receipt"
        )

    # Resolve + validate the destination bank/asset account.
    bank = (
        await session.execute(
            select(Account).where(
                Account.id == rcpt.bank_account_id,
                Account.company_id == rcpt.company_id,
            )
        )
    ).scalar_one_or_none()
    if bank is None:
        raise ReceiptError("Receipt bank account not found for this company")
    if bank.is_header:
        raise ReceiptError(
            "Receipt bank account is a header (group) account — pick a postable "
            "bank/asset account"
        )
    if bank.account_type != AccountType.ASSET:
        raise ReceiptError(
            "Receipt destination must be an ASSET (bank) account; "
            f"got {bank.account_type}"
        )

    # Validate + classify each line account (income vs expense).
    line_account_ids = list({ln.account_id for ln in rcpt.lines})
    acct_rows = (
        await session.execute(
            select(Account).where(Account.id.in_(line_account_ids))
        )
    ).scalars().all()
    acct_type_map: dict[uuid.UUID, AccountType] = {
        a.id: a.account_type for a in acct_rows
    }
    header_ids = {a.id for a in acct_rows if a.is_header}
    company_mismatch = {
        a.id for a in acct_rows if a.company_id != rcpt.company_id
    }
    bad_type = [
        ln.account_id
        for ln in rcpt.lines
        if acct_type_map.get(ln.account_id) not in (_INCOME_TYPES | _EXPENSE_TYPES)
    ]
    if company_mismatch:
        raise ReceiptError("Receipt line account does not belong to this company")
    if header_ids & {ln.account_id for ln in rcpt.lines}:
        raise ReceiptError(
            "Receipt line account is a header (group) account — pick a postable "
            "income or expense account"
        )
    if bad_type:
        raise ReceiptError(
            "Receipt line accounts must be income- or expense-type "
            "(INCOME / OTHER_INCOME / EXPENSE / COST_OF_SALES / OTHER_EXPENSE)"
        )

    gst_collected = await _get_gst_account(
        session, rcpt.company_id, "gst_collected_account_code"
    )
    gst_paid = await _get_gst_account(
        session, rcpt.company_id, "gst_paid_account_code"
    )

    # Dr bank/asset for the gross total.
    journal_lines: list[dict[str, object]] = [
        {
            "account_id": bank.id,
            "description": f"Receipt {rcpt.number}",
            "debit": rcpt.total,
            "credit": Decimal("0"),
        }
    ]
    # Cr each income/expense line; accumulate GST per direction so a single
    # GST line is added per GST account (matches the credit-note shape).
    gst_collected_total = Decimal("0")
    gst_paid_total = Decimal("0")
    for line in rcpt.lines:
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{rcpt.number}: {line.description}",
                "debit": Decimal("0"),
                "credit": line.amount,
                "tax_code_id": line.tax_code_id,
            }
        )
        if line.tax_amount > Decimal("0"):
            if acct_type_map.get(line.account_id) in _INCOME_TYPES:
                gst_collected_total += line.tax_amount
            else:
                gst_paid_total += line.tax_amount

    if gst_collected_total > Decimal("0"):
        if gst_collected is None:
            raise ReceiptError(
                "Receipt has GST on an income line but gst_collected_account_code "
                "is not configured — set it in settings before posting."
            )
        journal_lines.append(
            {
                "account_id": gst_collected.id,
                "description": f"{rcpt.number}: GST collected",
                "debit": Decimal("0"),
                "credit": gst_collected_total,
            }
        )
    if gst_paid_total > Decimal("0"):
        if gst_paid is None:
            raise ReceiptError(
                "Receipt has GST on an expense line but gst_paid_account_code "
                "is not configured — set it in settings before posting."
            )
        journal_lines.append(
            {
                "account_id": gst_paid.id,
                "description": f"{rcpt.number}: GST paid (input credit reversed)",
                "debit": Decimal("0"),
                "credit": gst_paid_total,
            }
        )

    entry = await journal_svc.create_draft(
        session,
        company_id=rcpt.company_id,
        tenant_id=rcpt.tenant_id,
        entry_date=rcpt.receipt_date,
        description=f"Receipt {rcpt.number}",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.RECEIPT,
        source_type="receipt",
        source_id=rcpt.id,
    )

    rcpt.status = ReceiptStatus.POSTED
    rcpt.journal_entry_id = posted.id
    rcpt.posted_at = datetime.now(UTC)
    rcpt.posted_by = posted_by
    await session.commit()
    result = await _get(session, rcpt.id)
    assert result is not None
    return result


async def api_post(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> Receipt:
    rcpt = await _get(session, receipt_id, tenant_id=tenant_id, company_id=company_id)
    if rcpt is None:
        raise ReceiptError(f"Receipt {receipt_id} not found")
    if rcpt.version != expected_version:
        raise VersionConflict(rcpt)
    if rcpt.status == ReceiptStatus.VOIDED:
        raise ReceiptError(f"Receipt {rcpt.id} is VOIDED and cannot be posted")
    if rcpt.status == ReceiptStatus.POSTED:
        raise ReceiptError(f"Receipt {rcpt.id} is already POSTED")

    rcpt = await post_receipt(session, receipt_id, posted_by=actor)

    rcpt.version = rcpt.version + 1
    await session.flush()

    loaded = await _get(session, receipt_id)
    assert loaded is not None
    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.RECEIPT_POST,
            table_name="receipts",
            row_id=str(loaded.id),
            row_snapshot=_serialise(loaded),
        )
    await change_log_svc.append(
        session,
        entity="receipt",
        entity_id=loaded.id,
        op="post",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, receipt_id)
    assert result is not None
    return result


async def void_receipt(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Receipt:
    rcpt = await _get(session, receipt_id)
    if rcpt is None:
        raise ReceiptError(f"Receipt {receipt_id} not found")
    if rcpt.status == ReceiptStatus.VOIDED:
        return rcpt
    if rcpt.status == ReceiptStatus.DRAFT:
        rcpt.status = ReceiptStatus.VOIDED
        await session.commit()
        return rcpt
    if rcpt.journal_entry_id is None:
        raise ReceiptError("Posted receipt has no journal entry id")
    reversal = await journal_svc.reverse(
        session,
        rcpt.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void receipt {rcpt.number}",
        tenant_id=rcpt.tenant_id,
    )
    rcpt.status = ReceiptStatus.VOIDED
    rcpt.void_journal_entry_id = reversal.id
    await session.commit()
    result = await _get(session, rcpt.id)
    assert result is not None
    return result


async def api_void(
    session: AsyncSession,
    receipt_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> Receipt:
    rcpt = await _get(session, receipt_id, tenant_id=tenant_id, company_id=company_id)
    if rcpt is None:
        raise ReceiptError(f"Receipt {receipt_id} not found")
    if rcpt.version != expected_version:
        raise VersionConflict(rcpt)
    if rcpt.status == ReceiptStatus.VOIDED:
        raise ReceiptError(f"Receipt {rcpt.id} is already VOIDED")
    if rcpt.status == ReceiptStatus.DRAFT:
        raise ReceiptError(
            f"Receipt {rcpt.id} is DRAFT — use DELETE to archive"
        )

    rcpt = await void_receipt(
        session, receipt_id, posted_by=actor, override_reason=f"API void by {actor}"
    )
    rcpt.version = rcpt.version + 1
    await session.flush()

    loaded = await _get(session, receipt_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="receipt",
        entity_id=loaded.id,
        op="void",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    result = await _get(session, receipt_id)
    assert result is not None
    return result


__all__ = [
    "ReceiptError",
    "ReceiptStatus",
    "VersionConflict",
    "api_create",
    "api_get",
    "api_post",
    "api_update",
    "api_void",
    "list_active",
    "post_receipt",
    "void_receipt",
]
