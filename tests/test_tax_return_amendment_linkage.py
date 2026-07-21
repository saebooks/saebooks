"""Test for ``TaxReturn.supersedes_return_id`` / ``amendment_reason``
(M1.5 P1 tail) — ``TaxReturnStatus.AMENDED`` already existed with no
supersedes/amended-by linkage; this closes that gap.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
from saebooks.models.tax_return import TaxReturn, TaxReturnStatus

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def test_amendment_return_links_to_original() -> None:
    company_id = await _seed_company_id()
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        period = TaxPeriod(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            jurisdiction="AUS",
            period_type=TaxPeriodType.QUARTERLY,
            period_start=date(2020, 1, 1),
            period_end=date(2020, 3, 31),
        )
        session.add(period)
        await session.flush()

        original = TaxReturn(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            jurisdiction="AUS",
            period_id=period.id,
            return_type=f"BAS-AMEND-TEST-{tag}",
            figures={"G1": "1000.00"},
            status=TaxReturnStatus.LODGED,
        )
        session.add(original)
        await session.flush()

        assert original.supersedes_return_id is None
        assert original.amendment_reason is None

        amendment = TaxReturn(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            jurisdiction="AUS",
            period_id=period.id,
            return_type=f"BAS-AMEND-TEST-{tag}",
            figures={"G1": "1100.00"},
            status=TaxReturnStatus.AMENDED,
            supersedes_return_id=original.id,
            amendment_reason="Corrected G1 after a late supplier invoice.",
        )
        session.add(amendment)
        await session.commit()
        await session.refresh(amendment)

        assert amendment.supersedes_return_id == original.id
        assert amendment.amendment_reason == "Corrected G1 after a late supplier invoice."

        # Deleting the original does not cascade-block or cascade-delete the
        # amendment — the FK is SET NULL, not RESTRICT/CASCADE.
        await session.delete(original)
        await session.commit()
        await session.refresh(amendment)
        assert amendment.supersedes_return_id is None
