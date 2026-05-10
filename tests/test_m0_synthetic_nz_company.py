"""Final acceptance test for the M0 multi-jurisdiction refactor.

Creates a synthetic NZ company (no AU coupling) and proves all four
M0 surfaces dispatch by jurisdiction:

1. ``services.tax_engine.get_engine('NZ')`` returns the M1 NZ engine
   and computes a deterministic treatment.
2. ``services.templates.apply_template(<nz_co>, 'nz/default')``
   raises ``NotImplementedError`` keyed to M1 (CoA template still
   pending — separate piece).
3. ``services.lodgement.get_adapter('NZ', 'gst101')`` returns the NZ
   stub adapter, and calling ``.lodge('gst101', ...)`` raises
   ``NotImplementedError`` keyed to M1.
4. ``services.business_identifiers.upsert(...)`` accepts the
   ``nz_nzbn`` scheme on a non-AU company without invoking any AU
   path.

The test deliberately does NOT touch the journal post path because
the synthetic NZ company has no chart of accounts (``apply_template``
is the stubbed entry point) — exercising the post path would
require AU coupling we're proving the absence of.
"""
from __future__ import annotations

import uuid

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.business_identifier import BusinessIdentifier
from saebooks.models.company import Company
from saebooks.services import business_identifiers as bi_svc
from saebooks.services.lodgement import get_adapter
from saebooks.services.tax_engine import get_engine
from saebooks.services.templates import apply_template

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_synthetic_nz_company() -> uuid.UUID:
    """Insert a NZ-jurisdiction Company directly. Cleaned up by the
    test that creates it (no autouse fixture required).
    """
    async with AsyncSessionLocal() as session:
        co = Company(
            tenant_id=_TENANT_ID,
            name=f"M0-Synthetic-NZ-{uuid.uuid4().hex[:8]}",
            legal_name="Synthetic NZ Trading Ltd",
            jurisdiction="NZ",
            coa_template_key="nz/default",
            base_currency="NZD",
        )
        session.add(co)
        await session.commit()
        await session.refresh(co)
        return co.id


async def _delete_company(company_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(BusinessIdentifier).where(
                BusinessIdentifier.company_id == company_id
            )
        )
        await session.execute(
            delete(Company).where(Company.id == company_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_m0_acceptance_nz_company_routes_through_jurisdiction_dispatchers() -> None:
    company_id = await _make_synthetic_nz_company()
    try:
        # 1. tax_engine dispatcher returns the M1 NZ engine and computes
        #    a real treatment (no AU coupling — the engine never reads
        #    company state, just snapshots the PostingContext).
        from datetime import date
        from decimal import Decimal

        from saebooks.models.account import AccountType
        from saebooks.services.tax_engine import PostingContext
        from saebooks.services.tax_engine.nz import NZTaxEngine

        nz_engine = get_engine("NZ")
        assert isinstance(nz_engine, NZTaxEngine)
        assert nz_engine.jurisdiction == "NZ"

        treatment = nz_engine.compute(
            PostingContext(
                company_id=company_id,
                jurisdiction="NZ",
                posting_date=date(2026, 4, 1),
                account_id=uuid.uuid4(),
                account_type=AccountType.INCOME,
                amount=Decimal("100.00"),
                rate=Decimal("0.15"),
                tax_code="GST",
                reporting_type="standard",
            )
        )
        assert treatment.jurisdiction == "NZ"
        assert treatment.tax == Decimal("15.00")
        assert treatment.direction == "output"

        # 2. CoA template dispatcher raises NotImplementedError(M1).
        async with AsyncSessionLocal() as session:
            with pytest.raises(NotImplementedError, match="M1"):
                await apply_template(session, company_id, "nz/default")

        # 3. Lodgement adapter dispatcher returns NZ stub; calling it
        #    raises NotImplementedError(M1).
        adapter = get_adapter("NZ", route="gst101")
        assert adapter.jurisdiction == "NZ"
        with pytest.raises(NotImplementedError, match="M1"):
            await adapter.lodge("gst101", b"<gst101/>", "id-1", {})

        # 4. business_identifiers accepts nz_nzbn scheme on the NZ co
        #    without invoking any AU path.
        async with AsyncSessionLocal() as session:
            bid = await bi_svc.upsert(
                session,
                company_id=company_id,
                scheme="nz_nzbn",
                value="9429000000000",
                tenant_id=_TENANT_ID,
            )
            await session.commit()
            assert bid.scheme == "nz_nzbn"
            assert bid.value == "9429000000000"
            assert bid.company_id == company_id

        # 5. AU dispatchers are still healthy when the NZ company is
        #    in the DB — proves jurisdiction routing isn't side-eyed
        #    by the existence of foreign-jurisdiction rows.
        au_engine = get_engine("AU")
        assert au_engine.jurisdiction == "AU"
        au_adapter = get_adapter("AU", route="bas")
        assert au_adapter.jurisdiction == "AU"
    finally:
        await _delete_company(company_id)
