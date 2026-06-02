"""Tests for the business_identifiers child table + service."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.business_identifier import BusinessIdentifier
from saebooks.models.company import Company
from saebooks.services import business_identifiers as bi_svc


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


async def test_upsert_and_get_round_trip() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session, company_id, "nz_nzbn", "9429000000001", tenant_id=tenant_id
        )
        await session.commit()
        assert row.id is not None
        assert row.scheme == "nz_nzbn"

    async with AsyncSessionLocal() as session:
        fetched = await bi_svc.get(session, company_id, "nz_nzbn")
        assert fetched is not None
        assert fetched.value == "9429000000001"


async def test_upsert_updates_existing_row() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        await bi_svc.upsert(
            session, company_id, "uk_crn", "01234567", tenant_id=tenant_id
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await bi_svc.upsert(
            session, company_id, "uk_crn", "07654321", tenant_id=tenant_id
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BusinessIdentifier).where(
                    BusinessIdentifier.company_id == company_id,
                    BusinessIdentifier.scheme == "uk_crn",
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "upsert created a duplicate row"
        assert rows[0].value == "07654321"


async def test_unknown_scheme_rejected() -> None:
    _, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        with pytest.raises(bi_svc.UnknownScheme):
            await bi_svc.upsert(session, company_id, "moon_id", "42")


async def test_rls_policy_installed_on_business_identifiers() -> None:
    """Verify ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation policy
    are both in place. The policy is the same shape as 0055/0083 — we
    don't re-test RLS enforcement (covered by tests/test_web_router_tenant_scope.py
    against shared infrastructure); we just assert this table joined the club.
    """
    async with AsyncSessionLocal() as session:
        rls_row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'business_identifiers'"
                )
            )
        ).first()
        assert rls_row is not None, "business_identifiers table missing"
        assert rls_row[0] is True, "RLS not ENABLED on business_identifiers"
        assert rls_row[1] is True, "RLS not FORCED on business_identifiers"

        policy_row = (
            await session.execute(
                text(
                    "SELECT polname FROM pg_policy "
                    "WHERE polrelid = 'business_identifiers'::regclass "
                    "  AND polname = 'tenant_isolation'"
                )
            )
        ).first()
        assert policy_row is not None, (
            "tenant_isolation policy missing on business_identifiers"
        )


async def test_backfill_seeded_au_abn_from_companies_abn() -> None:
    """Migration backfill: any company with companies.abn set should have
    a matching scheme='au_abn' row after migration. We tolerate the seed
    company having no ABN — only assert the row exists when the column
    is populated.
    """
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, company_id)
        if not co or not co.abn:
            pytest.skip("seed company has no ABN to backfill against")
        existing = await bi_svc.get(session, company_id, "au_abn")
        assert existing is not None, (
            "0101 backfill missed companies.abn → business_identifiers"
        )
        assert existing.value == co.abn
