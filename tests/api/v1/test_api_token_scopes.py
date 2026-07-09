"""API-token scope enforcement (A2 privilege-escalation fix).

`ApiToken.scopes` was stored, echoed, and shown in the admin UI but
NEVER read for an authz decision (a deferred "per-scope authorization"
item from the original build brief). A token a consumer believed was
"read-only" could POST/PUT/PATCH/DELETE. These tests pin the new
``require_scope`` enforcement layer:

* A ``["read"]``-scoped token is denied (403) on a mutating request
  but allowed on a GET.
* Backward compat: a token with empty/None scopes — or a full-access
  marker (``"*"`` / ``"full"`` / both ``"read"`` and ``"write"``) —
  keeps FULL access on BOTH GET and POST, exactly as before this
  change. This is what every existing live token looks like
  (``issue()`` defaults ``scopes=[]``), so no live token is affected.
* Interactive (static dev-bearer / JWT) auth is unaffected by scope
  logic — only ``saebk_*`` API-token auth is scope-gated.

Enforcement lives in ``require_bearer``'s API-token branch only.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.services import api_tokens as token_svc
from saebooks.services.companies import ensure_seed_company

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _db_available() -> bool:
    try:
        async with _owner_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _seed_user_id() -> uuid.UUID:
    """Reuse / create the pytest-admin user in the default tenant."""
    from sqlalchemy import select

    from saebooks.models.user import User, UserRole

    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == "pytest-scope-user")
            )
        ).scalars().first()
        if u is None:
            u = User(
                tenant_id=_DEFAULT_TENANT_ID,
                username="pytest-scope-user",
                role=UserRole.ADMIN.value,
            )
            session.add(u)
            await session.commit()
        return u.id


async def _issue_token(scopes: list[str] | None) -> str:
    """Issue an API token against the live test DB and return cleartext.

    Uses the same DB engine the app reads, so the token resolves
    through ``require_bearer``'s API-token branch in-process.
    """
    user_id = await _seed_user_id()
    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        _, cleartext = await token_svc.issue(
            session,
            user_id=user_id,
            company_id=company.id,
            name=f"scope-test-{uuid.uuid4().hex[:8]}",
            scopes=scopes,
        )
        await session.commit()
    return cleartext


def _bearer_client(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def _rand_name(prefix: str = "ScopeTest") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# (a) read-scoped token is DENIED on a mutating request
# ---------------------------------------------------------------------------
async def test_read_scoped_token_denied_on_post() -> None:
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    token = await _issue_token(["read"])
    async with _bearer_client(token) as ac:
        r = await ac.post(
            "/api/v1/contacts",
            json={"name": _rand_name(), "contact_type": "CUSTOMER"},
        )
    assert r.status_code == 403, (
        f"read-scoped API token POST should be 403, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# (b) the SAME read-scoped token still SUCCEEDS on a GET
# ---------------------------------------------------------------------------
async def test_read_scoped_token_allowed_on_get() -> None:
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    token = await _issue_token(["read"])
    async with _bearer_client(token) as ac:
        r = await ac.get("/api/v1/contacts")
    assert r.status_code == 200, (
        f"read-scoped API token GET should be 200, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# (c) BACKWARD COMPAT: empty / None / full-marker scopes keep FULL access
#     on BOTH GET and POST — identical to behaviour before this change.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "scopes",
    [
        [],                      # default issue() shape — every live token
        None,                    # explicit None
        ["*"],                   # wildcard marker
        ["full"],                # full marker
        ["read", "write"],       # both verbs == full access
    ],
)
async def test_full_or_empty_scope_token_unaffected(scopes) -> None:
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    token = await _issue_token(scopes)
    async with _bearer_client(token) as ac:
        # GET still works
        rg = await ac.get("/api/v1/contacts")
        assert rg.status_code == 200, (
            f"full/empty-scope token GET should be 200, got {rg.status_code}: {rg.text}"
        )
        # POST still works (the backward-compat guarantee)
        rp = await ac.post(
            "/api/v1/contacts",
            json={"name": _rand_name(), "contact_type": "CUSTOMER"},
        )
    assert rp.status_code == 201, (
        f"full/empty-scope token POST should be 201 (unchanged), "
        f"got {rp.status_code}: {rp.text}"
    )


# ---------------------------------------------------------------------------
# (d) write-scoped token CAN mutate (positive control for write)
# ---------------------------------------------------------------------------
async def test_write_scoped_token_allowed_on_post() -> None:
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    token = await _issue_token(["write"])
    async with _bearer_client(token) as ac:
        r = await ac.post(
            "/api/v1/contacts",
            json={"name": _rand_name(), "contact_type": "CUSTOMER"},
        )
    assert r.status_code == 201, (
        f"write-scoped API token POST should be 201, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# (d) interactive (static dev-bearer) auth is UNAFFECTED by scope logic.
#     The dev bearer carries no scopes and must keep full access on both
#     verbs — it's the path tests/scripts/web use.
# ---------------------------------------------------------------------------
async def test_static_dev_bearer_unaffected_by_scopes() -> None:
    if not await _db_available():
        pytest.skip("Postgres unavailable")
    token = current_token()
    async with _bearer_client(token) as ac:
        rg = await ac.get("/api/v1/contacts")
        assert rg.status_code == 200, rg.text
        rp = await ac.post(
            "/api/v1/contacts",
            json={"name": _rand_name("DevBearer"), "contact_type": "CUSTOMER"},
        )
    assert rp.status_code == 201, (
        f"static dev-bearer POST must be unaffected by scope logic, "
        f"got {rp.status_code}: {rp.text}"
    )
