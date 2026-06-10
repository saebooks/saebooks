"""Tests for the curated international tax-code seed (migration 0165).

``ensure_international_seed`` bakes a jurisdiction-tagged reference set into
the engine without any UI change: the rows exist for the engine but the app
list endpoints default to the home jurisdiction (AU), so they stay hidden.

Coverage:
- seed is idempotent (second run inserts nothing, no duplicates);
- the expected international + AU-extended codes exist after the seed;
- existing AU starter codes (AU_SEED) are untouched;
- the home-jurisdiction default on ``list_active`` hides the international
  codes from the app.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import tax_codes as svc
from saebooks.services.tax_codes import (
    AU_EXTENDED_SEED,
    AU_SEED,
    INTERNATIONAL_SEED,
    ensure_au_seed,
    ensure_international_seed,
)

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_company() -> uuid.UUID:
    """A throwaway company in the default tenant (no CoA needed for tax codes)."""
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as s:
        s.add(
            Company(
                id=cid,
                tenant_id=_DEFAULT_TENANT_ID,
                name=f"IntlTax {cid.hex[:8]}",
                base_currency="AUD",
                fin_year_start_month=7,
                audit_mode="immutable",
            )
        )
        await s.commit()
    return cid


async def _count_active(company_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(TaxCode)
                .where(
                    TaxCode.company_id == company_id,
                    TaxCode.archived_at.is_(None),
                )
            )
        ).scalar_one()


async def test_international_seed_creates_codes() -> None:
    cid = await _make_company()
    async with AsyncSessionLocal() as s:
        inserted = await ensure_international_seed(s, cid)

    expected = len(INTERNATIONAL_SEED) + len(AU_EXTENDED_SEED)
    assert inserted == expected

    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                select(TaxCode.jurisdiction, TaxCode.code, TaxCode.tax_system)
                .where(
                    TaxCode.company_id == cid, TaxCode.archived_at.is_(None)
                )
            )
        ).all()
    have = {(j, c) for (j, c, _ts) in rows}
    # Spot-check representative codes across every seeded jurisdiction.
    # International codes are jurisdiction-PREFIXED so the code strings are
    # globally distinct (no unqualified code == "GST" collision with AU).
    assert ("NZ", "NZ_GST") in have
    assert ("UK", "UK_STD") in have
    assert ("UK", "UK_RC") in have
    assert ("EU", "EU_RC") in have
    assert ("US", "US_NOTAX") in have
    assert ("GEN", "GEN_OOS") in have
    assert ("AU", "RCP") in have  # reverse-charge imported services
    assert ("AU", "IMP") in have  # GST on imports
    # No international code collides with the bare AU "GST"/"FRE" strings
    # that ~30 existing tests look up via code == "GST".
    intl_codes = {row["code"] for row in INTERNATIONAL_SEED}
    assert "GST" not in intl_codes
    assert "FRE" not in intl_codes
    # Every seed row landed.
    for row in INTERNATIONAL_SEED + AU_EXTENDED_SEED:
        assert (row["jurisdiction"], row["code"]) in have


async def test_international_seed_is_idempotent() -> None:
    cid = await _make_company()
    async with AsyncSessionLocal() as s:
        first = await ensure_international_seed(s, cid)
    after_first = await _count_active(cid)

    async with AsyncSessionLocal() as s:
        second = await ensure_international_seed(s, cid)
    after_second = await _count_active(cid)

    assert first > 0
    assert second == 0, "re-running the seed must insert nothing"
    assert after_first == after_second, "no duplicate rows on re-seed"


async def test_au_starter_codes_untouched_by_international_seed() -> None:
    cid = await _make_company()
    # Seed the AU starter set first, snapshot it, then seed international.
    async with AsyncSessionLocal() as s:
        await ensure_au_seed(s, cid)

    async def _au_snapshot() -> dict[str, tuple]:
        async with AsyncSessionLocal() as s:
            rows = (
                await s.execute(
                    select(
                        TaxCode.code,
                        TaxCode.rate,
                        TaxCode.reporting_type,
                        TaxCode.tax_system,
                    ).where(
                        TaxCode.company_id == cid,
                        TaxCode.jurisdiction == "AU",
                        TaxCode.tax_system == "GST",
                        TaxCode.code.in_([r["code"] for r in AU_SEED]),
                        TaxCode.archived_at.is_(None),
                    )
                )
            ).all()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    before = await _au_snapshot()
    assert set(before) == {r["code"] for r in AU_SEED}

    async with AsyncSessionLocal() as s:
        await ensure_international_seed(s, cid)

    after = await _au_snapshot()
    assert after == before, "AU starter codes must be unchanged after intl seed"


async def test_international_codes_hidden_from_app_list() -> None:
    cid = await _make_company()
    async with AsyncSessionLocal() as s:
        await ensure_au_seed(s, cid)
        await ensure_international_seed(s, cid)

    async with AsyncSessionLocal() as s:
        # Default (home jurisdiction) — app view: AU only, no intl codes.
        app_view = await svc.list_active(s, cid)
        # Explicit all-jurisdictions view — engine/admin.
        all_view = await svc.list_active(s, cid, jurisdiction=None)

    app_juris = {tc.jurisdiction for tc in app_view}
    assert app_juris == {"AU"}, "app list must only show the home jurisdiction"
    all_juris = {tc.jurisdiction for tc in all_view}
    assert {"NZ", "UK", "EU", "US", "GEN"}.issubset(all_juris)
    assert len(all_view) > len(app_view)
