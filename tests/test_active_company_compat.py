"""Tests for the ``first_company_compat`` shim and contextvar binding.

Gap P0-5: every router used to define ``async def _first_company()`` that
picked the oldest non-archived company by ``created_at``, ignoring the
user's active-company cookie. The fix replaces each helper body with a
delegate to ``active_svc.first_company_compat()``, which:

* Reads the contextvar bound by ``ActiveCompanyMiddleware`` for the
  current request — the cookie-selected company.
* Falls back to first-by-created-at when middleware didn't run (tests,
  /api paths, /healthz, /metrics, etc.).

Tests run against the live dev DB (same pattern as test_n1_reconciliation),
so they assume at least one non-archived company in the default tenant.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import active_company as active_svc


async def _seeded_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(
                Company.tenant_id == DEFAULT_TENANT_ID,
                Company.archived_at.is_(None),
            )
            .order_by(Company.created_at)
        )
        company = result.scalars().first()
    assert company is not None, "test DB must have ≥1 non-archived company"
    return company


def _stub_company(name: str) -> Company:
    """Transient (un-persisted) Company instance for contextvar binding."""
    return Company(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        name=name,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------- #
# Tests                                                            #
# ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_compat_returns_contextvar_when_bound() -> None:
    """When the contextvar is set, the shim returns it without DB lookup."""
    stub = _stub_company("ContextVarCo")
    token = active_svc.bind_active_company(stub)
    try:
        got = await active_svc.first_company_compat()
        assert got is stub
        assert got.name == "ContextVarCo"
    finally:
        active_svc.reset_active_company(token)


@pytest.mark.asyncio
async def test_compat_falls_back_to_first_by_created() -> None:
    """When the contextvar is unbound, the shim falls back to DB query."""
    # No leak from a sibling test
    assert active_svc.current_active_company() is None

    expected = await _seeded_company()
    got = await active_svc.first_company_compat()
    assert got.id == expected.id
    assert got.archived_at is None


@pytest.mark.asyncio
async def test_compat_or_none_returns_contextvar_when_bound() -> None:
    """The dashboard variant honours the contextvar too."""
    stub = _stub_company("DashboardCtxCo")
    token = active_svc.bind_active_company(stub)
    try:
        got = await active_svc.first_company_compat_or_none()
        assert got is stub
    finally:
        active_svc.reset_active_company(token)


@pytest.mark.asyncio
async def test_compat_or_none_falls_back_when_unbound() -> None:
    """The dashboard variant also falls back to first-by-created-at."""
    assert active_svc.current_active_company() is None
    expected = await _seeded_company()
    got = await active_svc.first_company_compat_or_none()
    assert got is not None
    assert got.id == expected.id


@pytest.mark.asyncio
async def test_contextvar_token_round_trip() -> None:
    """``bind`` + ``reset`` is symmetric — no leak between requests."""
    assert active_svc.current_active_company() is None
    stub = _stub_company("RoundTripCo")
    token = active_svc.bind_active_company(stub)
    assert active_svc.current_active_company() is stub
    active_svc.reset_active_company(token)
    assert active_svc.current_active_company() is None


@pytest.mark.asyncio
async def test_contextvar_overrides_first_by_created_when_different() -> None:
    """Confirms the cookie-selected company beats the legacy fallback.

    This is the heart of P0-5: when the user has switched to a company
    that ISN'T the first-by-created-at, ``_first_company()`` (now a thin
    delegate to the compat shim) must honour the switch — otherwise
    every router silently writes invoices/bills/JEs against the wrong
    company.
    """
    expected_legacy = await _seeded_company()

    stub = _stub_company("OtherCo-not-first")
    assert stub.id != expected_legacy.id
    token = active_svc.bind_active_company(stub)
    try:
        got = await active_svc.first_company_compat()
        assert got is stub  # contextvar wins, NOT the legacy fallback
        assert got.id != expected_legacy.id
    finally:
        active_svc.reset_active_company(token)

    # After reset, fallback is back in play
    got_after = await active_svc.first_company_compat()
    assert got_after.id == expected_legacy.id
