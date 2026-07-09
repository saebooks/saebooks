"""Cross-tenant isolation regression test (P0 leak).

Why this test exists
--------------------
``audit-trail/02-cross-tenant-leak-diagnosis.md`` documents three
compounding bugs that let an authenticated request for tenant B fetch
rows owned by tenant A:

1. ``_first_company_id`` returned the oldest active company in the
   whole DB.
2. RLS policies were ``ENABLE``d but not ``FORCE``d, so the schema
   owner role bypassed them.
3. The session-scoped ``app.current_tenant`` GUC was never set per
   request — every query saw "no tenant".

After the three fixes (migrations 0055/0056 + the ``Depends(get_session)``
refactor of every tier-1/-2/-3 router), the only surface that can leak
is a buggy service helper. This test parametrises over the seven
high-value resources and asserts:

* GET /<resource>/{tenant_a_row_id} with tenant A's JWT  → 200
* GET /<resource>/{tenant_a_row_id} with tenant B's JWT  → 404

Critical fixture detail: the API runs through a session bound to the
``saebooks_app`` Postgres role (NOSUPERUSER, NOBYPASSRLS). Without
this override the test would pass even with all three bugs reverted —
the schema owner sees every row regardless of policy. The seeding
session uses the original ``saebooks`` role so the test can place rows
into either tenant deterministically.

The ``saebooks_app`` password is set by the fixture (no rebuild
required) — see ``_set_app_role_password``.

If this test goes red on a future change, the fix sequence is:

* Confirm all three migrations are present and applied
  (``alembic upgrade head``).
* Confirm ``deps.get_session`` still hangs the tenant id off
  ``session.info`` and the module-level ``after_begin`` listener is
  still installed (it re-issues ``SET LOCAL app.current_tenant``
  on every transaction).
* Confirm the offending router uses ``Depends(get_session)`` and the
  service helper accepts ``tenant_id``.

Do not silence this test. The whole P0 fix exists to make these seven
assertions hold.
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

pytestmark = pytest.mark.postgres_only


# Pin the JWT secret BEFORE any saebooks module loads so the tokens
# we mint match what the running app verifies. ``conftest.py`` only
# sets SAEBOOKS_ENV, not the secret.
os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-for-cross-tenant-tests")

from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.payment import (
    Payment,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.services.jwt_tokens import (
    _reset_secret_cache,
    create_access_token,
)

# ---------------------------------------------------------------------------
# saebooks_app engine — connects via the locked-down runtime role.
# ---------------------------------------------------------------------------
# The app's normal engine (``saebooks.db.engine``) is created at import
# time from ``DATABASE_URL`` which today points at the schema-owner
# role. For this test we MUST go through ``saebooks_app`` so FORCE RLS
# binds the session — see file docstring.

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


def _build_app_engine_url() -> str:
    """Build a saebooks_app URL using the same host+port+db as DATABASE_URL.

    The previous hardcoded "db:5432/saebooks" assumed the production
    container layout; the test stack uses saebooks_test on the test
    network, so deriving from settings keeps this fixture portable.
    """
    from urllib.parse import urlsplit, urlunsplit

    from saebooks.config import settings

    parts = urlsplit(settings.database_url)
    netloc = f"saebooks_app:{_APP_ROLE_PASSWORD}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


_APP_ENGINE_URL = _build_app_engine_url()


async def _set_app_role_password() -> None:
    """Set the saebooks_app password to the test value.

    Idempotent: running this against a fresh DB or a DB whose password
    is already correct both end up with the test value. Uses the
    superuser engine so the ALTER ROLE works.
    """
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    """Module-scoped engine bound to the saebooks_app role."""
    await _set_app_role_password()
    eng = create_async_engine(_APP_ENGINE_URL, poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


# ---------------------------------------------------------------------------
# Tenant + entity seed (uses the OWNER engine — bypasses RLS for setup).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def seeded() -> dict[str, Any]:
    """Create two tenants, each with a company and one row of every entity.

    Returns a dict shaped like::

        {
            "tenant_a": {
                "tenant_id": UUID,
                "company_id": UUID,
                "ids": {"contact": UUID, "invoice": UUID, ...},
            },
            "tenant_b": { ... },
        }

    The seed uses the OWNER (saebooks superuser) engine deliberately:
    we need to write rows into BOTH tenants regardless of any
    ``app.current_tenant`` GUC.
    """
    Owner = async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )

    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}

    # Stage the inserts in dependency order — Tenant → Company →
    # (Contact, Account, TaxCode) → Invoice → InvoiceLine → Bill,
    # Payment, JournalEntry. Flush after each layer so SQLAlchemy
    # doesn't reorder the INSERTs (asyncpg's prepared-statement
    # batcher honours statement order, but only within a flush).
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
            payment_id = uuid.uuid4()
            journal_entry_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Iso-{label}-{suffix}",
                    slug=f"iso-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"Iso-{label}-{suffix}",
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
                    name=f"Iso-Contact-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"X{suffix[:4]}-{label[-1]}",
                    name="Iso Income",
                    account_type=AccountType.INCOME,
                )
            )
            session.add(
                Account(
                    id=bank_account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"B{suffix[:4]}-{label[-1]}",
                    name="Iso Bank",
                    account_type=AccountType.ASSET,
                )
            )
            session.add(
                TaxCode(
                    id=tax_code_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"X{suffix[:3]}{label[-1]}",
                    name="Iso GST",
                    rate=Decimal("10.000"),
                    tax_system="GST",
                    reporting_type="taxable",
                )
            )
            await session.flush()

            session.add(
                Invoice(
                    id=invoice_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    number=f"INV-ISO-{suffix}-{label[-1]}",
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
                    description="Iso line",
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
                    number=f"BILL-ISO-{suffix}-{label[-1]}",
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
                Payment(
                    id=payment_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    bank_account_id=bank_account_id,
                    number=f"PAY-ISO-{suffix}-{label[-1]}",
                    direction=PaymentDirection.INCOMING,
                    method=PaymentMethod.EFT,
                    status=PaymentStatus.DRAFT,
                    payment_date=date.today(),
                    amount=Decimal("100.00"),
                    currency="AUD",
                )
            )
            session.add(
                JournalEntry(
                    id=journal_entry_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    ref=f"JE-ISO-{suffix}-{label[-1]}",
                    entry_date=date.today(),
                    status=EntryStatus.DRAFT,
                )
            )
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "ids": {
                    "contact": contact_id,
                    "account": account_id,
                    "tax_code": tax_code_id,
                    "invoice": invoice_id,
                    "bill": bill_id,
                    "payment": payment_id,
                    "journal_entry": journal_entry_id,
                },
            }

        await session.commit()

    yield out

    # Best-effort cleanup. Foreign keys are ON DELETE CASCADE for child
    # rows that matter; we delete in dependency order to satisfy
    # RESTRICTED FKs (Payment→Account, Bill→Contact, etc).
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            ids = out[label]["ids"]
            tid = out[label]["tenant_id"]
            cid = out[label]["company_id"]
            await session.execute(
                text("DELETE FROM journal_entries WHERE id = :id"),
                {"id": ids["journal_entry"]},
            )
            await session.execute(
                text("DELETE FROM payments WHERE id = :id"),
                {"id": ids["payment"]},
            )
            await session.execute(
                text("DELETE FROM bills WHERE id = :id"),
                {"id": ids["bill"]},
            )
            await session.execute(
                text(
                    "DELETE FROM invoice_lines WHERE invoice_id = :id"
                ),
                {"id": ids["invoice"]},
            )
            await session.execute(
                text("DELETE FROM invoices WHERE id = :id"),
                {"id": ids["invoice"]},
            )
            await session.execute(
                text("DELETE FROM tax_codes WHERE id = :id"),
                {"id": ids["tax_code"]},
            )
            await session.execute(
                text("DELETE FROM contacts WHERE id = :id"),
                {"id": ids["contact"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :cid"),
                {"cid": cid},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": cid},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": tid},
            )
        await session.commit()


# ---------------------------------------------------------------------------
# JWT minting + dependency override.
# ---------------------------------------------------------------------------


def _mint_jwt(tenant_id: uuid.UUID) -> str:
    """Return a bearer token whose ``tenant_id`` claim is the given uuid."""
    _reset_secret_cache()
    return create_access_token(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "tenant_id": str(tenant_id),
        }
    )


@pytest_asyncio.fixture(scope="module")
async def configured_app(app_engine: Any) -> AsyncIterator[Any]:
    """Swap deps.AsyncSessionLocal so the dep yields saebooks_app sessions.

    Why monkeypatch and not ``app.dependency_overrides``:

    FastAPI's override mechanism rebuilds the dep's parameter spec from
    the override function's signature. ``deps.get_session`` takes
    ``request: Request`` — a special FastAPI parameter that maps to
    the live request — and FastAPI knows about it because the
    function is registered as a path-operation dep. When the override
    function is built fresh by FastAPI, the ``Request`` annotation
    isn't recognised the same way and ``request`` gets reclassified
    as a query parameter, producing 422 errors at request time.

    Replacing ``AsyncSessionLocal`` in the deps module is a smaller,
    semantically-equivalent change: ``get_session`` keeps its original
    signature and still calls ``AsyncSessionLocal()``; we just make
    that name resolve to a sessionmaker bound to the saebooks_app
    engine instead of the default owner engine.
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


@pytest_asyncio.fixture
async def client(configured_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=configured_app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Resource catalogue.
# ---------------------------------------------------------------------------
# Each entry is (resource_key, url_path_template). The url is built
# with the "tenant A" row id at request time. The seven resources
# match the audit-trail diagnosis — every tier-1/-2/-3 entity that has
# a GET /<resource>/{id} surface.

_RESOURCES: list[tuple[str, str]] = [
    ("contact", "/api/v1/contacts/{id}"),
    ("invoice", "/api/v1/invoices/{id}"),
    ("bill", "/api/v1/bills/{id}"),
    ("payment", "/api/v1/payments/{id}"),
    ("journal_entry", "/api/v1/journal_entries/{id}"),
    ("account", "/api/v1/accounts/{id}"),
    ("tax_code", "/api/v1/tax_codes/{id}"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource,path_template", _RESOURCES)
async def test_get_foreign_tenant_returns_404(
    client: AsyncClient,
    seeded: dict[str, Any],
    resource: str,
    path_template: str,
) -> None:
    """Tenant B's JWT must not be able to read tenant A's row.

    Three layers should all conspire to return 404:

    1. ``app.current_tenant`` GUC set from tenant B's JWT.
    2. RLS ``USING (tenant_id = current_setting(...)::uuid)`` filters
       the row out at the DB layer (FORCE'd against saebooks_app).
    3. The service helper's ``WHERE tenant_id = ...`` filter (the
       belt to RLS's braces) returns ``None``.

    A 200 response means at least one layer is broken.
    """
    a_row_id = seeded["tenant_a"]["ids"][resource]
    tenant_b_jwt = _mint_jwt(seeded["tenant_b"]["tenant_id"])

    url = path_template.format(id=a_row_id)
    r = await client.get(
        url,
        headers={"Authorization": f"Bearer {tenant_b_jwt}"},
    )
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: tenant B fetched tenant A's {resource} "
        f"row {a_row_id} via {url} — status {r.status_code}, body={r.text}"
    )


@pytest.mark.parametrize("resource,path_template", _RESOURCES)
async def test_get_own_tenant_returns_200(
    client: AsyncClient,
    seeded: dict[str, Any],
    resource: str,
    path_template: str,
) -> None:
    """Positive control: tenant A's JWT can read tenant A's row.

    If this fails the foreign-tenant test is meaningless — a blanket
    401/404 from a misconfigured override would pass the leak test
    for the wrong reason. Both assertions must hold.
    """
    a_row_id = seeded["tenant_a"]["ids"][resource]
    tenant_a_jwt = _mint_jwt(seeded["tenant_a"]["tenant_id"])

    url = path_template.format(id=a_row_id)
    r = await client.get(
        url,
        headers={"Authorization": f"Bearer {tenant_a_jwt}"},
    )
    assert r.status_code == 200, (
        f"Positive control failed: tenant A could not fetch its own "
        f"{resource} row {a_row_id} via {url} — status {r.status_code}, body={r.text}"
    )
    body = r.json()
    assert body["id"] == str(a_row_id), (
        f"Got 200 but body id mismatch — got {body.get('id')}, "
        f"expected {a_row_id}"
    )
