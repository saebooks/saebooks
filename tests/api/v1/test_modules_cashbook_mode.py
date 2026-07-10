"""Cashbook mode entry (M2 §5 step 6).

Registered as ``kind="mode"``, ``entitled=True`` unconditionally at
every edition, per ``docs/cashbook-edition-design.md`` — a UI mode over
the one double-entry ledger, not a licence flag. Deliberately NO
``FLAG_CASHBOOK`` constant (would pollute the strict-superset invariant
for zero value — see ``saebooks/services/module_registry.py``).
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
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token
from saebooks.services.module_registry import REGISTRY

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_cashbook_entry_registered_as_mode() -> None:
    cashbook = next(e for e in REGISTRY if e.id == "cashbook")
    assert cashbook.kind == "mode"
    assert cashbook.state == "enforced"


def test_no_flag_cashbook_constant_exists() -> None:
    from saebooks.services import features

    assert not hasattr(features, "FLAG_CASHBOOK")


def test_cashbook_not_in_all_flags() -> None:
    from saebooks.services.features import ALL_FLAGS

    assert "cashbook" not in ALL_FLAGS


async def test_catalogue_shows_cashbook_as_mode_community_tier() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/v1/modules")
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["cashbook"]["kind"] == "mode"
    assert by_id["cashbook"]["tier_membership"] == "community"
    assert by_id["cashbook"]["state"] == "enforced"


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {"sub": str(user.id), "role": user.role, "tenant_id": str(user.tenant_id)}
    )


@pytest_asyncio.fixture
async def cashbook_test_user() -> AsyncIterator[User]:
    user = User(
        id=uuid.uuid4(),
        tenant_id=_DEFAULT_TENANT,
        username="cashbook-mode-user",
        email="cashbook-mode-user@test.invalid",
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


async def test_usage_endpoint_cashbook_entitled_true_on_community(
    cashbook_test_user: User, monkeypatch
) -> None:
    """Entitled at the CHEAPEST edition too -- proves it's not riding
    along on a flag that happens to be on, it's unconditional."""
    monkeypatch.setattr(module_settings, "edition", "community")
    token = _mint(cashbook_test_user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get(
            "/api/v1/modules/usage", headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["cashbook"]["entitled"] is True
    assert by_id["cashbook"]["kind"] == "mode"
