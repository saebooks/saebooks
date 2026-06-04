"""Tests for ``saebooks.services.tenant``.

Covers the row-level company-scope guard:

* Listener is a no-op when the contextvar is unset (the default —
  existing single-company call sites keep working).
* With ``scope(company_a)``, queries against CompanyScoped entities
  return only company_a's rows.
* With ``scope(company_b)``, same queries see only company_b's rows.
* ``bypass_tenant_scope()`` disables the filter even when a scope
  is set (for admin / cross-tenant reports).
* Non-CompanyScoped entities (the ``Company`` row itself) are NEVER
  filtered — otherwise we'd be unable to list companies at all.
* A scope bound to a UUID for which nothing exists returns zero rows,
  never leaks across tenants.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services.tenant import (
    bypass_tenant_scope,
    current_company_id,
    scope,
)

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def two_companies() -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Create two scratch companies + one contact each, yield ids.

    Cleanup deletes the contacts + companies on teardown so the
    persistent dev DB stays clean across runs. Uses unique
    UUID-tagged names so re-runs don't collide.
    """
    tag = uuid.uuid4().hex[:8]
    name_a = f"Scope-A-{tag}"
    name_b = f"Scope-B-{tag}"
    contact_a_name = f"Customer-A-{tag}"
    contact_b_name = f"Customer-B-{tag}"

    _DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")

    async with AsyncSessionLocal() as session:
        company_a = Company(name=name_a, base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        company_b = Company(name=name_b, base_currency="AUD", tenant_id=_DEFAULT_TENANT)
        session.add_all([company_a, company_b])
        await session.flush()
        contact_a = Contact(
            company_id=company_a.id,
            tenant_id=_DEFAULT_TENANT,
            name=contact_a_name,
            contact_type=ContactType.CUSTOMER,
        )
        contact_b = Contact(
            company_id=company_b.id,
            tenant_id=_DEFAULT_TENANT,
            name=contact_b_name,
            contact_type=ContactType.CUSTOMER,
        )
        session.add_all([contact_a, contact_b])
        await session.commit()
        cid_a, cid_b = company_a.id, company_b.id

    yield cid_a, cid_b

    # bypass so the teardown can see both companies regardless of
    # whatever scope the last test left bound. Can't combine async-with
    # + sync-with on one line, so nest them explicitly.
    with bypass_tenant_scope():
        async with AsyncSessionLocal() as session:
            for cid in (cid_a, cid_b):
                contacts = (
                    await session.execute(
                        select(Contact).where(Contact.company_id == cid)
                    )
                ).scalars().all()
                for c in contacts:
                    await session.delete(c)
                company = await session.get(Company, cid)
                if company is not None:
                    await session.delete(company)
            await session.commit()


async def test_no_scope_sees_all_contacts(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Unset scope = no filter. Both companies' contacts are visible."""
    cid_a, cid_b = two_companies
    assert current_company_id() is None
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Contact).where(Contact.company_id.in_([cid_a, cid_b]))
            )
        ).scalars().all()
    names = {r.company_id for r in rows}
    assert cid_a in names
    assert cid_b in names


async def test_scope_a_hides_company_b(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """With scope bound to A, a SELECT(Contact) returns only A's rows."""
    cid_a, cid_b = two_companies
    with scope(cid_a):
        assert current_company_id() == cid_a
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(Contact))).scalars().all()
        companies_seen = {r.company_id for r in rows}
    assert cid_a in companies_seen
    assert cid_b not in companies_seen


async def test_scope_b_hides_company_a(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Symmetric — scope on B hides A."""
    cid_a, cid_b = two_companies
    with scope(cid_b):
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(Contact))).scalars().all()
        companies_seen = {r.company_id for r in rows}
    assert cid_b in companies_seen
    assert cid_a not in companies_seen


async def test_bypass_overrides_scope(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``bypass_tenant_scope`` lets cross-tenant admin queries through."""
    cid_a, cid_b = two_companies
    with scope(cid_a), bypass_tenant_scope():
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Contact).where(
                        Contact.company_id.in_([cid_a, cid_b])
                    )
                )
            ).scalars().all()
    companies_seen = {r.company_id for r in rows}
    assert cid_a in companies_seen
    assert cid_b in companies_seen


async def test_company_table_is_never_filtered(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``Company`` is NOT CompanyScoped — filter must not touch it.

    If we ever scoped the Company row, selecting companies while inside
    any scope would return zero rows and the app would 500 everywhere.
    """
    cid_a, cid_b = two_companies
    with scope(cid_a):
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Company).where(Company.id.in_([cid_a, cid_b]))
                )
            ).scalars().all()
    ids = {r.id for r in rows}
    assert cid_a in ids
    assert cid_b in ids  # must still be visible from inside scope(A)


async def test_unknown_company_scope_returns_empty(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A scope bound to a random UUID returns zero — never falls back."""
    random_cid = uuid.uuid4()
    with scope(random_cid):
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(Contact))).scalars().all()
    assert rows == []


async def test_scope_contextvar_resets_on_exit(
    two_companies: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``with scope(...)`` must reset the contextvar on __exit__."""
    cid_a, _ = two_companies
    assert current_company_id() is None
    with scope(cid_a):
        assert current_company_id() == cid_a
    assert current_company_id() is None
