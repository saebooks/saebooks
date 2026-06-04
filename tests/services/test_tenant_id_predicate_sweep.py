"""Lane 4 P0-2 regression — tenant_id predicate sweep across 8 service functions.

Agent H defence-in-depth: every list/get service function on tenant-scoped
models now filters on BOTH company_id AND tenant_id. These tests verify that
a corrupt row — one whose company_id and tenant_id disagree (injected via raw
SQL to bypass the ORM) — does NOT appear in the results when queried from the
correct tenant context.

Models tested: accounts, contacts, items, tax_codes, projects,
               journal_templates, departments, cost_centres.

For each model the probe pattern is:
1. Create tenant-A company + tenant-A valid row via the service (good row).
2. Create tenant-B company.
3. Inject a corrupt row directly via raw SQL: company_id = tenant-A's company
   but tenant_id = tenant-B's tenant.
4. Call the service's list function from tenant-A context
   (company_id=A, tenant_id=A).
5. Assert the good row appears and the corrupt row does NOT.

Note: raw SQL injection bypasses the 0128 trigger, which only applies to
the live schema.  The test database may not have run that migration yet, so
we use op.execute(text(...)) to disable the trigger for the injection step,
or simply insert via raw SQL with the trigger disabled at session level.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import AccountType
from saebooks.models.company import Company
from saebooks.models.contact import ContactType
from saebooks.models.tenant import Tenant
from saebooks.services import accounts as accounts_svc
from saebooks.services import contacts as contacts_svc
from saebooks.services import items as items_svc
from saebooks.services import journal_templates as jt_svc
from saebooks.services import projects as projects_svc
from saebooks.services import tax_codes as tax_codes_svc
from saebooks.services.departments import (
    list_cost_centres,
    list_departments,
)

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Shared fixture: two tenants, each with one company
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_tenants() -> dict:
    """Seed two isolated tenants (alpha, beta) each with one company.

    Returns a dict with 'alpha' and 'beta' keys, each holding:
        tenant_id, company_id
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict = {}

    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"H-{label}-{suffix}",
                    slug=f"h-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"H-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()

            out[label] = {"tenant_id": tenant_id, "company_id": company_id}

        await session.commit()

    yield out

    # Cleanup — companies.cascade removes child rows on most tables
    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            ids = out[label]
            # Cascade from companies handles most FK children
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": ids["company_id"]},
            )
        await session.commit()

    # Delete change_log rows referencing these tenants before deleting tenants
    # (change_log has FK constraint on tenant_id)
    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            ids = out[label]
            await session.execute(
                text("DELETE FROM change_log WHERE tenant_id = :tid"),
                {"tid": ids["tenant_id"]},
            )
        await session.commit()

    # Tenants after companies and change_log are gone
    async with AsyncSessionLocal() as session:
        for label in ("alpha", "beta"):
            ids = out[label]
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": ids["tenant_id"]},
            )
        await session.commit()


def _inject(table: str, row: dict) -> text:
    """Return a raw INSERT that bypasses the trigger.

    We temporarily disable the coherence trigger for this session so the
    corrupt row can be inserted.  This simulates pre-migration data corruption
    (e.g. from the critic seeding that caused the original leak).
    """
    cols = ", ".join(row.keys())
    vals = ", ".join(f":{k}" for k in row)
    return text(f"INSERT INTO {table} ({cols}) VALUES ({vals})")


async def _disable_trigger(session, table: str) -> None:
    """Disable the tenant-coherence trigger for this session (if it exists)."""
    tname = f"trg_{table}_tenant_coherence"
    try:
        await session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {tname}"))
    except Exception:
        # Trigger may not exist if migration 0128 hasn't run in the test DB yet
        await session.rollback()


async def _enable_trigger(session, table: str) -> None:
    """Re-enable the tenant-coherence trigger."""
    tname = f"trg_{table}_tenant_coherence"
    try:
        await session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {tname}"))
    except Exception:
        await session.rollback()


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


async def test_accounts_list_active_excludes_corrupt_row(two_tenants: dict) -> None:
    """A corrupt account (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]

    good_id = uuid.uuid4()
    corrupt_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:4]

    async with AsyncSessionLocal() as session:
        # Good row via service
        good = await accounts_svc.create(
            session,
            alpha["company_id"],
            code=f"6-{suffix}01",
            name=f"H-good-{suffix}",
            account_type=AccountType.EXPENSE,
            skip_validation=True,
            tenant_id=alpha["tenant_id"],
        )
        good_id = good.id

    # Inject corrupt row directly (company=alpha, tenant=beta)
    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "accounts")
        await session.execute(
            text(
                "INSERT INTO accounts "
                "(id, company_id, tenant_id, code, name, account_type, version) "
                "VALUES (:id, :cid, :tid, :code, :name, :atype, 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],  # WRONG tenant
                "code": f"6-{suffix}99",
                "name": f"H-corrupt-{suffix}",
                "atype": "EXPENSE",
            },
        )
        await _enable_trigger(session, "accounts")
        await session.commit()

    # Query from alpha context — corrupt row must not appear
    async with AsyncSessionLocal() as session:
        rows = await accounts_svc.list_active(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids, "Good row should be visible"
    assert corrupt_id not in ids, "Corrupt row must NOT leak to alpha"

    # Cleanup
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM accounts WHERE id = :id"), {"id": corrupt_id}
        )
        await session.commit()


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


async def test_contacts_list_active_excludes_corrupt_row(two_tenants: dict) -> None:
    """A corrupt contact (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        good = await contacts_svc.create(
            session,
            alpha["company_id"],
            tenant_id=alpha["tenant_id"],
            name=f"H-good-contact-{suffix}",
            contact_type=ContactType.SUPPLIER,
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "contacts")
        await session.execute(
            text(
                "INSERT INTO contacts "
                "(id, company_id, tenant_id, name, contact_type, version) "
                "VALUES (:id, :cid, :tid, :name, :ctype, 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "name": f"H-corrupt-contact-{suffix}",
                "ctype": "SUPPLIER",
            },
        )
        await _enable_trigger(session, "contacts")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await contacts_svc.list_active(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt contact must NOT leak to alpha"

    # search_by_name path too
    async with AsyncSessionLocal() as session:
        rows = await contacts_svc.search_by_name(
            session,
            alpha["company_id"],
            "H-corrupt",
            tenant_id=alpha["tenant_id"],
        )
    assert corrupt_id not in {r.id for r in rows}, "search_by_name must not surface corrupt row"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM contacts WHERE id = :id"), {"id": corrupt_id}
        )
        await session.commit()


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


async def test_items_list_items_excludes_corrupt_row(two_tenants: dict) -> None:
    """A corrupt item (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    # Need accounts for the item FKs - create them via raw SQL to avoid range validation
    inv_acct_id = uuid.uuid4()
    cogs_acct_id = uuid.uuid4()
    inc_acct_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        for acct_id, atype in [
            (inv_acct_id, "ASSET"),
            (cogs_acct_id, "EXPENSE"),
            (inc_acct_id, "INCOME"),
        ]:
            await session.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, company_id, tenant_id, code, name, account_type, version) "
                    "VALUES (:id, :cid, :tid, :code, :name, :atype, 1)"
                ),
                {
                    "id": acct_id,
                    "cid": alpha["company_id"],
                    "tid": alpha["tenant_id"],
                    "code": f"H{suffix}{atype[:2]}",
                    "name": f"H-item-acct-{atype[:2]}-{suffix}",
                    "atype": atype,
                },
            )
        await session.commit()

    async with AsyncSessionLocal() as session:
        good = await items_svc.create_for_api(
            session,
            alpha["company_id"],
            sku=f"H-{suffix}-GOOD",
            name=f"H-good-item-{suffix}",
            inventory_account_id=inv_acct_id,
            cogs_account_id=cogs_acct_id,
            income_account_id=inc_acct_id,
            tenant_id=alpha["tenant_id"],
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "items")
        await session.execute(
            text(
                "INSERT INTO items "
                "(id, company_id, tenant_id, sku, name, item_type, cost_method, "
                "on_hand_qty, wac_cost, default_sale_price, "
                "inventory_account_id, cogs_account_id, income_account_id, version) "
                "VALUES (:id, :cid, :tid, :sku, :name, :itype, :cm, "
                "0, 0, 0, :inv, :cogs, :inc, 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "sku": f"H-{suffix}-CORRUPT",
                "name": f"H-corrupt-item-{suffix}",
                "itype": "inventory",
                "cm": "WAC",
                "inv": inv_acct_id,
                "cogs": cogs_acct_id,
                "inc": inc_acct_id,
            },
        )
        await _enable_trigger(session, "items")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await items_svc.list_items(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt item must NOT leak to alpha"

    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM items WHERE id = :id"), {"id": corrupt_id})
        await session.commit()


# ---------------------------------------------------------------------------
# tax_codes
# ---------------------------------------------------------------------------


async def test_tax_codes_list_active_excludes_corrupt_row(two_tenants: dict) -> None:
    """A corrupt tax_code (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        good = await tax_codes_svc.create_for_api(
            session,
            alpha["company_id"],
            code=f"H{suffix}G",
            name=f"H-good-tc-{suffix}",
            rate=Decimal("10.000"),
            tenant_id=alpha["tenant_id"],
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "tax_codes")
        await session.execute(
            text(
                "INSERT INTO tax_codes "
                "(id, company_id, tenant_id, code, name, rate, tax_system, "
                "reporting_type, version) "
                "VALUES (:id, :cid, :tid, :code, :name, :rate, 'GST', 'taxable', 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "code": f"H{suffix}C",
                "name": f"H-corrupt-tc-{suffix}",
                "rate": "10.000",
            },
        )
        await _enable_trigger(session, "tax_codes")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await tax_codes_svc.list_active(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt tax_code must NOT leak to alpha"

    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM tax_codes WHERE id = :id"), {"id": corrupt_id})
        await session.commit()


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


async def test_projects_list_active_excludes_corrupt_row(two_tenants: dict) -> None:
    """Corrupt project (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        good = await projects_svc.api_create(
            session,
            alpha["company_id"],
            alpha["tenant_id"],
            "test:H-projects",
            code=f"HP-{suffix}-G",
            name=f"H-good-proj-{suffix}",
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "projects")
        await session.execute(
            text(
                "INSERT INTO projects "
                "(id, company_id, tenant_id, code, name, status, version) "
                "VALUES (:id, :cid, :tid, :code, :name, 'ACTIVE', 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "code": f"HP-{suffix}-C",
                "name": f"H-corrupt-proj-{suffix}",
            },
        )
        await _enable_trigger(session, "projects")
        await session.commit()

    # list_active (Jinja tier)
    async with AsyncSessionLocal() as session:
        rows = await projects_svc.list_active(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt project must NOT leak in list_active"

    # list_projects (API tier) — had tenant_id param but didn't use it
    async with AsyncSessionLocal() as session:
        rows_api, _total = await projects_svc.list_projects(
            session, alpha["company_id"], alpha["tenant_id"]
        )
    api_ids = {r.id for r in rows_api}
    assert good_id in api_ids
    assert corrupt_id not in api_ids, "Corrupt project must NOT leak in list_projects"

    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": corrupt_id})
        await session.commit()


# ---------------------------------------------------------------------------
# journal_templates
# ---------------------------------------------------------------------------


async def test_journal_templates_list_active_excludes_corrupt_row(two_tenants: dict) -> None:
    """Corrupt journal_template (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        good = await jt_svc.create(
            session,
            alpha["company_id"],
            name=f"H-good-tmpl-{suffix}",
            lines=[],
            tenant_id=alpha["tenant_id"],
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "journal_templates")
        await session.execute(
            text(
                "INSERT INTO journal_templates "
                "(id, company_id, tenant_id, name, lines) "
                "VALUES (:id, :cid, :tid, :name, '[]'::jsonb)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "name": f"H-corrupt-tmpl-{suffix}",
            },
        )
        await _enable_trigger(session, "journal_templates")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await jt_svc.list_active(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt journal_template must NOT leak to alpha"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM journal_templates WHERE id = :id"), {"id": corrupt_id}
        )
        await session.commit()


# ---------------------------------------------------------------------------
# departments
# ---------------------------------------------------------------------------


async def test_departments_list_excludes_corrupt_row(two_tenants: dict) -> None:
    """Corrupt department (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        from saebooks.services.departments import create_department
        good = await create_department(
            session,
            alpha["company_id"],
            alpha["tenant_id"],
            code=f"HD{suffix}G",
            name=f"H-good-dept-{suffix}",
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "departments")
        await session.execute(
            text(
                "INSERT INTO departments "
                "(id, company_id, tenant_id, code, name, version) "
                "VALUES (:id, :cid, :tid, :code, :name, 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "code": f"HD{suffix}C",
                "name": f"H-corrupt-dept-{suffix}",
            },
        )
        await _enable_trigger(session, "departments")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await list_departments(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt department must NOT leak to alpha"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM departments WHERE id = :id"), {"id": corrupt_id}
        )
        await session.commit()


# ---------------------------------------------------------------------------
# cost_centres
# ---------------------------------------------------------------------------


async def test_cost_centres_list_excludes_corrupt_row(two_tenants: dict) -> None:
    """Corrupt cost_centre (company=alpha, tenant=beta) must not appear in alpha list."""
    alpha = two_tenants["alpha"]
    beta = two_tenants["beta"]
    suffix = uuid.uuid4().hex[:4]
    corrupt_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        from saebooks.services.departments import create_cost_centre
        good = await create_cost_centre(
            session,
            alpha["company_id"],
            alpha["tenant_id"],
            code=f"HC{suffix}G",
            name=f"H-good-cc-{suffix}",
        )
        good_id = good.id

    async with AsyncSessionLocal() as session:
        await _disable_trigger(session, "cost_centres")
        await session.execute(
            text(
                "INSERT INTO cost_centres "
                "(id, company_id, tenant_id, code, name, version) "
                "VALUES (:id, :cid, :tid, :code, :name, 1)"
            ),
            {
                "id": corrupt_id,
                "cid": alpha["company_id"],
                "tid": beta["tenant_id"],
                "code": f"HC{suffix}C",
                "name": f"H-corrupt-cc-{suffix}",
            },
        )
        await _enable_trigger(session, "cost_centres")
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await list_cost_centres(
            session, alpha["company_id"], tenant_id=alpha["tenant_id"]
        )
    ids = {r.id for r in rows}
    assert good_id in ids
    assert corrupt_id not in ids, "Corrupt cost_centre must NOT leak to alpha"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM cost_centres WHERE id = :id"), {"id": corrupt_id}
        )
        await session.commit()
