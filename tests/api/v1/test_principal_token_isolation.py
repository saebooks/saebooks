"""The principal-token and user-token surfaces must be disjoint.

A principal session token must NEVER authenticate a normal user endpoint, and
a normal user JWT must NEVER authenticate a principal endpoint. This protects
the hard constraint "do not modify the existing user enforcement path" and
stops a token-type confusion from becoming a privilege crossover.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.asyncio]

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-token-iso")

from saebooks.main import app
from saebooks.services.jwt_tokens import create_access_token
from saebooks.services.principal_session import (
    PrincipalTokenError,
    decode_principal_token,
    make_principal_token,
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def test_user_jwt_is_not_a_principal_token() -> None:
    """A perfectly valid user JWT (no typ) is rejected by the principal
    decoder — its signature is fine, but it is not a principal token."""
    user_jwt = create_access_token(
        {"sub": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "role": "admin"}
    )
    with pytest.raises(PrincipalTokenError):
        decode_principal_token(user_jwt)


def test_principal_token_roundtrips() -> None:
    pid = uuid.uuid4()
    tok = make_principal_token(pid)
    claims = decode_principal_token(tok)
    assert claims["psub"] == str(pid)
    assert claims["typ"] == "principal"


@pytest.mark.postgres_only
async def test_user_jwt_cannot_call_principal_endpoint(
    client: AsyncClient,
) -> None:
    """A user JWT presented to an authenticated principal endpoint -> 401."""
    user_jwt = create_access_token(
        {"sub": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "role": "admin"}
    )
    resp = await client.get(
        "/api/v1/principal/tenants",
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.postgres_only
async def test_principal_token_cannot_call_user_endpoint(
    client: AsyncClient,
) -> None:
    """A principal token CANNOT operate a user endpoint (/users admin list).

    A principal token is a validly-SIGNED JWT (same secret), so the user
    ``require_bearer`` does decode it without error — but it carries NO ``sub``
    claim, so ``_stamp_user_from_sub`` hydrates no ``request.state.user`` and
    no role. The admin gate (``_require_admin``) then refuses it: with no user
    identity and no ``X-Admin: true`` header it is DENIED (403). The essential
    security property: a principal token confers ZERO user authority — it can
    neither read nor mutate any tenant's data through a user endpoint. We
    accept either 401 (rejected at auth) or 403 (rejected at the role gate);
    both mean "denied". It must never be 2xx.
    """
    principal_tok = make_principal_token(uuid.uuid4())
    resp = await client.get(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {principal_tok}"},
    )
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code != 200
