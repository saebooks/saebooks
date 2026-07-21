"""Tests for the not-for-profit charitable-registration columns on Company
(M1.5 P1 tail): acnc_registered / dgr_endorsed / dgr_category /
tax_concession_type.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only


async def _seed_company_id():
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def test_nfp_attributes_default_unregistered() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        assert company is not None
        assert company.acnc_registered is False
        assert company.dgr_endorsed is False
        assert company.dgr_category is None
        assert company.tax_concession_type is None


async def test_nfp_attributes_round_trip() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        assert company is not None
        company.acnc_registered = True
        company.dgr_endorsed = True
        company.dgr_category = "public_benevolent_institution"
        company.tax_concession_type = "income_tax_exempt"
        await session.commit()
    try:
        async with AsyncSessionLocal() as session:
            company = await session.get(Company, company_id)
            assert company is not None
            assert company.acnc_registered is True
            assert company.dgr_endorsed is True
            assert company.dgr_category == "public_benevolent_institution"
            assert company.tax_concession_type == "income_tax_exempt"
    finally:
        async with AsyncSessionLocal() as session:
            company = await session.get(Company, company_id)
            assert company is not None
            company.acnc_registered = False
            company.dgr_endorsed = False
            company.dgr_category = None
            company.tax_concession_type = None
            await session.commit()
