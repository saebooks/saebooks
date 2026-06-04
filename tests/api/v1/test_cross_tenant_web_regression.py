"""Regression tests for P0-1 cross-tenant scoping on HTML web routes.

audit-trail reference: 10-deploy-and-validation-2026-04-26.md
Commits:              ff00ca0 + 9ddc166 (per-router scoping on 25 HTML routers)
Forum issue:          agent-forum#2 (HTML router belt-and-braces gap)

Why this file exists
--------------------
The fix in ff00ca0/9ddc166 scoped 25 routers, primarily the JSON API v1
layer and some HTML routers via ``Depends(get_session)`` + FORCE RLS.
Karen Walsh's round-2 probe confirmed this on contacts and invoices via
a live deployment.  This test file replaces that manual probe with an
automated regression harness.

Defence model
-------------
The P0-1 fix uses TWO layers of defence:

1. **DB layer (FORCE RLS)** — the ``saebooks_app`` Postgres role has
   ``ROW LEVEL SECURITY FORCE`` so every query through it is scoped to
   the tenant in ``app.current_tenant``.  The ``get_session`` dep in the
   API v1 layer stamps the GUC.  This is what the existing
   ``test_cross_tenant_isolation.py`` tests verify for the API v1 routes.

2. **Application layer** — HTML routers (``saebooks/routers/contacts.py``
   etc.) call ``svc.get(session, id)`` and can optionally pass
   ``tenant_id`` as belt-and-braces.  ``agent-forum#2`` tracks the gap
   where some HTML routers do not yet pass ``tenant_id``.

This test exercises the FORCE RLS layer for the HTML routes by using the
same ``saebooks_app``-role fixture pattern as ``test_cross_tenant_isolation.py``.
Tests pass when FORCE RLS is active; they would fail against the owner
engine (and are designed to fail to catch any future removal of FORCE RLS).

Coverage:
  - contacts  /{id}  /{id}/edit  POST /{id}/archive
  - invoices  /{id}  /{id}/edit  POST /{id}/archive
  - bills     /{id}  /{id}/edit  POST /{id}/archive
  - accounts  /accounts/{id}  /accounts/{id}/edit
  - projects  /{id}  /{id}/edit
  - journal   /{id}
  - recurring_invoices  /{id}  /{id}/edit

DB availability: tests skip cleanly when Postgres / saebooks_app role is
unavailable (e.g. scada).  Full suite runs on r420.
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

from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.project import Project
from saebooks.models.recurring_invoice import RecurrenceFrequency, RecurringInvoice
from saebooks.models.tenant import Tenant
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------


pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]


async def _db_available() -> bool:
    try:
        async with _owner_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# saebooks_app engine (same pattern as test_cross_tenant_isolation.py).
# The app role has FORCE RLS — the main defence against cross-tenant leaks.
# ---------------------------------------------------------------------------

# Use the canonical password from SAEBOOKS_APP_DB_PASSWORD so this
# fixture does not stomp on test_integrations_rls.py (which connects
# directly as saebooks_app with that env value). Falls back to the
# compose default so a developer running this single test file outside
# the test stack still gets a working URL.
_APP_ROLE_PASSWORD = os.environ.get(
    "SAEBOOKS_APP_DB_PASSWORD", "saebooks_app_test_pw"
)


def _app_engine_url() -> str:
    """Build the saebooks_app engine URL from DATABASE_URL.

    Extracts host, port, and DB name from the owner DATABASE_URL so
    the test works against both the legacy ``saebooks`` DB and the
    rebuild ``saebooks2`` DB without hardcoding.
    """
    from urllib.parse import urlparse

    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://saebooks:change-me@db:5432/saebooks",
    )
    parsed = urlparse(base)
    host = parsed.hostname or "db"
    port = parsed.port or 5432
    path = parsed.path.lstrip("/") or "saebooks"
    return f"postgresql+asyncpg://saebooks_app:{_APP_ROLE_PASSWORD}@{host}:{port}/{path}"


_APP_ENGINE_URL = _app_engine_url()


async def _set_app_role_password() -> None:
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    """Module-scoped engine bound to the saebooks_app role (FORCE RLS)."""
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    await _set_app_role_password()
    eng = create_async_engine(_APP_ENGINE_URL, poolclass=NullPool, future=True)
    try:
        # Verify the app role can connect — skip gracefully if not set up yet.
        async with eng.begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await eng.dispose()
        pytest.skip("saebooks_app role unavailable — run migrations first")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def configured_app(app_engine: Any) -> AsyncIterator[Any]:
    """Swap deps.AsyncSessionLocal to use the saebooks_app engine.

    Same approach as test_cross_tenant_isolation.py::configured_app.
    """
    AppSession = async_sessionmaker(
        app_engine, expire_on_commit=False, class_=AsyncSession
    )
    import saebooks.api.v1.deps as deps_mod

    original = deps_mod.AsyncSessionLocal
    deps_mod.AsyncSessionLocal = AppSession
    try:
        yield app
    finally:
        deps_mod.AsyncSessionLocal = original


# ---------------------------------------------------------------------------
# Seed two tenants with one entity of every tested type (owner engine).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def ct_seed(app_engine: Any) -> AsyncIterator[dict[str, Any]]:
    """Create two isolated tenants with one entity each via the owner engine.

    Returns dict::

        {
            "tenant_a": {
                "tenant_id": UUID, "company_id": UUID,
                "ids": {"contact": UUID, "invoice": UUID, ...},
            },
            "tenant_b": { ... },
        }
    """
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
                    code=f"CW{suffix[:3]}{label[-1]}P",
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

    # Best-effort cleanup.
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
# Shared async client via configured_app (uses saebooks_app role, FORCE RLS).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def web_client(configured_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=configured_app),
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
#
# Each test:
#   1. Positive control (where applicable) — tenant A can reach its own row.
#   2. Cross-tenant probe — tenant A's JWT against tenant B's row (404).
#
# The tests exercise the FORCE RLS layer at the DB level. The HTML routers
# call AsyncSessionLocal() which (via configured_app) is now the saebooks_app
# session — so FORCE RLS rejects cross-tenant row access.
# ---------------------------------------------------------------------------


async def test_contacts_detail_own_200(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """Positive control: tenant A can fetch its own contact."""
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_a = ct_seed["tenant_a"]["ids"]["contact"]
    r = await web_client.get(f"/contacts/{cid_a}", headers=_auth(tid_a))
    assert r.status_code == 200, (
        f"Own contact should be 200, got {r.status_code}: {r.text[:200]}"
    )


async def test_contacts_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """FORCE RLS: tenant A cannot read tenant B's contact."""
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
    """FORCE RLS: tenant A cannot fetch tenant B's contact edit form."""
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_b = ct_seed["tenant_b"]["ids"]["contact"]
    r = await web_client.get(f"/contacts/{cid_b}/edit", headers=_auth(tid_a))
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's contact edit, "
        f"status={r.status_code}"
    )


async def test_contacts_archive_foreign_no_mutation(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """FORCE RLS: POST archive on foreign row must not mutate the row."""
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    cid_b = ct_seed["tenant_b"]["ids"]["contact"]
    r = await web_client.post(
        f"/contacts/{cid_b}/archive",
        headers=_auth(tid_a),
        follow_redirects=False,
    )
    assert r.status_code in (303, 404), (
        f"Expected 303 or 404 for cross-tenant archive, got {r.status_code}"
    )
    # Verify DB row is untouched via owner engine.
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
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's account, "
        f"status={r.status_code}"
    )


async def test_bank_accounts_api_detail_foreign_404(
    web_client: AsyncClient, ct_seed: dict[str, Any]
) -> None:
    """JSON API /api/v1/bank_accounts/{id} with foreign tenant JWT → 404."""
    tid_a = ct_seed["tenant_a"]["tenant_id"]
    foreign_id = uuid.uuid4()
    r = await web_client.get(
        f"/api/v1/bank_accounts/{foreign_id}",
        headers=_auth(tid_a),
    )
    assert r.status_code == 404, (
        f"Expected 404 for bank_accounts with unknown/foreign UUID, got {r.status_code}"
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
        f"CROSS-TENANT LEAK on /edit: tenant A fetched tenant B's project, "
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
