"""inventory_cost_layers — RLS coverage for the FIFO cost-layer table (Wave D).

The FIFO costing method (Richard's decision 2) introduces a NEW
tenant-scoped table, ``inventory_cost_layers`` (migration 0186). This
file runs the non-negotiable new-table RLS checklist against it,
mirroring tests/test_dutiable_events.py:

Structural (owner engine):
  * RLS ENABLE + FORCE on the table.
  * A ``tenant_isolation`` policy with the standard tenant predicate.

Cross-tenant probe (NOBYPASSRLS ``saebooks_app`` role):
  * Tenant A can read its own layer.
  * Tenant A CANNOT read tenant B's layer.
  * With no ``app.current_tenant`` set, zero rows (deny by default).
  * The 0131 tenant-coherence trigger rejects a layer whose company_id
    belongs to another tenant.
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
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.db import engine as _owner_engine
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.inventory_cost_layer import InventoryCostLayer
from saebooks.models.item import Item, ItemType
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only

_TABLE = "inventory_cost_layers"
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


# --------------------------------------------------------------------------- #
# Structural RLS assertions
# --------------------------------------------------------------------------- #
async def test_cost_layers_has_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :n"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, (
        "inventory_cost_layers absent from pg_class — migration 0186 missing"
    )
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "RLS not ENABLE+FORCE on inventory_cost_layers — migration 0186 incomplete"
    )


async def test_cost_layers_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT qual FROM pg_policies "
                    "WHERE tablename = :n AND policyname = 'tenant_isolation'"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, "inventory_cost_layers missing tenant_isolation policy"
    assert "tenant_id" in row.qual and "current_setting" in row.qual, (
        f"inventory_cost_layers policy is not the standard tenant predicate: {row.qual!r}"
    )


# --------------------------------------------------------------------------- #
# Cross-tenant probe via the NOBYPASSRLS saebooks_app role
# --------------------------------------------------------------------------- #
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
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a company + one item + one cost layer."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            inv_id = uuid.uuid4()
            cogs_id = uuid.uuid4()
            income_id = uuid.uuid4()
            item_id = uuid.uuid4()
            layer_id = uuid.uuid4()
            session.add(
                Tenant(id=tid, name=f"WDL-{label}-{suffix}", slug=f"wdl-{label}-{suffix}")
            )
            await session.flush()
            session.add(
                Company(id=cid, tenant_id=tid, name=f"WDL-{label}-{suffix}",
                        base_currency="AUD")
            )
            await session.flush()
            session.add_all([
                Account(id=inv_id, company_id=cid, tenant_id=tid,
                        code=f"1-13{suffix[:2]}", name="Trading Stock",
                        account_type=AccountType.ASSET),
                Account(id=cogs_id, company_id=cid, tenant_id=tid,
                        code=f"5-20{suffix[:2]}", name="COGS",
                        account_type=AccountType.EXPENSE),
                Account(id=income_id, company_id=cid, tenant_id=tid,
                        code=f"4-20{suffix[:2]}", name="Sales",
                        account_type=AccountType.INCOME),
            ])
            await session.flush()
            session.add(
                Item(id=item_id, company_id=cid, tenant_id=tid,
                     sku=f"WDL-{label}-{suffix}", name="WDL item",
                     item_type=ItemType.INVENTORY,
                     inventory_account_id=inv_id, cogs_account_id=cogs_id,
                     income_account_id=income_id)
            )
            await session.flush()
            session.add(
                InventoryCostLayer(
                    id=layer_id, tenant_id=tid, company_id=cid, item_id=item_id,
                    received_date=date(2026, 7, 10),
                    original_qty=Decimal("10.0000"),
                    remaining_qty=Decimal("10.0000"),
                    unit_cost=Decimal("5.0000"),
                )
            )
            await session.flush()
            out[label] = {
                "tenant_id": tid, "company_id": cid, "item_id": item_id,
                "layer_id": layer_id, "inv_id": inv_id,
            }
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(
                text(f"DELETE FROM {_TABLE} WHERE id = :i"), {"i": row["layer_id"]}
            )
            await session.execute(
                text("DELETE FROM items WHERE id = :i"), {"i": row["item_id"]}
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :i"),
                {"i": row["company_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :i"), {"i": row["company_id"]}
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :i"), {"i": row["tenant_id"]}
            )
        await session.commit()


async def test_layer_visible_to_own_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_two_tenants["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text(f"SELECT id FROM {_TABLE} WHERE id = :i"), {"i": a["layer_id"]}
            )
        ).all()
    assert len(visible) == 1, "tenant A cannot see its own cost layer — RLS too tight"


async def test_layer_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_layer = seeded_two_tenants["tenant_b"]["layer_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text(f"SELECT id FROM {_TABLE} WHERE id = :i"), {"i": b_layer}
            )
        ).all()
    assert len(visible) == 0, (
        f"tenant A leaked tenant B's cost layer {b_layer} — tenant_isolation broken"
    )


async def test_layer_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (await session.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    assert rows == 0, f"expected 0 cost layers with no tenant set, got {rows}"


async def test_coherence_trigger_rejects_foreign_company(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    a = seeded_two_tenants["tenant_a"]
    b = seeded_two_tenants["tenant_b"]
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)").bindparams(
                tid=str(a["tenant_id"])
            )
        )
        # Use tenant_b's REAL item/company so the composite (item_id,
        # company_id) FK is satisfied and the tenant-coherence trigger is
        # the sole possible failure — an FK violation on a nonexistent item
        # would otherwise race with the trigger and make this assertion
        # nondeterministic. tenant_id=A disagrees with company B's tenant.
        with pytest.raises(DBAPIError, match="row-level security|tenant_coherence"):
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(tenant_id, company_id, item_id, received_date, "
                    " original_qty, remaining_qty, unit_cost) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), "
                    "        CAST(:item AS uuid), '2026-07-10', 1, 1, 1)"
                ).bindparams(
                    tid=str(a["tenant_id"]),
                    cid=str(b["company_id"]),
                    item=str(b["item_id"]),
                )
            )
