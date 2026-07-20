"""Paid-at-checkout expense service — create, update, post, void, archive.

Sibling of ``services/bills.py``. All GL-impacting operations go
through ``services/journal.py`` — expenses never touch
``journal_entries`` directly. GST auto-posting via ``gst.py`` adds a
matching Dr GST Paid line for any line with ``tax_code_id`` and an
EXPENSE account.

Posting journal shape (ex-GST line treatment):

    Dr Expense (per line) ......... line_subtotal
    Dr GST Paid ................... line_tax (auto-posted by gst.py)
    Cr Payment account (bank/card/cash) ... total

The payment account credited is the expense's ``payment_account_id``,
not the AP control account. There is no second-step Payment row — the
expense is settled the moment it posts.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact
from saebooks.models.expense import Expense, ExpenseLine, ExpenseStatus
from saebooks.models.journal import JournalOrigin
from saebooks.models.tax_code import TaxCode
from saebooks.money import decimal_places_for, round_money
from saebooks.services import change_log as change_log_svc
from saebooks.services import journal as journal_svc
from saebooks.services import numbering
from saebooks.services.tax_engine.ee import RC_DUAL_REPORTING_TYPES

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Account types that may credit an expense at checkout. Anything else
# (income, expense, equity) would post a nonsensical journal — block
# it at the service layer with a clear error rather than letting RLS /
# the journal balance check catch it downstream.
_VALID_PAYMENT_ACCOUNT_TYPES: frozenset[AccountType] = frozenset(
    {AccountType.ASSET, AccountType.LIABILITY}
)


class ExpenseError(ValueError):
    """Raised on expense validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Expense) -> None:
        super().__init__(
            f"Expense {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------- #
# Math                                                                    #
# ---------------------------------------------------------------------- #


def _q2(value: Decimal, places: int = 2) -> Decimal:
    """ROUND_HALF_UP to a currency's minor unit (default AUD/base — 2)."""
    return round_money(value, places)


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _reject_unsupported_reverse_charge(
    session: AsyncSession, lines: list[ExpenseLine]
) -> None:
    """Critic-round-4 fix — same gap and same remedy as
    ``services/bills.py``'s ``_reject_unsupported_reverse_charge``
    (see that docstring for the full analysis): this module's
    ``post_expense`` credits the payment account for
    ``expense.base_total`` (subtotal + tax), which is wrong for an
    EU-acquisition reverse-charge tax code — the "payment" side never
    actually pays the self-assessed VAT to the foreign supplier, and no
    output-side VAT-payable liability is booked. Fail loud instead of
    silently overstating the payment-account credit.
    """
    tc_ids = {line.tax_code_id for line in lines if line.tax_code_id is not None}
    if not tc_ids:
        return
    result = await session.execute(
        select(TaxCode.reporting_type).where(TaxCode.id.in_(tc_ids))
    )
    reporting_types = {rt for (rt,) in result.all() if rt is not None}
    hit = reporting_types & RC_DUAL_REPORTING_TYPES
    if hit:
        raise ExpenseError(
            "Cannot post: reverse-charge EU-acquisition tax code(s) "
            f"{sorted(hit)} are not yet supported by post_expense — the "
            "correct GL posting (payment account for the net amount "
            "actually paid + a separate VAT self-assessed payable "
            "liability line) is not implemented. Posting this expense "
            "as-is would overstate the payment-account credit by the "
            "self-assessed VAT and never book the liability. This is a "
            "known, tracked gap — see "
            "_reject_unsupported_reverse_charge's docstring."
        )


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    project_id: uuid.UUID | None


def _compute_line_totals(
    line: _LineInput, tax_rate: Decimal, places: int = 2
) -> tuple[Decimal, Decimal, Decimal]:
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor, places)
    tax = _q2(subtotal * tax_rate / Decimal("100"), places)
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
        raise ExpenseError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


# ---------------------------------------------------------------------- #
# Tenant-scoped FK validation                                              #
# ---------------------------------------------------------------------- #


async def _validate_contact_tenant(
    session: AsyncSession,
    contact_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(Contact.id).where(
            Contact.id == contact_id,
            Contact.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ExpenseError("contact not found in current tenant")


async def _validate_account_tenant(
    session: AsyncSession,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(Account.id).where(
            Account.id == account_id,
            Account.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ExpenseError("account not found in current tenant")


async def _validate_tax_code_tenant(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(TaxCode.id).where(
            TaxCode.id == tax_code_id,
            TaxCode.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ExpenseError("tax_code not found in current tenant")


async def _validate_payment_account(
    session: AsyncSession,
    payment_account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Account:
    """Resolve + tenant-check the payment account; restrict to ASSET / LIABILITY."""
    result = await session.execute(
        select(Account).where(
            Account.id == payment_account_id,
            Account.tenant_id == tenant_id,
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise ExpenseError("payment_account not found in current tenant")
    if acct.account_type not in _VALID_PAYMENT_ACCOUNT_TYPES:
        raise ExpenseError(
            "payment_account must be an ASSET or LIABILITY account "
            f"(got {acct.account_type.value})"
        )
    return acct


async def _validate_line_fks(
    session: AsyncSession,
    lines: list[dict],
    tenant_id: uuid.UUID,
) -> None:
    for raw in lines:
        account_raw = raw.get("account_id")
        if account_raw is not None:
            account_id = (
                account_raw
                if isinstance(account_raw, uuid.UUID)
                else uuid.UUID(str(account_raw))
            )
            await _validate_account_tenant(session, account_id, tenant_id)

        tax_code_raw = raw.get("tax_code_id")
        if tax_code_raw:
            tax_code_id = (
                tax_code_raw
                if isinstance(tax_code_raw, uuid.UUID)
                else uuid.UUID(str(tax_code_raw))
            )
            await _validate_tax_code_tenant(session, tax_code_id, tenant_id)


# ---------------------------------------------------------------------- #
# Internal CRUD                                                            #
# ---------------------------------------------------------------------- #


async def _get_with_lines(
    session: AsyncSession, expense_id: uuid.UUID
) -> Expense | None:
    result = await session.execute(
        select(Expense)
        .options(selectinload(Expense.lines), selectinload(Expense.one_off_vendor))
        .where(Expense.id == expense_id)
    )
    return result.scalar_one_or_none()


async def _replace_lines(
    session: AsyncSession,
    expense: Expense,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        sa_delete(ExpenseLine).where(ExpenseLine.expense_id == expense.id)
    )
    await session.flush()
    session.expire(expense, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        project_id = raw.get("project_id")
        if isinstance(project_id, str) and project_id:
            project_id = uuid.UUID(project_id)
        elif not project_id:
            project_id = None

        account_id = _as_uuid(raw["account_id"])
        if company_id is not None:
            acct_chk = await session.execute(
                select(Account.id).where(
                    Account.id == account_id, Account.company_id == company_id
                )
            )
            if acct_chk.scalar_one_or_none() is None:
                raise ExpenseError(f"account {account_id} not found")

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=account_id,
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
            project_id=project_id if isinstance(project_id, uuid.UUID) else None,
        )
        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id, company_id)
        subtotal, tax, total = _compute_line_totals(
            line_input, tax_rate, decimal_places_for(expense.currency)
        )
        session.add(
            ExpenseLine(
                expense_id=expense.id,
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
                project_id=line_input.project_id,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, expense: Expense) -> None:
    lines = (
        await session.execute(
            select(ExpenseLine).where(ExpenseLine.expense_id == expense.id)
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    doc_places = decimal_places_for(expense.currency)
    expense.subtotal = _q2(Decimal(subtotal), doc_places)
    expense.tax_total = _q2(Decimal(tax), doc_places)
    expense.total = expense.subtotal + expense.tax_total

    rate = Decimal(str(expense.fx_rate or Decimal("1")))
    base_subtotal = sum(
        (_q2(ln.line_subtotal * rate) for ln in lines), Decimal("0")
    )
    base_tax = sum((_q2(ln.line_tax * rate) for ln in lines), Decimal("0"))
    expense.base_subtotal = _q2(Decimal(base_subtotal))
    expense.base_tax_total = _q2(Decimal(base_tax))
    expense.base_total = expense.base_subtotal + expense.base_tax_total


# ---------------------------------------------------------------------- #
# Legacy CRUD (no version, no change_log — used by importers + tests)     #
# ---------------------------------------------------------------------- #


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    payment_account_id: uuid.UUID,
    expense_date: date,
    contact_id: uuid.UUID | None = None,
    reference: str | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Expense:
    expense = Expense(
        company_id=company_id,
        contact_id=contact_id,
        payment_account_id=payment_account_id,
        expense_date=expense_date,
        reference=reference,
        notes=notes,
        status=ExpenseStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
    )
    if tenant_id is not None:
        expense.tenant_id = tenant_id
    session.add(expense)
    await session.flush()

    if lines:
        await _replace_lines(session, expense, lines, company_id=company_id)

    await _recalc(session, expense)
    await session.commit()
    return await get(session, expense.id)


async def get(
    session: AsyncSession,
    expense_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Expense:
    stmt = (
        select(Expense)
        .options(selectinload(Expense.lines), selectinload(Expense.one_off_vendor))
        .where(Expense.id == expense_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(Expense.tenant_id == tenant_id)
    result = await session.execute(stmt)
    expense = result.scalar_one_or_none()
    if expense is None:
        raise ExpenseError(f"Expense {expense_id} not found")
    return expense


async def list_expenses(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: ExpenseStatus | None = None,
    contact_id: uuid.UUID | None = None,
    payment_account_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Expense]:
    stmt = (
        select(Expense)
        .options(selectinload(Expense.lines), selectinload(Expense.one_off_vendor))
        .where(Expense.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Expense.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Expense.status == status)
    if contact_id is not None:
        stmt = stmt.where(Expense.contact_id == contact_id)
    if payment_account_id is not None:
        stmt = stmt.where(Expense.payment_account_id == payment_account_id)
    stmt = stmt.order_by(Expense.expense_date.desc(), Expense.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


# ---------------------------------------------------------------------- #
# Post / void                                                              #
# ---------------------------------------------------------------------- #


async def post_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Expense:
    expense = await get(session, expense_id)
    if expense.status == ExpenseStatus.POSTED:
        raise ExpenseError(f"Expense {expense.id} is already posted")
    if expense.status == ExpenseStatus.VOIDED:
        raise ExpenseError(f"Expense {expense.id} is voided; raise a new one")
    if not expense.lines:
        raise ExpenseError("Cannot post an expense with no lines")
    if expense.total <= Decimal("0"):
        raise ExpenseError(
            f"Cannot post expense with non-positive total {expense.total}"
        )
    await _reject_unsupported_reverse_charge(session, expense.lines)

    # Sanity-check the payment account still exists + has a sane type.
    # (Catches a CoA edit between draft + post.)
    await _validate_payment_account(
        session, expense.payment_account_id, expense.tenant_id
    )

    if not expense.number:
        expense.number = await numbering.next_number(
            session, expense.company_id, "expense"
        )

    rate = Decimal(str(expense.fx_rate or Decimal("1")))
    ref = expense.reference or expense.number

    journal_lines: list[dict[str, object]] = []
    for line in expense.lines:
        line_base_subtotal = _q2(line.line_subtotal * rate)
        line_base_tax = (
            _q2(line.line_tax * rate) if line.line_tax > 0 else None
        )
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{expense.number}: {line.description}",
                "debit": line_base_subtotal,
                "credit": Decimal("0"),
                "tax_code_id": line.tax_code_id,
                "gst_amount": line_base_tax,
                "project_id": line.project_id,
            }
        )
    # Single Cr line to the chosen payment account — no AP intermediary.
    journal_lines.append(
        {
            "account_id": expense.payment_account_id,
            "description": f"Expense {expense.number} ({ref})",
            "debit": Decimal("0"),
            "credit": expense.base_total,
        }
    )

    entry = await journal_svc.create_draft(
        session,
        company_id=expense.company_id,
        tenant_id=expense.tenant_id,
        entry_date=expense.expense_date,
        description=f"Expense {expense.number} ({ref})",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.EXPENSE,
        source_type="expense",
        source_id=expense.id,
    )

    expense.status = ExpenseStatus.POSTED
    expense.journal_entry_id = posted.id
    expense.posted_at = datetime.now(UTC)
    expense.posted_by = posted_by
    await session.commit()
    return await get(session, expense.id)


async def void_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Expense:
    expense = await get(session, expense_id)
    if expense.status == ExpenseStatus.VOIDED:
        return expense
    if expense.status == ExpenseStatus.DRAFT:
        expense.status = ExpenseStatus.VOIDED
        await session.commit()
        return expense
    if expense.journal_entry_id is None:
        raise ExpenseError(f"Posted expense {expense.id} has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        expense.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void expense {expense.number}",
        tenant_id=expense.tenant_id,
    )
    expense.status = ExpenseStatus.VOIDED
    expense.void_journal_entry_id = reversal.id
    await session.commit()
    return expense


async def archive(
    session: AsyncSession,
    expense_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Expense:
    expense = await get(session, expense_id, tenant_id=tenant_id)
    expense.archived_at = datetime.now(UTC)
    await session.commit()
    return expense


# ========================================================================== #
# API-oriented service — optimistic locking + change_log                     #
# ========================================================================== #


_EXPENSE_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "payment_account_id",
    "number",
    "reference",
    "expense_date",
    "status",
    "subtotal",
    "tax_total",
    "total",
    "currency",
    "notes",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise_expense(expense: Expense) -> dict:
    data: dict = {}
    for key in _EXPENSE_COLUMNS:
        val = getattr(expense, key, None)
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


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    payment_account_id: uuid.UUID | None = None,
    status: ExpenseStatus | None = None,
    flagged: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Expense], int]:
    base_where = [
        Expense.company_id == company_id,
        Expense.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(Expense.contact_id == contact_id)
    if payment_account_id is not None:
        base_where.append(Expense.payment_account_id == payment_account_id)
    if status is not None:
        base_where.append(Expense.status == status)
    if flagged is not None:
        base_where.append(Expense.flagged_for_review.is_(flagged))
    if date_from is not None:
        base_where.append(Expense.expense_date >= date_from)
    if date_to is not None:
        base_where.append(Expense.expense_date <= date_to)

    count_stmt = select(func.count()).select_from(Expense).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Expense)
        .options(selectinload(Expense.lines), selectinload(Expense.one_off_vendor))
        .where(*base_where)
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    expenses = list((await session.execute(stmt)).scalars().unique().all())
    return expenses, total


async def api_get(
    session: AsyncSession,
    expense_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Expense | None:
    if tenant_id is None and company_id is None:
        return await _get_with_lines(session, expense_id)
    clauses = [Expense.id == expense_id]
    if tenant_id is not None:
        clauses.append(Expense.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Expense.company_id == company_id)
    result = await session.execute(
        select(Expense)
        .options(selectinload(Expense.lines), selectinload(Expense.one_off_vendor))
        .where(*clauses)
    )
    return result.scalar_one_or_none()


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    payment_account_id: uuid.UUID,
    expense_date: date,
    contact_id: uuid.UUID | None = None,
    lines: list[dict] | None = None,
    reference: str | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    commit: bool = True,
) -> Expense:
    await _validate_payment_account(session, payment_account_id, tenant_id)
    if contact_id is not None:
        await _validate_contact_tenant(session, contact_id, tenant_id)
    if lines:
        await _validate_line_fks(session, lines, tenant_id)

    locked_through = await journal_svc.get_locked_through(session, company_id)
    if locked_through is not None and expense_date <= locked_through:
        raise ExpenseError(
            f"Expense date {expense_date} falls inside locked period "
            f"(ends {locked_through}); contact your controller to adjust period lock"
        )

    expense = Expense(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        payment_account_id=payment_account_id,
        expense_date=expense_date,
        reference=reference,
        notes=notes,
        status=ExpenseStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
        version=1,
    )
    session.add(expense)
    await session.flush()
    await session.refresh(expense)

    if lines:
        await _replace_lines(session, expense, lines)
        await _recalc(session, expense)

    await session.flush()

    expense_loaded = await _get_with_lines(session, expense.id)
    assert expense_loaded is not None

    await change_log_svc.append(
        session,
        entity="expense",
        entity_id=expense_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise_expense(expense_loaded),
        version=expense_loaded.version,
    )
    if commit:
        await session.commit()
    return await _get_with_lines(session, expense_loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    expense_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    payment_account_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    expense_date: date | None = None,
    notes: str | None = None,
    reference: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
    lines: list[dict] | None = None,
) -> Expense:
    expense = await _get_with_lines(session, expense_id)
    if expense is None:
        raise ExpenseError(f"Expense {expense_id} not found")
    if expense.version != expected_version:
        raise VersionConflict(expense)
    if not force and expense.status != ExpenseStatus.DRAFT:
        # Non-financial metadata (notes, reference) may be corrected on
        # POSTED / VOIDED expenses — it never feeds totals, GST or the
        # posted journal entry. Everything financial stays DRAFT-only
        # (or behind the FLAG_EDIT_FROZEN_STATE force gate): void the
        # expense and raise a new one. Version bump + change_log as usual.
        financial_change = (
            payment_account_id is not None
            or contact_id is not None
            or expense_date is not None
            or currency is not None
            or fx_rate is not None
            or lines is not None
        )
        if financial_change:
            raise ExpenseError(
                f"Cannot edit expense {expense.id} in state {expense.status.value}; "
                "void the existing expense and raise a new one instead."
            )

    if payment_account_id is not None:
        await _validate_payment_account(session, payment_account_id, expense.tenant_id)
        expense.payment_account_id = payment_account_id
    if contact_id is not None:
        await _validate_contact_tenant(session, contact_id, expense.tenant_id)
        expense.contact_id = contact_id
    if lines is not None:
        await _validate_line_fks(session, lines, expense.tenant_id)
    if expense_date is not None:
        expense.expense_date = expense_date
    if notes is not None:
        expense.notes = notes
    if reference is not None:
        expense.reference = reference
    if currency is not None:
        expense.currency = currency.upper()
    if fx_rate is not None:
        expense.fx_rate = fx_rate
    if lines is not None:
        await _replace_lines(session, expense, lines)
        await _recalc(session, expense)
    elif fx_rate is not None:
        await _recalc(session, expense)

    expense.version = expense.version + 1
    await session.flush()
    await session.refresh(expense)

    expense_loaded = await _get_with_lines(session, expense_id)
    assert expense_loaded is not None

    await change_log_svc.append(
        session,
        entity="expense",
        entity_id=expense_loaded.id,
        op="update",
        actor=actor,
        payload=_serialise_expense(expense_loaded),
        version=expense_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, expense_id)  # type: ignore[return-value]


async def api_archive(
    session: AsyncSession,
    expense_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Expense:
    """Soft-delete (archive) an expense with optimistic locking + change_log.

    Distinct from ``api_void_expense`` — archive only flips
    ``archived_at`` + bumps version; it does NOT reverse the journal
    entry. Call the void transition for that.
    """
    expense = await _get_with_lines(session, expense_id)
    if expense is None:
        raise ExpenseError(f"Expense {expense_id} not found")
    if expense.version != expected_version:
        raise VersionConflict(expense)

    expense.archived_at = datetime.now(UTC)
    expense.version = expense.version + 1
    await session.flush()
    await session.refresh(expense)

    expense_loaded = await _get_with_lines(session, expense_id)
    assert expense_loaded is not None

    await change_log_svc.append(
        session,
        entity="expense",
        entity_id=expense_loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise_expense(expense_loaded),
        version=expense_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, expense_id)  # type: ignore[return-value]


async def api_post_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Expense:
    """Transition DRAFT → POSTED with JE generation, locking + change_log."""
    expense = await _get_with_lines(session, expense_id)
    if expense is None:
        raise ExpenseError(f"Expense {expense_id} not found")
    if tenant_id is not None and expense.tenant_id != tenant_id:
        raise ExpenseError(f"Expense {expense_id} not found")
    if expense.version != expected_version:
        raise VersionConflict(expense)
    if expense.status == ExpenseStatus.VOIDED:
        raise ExpenseError(f"Expense {expense.id} is VOIDED and cannot be posted")
    if expense.status == ExpenseStatus.POSTED:
        raise ExpenseError(f"Expense {expense.id} is already POSTED")
    if not expense.lines:
        raise ExpenseError("Cannot post an expense with no lines")

    try:
        expense = await post_expense(
            session,
            expense_id,
            posted_by=actor,
        )
    except journal_svc.PostingError as exc:
        raise ExpenseError(str(exc)) from exc

    expense.version = expense.version + 1
    await session.flush()
    await session.refresh(expense)

    expense_loaded = await _get_with_lines(session, expense_id)
    assert expense_loaded is not None

    await change_log_svc.append(
        session,
        entity="expense",
        entity_id=expense_loaded.id,
        op="post",
        actor=actor,
        payload=_serialise_expense(expense_loaded),
        version=expense_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, expense_id)  # type: ignore[return-value]


async def api_void_expense(
    session: AsyncSession,
    expense_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Expense:
    """Transition any non-VOIDED → VOIDED, reversing JE if POSTED."""
    expense = await _get_with_lines(session, expense_id)
    if expense is None:
        raise ExpenseError(f"Expense {expense_id} not found")
    if tenant_id is not None and expense.tenant_id != tenant_id:
        raise ExpenseError(f"Expense {expense_id} not found")
    if expense.version != expected_version:
        raise VersionConflict(expense)
    if expense.status == ExpenseStatus.VOIDED:
        raise ExpenseError(f"Expense {expense.id} is already VOIDED")

    expense = await void_expense(
        session,
        expense_id,
        posted_by=actor,
        override_reason=f"API void by {actor}",
    )

    expense.version = expense.version + 1
    await session.flush()
    await session.refresh(expense)

    expense_loaded = await _get_with_lines(session, expense_id)
    assert expense_loaded is not None

    await change_log_svc.append(
        session,
        entity="expense",
        entity_id=expense_loaded.id,
        op="void",
        actor=actor,
        payload=_serialise_expense(expense_loaded),
        version=expense_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, expense_id)  # type: ignore[return-value]
