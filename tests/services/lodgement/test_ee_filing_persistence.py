"""Option A persistence — the EE X-Road filing ref columns round-trip.

postgres_only: exercises the additive nullable columns added to ``tax_returns``
by migration ``0196_ee_filing_ref_cols`` (ee_filing_request_id / ee_filing_state /
ee_filing_receipt). Proves the async-lifecycle handles (UUID + state + receipt)
persist between calls on the existing return row — no new tenant table, so no RLS
checklist applies (the columns inherit tax_returns' own tenant_id + RLS).

The ``ee_filing_state`` token written is an ``EEFilingState`` value — the same
enum that drives the in-memory transition machine (single source of truth).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
from saebooks.models.tax_return import TaxReturn, TaxReturnStatus
from saebooks.services.lodgement.adapters.ee_client import EEFilingState
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only


async def test_ee_filing_ref_columns_round_trip() -> None:
    company_id = await _make_ee_company(jurisdiction="EE")
    period_id = uuid.uuid4()
    return_id = uuid.uuid4()
    request_uuid = "c99bbd83-28f8-48a8-ad2e-02fcad97804f"

    async with AsyncSessionLocal() as session:
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EST",
                period_type=TaxPeriodType.MONTHLY,
                period_start=date(2027, 1, 1),
                period_end=date(2027, 1, 31),
            )
        )
        await session.commit()

    # Insert a return in the SUBMITTED state (UUID known, no receipt yet).
    async with AsyncSessionLocal() as session:
        session.add(
            TaxReturn(
                id=return_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EE",
                period_id=period_id,
                return_type="KMD",
                figures={"boxes": {}},
                status=TaxReturnStatus.LODGED,
                ee_filing_request_id=request_uuid,
                ee_filing_state=EEFilingState.SUBMITTED.value,
                ee_filing_receipt=None,
            )
        )
        await session.commit()

    # Read back — SUBMITTED, no receipt.
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(TaxReturn).where(TaxReturn.id == return_id))
        ).scalar_one()
        assert row.ee_filing_request_id == request_uuid
        assert row.ee_filing_state == "submitted"
        assert row.ee_filing_receipt is None

    # Advance to ACCEPTED with a persisted receipt (the koondvaade / feedback).
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(TaxReturn).where(TaxReturn.id == return_id))
        ).scalar_one()
        row.ee_filing_state = EEFilingState.ACCEPTED.value
        row.ee_filing_receipt = {
            "accepted": True,
            "vat_payable": str(Decimal("1234.56")),
            "declaration_state": "SUBMITTED",
        }
        row.status = TaxReturnStatus.ACCEPTED
        await session.commit()

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(TaxReturn).where(TaxReturn.id == return_id))
        ).scalar_one()
        assert row.ee_filing_state == "accepted"
        assert row.ee_filing_receipt["vat_payable"] == "1234.56"
        assert row.status == TaxReturnStatus.ACCEPTED


async def test_ee_filing_columns_default_null_for_non_ee_returns() -> None:
    """A non-EE return leaves the EE ref columns NULL (additive, opt-in)."""
    company_id = await _make_ee_company(jurisdiction="AU")
    period_id = uuid.uuid4()
    return_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="AUS",
                period_type=TaxPeriodType.QUARTERLY,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        session.add(
            TaxReturn(
                id=return_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="AU",
                period_id=period_id,
                return_type="BAS",
                figures={"boxes": {}},
                status=TaxReturnStatus.DRAFT,
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(TaxReturn).where(TaxReturn.id == return_id))
        ).scalar_one()
        assert row.ee_filing_request_id is None
        assert row.ee_filing_state is None
        assert row.ee_filing_receipt is None
