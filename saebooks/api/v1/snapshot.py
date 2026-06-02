"""``GET /api/v1/snapshot`` — NDJSON dump of current server state.

Phase 1 scope: all 14 owned entities in dependency order.  Clients call
this once on bootstrap, then switch to
``/api/v1/changes?since=<cursor>`` for incremental updates.

Stream format
-------------
Each entity block starts with a marker line::

    {"_entity": "<name>", "_count": N}

followed by N rows serialised with their Pydantic ``Out`` schema.
Invoices, bills, journal_entries and payments are header-only (no nested
line items) — clients fetch detail on demand via the individual GET
endpoints.

The response's last line is a ``{"_cursor": <id>}`` marker carrying the
change_log ``id`` at the moment the snapshot was read; clients seed their
local cursor from that value.

Env vars
--------
``SAEBOOKS_SNAPSHOT_ENTITY_LIMIT``
    Maximum rows per entity (default: 10 000).
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.schemas import (
    AccountOut,
    BankAccountOut,
    BankStatementLineOut,
    BillOut,
    BudgetOut,
    CompanyOut,
    ContactOut,
    FixedAssetOut,
    InvoiceOut,
    ItemOut,
    JournalEntryHeaderOut,
    PaymentOut,
    ProjectOut,
    TaxCodeOut,
)
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.bill import Bill
from saebooks.models.budget import Budget
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.invoice import Invoice
from saebooks.models.item import Item
from saebooks.models.journal import JournalEntry
from saebooks.models.payment import Payment
from saebooks.models.project import Project
from saebooks.models.tax_code import TaxCode

router = APIRouter(
    prefix="/snapshot",
    tags=["sync"],
    dependencies=[Depends(require_bearer)],
)

_DEFAULT_ENTITY_LIMIT = 10_000


def _entity_limit() -> int:
    raw = os.getenv("SAEBOOKS_SNAPSHOT_ENTITY_LIMIT", "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_ENTITY_LIMIT
    except ValueError:
        return _DEFAULT_ENTITY_LIMIT


def _dump(schema_instance) -> str:
    return schema_instance.model_dump_json()


async def _generate(
    company_id, max_id: int, limit: int, tenant_id
) -> AsyncGenerator[str, None]:
    """Yield NDJSON lines for all entities then the cursor marker."""
    async with AsyncSessionLocal() as session:
        # FORCE-RLS: this generator opens its OWN session (the request-scoped
        # one closes when the response starts streaming), so it must stamp the
        # tenant itself — the process-wide after_begin listener then issues
        # SET LOCAL app.current_tenant. Without this, under the NOBYPASSRLS
        # saebooks_app role every entity below streams zero rows.
        session.info["tenant_id"] = str(tenant_id)
        # ------------------------------------------------------------------ #
        # 1. companies — the current company (single row, no company_id filter
        #    needed; we already resolved company_id from it)
        # ------------------------------------------------------------------ #
        companies = (
            await session.execute(
                select(Company)
                .where(Company.id == company_id)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "companies", "_count": len(companies)}) + "\n"
        for row in companies:
            yield _dump(CompanyOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 2. tax_codes
        # ------------------------------------------------------------------ #
        tax_codes = (
            await session.execute(
                select(TaxCode)
                .where(TaxCode.company_id == company_id)
                .order_by(TaxCode.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "tax_codes", "_count": len(tax_codes)}) + "\n"
        for row in tax_codes:
            yield _dump(TaxCodeOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 3. accounts
        # ------------------------------------------------------------------ #
        accounts = (
            await session.execute(
                select(Account)
                .where(Account.company_id == company_id)
                .order_by(Account.code)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "accounts", "_count": len(accounts)}) + "\n"
        for row in accounts:
            yield _dump(AccountOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 4. contacts
        # ------------------------------------------------------------------ #
        contacts = (
            await session.execute(
                select(Contact)
                .where(Contact.company_id == company_id)
                .order_by(Contact.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "contacts", "_count": len(contacts)}) + "\n"
        for row in contacts:
            yield _dump(ContactOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 5. items
        # ------------------------------------------------------------------ #
        items = (
            await session.execute(
                select(Item)
                .where(Item.company_id == company_id)
                .order_by(Item.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "items", "_count": len(items)}) + "\n"
        for row in items:
            yield _dump(ItemOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 6. projects
        # ------------------------------------------------------------------ #
        projects = (
            await session.execute(
                select(Project)
                .where(Project.company_id == company_id)
                .order_by(Project.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "projects", "_count": len(projects)}) + "\n"
        for row in projects:
            yield _dump(ProjectOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 7. invoices — headers only (exclude lines)
        # ------------------------------------------------------------------ #
        invoices = (
            await session.execute(
                select(Invoice)
                .options(selectinload(Invoice.lines))
                .where(Invoice.company_id == company_id)
                .order_by(Invoice.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "invoices", "_count": len(invoices)}) + "\n"
        for row in invoices:
            yield (
                InvoiceOut.model_validate(row).model_dump_json(exclude={"lines"}) + "\n"
            )

        # ------------------------------------------------------------------ #
        # 8. bills — headers only (exclude lines)
        # ------------------------------------------------------------------ #
        bills = (
            await session.execute(
                select(Bill)
                .options(selectinload(Bill.lines))
                .where(Bill.company_id == company_id)
                .order_by(Bill.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "bills", "_count": len(bills)}) + "\n"
        for row in bills:
            yield BillOut.model_validate(row).model_dump_json(exclude={"lines"}) + "\n"

        # ------------------------------------------------------------------ #
        # 9. payments (allocations excluded for headers-only)
        # ------------------------------------------------------------------ #
        payments = (
            await session.execute(
                select(Payment)
                .options(selectinload(Payment.allocations))
                .where(Payment.company_id == company_id)
                .order_by(Payment.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "payments", "_count": len(payments)}) + "\n"
        for row in payments:
            yield (
                PaymentOut.model_validate(row).model_dump_json(exclude={"allocations"})
                + "\n"
            )

        # ------------------------------------------------------------------ #
        # 10. journal_entries — headers only via JournalEntryHeaderOut.
        # JournalEntryOut.lines walks each JournalLine, whose `account`
        # relationship is lazy='raise' — chained selectinload would work
        # but loads rows we'd immediately drop. The header-only schema
        # avoids the walk entirely.
        # ------------------------------------------------------------------ #
        journal_entries = (
            await session.execute(
                select(JournalEntry)
                .where(JournalEntry.company_id == company_id)
                .order_by(JournalEntry.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps(
            {"_entity": "journal_entries", "_count": len(journal_entries)}
        ) + "\n"
        for row in journal_entries:
            yield JournalEntryHeaderOut.model_validate(row).model_dump_json() + "\n"

        # ------------------------------------------------------------------ #
        # 11. bank_accounts — view over accounts where bsb IS NOT NULL
        # ------------------------------------------------------------------ #
        bank_accounts = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.bsb.isnot(None),
                )
                .order_by(Account.code)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps(
            {"_entity": "bank_accounts", "_count": len(bank_accounts)}
        ) + "\n"
        for row in bank_accounts:
            yield _dump(BankAccountOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 12. bank_statement_lines
        # ------------------------------------------------------------------ #
        bsl = (
            await session.execute(
                select(BankStatementLine)
                .where(BankStatementLine.company_id == company_id)
                .order_by(BankStatementLine.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps(
            {"_entity": "bank_statement_lines", "_count": len(bsl)}
        ) + "\n"
        for row in bsl:
            yield _dump(BankStatementLineOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 13. fixed_assets
        # ------------------------------------------------------------------ #
        fixed_assets = (
            await session.execute(
                select(FixedAsset)
                .options(selectinload(FixedAsset.depreciation_model))
                .where(FixedAsset.company_id == company_id)
                .order_by(FixedAsset.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps(
            {"_entity": "fixed_assets", "_count": len(fixed_assets)}
        ) + "\n"
        for row in fixed_assets:
            yield _dump(FixedAssetOut.model_validate(row)) + "\n"

        # ------------------------------------------------------------------ #
        # 14. budgets
        # ------------------------------------------------------------------ #
        budgets = (
            await session.execute(
                select(Budget)
                .where(Budget.company_id == company_id)
                .order_by(Budget.created_at)
                .limit(limit)
            )
        ).scalars().all()
        yield json.dumps({"_entity": "budgets", "_count": len(budgets)}) + "\n"
        for row in budgets:
            yield _dump(BudgetOut.model_validate(row)) + "\n"

    # Cursor line — emitted outside the session (session already closed)
    yield json.dumps({"_cursor": max_id}) + "\n"


@router.get("")
async def snapshot(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    limit = _entity_limit()
    tenant_id = resolve_tenant_id(request)

    # Read the change_log head first — cursor must be <= last visible change.
    max_id = (
        await session.execute(select(func.coalesce(func.max(ChangeLog.id), 0)))
    ).scalar_one()

    # Resolve the first active company for this tenant.
    company = (
        await session.execute(
            select(Company)
            .where(
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
            .order_by(Company.created_at)
        )
    ).scalars().first()

    if company is None:
        # No company yet — stream empty entity markers + cursor.
        async def _empty() -> AsyncGenerator[str, None]:
            for name in (
                "companies", "tax_codes", "accounts", "contacts", "items",
                "projects", "invoices", "bills", "payments", "journal_entries",
                "bank_accounts", "bank_statement_lines", "fixed_assets", "budgets",
            ):
                yield json.dumps({"_entity": name, "_count": 0}) + "\n"
            yield json.dumps({"_cursor": max_id}) + "\n"

        return StreamingResponse(
            _empty(),
            media_type="application/x-ndjson",
            headers={"X-Cursor-Next": str(max_id)},
        )

    company_id = company.id

    # _generate opens its own AsyncSessionLocal — async generators cannot
    # share the request-scoped session after it closes at response end. It
    # therefore re-stamps the tenant on its own session (see _generate); we
    # pass the already-resolved tenant_id through so the GUC + explicit tenant
    # filter + RLS all hold on the streamed transaction.
    return StreamingResponse(
        _generate(company_id, max_id, limit, tenant_id),
        media_type="application/x-ndjson",
        headers={"X-Cursor-Next": str(max_id)},
    )
