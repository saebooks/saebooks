"""PayRun service layer -- Cat-C community-tier.

All writes go through these helpers. Change-log entries and optimistic
locking live here.
"""
from __future__ import annotations

import base64
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.journal import PeriodLock
from saebooks.models.pay_run import PayRun, PayRunLine, PayRunStatus
from saebooks.services import change_log as cl_svc
from saebooks.services import journal as journal_svc
from saebooks.services.aba import (
    TXN_CREDIT_GENERAL,
    AbaDetail,
    AbaError,
    AbaHeader,
    build_aba,
    dollars_to_cents,
)


class PayRunError(ValueError):
    """Raised for domain-level validation failures."""


class VersionConflict(Exception):
    def __init__(self, current: PayRun) -> None:
        self.current = current
        super().__init__(f"version mismatch on pay_run {current.id}")


async def _get_with_lines(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> PayRun | None:
    stmt = (
        select(PayRun)
        .options(selectinload(PayRun.lines))
        .where(PayRun.id == pay_run_id, PayRun.archived_at.is_(None))
    )
    if tenant_id is not None:
        stmt = stmt.where(PayRun.tenant_id == tenant_id)
    if company_id is not None:
        stmt = stmt.where(PayRun.company_id == company_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def _check_period_lock_pre(
    session: AsyncSession,
    company_id: uuid.UUID,
    entry_date: date,
) -> None:
    result = await session.execute(
        select(func.max(PeriodLock.locked_through)).where(
            PeriodLock.company_id == company_id
        )
    )
    locked_through = result.scalar_one_or_none()
    if locked_through is not None and entry_date <= locked_through:
        raise PayRunError(
            f"Period is locked through {locked_through}. "
            f"payment_date {entry_date} falls in a locked period."
        )


async def _resolve_pending_account(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "2-1150",
            Account.archived_at.is_(None),
        )
    )
    account = result.scalars().first()
    if account is None:
        raise PayRunError(
            "Account 2-1150 'Payments - Pending' not found. "
            "Re-run the AU CoA seed to add it."
        )
    return account


async def _wages_account(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> Account | None:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "2-1300",
            Account.archived_at.is_(None),
        )
    )
    return result.scalars().first()


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    period_start: date,
    period_end: date,
    payment_date: date,
    description: str | None = None,
    actor: str,
) -> PayRun:
    if period_end < period_start:
        raise PayRunError("period_end must be >= period_start")

    pay_run = PayRun(
        company_id=company_id,
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
        payment_date=payment_date,
        description=description,
        status=PayRunStatus.DRAFT,
        version=1,
    )
    session.add(pay_run)
    await session.flush()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="create",
        actor=actor,
        payload={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "payment_date": payment_date.isoformat(),
        },
        version=1,
    )
    await session.commit()
    refreshed = await _get_with_lines(session, pay_run.id)
    assert refreshed is not None
    return refreshed


async def get(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> PayRun | None:
    return await _get_with_lines(
        session, pay_run_id, tenant_id=tenant_id, company_id=company_id
    )


async def list_runs(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    period: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PayRun], int]:
    base = (
        select(PayRun)
        .where(
            PayRun.company_id == company_id,
            PayRun.archived_at.is_(None),
        )
    )
    if status is not None:
        base = base.where(PayRun.status == status)
    if period is not None:
        base = base.where(
            PayRun.period_start <= period,
            PayRun.period_end >= period,
        )

    count_result = await session.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar_one()

    stmt = (
        base.options(selectinload(PayRun.lines))
        .order_by(PayRun.payment_date.desc(), PayRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all()), total


async def add_line(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    employee_id: uuid.UUID,
    gross: Decimal,
    tax: Decimal,
    super_amount: Decimal,
    net: Decimal,
    actor: str,
) -> PayRunLine:
    pay_run = await _get_with_lines(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunError(f"Pay run {pay_run_id} not found")
    if pay_run.status != PayRunStatus.DRAFT:
        raise PayRunError(
            f"Cannot add lines to a pay run with status '{pay_run.status}'. "
            "Only DRAFT pay runs accept new lines."
        )

    emp_result = await session.execute(
        select(Contact).where(
            Contact.id == employee_id,
            Contact.company_id == pay_run.company_id,
            Contact.archived_at.is_(None),
        )
    )
    if emp_result.scalars().first() is None:
        raise PayRunError(
            f"Employee contact {employee_id} not found for this company"
        )

    line = PayRunLine(
        pay_run_id=pay_run_id,
        employee_id=employee_id,
        gross=gross,
        tax=tax,
        super_amount=super_amount,
        net=net,
    )
    session.add(line)
    await session.flush()

    await cl_svc.append(
        session,
        entity="pay_run_line",
        entity_id=line.id,
        op="create",
        actor=actor,
        payload={"pay_run_id": str(pay_run_id), "net": str(net)},
        version=1,
    )
    await session.commit()
    await session.refresh(line)
    return line


async def delete_line(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    line_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    actor: str,
) -> None:
    pay_run = await _get_with_lines(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunError(f"Pay run {pay_run_id} not found")
    if pay_run.status != PayRunStatus.DRAFT:
        raise PayRunError(
            f"Cannot delete lines from a pay run with status '{pay_run.status}'"
        )

    line = next((ln for ln in pay_run.lines if ln.id == line_id), None)
    if line is None:
        raise PayRunError(f"Line {line_id} not found in pay run {pay_run_id}")

    await session.delete(line)
    await cl_svc.append(
        session,
        entity="pay_run_line",
        entity_id=line_id,
        op="archive",
        actor=actor,
        payload={"pay_run_id": str(pay_run_id)},
        version=1,
    )
    await session.commit()


async def export_aba(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    expected_version: int,
) -> tuple[str, uuid.UUID]:
    pay_run = await _get_with_lines(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunError(f"Pay run {pay_run_id} not found")
    if pay_run.version != expected_version:
        raise VersionConflict(pay_run)
    if pay_run.status != PayRunStatus.DRAFT:
        raise PayRunError(
            f"Can only export ABA for DRAFT pay runs (current: {pay_run.status})"
        )
    if not pay_run.lines:
        raise PayRunError("Pay run has no lines -- add at least one before exporting")

    await _check_period_lock_pre(session, pay_run.company_id, pay_run.payment_date)

    company_result = await session.execute(
        select(Company).where(Company.id == pay_run.company_id)
    )
    company = company_result.scalars().first()
    if company is None:
        raise PayRunError("Company not found")

    # Resolve remitter bank account.
    ba_result = await session.execute(
        select(Account).where(
            Account.company_id == pay_run.company_id,
            Account.bsb.is_not(None),
            Account.apca_user_id.is_not(None),
            Account.archived_at.is_(None),
        ).order_by(Account.code)
    )
    remitter_account = ba_result.scalars().first()
    if remitter_account is None:
        raise PayRunError(
            "No bank account with BSB and APCA User ID found. "
            "Configure a bank account before exporting ABA."
        )

    pending_account = await _resolve_pending_account(
        session, pay_run.company_id, tenant_id
    )

    aba_details: list[AbaDetail] = []
    journal_lines: list[dict[str, Any]] = []

    for line in pay_run.lines:
        emp_result = await session.execute(
            select(Contact).where(Contact.id == line.employee_id)
        )
        emp = emp_result.scalars().first()
        if emp is None:
            raise PayRunError(f"Employee contact {line.employee_id} not found")
        if not emp.bank_bsb or not emp.bank_account_number:
            raise PayRunError(
                f"Employee '{emp.name}' is missing bank BSB or account number."
            )

        try:
            aba_details.append(
                AbaDetail(
                    payee_bsb=emp.bank_bsb,
                    payee_account_number=emp.bank_account_number,
                    payee_account_title=emp.bank_account_title or emp.name[:32],
                    amount_cents=dollars_to_cents(line.net),
                    lodgement_reference=f"PR {pay_run.period_start.strftime('%d%m%y')}",
                    remitter_bsb=remitter_account.bsb,
                    remitter_account_number=remitter_account.bank_account_number or "",
                    remitter_name=remitter_account.bank_account_title or (company.name[:16]),
                    txn_code=TXN_CREDIT_GENERAL,
                )
            )
        except AbaError as exc:
            raise PayRunError(f"ABA build error for {emp.name}: {exc}") from exc

        # Cr 2-1150 for each net-pay line.
        journal_lines.append(
            {
                "account_id": pending_account.id,
                "description": f"Net pay: {emp.name}",
                "debit": Decimal("0"),
                "credit": line.net,
            }
        )

    # Dr wages account for total.
    wages_account = await _wages_account(session, pay_run.company_id)
    total_net = sum(ln.net for ln in pay_run.lines)

    if wages_account is None:
        raise PayRunError(
            "Account 2-1300 'Wages & Salaries' not found. "
            "Re-run the AU CoA seed or create the account manually before exporting ABA."
        )

    all_journal_lines: list[dict[str, Any]] = [
        {
            "account_id": wages_account.id,
            "description": f"Net payroll: {pay_run.period_start} to {pay_run.period_end}",
            "debit": total_net,
            "credit": Decimal("0"),
        },
        *journal_lines,
    ]

    bank_abbr = remitter_account.bank_abbreviation or "CBA"
    ddmmyy = pay_run.payment_date.strftime("%d%m%y")
    header = AbaHeader(
        bank_abbreviation=bank_abbr,
        user_name=(company.trading_name or company.name)[:26],
        apca_user_id=remitter_account.apca_user_id,
        description="PAYROLL",
        process_date_ddmmyy=ddmmyy,
    )

    try:
        aba_text = build_aba(header, aba_details)
    except AbaError as exc:
        raise PayRunError(f"ABA generation failed: {exc}") from exc

    aba_b64 = base64.b64encode(aba_text.encode("ascii")).decode("ascii")

    entry = await journal_svc.create_draft(
        session,
        company_id=pay_run.company_id,
        entry_date=pay_run.payment_date,
        description=f"Payroll disbursement {pay_run.period_start} to {pay_run.period_end}",
        lines=all_journal_lines,
        tenant_id=tenant_id,
    )

    pay_run.journal_id = entry.id
    pay_run.status = PayRunStatus.ABA_EXPORTED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={"status": "aba_exported", "journal_id": str(entry.id)},
        version=pay_run.version,
    )
    await session.commit()

    return aba_b64, entry.id


async def finalize(
    session: AsyncSession,
    pay_run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    expected_version: int,
) -> PayRun:
    pay_run = await _get_with_lines(session, pay_run_id, tenant_id=tenant_id)
    if pay_run is None:
        raise PayRunError(f"Pay run {pay_run_id} not found")
    if pay_run.version != expected_version:
        raise VersionConflict(pay_run)
    if pay_run.status != PayRunStatus.ABA_EXPORTED:
        raise PayRunError(
            f"Pay run must be in 'aba_exported' status to finalize "
            f"(current: {pay_run.status})"
        )
    if pay_run.journal_id is None:
        raise PayRunError("No journal associated with this pay run")

    try:
        await journal_svc.post(
            session,
            pay_run.journal_id,
            posted_by=actor,
            tenant_id=tenant_id,
        )
    except journal_svc.PostingError as exc:
        raise PayRunError(f"Journal post failed: {exc}") from exc

    pay_run.status = PayRunStatus.FINALIZED
    pay_run.version += 1
    pay_run.updated_at = datetime.utcnow()

    await cl_svc.append(
        session,
        entity="pay_run",
        entity_id=pay_run.id,
        op="update",
        actor=actor,
        payload={"status": "finalized"},
        version=pay_run.version,
    )
    await session.commit()

    # Payday Super Phase 1 — best-effort lodgement build. Gated by
    # SAEBOOKS_PAYDAY_SUPER / SAEBOOKS_ENV. Failures are logged and
    # swallowed; the pay-run finalise must not roll back if super
    # lodgement generation fails.
    from saebooks.services.super_stream import maybe_build_after_finalize

    await maybe_build_after_finalize(
        session,
        tenant_id=tenant_id,
        company_id=pay_run.company_id,
        pay_run_id=pay_run.id,
    )

    refreshed = await _get_with_lines(session, pay_run_id)
    assert refreshed is not None
    return refreshed
