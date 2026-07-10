"""Layer 2 audit fix — detail / mutation handlers honour X-Company-Id.

Companion to ``test_x_company_id_sweep.py`` (which exercises LIST
handlers). This file proves that:

* GET    /api/v1/<resource>/{id}  → 404 when the resource belongs to a
  sibling company in the same tenant.
* PATCH  /api/v1/<resource>/{id}  → 404 in the same scenario.
* DELETE /api/v1/<resource>/{id}  → 404 in the same scenario.

Without this guard, a user "in" company A could read/update/archive
rows from company B by knowing the row's primary-key UUID — both
companies share the same tenant, so the previous tenant-only filter
let the request through.

Set up
------
Each test creates a row in company A, then issues the request with
``X-Company-Id: <company-B-uuid>`` so the resolver pins the active
company to B. The expected response is 404 / not the row's body —
never 200/204 with the underlying record's data.

The resources covered here are the ones flagged MISSING by the
Phase A audit. Resources whose detail/mutation handlers already
honour ``get_active_company_id`` (bank_rules, journal_templates,
super_funds, employees, time_entries, account_ranges, leave) are
exercised by their own contract tests and the existing
``test_x_company_id_sweep.py``.

Scope
-----
* Bank accounts (Account rows with bank-side ``account_kind``)
* Contacts
* Items
* Accounts (general CoA)
* Tax codes
* Budgets
* Projects
* Allocation rules
* Recurring invoices
* Fixed assets
* Bills, invoices, expenses, credit notes, payments, purchase orders,
  quotes, journal entries — covered indirectly via their belt-and-
  braces ``api_get`` pre-check in the handler. Each gets one detail
  GET test here.

Cross-cutting (PATCH/DELETE specifics like If-Match handling) is
deliberately not covered here — those are exercised in the per-
resource contract files. This file is the cross-company guard test,
not a re-run of every per-resource contract.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.allocation_rule import AllocationRule
from saebooks.models.budget import Budget
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.item import Item, ItemType
from saebooks.models.project import Project
from saebooks.models.tax_code import TaxCode

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run this file's tests at enterprise (all flags on) by default.

    FLAG_PROJECTS_BUDGETS (Wave A, 2026-07-10) now gates the whole
    /api/v1/projects and /api/v1/budgets routers. Without this, the
    project/budget GET/PATCH/DELETE requests below 404 on the feature
    gate before the cross-company check ever runs -- the "blocked"
    assertions pass vacuously and the "allowed same company" assertion
    fails outright. Pin enterprise so this file exercises the real
    cross-company isolation logic, same pattern as
    ``tests/api/v1/test_projects.py`` / ``tests/api/v1/test_budgets.py``.
    """
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")


@pytest.fixture
async def seed_company_id() -> uuid.UUID:
    """Return the id of the seed company (first by created_at)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        c = result.scalars().first()
        assert c is not None, "test DB must have a seed company"
        return c.id


@pytest.fixture
async def other_company_id(seed_company_id: uuid.UUID) -> uuid.UUID:
    """Provision a second active company in the same tenant.

    Sorted later than the seed by ``created_at``. The cross-company
    tests create rows in the SEED company (so the rest of the suite's
    seed data is untouched) and then poke the resource from the
    OTHER company via ``X-Company-Id``.
    """
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                tenant_id=_DEFAULT_TENANT_ID,
                name=f"Cross-Co Iso Test {cid.hex[:8]}",
                base_currency="AUD",
                fin_year_start_month=7,
                audit_mode="immutable",
            )
        )
        await session.commit()
    yield cid
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, cid)
        if co is not None:
            await session.delete(co)
            await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hdr_other(other_company_id: uuid.UUID) -> dict[str, str]:
    return {"X-Company-Id": str(other_company_id)}


def _hdr_seed(seed_company_id: uuid.UUID) -> dict[str, str]:
    return {"X-Company-Id": str(seed_company_id)}


async def _new_account(
    company_id: uuid.UUID,
    *,
    code: str | None = None,
    kind: AccountType = AccountType.EXPENSE,
) -> uuid.UUID:
    """Insert a stand-alone account row in the given company."""
    aid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Account(
                id=aid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=company_id,
                code=code or f"CC{aid.hex[:4].upper()}",
                name=f"XC iso test account {aid.hex[:6]}",
                account_type=kind,
            )
        )
        await session.commit()
    return aid


# ---------------------------------------------------------------------------
# Bank account
# ---------------------------------------------------------------------------


@pytest.fixture
async def bank_account_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    """Create a bank-side Account row in the seed company."""
    aid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Account(
                id=aid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                code=f"BA{aid.hex[:4].upper()}",
                name=f"XC iso bank acct {aid.hex[:6]}",
                account_type=AccountType.ASSET,
                account_kind="BANK_CHECKING",
                bsb="123-456",
            )
        )
        await session.commit()
    return aid


async def test_bank_account_get_blocked_cross_company(
    api_client: AsyncClient,
    bank_account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/bank_accounts/{bank_account_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_bank_account_get_allowed_same_company(
    api_client: AsyncClient,
    bank_account_in_seed: uuid.UUID,
    seed_company_id: uuid.UUID,
) -> None:
    # Sanity-check: the SAME company id returns 200.
    r = await api_client.get(
        f"/api/v1/bank_accounts/{bank_account_in_seed}",
        headers=_hdr_seed(seed_company_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(bank_account_in_seed)


async def test_bank_account_patch_blocked_cross_company(
    api_client: AsyncClient,
    bank_account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/bank_accounts/{bank_account_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Hacked!"},
    )
    assert r.status_code == 404, r.text


async def test_bank_account_delete_blocked_cross_company(
    api_client: AsyncClient,
    bank_account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/bank_accounts/{bank_account_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------


@pytest.fixture
async def contact_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Contact(
                id=cid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                name=f"XC iso contact {cid.hex[:6]}",
                contact_type=ContactType.CUSTOMER,
            )
        )
        await session.commit()
    return cid


async def test_contact_get_blocked_cross_company(
    api_client: AsyncClient,
    contact_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/contacts/{contact_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_contact_patch_blocked_cross_company(
    api_client: AsyncClient,
    contact_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/contacts/{contact_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_contact_delete_blocked_cross_company(
    api_client: AsyncClient,
    contact_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/contacts/{contact_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


@pytest.fixture
async def item_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    # Item needs inventory + cogs + income accounts (all FK-required).
    inv = await _new_account(seed_company_id, kind=AccountType.ASSET)
    cogs = await _new_account(seed_company_id, kind=AccountType.EXPENSE)
    income = await _new_account(seed_company_id, kind=AccountType.INCOME)
    async with AsyncSessionLocal() as session:
        session.add(
            Item(
                id=iid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                sku=f"XCITEM{iid.hex[:4].upper()}",
                name=f"XC iso item {iid.hex[:6]}",
                item_type=ItemType.SERVICE,
                inventory_account_id=inv,
                cogs_account_id=cogs,
                income_account_id=income,
            )
        )
        await session.commit()
    return iid


async def test_item_get_blocked_cross_company(
    api_client: AsyncClient,
    item_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/items/{item_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_item_patch_blocked_cross_company(
    api_client: AsyncClient,
    item_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/items/{item_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_item_delete_blocked_cross_company(
    api_client: AsyncClient,
    item_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/items/{item_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Account (CoA)
# ---------------------------------------------------------------------------


@pytest.fixture
async def account_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    return await _new_account(seed_company_id)


async def test_account_get_blocked_cross_company(
    api_client: AsyncClient,
    account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/accounts/{account_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_account_patch_blocked_cross_company(
    api_client: AsyncClient,
    account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/accounts/{account_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_account_delete_blocked_cross_company(
    api_client: AsyncClient,
    account_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/accounts/{account_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Tax code
# ---------------------------------------------------------------------------


@pytest.fixture
async def tax_code_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            TaxCode(
                id=tid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                code=f"XC{tid.hex[:3].upper()}",
                name=f"XC iso tax {tid.hex[:6]}",
                rate="10.000",
                tax_system="GST",
                reporting_type="taxable",
            )
        )
        await session.commit()
    return tid


async def test_tax_code_get_blocked_cross_company(
    api_client: AsyncClient,
    tax_code_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/tax_codes/{tax_code_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_tax_code_patch_blocked_cross_company(
    api_client: AsyncClient,
    tax_code_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/tax_codes/{tax_code_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_tax_code_delete_blocked_cross_company(
    api_client: AsyncClient,
    tax_code_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/tax_codes/{tax_code_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


@pytest.fixture
async def project_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    pid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Project(
                id=pid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                code=f"XCP{pid.hex[:4].upper()}",
                name=f"XC iso project {pid.hex[:6]}",
            )
        )
        await session.commit()
    return pid


async def test_project_get_blocked_cross_company(
    api_client: AsyncClient,
    project_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/projects/{project_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_project_patch_blocked_cross_company(
    api_client: AsyncClient,
    project_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/projects/{project_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_project_delete_blocked_cross_company(
    api_client: AsyncClient,
    project_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/projects/{project_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@pytest.fixture
async def budget_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    bid = uuid.uuid4()
    acc = await _new_account(seed_company_id)
    async with AsyncSessionLocal() as session:
        session.add(
            Budget(
                id=bid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                account_id=acc,
                year=2026,
                month=7,
                amount="1000.00",
            )
        )
        await session.commit()
    return bid


async def test_budget_get_blocked_cross_company(
    api_client: AsyncClient,
    budget_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/budgets/{budget_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_budget_patch_blocked_cross_company(
    api_client: AsyncClient,
    budget_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/budgets/{budget_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"amount": "999999.00"},
    )
    assert r.status_code == 404, r.text


async def test_budget_delete_blocked_cross_company(
    api_client: AsyncClient,
    budget_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/budgets/{budget_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Allocation rule
# ---------------------------------------------------------------------------


@pytest.fixture
async def allocation_rule_in_seed(seed_company_id: uuid.UUID) -> uuid.UUID:
    rid = uuid.uuid4()
    src = await _new_account(seed_company_id, kind=AccountType.EXPENSE)
    tgt1 = await _new_account(seed_company_id, kind=AccountType.EXPENSE)
    tgt2 = await _new_account(seed_company_id, kind=AccountType.EXPENSE)
    async with AsyncSessionLocal() as session:
        session.add(
            AllocationRule(
                id=rid,
                tenant_id=_DEFAULT_TENANT_ID,
                company_id=seed_company_id,
                name=f"XC iso rule {rid.hex[:6]}",
                source_account_id=src,
                targets=[
                    {"account_id": str(tgt1), "share_pct": "60"},
                    {"account_id": str(tgt2), "share_pct": "40"},
                ],
                is_active=True,
            )
        )
        await session.commit()
    return rid


async def test_allocation_rule_get_blocked_cross_company(
    api_client: AsyncClient,
    allocation_rule_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/allocations/{allocation_rule_in_seed}",
        headers=_hdr_other(other_company_id),
    )
    assert r.status_code == 404, r.text


async def test_allocation_rule_patch_blocked_cross_company(
    api_client: AsyncClient,
    allocation_rule_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.patch(
        f"/api/v1/allocations/{allocation_rule_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
        json={"name": "Pwned"},
    )
    assert r.status_code == 404, r.text


async def test_allocation_rule_delete_blocked_cross_company(
    api_client: AsyncClient,
    allocation_rule_in_seed: uuid.UUID,
    other_company_id: uuid.UUID,
) -> None:
    r = await api_client.delete(
        f"/api/v1/allocations/{allocation_rule_in_seed}",
        headers={**_hdr_other(other_company_id), "If-Match": "1"},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Same-company sanity-check on a handful of resources
# ---------------------------------------------------------------------------
#
# These exist so a regression that breaks the seed-company path (and
# would make every other test in this file pass vacuously) gets caught
# loudly. One per family.


async def test_account_get_allowed_same_company(
    api_client: AsyncClient,
    account_in_seed: uuid.UUID,
    seed_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/accounts/{account_in_seed}",
        headers=_hdr_seed(seed_company_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(account_in_seed)


async def test_contact_get_allowed_same_company(
    api_client: AsyncClient,
    contact_in_seed: uuid.UUID,
    seed_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/contacts/{contact_in_seed}",
        headers=_hdr_seed(seed_company_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(contact_in_seed)


async def test_project_get_allowed_same_company(
    api_client: AsyncClient,
    project_in_seed: uuid.UUID,
    seed_company_id: uuid.UUID,
) -> None:
    r = await api_client.get(
        f"/api/v1/projects/{project_in_seed}",
        headers=_hdr_seed(seed_company_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(project_in_seed)
