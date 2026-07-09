"""Tests for the bank_routing_identifiers table + service (M1.5 · T10).

Round-trip upsert/get plus scheme/owner_type validation mirror
``tests/services/test_business_identifiers.py``. The RLS section
additionally runs a LIVE cross-tenant probe through the NOBYPASSRLS
``saebooks_app`` role (pattern from ``tests/test_rls_supplier_rules.py``)
rather than just asserting the policy exists — the migration lands in
the same commit as this probe, per the project RLS checklist.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.models.bank_routing_identifier import BankRoutingIdentifier
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
from saebooks.services import bank_routing_identifiers as bri_svc

pytestmark = pytest.mark.postgres_only

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_TABLE = "bank_routing_identifiers"


async def _seed_company() -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company missing"
        return co.tenant_id, co.id


# ---------------------------------------------------------------------------
# Round-trip + validation
# ---------------------------------------------------------------------------


async def test_upsert_and_get_round_trip() -> None:
    tenant_id, company_id = await _seed_company()
    owner_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        row = await bri_svc.upsert(
            session,
            company_id,
            "account",
            owner_id,
            "iban",
            "DE89370400440532013000",
            tenant_id=tenant_id,
            bic="COBADEFFXXX",
        )
        await session.commit()
        assert row.id is not None
        assert row.routing_scheme == "iban"

    async with AsyncSessionLocal() as session:
        fetched = await bri_svc.get(session, company_id, "account", owner_id, "iban")
        assert fetched is not None
        assert fetched.scheme_value == "DE89370400440532013000"
        assert fetched.bic == "COBADEFFXXX"


async def test_upsert_updates_existing_row() -> None:
    tenant_id, company_id = await _seed_company()
    owner_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await bri_svc.upsert(
            session, company_id, "contact", owner_id, "uk_sort_code", "12-34-56",
            tenant_id=tenant_id,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await bri_svc.upsert(
            session, company_id, "contact", owner_id, "uk_sort_code", "65-43-21",
            tenant_id=tenant_id,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BankRoutingIdentifier).where(
                    BankRoutingIdentifier.company_id == company_id,
                    BankRoutingIdentifier.owner_type == "contact",
                    BankRoutingIdentifier.owner_id == owner_id,
                    BankRoutingIdentifier.routing_scheme == "uk_sort_code",
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "upsert created a duplicate row"
        assert rows[0].scheme_value == "65-43-21"


async def test_upsert_preserves_bic_and_account_number_when_omitted() -> None:
    """A later upsert that only touches scheme_value (e.g. an IBAN typo
    fix) must not silently null out a previously-recorded bic/account_number
    just because it didn't re-pass them."""
    tenant_id, company_id = await _seed_company()
    owner_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await bri_svc.upsert(
            session, company_id, "contact", owner_id, "iban",
            "DE89370400440532013000",
            tenant_id=tenant_id, bic="COBADEFFXXX", account_number="0532013000",
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        # Correct only the scheme_value — bic/account_number intentionally
        # omitted, as a caller that doesn't carry them through would do.
        await bri_svc.upsert(
            session, company_id, "contact", owner_id, "iban",
            "DE89370400440532013001",
            tenant_id=tenant_id,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        fetched = await bri_svc.get(session, company_id, "contact", owner_id, "iban")
        assert fetched is not None
        assert fetched.scheme_value == "DE89370400440532013001"
        assert fetched.bic == "COBADEFFXXX"
        assert fetched.account_number == "0532013000"


async def test_owner_may_carry_multiple_schemes() -> None:
    """The same owner can hold a local BSB *and* an IBAN side by side —
    the point of keying on (owner, scheme) rather than just (owner)."""
    tenant_id, company_id = await _seed_company()
    owner_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await bri_svc.upsert(
            session, company_id, "employee", owner_id, "au_bsb", "062-000",
            tenant_id=tenant_id, account_number="12345678",
        )
        await bri_svc.upsert(
            session, company_id, "employee", owner_id, "iban",
            "GB29NWBK60161331926819", tenant_id=tenant_id,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = await bri_svc.list_for_owner(session, company_id, "employee", owner_id)
        assert {r.routing_scheme for r in rows} == {"au_bsb", "iban"}


async def test_unknown_scheme_rejected() -> None:
    _, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        with pytest.raises(bri_svc.UnknownScheme):
            await bri_svc.upsert(
                session, company_id, "account", uuid.uuid4(), "moon_routing", "42"
            )


async def test_unknown_owner_type_rejected() -> None:
    _, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        with pytest.raises(bri_svc.UnknownOwnerType):
            await bri_svc.upsert(
                session, company_id, "spaceship", uuid.uuid4(), "iban", "X"
            )


# ---------------------------------------------------------------------------
# RLS — catalog facts + live cross-tenant probe
# ---------------------------------------------------------------------------


async def test_rls_enabled_and_forced() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :t"
                ).bindparams(t=_TABLE)
            )
        ).one()
    assert row.relrowsecurity is True, "ROW LEVEL SECURITY not enabled"
    assert row.relforcerowsecurity is True, "FORCE ROW LEVEL SECURITY missing"


async def test_tenant_isolation_policy_predicate() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pg_get_expr(polqual, polrelid) AS qual, "
                    "pg_get_expr(polwithcheck, polrelid) AS with_check "
                    "FROM pg_policy WHERE polname = 'tenant_isolation' "
                    "AND polrelid = :t ::regclass"
                ).bindparams(t=_TABLE)
            )
        ).one_or_none()
    assert row is not None, "tenant_isolation policy missing"
    for expr in (row.qual, row.with_check):
        assert "app.current_tenant" in expr
        assert "tenant_id" in expr


def _resolve_app_url() -> str:
    url = _owner_engine.url.set(username="saebooks_app", password=_APP_ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


async def _ensure_app_role_login() -> bool:
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return False
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company and one bank-routing identifier,
    inserted through the BYPASSRLS owner engine."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"suffix": suffix}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            session.add(
                Tenant(
                    id=tid,
                    name=f"BRI178-{label}-{suffix}",
                    slug=f"bri178-{label}-{suffix}",
                )
            )
            await session.flush()
            cid = uuid.uuid4()
            session.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"BRI178-co-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            owner_id = uuid.uuid4()
            row = BankRoutingIdentifier(
                tenant_id=tid,
                company_id=cid,
                owner_type="account",
                owner_id=owner_id,
                routing_scheme="iban",
                scheme_value=f"IBAN-{label}-{suffix}",
            )
            session.add(row)
            await session.flush()
            out[label] = {
                "tenant_id": tid,
                "company_id": cid,
                "owner_id": owner_id,
                "row_id": row.id,
            }
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
            await session.execute(
                text("DELETE FROM companies WHERE tenant_id = :tid").bindparams(
                    tid=out[label]["tenant_id"]
                )
            )
        await session.execute(
            text("DELETE FROM tenants WHERE slug LIKE :pat").bindparams(
                pat=f"bri178-%-{suffix}"
            )
        )
        await session.commit()


async def test_tenant_a_sees_only_its_rows(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        ids = {row.id for row in await conn.execute(text(f"SELECT id FROM {_TABLE}"))}
    assert a["row_id"] in ids
    assert b["row_id"] not in ids


async def test_foreign_row_invisible_by_id(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(b["tenant_id"])
            )
        )
        found = (
            await conn.execute(
                text(f"SELECT id FROM {_TABLE} WHERE id = CAST(:i AS uuid)").bindparams(
                    i=str(a["row_id"])
                )
            )
        ).first()
    assert found is None, "cross-tenant row leaked through RLS by id probe"


async def test_no_guc_sees_zero_rows(app_engine: Any, seeded: dict[str, Any]) -> None:
    async with app_engine.connect() as conn:
        count = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    assert count == 0, "deny-by-default violated: rows visible with no tenant GUC"


async def test_with_check_blocks_foreign_tenant_write(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        # The foreign-tenant write is blocked — but the tenant-coherence
        # trigger fires BEFORE the RLS WITH CHECK (under tenant_a's context
        # tenant_b's company isn't visible, so the coherence lookup raises
        # first). Either guard blocking the write satisfies the security
        # property, so accept both messages.
        with pytest.raises(
            DBAPIError, match="row-level security|tenant_coherence"
        ):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, owner_type, owner_id, routing_scheme, "
                    " scheme_value) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), 'account', "
                    "        CAST(:oid AS uuid), 'iban', 'smuggled')"
                ).bindparams(
                    tid=str(b["tenant_id"]),
                    cid=str(b["company_id"]),
                    oid=str(uuid.uuid4()),
                )
            )


async def test_coherence_trigger_rejects_foreign_company(
    app_engine: Any, seeded: dict[str, Any]
) -> None:
    a = seeded["tenant_a"]
    b = seeded["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        with pytest.raises(DBAPIError, match="tenant_coherence"):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, owner_type, owner_id, routing_scheme, "
                    " scheme_value) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), 'account', "
                    "        CAST(:oid AS uuid), 'iban', 'foreign-co')"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    cid=str(b["company_id"]),
                    oid=str(uuid.uuid4()),
                )
            )
