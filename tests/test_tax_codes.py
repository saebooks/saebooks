from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import tax_codes as svc

pytestmark = pytest.mark.postgres_only


async def _active_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        assert company is not None
        return company


async def test_seed_codes_present() -> None:
    """Seed AU tax codes are in the DB after migrations."""
    company = await _active_company()
    async with AsyncSessionLocal() as session:
        codes = {
            row.code
            for row in await svc.list_active(session, company.id)
        }
    for code in ("GST", "FRE", "INP", "EXP", "N-T", "CAP"):
        assert code in codes


async def test_create_update_archive_round_trip() -> None:
    company = await _active_company()
    async with AsyncSessionLocal() as session:
        tc = await svc.create(
            session,
            company.id,
            code="TST1",
            name="Test code",
            rate=Decimal("5.5"),
            tax_system="VAT",
            reporting_type="taxable",
        )
        assert tc.rate == Decimal("5.500")

        updated = await svc.update(
            session, tc.id, name="Renamed", rate=Decimal("7.25")
        )
        assert updated.name == "Renamed"
        assert updated.rate == Decimal("7.250")

        await svc.archive(session, tc.id)

        # Archived row excluded from list_active
        active = await svc.list_active(session, company.id)
        assert all(row.code != "TST1" for row in active)


async def test_duplicate_code_blocked_within_company() -> None:
    company = await _active_company()
    async with AsyncSessionLocal() as session:
        await svc.create(session, company.id, code="DUP1", name="first", rate=Decimal("0"))
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await svc.create(session, company.id, code="DUP1", name="second", rate=Decimal("0"))

    # Cleanup
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "DUP1")
        )
        for row in rows.scalars().all():
            await session.delete(row)
        await session.commit()


async def test_archive_then_reuse_code() -> None:
    """After archive, the same code can be re-created (partial-unique index)."""
    company = await _active_company()

    # Cleanup from any prior run
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(TaxCode).where(
                TaxCode.company_id == company.id, TaxCode.code == "REUSE"
            )
        )
        for row in rows.scalars().all():
            await session.delete(row)
        await session.commit()

    async with AsyncSessionLocal() as session:
        first = await svc.create(
            session, company.id, code="REUSE", name="v1", rate=Decimal("10")
        )
        await svc.archive(session, first.id)

    async with AsyncSessionLocal() as session:
        # Should succeed — the archived one no longer occupies the unique slot
        await svc.create(
            session, company.id, code="REUSE", name="v2", rate=Decimal("0")
        )

    # Cleanup
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(TaxCode).where(
                TaxCode.company_id == company.id, TaxCode.code == "REUSE"
            )
        )
        for row in rows.scalars().all():
            await session.delete(row)
        await session.commit()
