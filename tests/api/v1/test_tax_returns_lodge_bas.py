"""Wiring test: lodge_tax_return BAS path's pre-flight guards.

Covers ``_build_bas_envelope``'s 422 guards (missing company ABN / missing
tax period) — the figures JSONB + tax-period + company ABN resolution that
happens *before* handing off to the (community-edition-stubbed) SBR XBRL
document generator. The generator itself (``build_bas_document``) is a
commercial SAE Books feature — see ``saebooks/services/lodgement/sbr/bas.py``
and its own stub — so the "builds a real SBR XBRL envelope" happy-path test
that used to live here was removed for the public tree.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from saebooks.api.v1.tax_returns import _build_bas_envelope
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType

pytestmark = pytest.mark.postgres_only


async def _seed_company_and_period(
    abn: str | None,
    *,
    period_start: date = date(2026, 1, 1),
    period_end: date = date(2026, 3, 31),
):
    """Return (company_id, period_id), setting the seed AU company's ABN.

    Idempotent on the period: ``tax_periods`` has a unique constraint on
    (company_id, jurisdiction, period_start), so reuse an existing row rather
    than colliding across tests / reruns.
    """
    async with AsyncSessionLocal() as s:
        co = (
            await s.execute(select(Company).where(Company.jurisdiction == "AU").limit(1))
        ).scalars().first()
        assert co is not None, "expected a seed AU company"
        co.abn = abn
        period = (
            await s.execute(
                select(TaxPeriod).where(
                    TaxPeriod.company_id == co.id,
                    TaxPeriod.jurisdiction == "AUS",
                    TaxPeriod.period_start == period_start,
                )
            )
        ).scalars().first()
        if period is None:
            period = TaxPeriod(
                id=uuid.uuid4(),
                company_id=co.id,
                tenant_id=co.tenant_id,
                jurisdiction="AUS",
                period_type=TaxPeriodType.QUARTERLY,
                period_start=period_start,
                period_end=period_end,
            )
            s.add(period)
        await s.commit()
        return co.id, period.id


async def test_build_bas_envelope_422_without_company_abn():
    company_id, period_id = await _seed_company_and_period(
        None, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30)
    )
    async with AsyncSessionLocal() as s:
        with pytest.raises(HTTPException) as ei:
            await _build_bas_envelope(
                s, company_id=company_id, period_id=period_id, figures={"G1": 100}
            )
    assert ei.value.status_code == 422
    assert "ABN" in ei.value.detail


async def test_build_bas_envelope_422_when_period_missing():
    company_id, _ = await _seed_company_and_period(
        "51824753556", period_start=date(2026, 7, 1), period_end=date(2026, 9, 30)
    )
    async with AsyncSessionLocal() as s:
        with pytest.raises(HTTPException) as ei:
            await _build_bas_envelope(
                s, company_id=company_id, period_id=uuid.uuid4(), figures={"G1": 100}
            )
    assert ei.value.status_code == 422
