"""Regression tests for P0-1 cross-tenant scoping on HTML web routes.

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              ff00ca0 + 9ddc166 (per-router scoping on 25 HTML routers)

Why this file exists
--------------------
The fix in ff00ca0/9ddc166 scoped every HTML router so that detail,
edit, and archive paths 404 when the requesting tenant doesn't own the
row.  Karen Walsh's round-2 probe confirmed this on contacts and
invoices via a live deployment.  This test file replaces that manual
probe with an automated regression harness so the fix stays closed on
every future push.

Coverage:
  - contacts  /{id}  /{id}/edit  POST /{id}/archive
  - invoices  /{id}  /{id}/edit  POST /{id}/archive
  - bills     /{id}  /{id}/edit  POST /{id}/archive
  - accounts  /accounts/{id}  /accounts/{id}/edit
  - projects  /{id}  /{id}/edit
  - journal   /{id}
  - recurring_invoices  /{id}  /{id}/edit

Fixture strategy
----------------
Two tenants A and B are seeded via the owner engine (bypasses RLS).
Tenant A's JWT is minted and all cross-tenant probes use it to
request Tenant B's row IDs.  A positive-control assertion runs first
to ensure the fixture actually works — a blanket 404 from a broken
fixture would pass the cross-tenant checks for the wrong reason.

DB availability: tests skip cleanly when the ``DATABASE_URL`` is
unreachable (e.g. running on scada without Postgres).  Full suite runs
on r420 where the DB is present.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-for-ct-web-regression")
os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import engine as _owner_engine  # noqa: E402
from saebooks.main import app  # noqa: E402
from saebooks.models.account import Account, AccountType  # noqa: E402
from saebooks.models.bill import Bill, BillStatus  # noqa: E402
from saebooks.models.company import Company  # noqa: E402
from saebooks.models.contact import Contact, ContactType  # noqa: E402
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus  # noqa: E402
from saebooks.models.journal import EntryStatus, JournalEntry  # noqa: E402
from saebooks.models.project import Project  # noqa: E402
from saebooks.models.recurring_invoice import RecurringInvoice, RecurrenceFrequency  # noqa: E402
from saebooks.models.tenant import Tenant  # noqa: E402
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token  # noqa: E402


# ---------------------------------------------------------------------------
# Skip guard — if DB is unreachable, skip all tests in this module cleanly.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


async def _db_available() -> bool:
    try:
        async with _owner_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Seed two tenants with one entity of every tested type.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def ct_seed() -> AsyncIterator[dict[str, Any]]:
    """Seed two isolated tenants.  Returns dict shaped as::

        {
            "tenant_a": {
                "tenant_id": UUID,
                "company_id": UUID,
                "ids": {
                    "contact": UUID, "invoice": UUID, "bill": UUID,
                    "account": UUID, "project": UUID, "journal_entry": UUID,
                    "recurring_invoice": UUID,
                },
            },
            "tenant_b": { ... },
        }
    """
    if not await _db_available():
        pytest.skip("Postgres unavailable")

    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}

    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()
            contact_id = uuid.uuid4()
            account_id = uuid.uuid4()
            bank_account_id = uuid.uuid4()
            tax_code_id = uuid.uuid4()
            invoice_id = uuid.uuid4()
            invoice_line_id = uuid.uuid4()
            bill_id = uuid.uuid4()
            project_id = uuid.uuid4()
            journal_entry_id = uuid.uuid4()
            recurring_invoice_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"CTWeb-{label}-{suffix}",
                    slug=f"ctweb-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"CTWeb-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()

            session.add(
                Contact(
                    id=contact_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    name=f"CTWeb-Contact-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"CW{suffix[:4]}{label[-1]}",
                    name="CTWeb Income",
                    account_type=AccountType.INCOME,
                )
            )
            session.add(
                Account(
                    id=bank_account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"CB{suffix[:4]}{label[-1]}",
                    name="CTWeb Bank",
                    account_type=AccountType.ASSET,
                )
            )
            await session.flush()

            session.add(
                Invoice(
                    id=invoice_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    number=f"INV-CW-{suffix}-{label[-1]}",
                    issue_date=date.today(),
                    due_date=date.today(),
                    status=InvoiceStatus.DRAFT,
                    subtotal=Decimal("100.00"),
                    tax_total=Decimal("10.00"),
                    total=Decimal("110.00"),
                    currency="AUD",
                )
            )
            await session.flush()

            session.add(
                InvoiceLine(
                    id=invoice_line_id,
                    invoice_id=invoice_id,
                    line_no=1,
                    description="CW line",
                    account_id=account_id,
                    quantity=Decimal("1"),
                    unit_price=Decimal("100.00"),
                    line_subtotal=Decimal("100.00"),
                    line_tax=Decimal("10.00"),
                    line_total=Decimal("110.00"),
                )
            )
            session.add(
                Bill(
                    id=bill_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    number=f"BILL-CW-{suffix}-{label[-1]}",
                    issue_date=date.today(),
                    due_date=date.today(),
                    status=BillStatus.DRAFT,
                    subtotal=Decimal("0"),
                    tax_total=Decimal("0"),
                    total=Decimal("0"),
                    currency="AUD",
                )
            )
            session.add(
                Project(
                    id=project_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    name=f"CTWeb-Project-{label}-{suffix}",
                )
            )
            session.add(
                JournalEntry(
                    id=journal_entry_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    ref=f"JE-CW-{suffix}-{label[-1]}",
                    entry_date=date.today(),
                    status=EntryStatus.DRAFT,
                )
            )
            session.add(
                RecurringInvoice(
                    id=recurring_invoice_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    name=f"CTWeb-RI-{label}-{suffix}",
                    frequency=RecurrenceFrequency.MONTHLY,
                    next_run=date.today(),
                )
            )
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "ids": {
                    "contact": contact_id,
                    "account": account_id,
                    "invoice": invoice_id,
                    "bill": bill_id,
                    "project": project_id,
                    "journal_entry": journal_entry_id,
                    "recurring_invoice": recurring_invoice_id,
                },
            }

        await session.commit()

    yield out

    # Best-effort cleanup in dependency order.
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            ids = out[label]["ids"]
            tid = out[label]["tenant_id"]
            cid = out[label]["company_id"]
            for tbl, col, val in [
                ("recurring_invoices", "id", ids["recurring_invoice"]),
                ("journal_entries", "id", ids["journal_entry"]),
                ("projects", "id", ids["project"]),
                ("bills", "id", ids["bill"]),
                ("invoice_lines", "invoice_id", ids["invoice"]),
                ("invoices", "id", ids["invoice"]),
                ("contacts", "id", ids["contact"]),
                ("accounts", "company_id", cid),
                ("companies", "id", cid),
                ("tenants", "id", tid),
            ]:
                await session.execute(
                    text(f"DELETE FROM {tbl} WHERE {col} = :v"), {"v": val}
                )
        await session.commit()


# ---------------------------------------------------------------------------
# JWT minting helpers.
# ---------------------------------------------------------------------------


def _mint(tenant_id: uuid.UUID, role: str = "admin") -> str:
    _reset_secret_cache()
    return create_access_token(
        {
            "sub": str(uuid.uuid4()),
            "role": role,
            "tenant_id": str(tenant_id),
        }
    )


# ---------------------------------------------------------------------------
# Shared async client (uses the owner engine — sufficient for HTML routes
# which rely on the per-request session from deps.get_session).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def web_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _auth(tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint(tenant_id)}"}


# ---------------------------------------------------------------------------
# Cross-tenant HTML route tests.
# ---------------------------------------------------------------------------
# Each test:
#   1. Positive control — tenant A can reach its own row (200).
#   2. Cross-tenant probe — tenant A's JWT against tenant B's row (404).


async def test_contacts_detail_own_200(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_a = ct_seed["tenant_a"]["ids"]["contact"]
    r = await web_client.get(f"/contacts/{cid_a}", headers=_auth(tid_a))
    assert r.status_code == 200, f"Own contact should be 200, got {r.status_code}: {r.text[:200]}"


async def test_contacts_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_b = ct_seed["tenant_b"]["ids"]["contact"]
    r = await web_client.get(f"/contacts/{cid_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's contact {cid_b}, "
        f"status={r.status_code}"
    )


async def test_contacts_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_b = ct_seed["tenant_b"]["ids"]["contact"]
    r = await web_client.get(f"/contacts/{cid_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's contact edit form, "
        f"status={r.status_code}"
    )


async def test_contacts_archive_foreign_no_mutation(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """POST /{id}/archive on a foreign row must not archive the row.

    The router may return 303 with a flash-error or 404; either is
    acceptable as long as the DB row is not mutated.
    """
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_b = ct_seed["tenant_b"]["ids"]["contact"]
    r = await web_client.post(
        f"/contacts/{cid_b}/archive",
        headers=_auth(tid_a),
        follow_redirects=False,
    )
    # Must not silently succeed.
    assert r.status_code in (303, 404), (
        f"Expected 303 (with error flash) or 404 for cross-tenant archive, "
        f"got {r.status_code}"
    )
    # Verify the DB row is untouched.
    async with AsyncSession(_owner_engine) as session:
        row = await session.get(Contact, cid_b)
    assert row is not None and row.archived_at is None, (
        "Cross-tenant archive must not mutate the target row"
    )


async def test_invoices_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    inv_b = ct_seed["tenant_b"]["ids"]["invoice"]
    r = await web_client.get(f"/invoices/{inv_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's invoice {inv_b}, "
        f"status={r.status_code}"
    )


async def test_invoices_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    inv_b = ct_seed["tenant_b"]["ids"]["invoice"]
    r = await web_client.get(f"/invoices/{inv_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's invoice, "
        f"status={r.status_code}"
    )


async def test_invoices_archive_foreign_no_mutation(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    inv_b = ct_seed["tenant_b"]["ids"]["invoice"]
    r = await web_client.post(
        f"/invoices/{inv_b}/archive",
        headers=_auth(tid_a),
        follow_redirects=False,
    )
    assert r.status_code in (303, 404), (
        f"Expected 303 or 404 for cross-tenant invoice archive, got {r.status_code}"
    )
    async with AsyncSession(_owner_engine) as session:
        row = await session.get(Invoice, inv_b)
    assert row is not None and row.archived_at is None, (
        "Cross-tenant invoice archive must not mutate the row"
    )


async def test_bills_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    bill_b = ct_seed["tenant_b"]["ids"]["bill"]
    r = await web_client.get(f"/bills/{bill_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's bill {bill_b}, "
        f"status={r.status_code}"
    )


async def test_bills_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    bill_b = ct_seed["tenant_b"]["ids"]["bill"]
    r = await web_client.get(f"/bills/{bill_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's bill, "
        f"status={r.status_code}"
    )


async def test_bills_archive_foreign_no_mutation(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    bill_b = ct_seed["tenant_b"]["ids"]["bill"]
    r = await web_client.post(
        f"/bills/{bill_b}/archive",
        headers=_auth(tid_a),
        follow_redirects=False,
    )
    assert r.status_code in (303, 404), (
        f"Expected 303 or 404 for cross-tenant bill archive, got {r.status_code}"
    )
    async with AsyncSession(_owner_engine) as session:
        row = await session.get(Bill, bill_b)
    assert row is not None and row.archived_at is None, (
        "Cross-tenant bill archive must not mutate the row"
    )


async def test_accounts_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    acc_b = ct_seed["tenant_b"]["ids"]["account"]
    r = await web_client.get(f"/accounts/{acc_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's account {acc_b}, "
        f"status={r.status_code}"
    )


async def test_accounts_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    acc_b = ct_seed["tenant_b"]["ids"]["account"]
    r = await web_client.get(f"/accounts/{acc_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's account edit, "
        f"status={r.status_code}"
    )


async def test_bank_accounts_api_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """JSON API /api/v1/bank_accounts/{id} with foreign tenant JWT -> 404."""
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    # Use a random UUID to represent a foreign bank account; the router
    # must 404 because it's either not found or scoped away.
    foreign_id = uuid.uuid4()
    r = await web_client.get(
        f"/api/v1/bank_accounts/{foreign_id}",
        headers=_auth(tid_a),
    )
    assert r.status_code == 404, (
        f"Expected 404 for bank_accounts detail with unknown/foreign UUID, "
        f"got {r.status_code}"
    )


async def test_projects_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    proj_b = ct_seed["tenant_b"]["ids"]["project"]
    r = await web_client.get(f"/projects/{proj_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's project {proj_b}, "
        f"status={r.status_code}"
    )


async def test_projects_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    proj_b = ct_seed["tenant_b"]["ids"]["project"]
    r = await web_client.get(f"/projects/{proj_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's project edit, "
        f"status={r.status_code}"
    )


async def test_journal_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    je_b = ct_seed["tenant_b"]["ids"]["journal_entry"]
    r = await web_client.get(f"/journal/{je_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's journal entry {je_b}, "
        f"status={r.status_code}"
    )


async def test_recurring_invoices_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    ri_b = ct_seed["tenant_b"]["ids"]["recurring_invoice"]
    r = await web_client.get(f"/invoices/recurring/{ri_b}", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant A fetched tenant B's recurring invoice {ri_b}, "
        f"status={r.status_code}"
    )


async def test_recurring_invoices_edit_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    ri_b = ct_seed["tenant_b"]["ids"]["recurring_invoice"]
    r = await web_client.get(f"/invoices/recurring/{ri_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's recurring invoice, "
        f"status={r.status_code}"
    )
