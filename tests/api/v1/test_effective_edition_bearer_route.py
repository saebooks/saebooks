"""Real end-to-end coverage for the launch-promo-JWT path on a bearer-gated,
``require_feature``-gated route (M2 §5 build-sequence step 1).

``tests/services/test_features.py::test_require_feature_consults_per_user_promo_jwt``
already pins the *dependency* contract against a synthetic FastAPI app with a
fake middleware that hand-stamps ``request.state.user`` — it never exercises
the real ``require_bearer`` dependency that stamps that state in production.
This file closes that gap: it mints a real JWT via ``create_access_token``,
sends it through the real app's ``require_bearer`` -> ``request.state.user``
-> ``_effective_edition_for_request`` -> ``require_feature`` chain against a
real Business+-gated router (``/api/v1/bank-feeds/connections``), with the
process-wide singleton pinned at Community throughout. Mirrors the pattern in
``tests/api/v1/test_admin_gate_jwt_role.py``.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete

from saebooks.config import settings as module_settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token
from saebooks.services.licence import resolver as resolver_mod

# In the open/AGPL build the licence resolver is the PUBLIC SHIM (commercial
# launch-promo control-plane stubbed); the real private resolver never sets this
# flag, so this skip is a no-op in the private tree and only fires in the open tree.
_LICENCE_STUBBED = getattr(resolver_mod, "__OPEN_ENGINE_STUB__", False)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GATED_ROUTE = "/api/v1/bank-feeds/connections"  # router-level require_feature(FLAG_BANK_FEEDS), Business+


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {
            "sub": str(user.id),
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        }
    )


@pytest_asyncio.fixture
async def _community_singleton(monkeypatch):
    """Pin the process-wide singleton at Community for the whole test."""
    monkeypatch.setattr(module_settings, "edition", "community")
    yield


@pytest_asyncio.fixture
async def promo_pro_user(_community_singleton) -> AsyncIterator[User]:
    """A user row carrying a (stubbed-valid) Pro launch-promo JWT."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        username="promo-pro-user",
        email="promo-pro-user@test.invalid",
        role="bookkeeper",
        launch_promo_jwt="header.payload.sig",
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
async def plain_user(_community_singleton) -> AsyncIterator[User]:
    """A user row with no launch-promo JWT — must stay bound to Community."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        username="plain-community-user",
        email="plain-community-user@test.invalid",
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


@pytest.mark.skipif(
    _LICENCE_STUBBED,
    reason="commercial launch-promo JWT licence control-plane is stubbed in the open/AGPL engine",
)
async def test_promo_pro_jwt_clears_business_flag_gate_on_community_singleton(
    promo_pro_user: User, monkeypatch
) -> None:
    """The exact bug the launch-promo fix targets, exercised end-to-end.

    SAEBOOKS_EDITION=community (process-wide singleton) + a real bearer
    token for a user whose row carries a launch_promo_jwt that resolves to
    Pro must clear a Business+ ``require_feature(FLAG_BANK_FEEDS)`` gate —
    proving ``require_bearer`` -> ``request.state.user`` ->
    ``_effective_edition_for_request`` -> ``require_feature`` actually wires
    together in the real app, not just in the synthetic-app unit test.
    """
    from saebooks.services.licence import LicenceSource, ResolvedLicence, caps_for

    def _fake_decode(_token: str) -> ResolvedLicence:
        return ResolvedLicence(
            edition="pro", source=LicenceSource.JWT, caps=caps_for("pro")
        )

    monkeypatch.setattr(resolver_mod, "_decode_user_promo_jwt", _fake_decode)

    token = _mint(promo_pro_user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            _GATED_ROUTE, headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text


async def test_no_promo_jwt_stays_404_on_community_singleton(
    plain_user: User,
) -> None:
    """Sanity/negative: no promo JWT -> singleton still governs -> 404.

    Guards against the positive test above passing for the wrong reason
    (e.g. the gate silently no-op'ing rather than genuinely resolving the
    promo edition).
    """
    token = _mint(plain_user)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            _GATED_ROUTE, headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 404, r.text
