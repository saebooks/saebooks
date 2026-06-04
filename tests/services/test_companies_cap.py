"""Cap-enforcement tests for ``saebooks.services.companies``.

These tests exercise ``create_company`` against a live test database,
patching ``resolve_licence`` to pin the active edition and rolling
back everything created so the shared DB stays clean.

The existing seed company already counts toward the cap in the test
DB, so the "below cap" case is checked via a tier that still has
headroom (Business / Pro / Enterprise) and the "at cap" case via
Offline/Community which allow exactly one company.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import companies as companies_svc
from saebooks.services.licence import LicenceSource, ResolvedLicence, caps_for


async def _purge_test_companies(prefix: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Company).where(Company.name.like(f"{prefix}%"))
        )
        await session.commit()


def _fake_licence(edition: str) -> ResolvedLicence:
    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.COMMUNITY_FALLBACK,
        caps=caps_for(edition),
    )


async def test_create_company_succeeds_on_enterprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("enterprise")
    )
    tag = uuid.uuid4().hex[:8]
    name = f"CAP_TEST_{tag}"
    try:
        async with AsyncSessionLocal() as session:
            company = await companies_svc.create_company(session, name=name)
            assert company.id is not None
            assert company.name == name
    finally:
        await _purge_test_companies("CAP_TEST_")


async def test_create_company_succeeds_on_developer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ``ValueError: Unknown edition: 'developer'``.

    The live sauer stack runs ``SAEBOOKS_EDITION=developer`` (internal
    guardrails-off edition, CHARTER 12.4). Before the cap entry existed,
    ``create_company`` -> ``resolve_licence`` -> ``caps_for("developer")``
    raised on an unknown edition. Developer has unlimited company caps,
    so a create must succeed regardless of how many companies already
    exist in the DB.
    """
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("developer")
    )
    tag = uuid.uuid4().hex[:8]
    name = f"CAP_TEST_DEV_{tag}"
    try:
        async with AsyncSessionLocal() as session:
            company = await companies_svc.create_company(session, name=name)
            assert company.id is not None
            assert company.name == name
    finally:
        await _purge_test_companies("CAP_TEST_DEV_")


async def test_create_company_blocks_on_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Community caps at 1 company — the seed already fills that."""
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("community")
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(companies_svc.CompanyCapExceeded) as exc:
            await companies_svc.create_company(session, name="CAP_TEST_community")
        assert exc.value.edition == "community"
        assert exc.value.limit == 1


async def test_create_company_blocks_on_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Company caps are always hard — Offline's soft cap is seats-only."""
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("offline")
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(companies_svc.CompanyCapExceeded):
            await companies_svc.create_company(session, name="CAP_TEST_offline")


async def test_count_active_companies_excludes_archived() -> None:
    """Archived companies shouldn't count against the cap."""
    tag = uuid.uuid4().hex[:8]
    try:
        async with AsyncSessionLocal() as session:
            baseline = await companies_svc.count_active_companies(session)

            # Add one archived company — baseline should stay put.
            arch = Company(
                name=f"CAP_TEST_archived_{tag}",
                base_currency="AUD",
                archived_at=__import__("datetime").datetime.now(),
            )
            session.add(arch)
            await session.commit()

            after = await companies_svc.count_active_companies(session)
            assert after == baseline
    finally:
        await _purge_test_companies("CAP_TEST_archived_")


async def test_create_company_blocks_paid_tiers_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pro caps at 3 — create enough companies to hit the cap, then assert."""
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("pro")
    )
    cap = 3
    tag = uuid.uuid4().hex[:8]
    try:
        async with AsyncSessionLocal() as session:
            current = await companies_svc.count_active_companies(session)
            # Create however many companies are needed to reach the cap.
            for i in range(max(0, cap - current)):
                filler = Company(
                    name=f"CAP_TEST_filler_{tag}_{i}",
                    base_currency="AUD",
                )
                session.add(filler)
            await session.commit()

        # Now at the cap — one more must raise.
        async with AsyncSessionLocal() as session:
            with pytest.raises(companies_svc.CompanyCapExceeded) as exc:
                await companies_svc.create_company(
                    session, name=f"CAP_TEST_over_{tag}"
                )
        assert exc.value.edition == "pro"
        assert exc.value.limit == cap
    finally:
        await _purge_test_companies(f"CAP_TEST_filler_{tag}")
        await _purge_test_companies(f"CAP_TEST_over_{tag}")


async def test_create_company_persists_valid_entity_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the entity_type enum/string drift 500.

    ``companies.entity_type`` is a Postgres ENUM (``entity_type_enum``,
    migration 0133). The ORM model previously mapped it as ``String(32)``,
    so asyncpg bound the INSERT parameter as ``$n::VARCHAR`` and Postgres
    refused the implicit varchar->enum cast — every ``create_company`` 500'd
    with ``column "entity_type" is of type entity_type_enum but expression
    is of type character varying``.

    With the model mapped to the native enum, create_company must succeed
    and the row must round-trip a valid enum label (the default ``COMPANY``).
    """
    monkeypatch.setattr(
        companies_svc, "resolve_licence", lambda: _fake_licence("enterprise")
    )
    tag = uuid.uuid4().hex[:8]
    name = f"CAP_TEST_entity_{tag}"
    try:
        async with AsyncSessionLocal() as session:
            company = await companies_svc.create_company(session, name=name)
            assert company.id is not None
            # Default is COMPANY and must be a valid enum label.
            assert company.entity_type == "COMPANY"

        # Re-read in a fresh session to confirm it actually persisted as the
        # enum (asyncpg returns the label as a str on SELECT).
        async with AsyncSessionLocal() as session:
            persisted = (
                await session.execute(
                    select(Company.entity_type).where(Company.name == name)
                )
            ).scalar_one()
            assert persisted == "COMPANY"
    finally:
        await _purge_test_companies("CAP_TEST_entity_")
