"""Unit tests for ``saebooks.services.fx.gate.gate_non_base_currency`` —
the shared FLAG_MULTI_CURRENCY create-time check wired into the
invoice/bill/expense/payment create routes (Wave A, 2026-07-10).

Exercised directly against the service function (not HTTP) so the
same test covers every call site (expenses/payments included) without
needing a full E2E fixture for each — the HTTP-level coverage for
invoices/bills + the fx_revaluation report gate lives in
``tests/api/v1/test_reports_fx.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services.fx import gate_non_base_currency

pytestmark = pytest.mark.postgres_only


def _fake_request(edition: str | None = None) -> SimpleNamespace:
    """A minimal stand-in for ``fastapi.Request``.

    ``gate_non_base_currency`` -> ``require_feature_inline`` ->
    ``_effective_edition_for_request`` only ever reads
    ``request.state.user`` (falling back to the process-wide
    ``settings.edition`` singleton when it's ``None``), so a bare
    object with that one attribute is a faithful stand-in — no need
    for a real Starlette ``Request``/ASGI scope.
    """
    return SimpleNamespace(state=SimpleNamespace(user=None))


@pytest.fixture
async def default_company_id() -> str:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None, "Test DB has no company"
        assert company.base_currency == "AUD", (
            "This test assumes the default seeded company is AUD-base "
            f"(got {company.base_currency!r}) — update the base-currency "
            "assertions below if that ever changes."
        )
        return str(company.id)


async def test_base_currency_is_never_gated_at_community(
    default_company_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUD request against an AUD-base company -> no-op at community."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        await gate_non_base_currency(
            session, _fake_request(), default_company_id, "AUD"
        )  # must not raise


async def test_foreign_currency_gated_at_community(
    default_company_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USD request against an AUD-base company -> 404 at community."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await gate_non_base_currency(
                session, _fake_request(), default_company_id, "USD"
            )
    assert exc_info.value.status_code == 404


async def test_foreign_currency_gated_at_offline(
    default_company_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG_MULTI_CURRENCY turns on at offline -> no raise for USD."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    async with AsyncSessionLocal() as session:
        await gate_non_base_currency(
            session, _fake_request(), default_company_id, "USD"
        )  # must not raise


async def test_foreign_currency_ungated_at_enterprise(
    default_company_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enterprise (every flag on) -> no-op, matching prod's all-flags instance."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "enterprise")
    async with AsyncSessionLocal() as session:
        await gate_non_base_currency(
            session, _fake_request(), default_company_id, "EUR"
        )  # must not raise
