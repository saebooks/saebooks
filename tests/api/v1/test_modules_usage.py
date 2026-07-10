"""Contract tests for ``GET /api/v1/modules/usage`` (M2 §5 step 5).

Bearer-gated, tenant-scoped module usage/entitlement. Covers:

* bearer required (401 without/with a bad token);
* response shape (edition/effective_edition/bookkeeping_mode/modules/caps);
* per-user entitlement resolves off the caller's effective edition, not
  the process-wide singleton alone (mirrors the step-1 promo-JWT test,
  here exercised via a direct singleton edition change since the point
  under test is entitlement *computation*, not resolver precedence);
* the delegated-module entitled-union case: ``capture`` tracks the OR of
  FLAG_DOCUMENT_INBOX/FLAG_BANK_FEEDS/FLAG_AI_EXTRACTION, NOT a hardcoded
  True (the leak the audit calls out); ``platform``/``preaccounting`` are
  unconditionally entitled at every edition (wrap no flag-gated
  capability);
* the six developer-only flags never appear;
* RLS: a foreign tenant's company id passed via ``X-Company-Id`` is
  ignored, not leaked into ``bookkeeping_mode``.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete

from saebooks.config import settings as module_settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token
from saebooks.services.module_registry import DEVELOPER_ONLY_FLAGS

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000099")
_URL = "/api/v1/modules/usage"


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {"sub": str(user.id), "role": user.role, "tenant_id": str(user.tenant_id)}
    )


@pytest_asyncio.fixture
async def default_tenant_user() -> AsyncIterator[User]:
    user = User(
        id=uuid.uuid4(),
        tenant_id=_DEFAULT_TENANT,
        username="modules-usage-user",
        email="modules-usage-user@test.invalid",
        role="bookkeeper",
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    try:
        yield user
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(User).where(User.id == user.id))
            await session.commit()


@pytest_asyncio.fixture
async def other_tenant_company() -> AsyncIterator[Company]:
    """A company belonging to a DIFFERENT tenant -- used to prove
    X-Company-Id can't be used to read across tenants.

    The foreign Tenant row must exist before its Company (FK
    ``fk_companies_tenant_id``) -- mirrors the hermetic
    Tenant-then-Company setup used by
    ``tests/services/test_business_identifiers.py`` and the
    cross-tenant fixtures in ``tests/test_transfers.py`` /
    ``tests/test_intercompany.py``. ``AsyncSessionLocal`` is the
    BYPASSRLS owner role in the test environment (see
    ``saebooks/db.py``), so no ``app.current_tenant`` GUC needs to be
    set to seed a second tenant's row here -- the RLS boundary this
    test exercises is enforced at the API layer (module_usage's
    company-lookup filtering), not at INSERT time for this fixture.
    """
    company = Company(
        id=uuid.uuid4(),
        tenant_id=_OTHER_TENANT,
        name="Other Tenant Co",
        base_currency="AUD",
        fin_year_start_month=7,
    )
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=_OTHER_TENANT,
                name="Other Tenant",
                slug="modules-usage-other-tenant",
            )
        )
        await session.flush()
        session.add(company)
        await session.commit()
        await session.refresh(company)
    try:
        yield company
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(Company).where(Company.id == company.id))
            await session.execute(sa_delete(Tenant).where(Tenant.id == _OTHER_TENANT))
            await session.commit()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------- #
# Auth gate                                                              #
# ---------------------------------------------------------------------- #


async def test_requires_bearer() -> None:
    async with await _client() as c:
        r = await c.get(_URL)
    assert r.status_code == 401


async def test_rejects_bad_bearer() -> None:
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


# ---------------------------------------------------------------------- #
# Response shape                                                         #
# ---------------------------------------------------------------------- #


async def test_response_shape(default_tenant_user: User, monkeypatch) -> None:
    monkeypatch.setattr(module_settings, "edition", "business")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "edition", "effective_edition", "bookkeeping_mode", "modules", "caps",
    }
    assert body["edition"] == "business"
    assert body["effective_edition"] == "business"
    for entry in body["modules"]:
        assert set(entry.keys()) == {"id", "kind", "entitled", "health"}
        assert isinstance(entry["entitled"], bool)
        assert entry["health"] in {"ok", "degraded", "unavailable", "not_installed"}
    assert set(body["caps"].keys()) == {"admin_seats", "employee_seats", "companies"}
    for cap in body["caps"].values():
        assert set(cap.keys()) == {"outcome", "limit", "current", "reason"}
        assert cap["outcome"] in {"allow", "warn", "block"}


async def test_excludes_developer_only_flags(
    default_tenant_user: User, monkeypatch
) -> None:
    monkeypatch.setattr(module_settings, "edition", "business")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    ids = {m["id"] for m in r.json()["modules"]}
    for dev_flag in DEVELOPER_ONLY_FLAGS:
        assert dev_flag not in ids


# ---------------------------------------------------------------------- #
# Entitlement — flag modules track the effective edition                #
# ---------------------------------------------------------------------- #


async def test_flag_module_not_entitled_below_its_tier(
    default_tenant_user: User, monkeypatch
) -> None:
    """Split into its own test function (rather than flipping edition
    twice inside one test) deliberately: resolve_licence_for_user falls
    through to resolve_licence(), which caches the resolved edition in
    the process-global ``_RESOLVED_LICENCE`` singleton
    (services/licence/resolver.py). The ``_restore_settings_edition``
    autouse fixture busts that cache once per TEST FUNCTION, not
    between requests within one function -- so flipping
    ``module_settings.edition`` mid-test and making a second request in
    the same function would silently keep serving the first request's
    cached resolution. Two separate test functions each get their own
    fresh cache reset."""
    monkeypatch.setattr(module_settings, "edition", "community")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["bank_feeds"]["entitled"] is False


async def test_flag_module_entitled_once_its_tier_is_reached(
    default_tenant_user: User, monkeypatch
) -> None:
    monkeypatch.setattr(module_settings, "edition", "business")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["bank_feeds"]["entitled"] is True


# ---------------------------------------------------------------------- #
# Entitlement — delegated-module union (the leak the audit calls out)   #
# ---------------------------------------------------------------------- #


async def test_delegated_capture_entitled_is_union_not_hardcoded_true(
    default_tenant_user: User, monkeypatch
) -> None:
    """capture's entitled must track the OR of its wrapped flags at the
    caller's effective edition -- NOT hardcoded True. On Community none
    of FLAG_DOCUMENT_INBOX/FLAG_BANK_FEEDS/FLAG_AI_EXTRACTION are on, so
    capture must show entitled=False, matching document_inbox/
    bank_feeds's own entitled=False for the same underlying capability
    (the exact discovery-layer leak 404-not-403 exists to prevent)."""
    monkeypatch.setattr(module_settings, "edition", "community")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["capture"]["entitled"] is False
    assert by_id["document_inbox"]["entitled"] is False
    assert by_id["bank_feeds"]["entitled"] is False
    assert by_id["capture"]["health"] == "not_installed"


async def test_delegated_capture_entitled_true_once_any_wrapped_flag_is_on(
    default_tenant_user: User, monkeypatch
) -> None:
    """document_inbox alone turns on at Offline -- capture must flip to
    entitled=True even though bank_feeds/ai_extraction (Business+) are
    still off, proving the union (not an all-flags-required AND)."""
    monkeypatch.setattr(module_settings, "edition", "offline")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["document_inbox"]["entitled"] is True
    assert by_id["bank_feeds"]["entitled"] is False
    assert by_id["ai_extraction"]["entitled"] is False
    assert by_id["capture"]["entitled"] is True  # union: at least one wrapped flag on


async def test_platform_and_preaccounting_always_entitled(
    default_tenant_user: User, monkeypatch
) -> None:
    """Wrap zero flag-gated capability -- entitled at every edition,
    including Community."""
    monkeypatch.setattr(module_settings, "edition", "community")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(_URL, headers={"Authorization": f"Bearer {token}"})
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["platform"]["entitled"] is True
    assert by_id["preaccounting"]["entitled"] is True
    assert by_id["platform"]["health"] == "not_installed"
    assert by_id["preaccounting"]["health"] == "not_installed"


# ---------------------------------------------------------------------- #
# RLS — a foreign tenant's company can't be read via X-Company-Id       #
# ---------------------------------------------------------------------- #


async def test_foreign_tenant_company_id_is_ignored_not_leaked(
    default_tenant_user: User, other_tenant_company: Company, monkeypatch
) -> None:
    monkeypatch.setattr(module_settings, "edition", "business")
    token = _mint(default_tenant_user)
    async with await _client() as c:
        r = await c.get(
            _URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Company-Id": str(other_tenant_company.id),
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # The foreign company_id must NOT resolve -- bookkeeping_mode falls
    # back to null (no active company matched for THIS tenant), never
    # the other tenant's company's actual bookkeeping_mode value.
    assert body["bookkeeping_mode"] is None
