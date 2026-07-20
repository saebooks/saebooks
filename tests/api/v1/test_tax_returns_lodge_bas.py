"""Wiring test: lodge_tax_return BAS path builds a real SBR XBRL envelope.

Covers ``_build_bas_envelope`` — figures JSONB + tax-period + company ABN ->
XBRL business document, and the 422 guard when the company has no ABN.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from lxml import etree
from sqlalchemy import select

from saebooks.api.v1.tax_returns import _build_bas_envelope
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
from saebooks.services import business_identifiers
from saebooks.services.lodgement.sbr import bas as _sbr_bas

pytestmark = pytest.mark.postgres_only

# The XBRL envelope generator is the certified ATO SBR transmission path; in the
# open/AGPL build it is the PUBLIC SHIM (raises NotImplementedError). The real
# private generator never sets this flag → no-op in the private tree, fires only
# in the open tree. The 422 input-guard tests below stay real (open-engine surface).
_SBR_STUBBED = getattr(_sbr_bas, "__OPEN_ENGINE_STUB__", False)

XBRLI = "http://www.xbrl.org/2003/instance"


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
        # ABN recorded under its ``au_abn`` business identifier (the legacy
        # ``companies.abn`` column was dropped in 0204). None clears it.
        if abn:
            await business_identifiers.upsert(
                s, co.id, "au_abn", abn, tenant_id=co.tenant_id
            )
        else:
            _bi = await business_identifiers.get(s, co.id, "au_abn")
            if _bi is not None:
                await s.delete(_bi)
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


@pytest.mark.skipif(
    _SBR_STUBBED,
    reason="commercial ATO SBR XBRL envelope generation is stubbed in the open/AGPL engine",
)
async def test_build_bas_envelope_generates_xbrl_from_figures():
    company_id, period_id = await _seed_company_and_period("51824753556")
    figures = {"G1": 11000, "G3": 500, "G10": 2200, "G11": 3300, "1A": 1000, "1B": 500}

    async with AsyncSessionLocal() as s:
        doc = await _build_bas_envelope(
            s, company_id=company_id, period_id=period_id, figures=figures
        )

    root = etree.fromstring(doc)
    assert root.tag == f"{{{XBRLI}}}xbrl"
    assert root.find(f".//{{{XBRLI}}}identifier").text == "51824753556"
    assert root.find(f".//{{{XBRLI}}}startDate").text == "2026-01-01"
    # G1 fact present with the supplied value (placeholder concept ns is fine here)
    assert b"11000" in doc and b"500" in doc


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
