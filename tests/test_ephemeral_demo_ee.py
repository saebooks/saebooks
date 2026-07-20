"""Ephemeral demo — EE (``DEMO_SEED_FLAVOUR=ee``) flavour coverage.

Complements ``test_ephemeral_demo_tenants.py`` (the AU/default + cashbook
guarantees, which must stay green) with the Estonian flavour:

(a) provision() twice under the ee flavour → two isolated tenants, each with
    the EE chart (accounts 1200/2100 carry their Estonian names), EE käibemaks
    tax codes, the seeded posted invoices, and a registrikood identifier — plus
    the template-carried kmv (ee_vat), proving the FK-graph-generic clone spans
    ``business_identifiers``.
(b) cross-tenant: A's contact list excludes B's rows and a forced-ID GET of B's
    contact returns 404 (RLS row-invisibility, not 403).
(c) reap A, assert B intact.

Postgres-only — same rationale as the sibling file (RLS + cascade FKs + the
pure-SQL clone).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.config import settings
from saebooks.db import LoginSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.services import ephemeral_demo


def _is_postgres() -> bool:
    return _owner_engine.url.get_backend_name().startswith("postgres")


pytestmark = pytest.mark.skipif(
    not _is_postgres(),
    reason="ephemeral demo tenants rely on Postgres RLS + cascade FKs.",
)


@pytest.fixture(autouse=True)
def _ee_flavour() -> Iterator[None]:
    """Run each test under the EE flavour with demos enabled, then restore."""
    prev_enabled = settings.demo_ephemeral_enabled
    prev_flavour = settings.demo_seed_flavour
    settings.demo_ephemeral_enabled = True
    settings.demo_seed_flavour = "ee"
    ephemeral_demo._reset_rate_limiter()
    yield
    settings.demo_ephemeral_enabled = prev_enabled
    settings.demo_seed_flavour = prev_flavour
    ephemeral_demo._reset_rate_limiter()


@pytest_asyncio.fixture
async def cleanup_demos() -> AsyncIterator[list[uuid.UUID]]:
    """Track provisioned demo company ids and hard-delete any survivors.

    Mirrors the sibling file: provisioned companies are removed, orphaned
    ``demo-*`` tenants dropped. The reaper-exempt EE template company is left
    in place (its tenant is not orphaned), exactly like the AU template.
    """
    ids: list[uuid.UUID] = []
    yield ids
    for cid in ids:
        async with LoginSessionLocal() as session:
            try:
                await session.execute(text("SET LOCAL app.db_rebuild = 'on'"))
                await session.execute(
                    text("DELETE FROM companies WHERE id = :cid").bindparams(cid=cid)
                )
                await session.commit()
            except Exception:
                await session.rollback()
    async with LoginSessionLocal() as session:
        try:
            await session.execute(
                text(
                    "DELETE FROM tenants WHERE slug LIKE 'demo-%' "
                    "AND slug NOT LIKE 'demo-template-%' "
                    "AND id NOT IN (SELECT tenant_id FROM companies)"
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()


# --------------------------------------------------------------------------- #
# (a) Two EE provisions — Estonian chart, tax codes, dataset, identifiers.     #
# --------------------------------------------------------------------------- #


async def _count(sql: str, **params: object) -> int:
    async with LoginSessionLocal() as session:
        return int(
            (await session.execute(text(sql).bindparams(**params))).scalar_one()
        )


async def test_ee_provision_twice_seeds_estonian_chart_and_dataset(
    cleanup_demos: list[uuid.UUID],
) -> None:
    a = await ephemeral_demo.provision(source_ip="198.51.100.10")
    b = await ephemeral_demo.provision(source_ip="198.51.100.11")
    cleanup_demos.extend([a.company_id, b.company_id])

    assert a.tenant_id != b.tenant_id, "two visits must be two tenants"

    for r in (a, b):
        # EE chart: AR at 1200 / AP at 2100 with their Estonian names, under
        # the visit's OWN tenant_id (the RLS-isolation stamp).
        async with LoginSessionLocal() as session:
            ar = (
                await session.execute(
                    text(
                        "SELECT name FROM accounts WHERE company_id = :cid "
                        "AND code = '1200' AND tenant_id = :tid"
                    ).bindparams(cid=r.company_id, tid=r.tenant_id)
                )
            ).scalar_one_or_none()
            ap = (
                await session.execute(
                    text(
                        "SELECT name FROM accounts WHERE company_id = :cid "
                        "AND code = '2100' AND tenant_id = :tid"
                    ).bindparams(cid=r.company_id, tid=r.tenant_id)
                )
            ).scalar_one_or_none()
        assert ar is not None and "Ostjatega" in ar, f"AR 1200 EE name missing: {ar!r}"
        assert ap is not None and "Hankijatega" in ap, f"AP 2100 EE name missing: {ap!r}"

        # EE käibemaks tax codes present under the visit's tenant (STD @ 24%).
        std_rate = (
            await _count(
                "SELECT count(*) FROM tax_codes WHERE company_id = :cid "
                "AND tenant_id = :tid AND code = 'STD' AND rate = 24 "
                "AND jurisdiction = 'EE'",
                cid=r.company_id,
                tid=r.tenant_id,
            )
        )
        assert std_rate == 1, "EE STD 24% tax code not seeded under the tenant"

        # Seeded, POSTED sales invoices exist for the visit's company.
        n_inv = await _count(
            "SELECT count(*) FROM invoices WHERE company_id = :cid",
            cid=r.company_id,
        )
        assert n_inv >= 2, f"expected >=2 seeded invoices, got {n_inv}"

        # registrikood (ee_regcode, written per-visit) present.
        rk = await _count(
            "SELECT count(*) FROM business_identifiers WHERE company_id = :cid "
            "AND scheme = 'ee_regcode'",
            cid=r.company_id,
        )
        assert rk == 1, "per-visit registrikood (ee_regcode) identifier missing"

        # kmv (ee_vat) present — copied by the clone from the template, proving
        # the FK-graph-generic clone spans business_identifiers.
        kmv = await _count(
            "SELECT count(*) FROM business_identifiers WHERE company_id = :cid "
            "AND scheme = 'ee_vat'",
            cid=r.company_id,
        )
        assert kmv == 1, "template kmv (ee_vat) not cloned onto the visit"

    # Both visits carry the SAME fake registrikood value — legal because each
    # is its own tenant (per-tenant value-uniqueness never clashes).
    async with LoginSessionLocal() as session:
        vals = (
            await session.execute(
                text(
                    "SELECT value FROM business_identifiers "
                    "WHERE company_id IN (:a, :b) AND scheme = 'ee_regcode'"
                ).bindparams(a=a.company_id, b=b.company_id)
            )
        ).scalars().all()
    assert set(vals) == {"10000000"}, f"unexpected registrikood values: {vals}"

    # The minted JWT authenticates and reports the EE tenant.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        me = await ac.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {a.access_token}"},
        )
    assert me.status_code == 200, me.text
    assert me.json()["tenant_id"] == str(a.tenant_id)


# --------------------------------------------------------------------------- #
# (b) Cross-tenant: A cannot see B's contacts; forced-ID GET is a 404.         #
# --------------------------------------------------------------------------- #


async def test_ee_provision_seeds_tax_periods(
    cleanup_demos: list[uuid.UUID],
) -> None:
    """The EE flavour seeds monthly EST tax periods so the Deklaratsioonid
    generate flow (KMD/TSD) is clickable in a fresh demo."""
    a = await ephemeral_demo.provision(source_ip="198.51.100.40")
    cleanup_demos.append(a.company_id)
    n = await _count(
        "SELECT count(*) FROM tax_periods WHERE company_id = :cid "
        "AND jurisdiction = 'EST'",
        cid=a.company_id,
    )
    assert n >= 2, f"expected >=2 seeded EST tax periods, found {n}"


async def test_ee_cross_tenant_contact_isolation_forced_id_404(
    cleanup_demos: list[uuid.UUID],
) -> None:
    a = await ephemeral_demo.provision(source_ip="198.51.100.20")
    b = await ephemeral_demo.provision(source_ip="198.51.100.21")
    cleanup_demos.extend([a.company_id, b.company_id])

    # B's cloned contact ids (owner session — cross-tenant visibility).
    async with LoginSessionLocal() as session:
        b_contacts = (
            await session.execute(
                text(
                    "SELECT id FROM contacts WHERE company_id = :cid "
                    "AND archived_at IS NULL"
                ).bindparams(cid=b.company_id)
            )
        ).scalars().all()
    assert b_contacts, "B has no seeded contacts to probe"
    b_contact_id = b_contacts[0]

    transport = ASGITransport(app=app)
    headers_a = {"Authorization": f"Bearer {a.access_token}"}
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        listing = await ac.get("/api/v1/contacts", headers=headers_a)
        assert listing.status_code == 200, listing.text
        listed_ids = {row["id"] for row in listing.json()["items"]}
        assert str(b_contact_id) not in listed_ids, (
            "tenant A's contact list leaked a tenant B contact"
        )

        # Forced-ID GET of B's contact as A → 404 (RLS row-invisibility).
        forced = await ac.get(
            f"/api/v1/contacts/{b_contact_id}", headers=headers_a
        )
    assert forced.status_code == 404, (
        f"forced-ID GET of B's contact returned {forced.status_code}, "
        "expected 404 (row-invisibility)"
    )


# --------------------------------------------------------------------------- #
# (c) Reap A, leave B intact.                                                  #
# --------------------------------------------------------------------------- #


async def test_ee_reap_one_leaves_other(
    cleanup_demos: list[uuid.UUID],
) -> None:
    a = await ephemeral_demo.provision(source_ip="198.51.100.30")
    b = await ephemeral_demo.provision(source_ip="198.51.100.31")
    cleanup_demos.extend([a.company_id, b.company_id])

    # Backdate A past the idle TTL so the reaper takes it.
    async with LoginSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE ephemeral_demo_tenants SET last_seen_at = :ts "
                "WHERE company_id = :cid"
            ).bindparams(
                ts=datetime.now(UTC) - timedelta(seconds=settings.demo_idle_ttl + 60),
                cid=a.company_id,
            )
        )
        await session.commit()

    reaped = await ephemeral_demo.reap_once()
    assert a.company_id in reaped, "idle EE demo A was not reaped"
    assert b.company_id not in reaped, "fresh EE demo B wrongly reaped"

    a_gone = await _count(
        "SELECT count(*) FROM companies WHERE id = :cid", cid=a.company_id
    )
    b_alive = await _count(
        "SELECT count(*) FROM companies WHERE id = :cid", cid=b.company_id
    )
    assert a_gone == 0, "reaped EE demo A still present"
    assert b_alive == 1, "EE demo B wrongly deleted by the reaper"


# --------------------------------------------------------------------------- #
# (d) Belt-and-braces: the POLICY does the isolating, not an app filter.      #
# --------------------------------------------------------------------------- #

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


async def _app_engine_url() -> str | None:
    """Set a known password on saebooks_app (owner engine) and build its URL.

    Same role-flip pattern as tests/test_rls_multijurisdiction.py.
    """
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return None
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return f"postgresql+asyncpg://saebooks_app:{_APP_ROLE_PASSWORD}@db:5432/{db_name}"


async def test_ee_demo_isolation_is_rls_policy_not_app_filter(
    cleanup_demos: list[uuid.UUID],
) -> None:
    """Raw SQL as the FORCE-RLS app role scoped to tenant A sees zero of B.

    No API layer, no service filter — a plain ``SELECT`` against ``contacts``
    with ``WHERE tenant_id = :b``. If the tenant_isolation policy were absent,
    this would return B's seeded contacts; RLS row-invisibility makes it 0.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    url = await _app_engine_url()
    if url is None:
        pytest.skip("saebooks_app role missing — migration 0056 not applied")

    a = await ephemeral_demo.provision(source_ip="198.51.100.30")
    b = await ephemeral_demo.provision(source_ip="198.51.100.31")
    cleanup_demos.extend([a.company_id, b.company_id])

    eng = create_async_engine(url, poolclass=NullPool, future=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :tid, false)")
                .bindparams(tid=str(a.tenant_id))
            )
            own = (
                await conn.execute(
                    text("SELECT count(*) FROM contacts WHERE tenant_id = :t")
                    .bindparams(t=a.tenant_id)
                )
            ).scalar_one()
            foreign = (
                await conn.execute(
                    text("SELECT count(*) FROM contacts WHERE tenant_id = :t")
                    .bindparams(t=b.tenant_id)
                )
            ).scalar_one()
    finally:
        await eng.dispose()

    assert own > 0, "A's session cannot see A's own seeded contacts"
    assert foreign == 0, (
        "app role scoped to tenant A can see tenant B's rows — "
        "the tenant_isolation policy is not doing the isolating"
    )
