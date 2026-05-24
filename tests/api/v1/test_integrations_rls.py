"""End-to-end RLS test for the paperless inbound webhook.

Proves two coupled invariants of fix-F:

1. The ``saebooks_app`` role created by migration 0056 (and password-set
   by 0128) is NOSUPERUSER + NOBYPASSRLS.
2. The paperless webhook handler in ``saebooks/api/v1/integrations.py``
   sets ``app.current_tenant`` via ``SET LOCAL`` before reading
   ``paperless_webhook_secrets``, so the lookup still finds the row
   under FORCE-RLS — i.e. Lane 5 P0-005 is closed.

The test deliberately operates outside the ORM Session wiring that
``deps.get_session`` provides — the webhook is a *public* endpoint that
never sees a JWT, so the GUC has to be set in the handler itself, not
by the request-scoped session listener.

The test stack is the only place this can run because it has both the
``saebooks`` owner role (for seeding) and the ``saebooks_app`` runtime
role (for asserting RLS enforcement). Marked ``postgres_only`` so it
auto-skips on the SQLite Cashbook backend.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")
os.environ.setdefault("SAEBOOKS_TEST_TRUSTED_USER_HEADER", "1")

from saebooks.config import settings  # noqa: E402
from saebooks.db import AsyncSessionLocal, engine  # noqa: E402
from saebooks.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _set_edition_enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Elevate edition so the FLAG_PAPERLESS_INTEGRATION gate doesn't 404
    the endpoint before the handler is reached. Mirrors the autouse
    fixture in test_integrations.py."""
    monkeypatch.setattr(settings, "edition", "enterprise")


# Migration 0056 docstring guarantees this role name; the test stack's
# ``docker/test-initdb/01-test-roles.sql`` seeds it with this password,
# and migration 0128 re-asserts the same password from
# ``SAEBOOKS_APP_DB_PASSWORD`` env (kept aligned in
# ``docker-compose.test.yml``).
_APP_ROLE = "saebooks_app"
_APP_PASSWORD = "saebooks_app_test_pw"


def _app_role_url() -> str:
    """Derive a runtime-role URL from the owner-role DATABASE_URL.

    The test stack only sets ``DATABASE_URL`` (owner). We mirror the
    host/port/db parts and substitute the role + password — same
    pattern the production deploy plan uses (see
    ``docs/db-role-split.md``).
    """
    base = settings.database_url
    from sqlalchemy.engine.url import make_url

    url = make_url(base)
    return (
        url.set(username=_APP_ROLE, password=_APP_PASSWORD).render_as_string(hide_password=False)
    )


def _sign(body: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ---------------------------------------------------------------------------
# Pre-flight — role attributes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_saebooks_app_role_is_not_bypassrls_or_superuser() -> None:
    """Migration 0056 + 0128 produce a properly-constrained app role.

    If this fails, Lane 4 P0-1 is still open and nothing else in this
    file can prove RLS is enforced.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcanlogin "
                    "FROM pg_roles WHERE rolname = :r"
                ).bindparams(r=_APP_ROLE)
            )
        ).first()
    assert row is not None, f"role {_APP_ROLE} not present in test DB"
    assert row[0] is False, f"{_APP_ROLE} must NOT be SUPERUSER (got rolsuper={row[0]})"
    assert row[1] is False, f"{_APP_ROLE} must NOT be BYPASSRLS (got rolbypassrls={row[1]})"
    assert row[2] is True, f"{_APP_ROLE} must have LOGIN (got rolcanlogin={row[2]})"


# ---------------------------------------------------------------------------
# RLS enforcement — direct probe under the app role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_paperless_secret_invisible_without_set_local() -> None:
    """Under saebooks_app + no GUC, FORCE-RLS hides every row.

    This is the structural proof that RLS is doing something at all.
    Compare with ``test_paperless_secret_visible_with_set_local``
    below — same query, same role, but with ``app.current_tenant``
    set the row is visible.
    """
    tenant_id = uuid.uuid4()
    label = f"rls-probe-{uuid.uuid4().hex[:8]}"

    # 1. Seed a secret as the owner role (BYPASSRLS — no GUC needed).
    async with AsyncSessionLocal() as session:
        # Need a tenants row first so the FK to tenants(id) holds.
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (:id, :name, :slug) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=tenant_id, name=label, slug=label),
        )
        await session.execute(
            text(
                "INSERT INTO paperless_webhook_secrets "
                "(id, tenant_id, label, secret_ciphertext, created_at) "
                "VALUES (gen_random_uuid(), :tid, :lab, decode('00', 'hex'), now())"
            ).bindparams(tid=tenant_id, lab=label),
        )
        await session.commit()

    # 2. Connect as the runtime app role and probe with NO GUC.
    app_engine = create_async_engine(
        _app_role_url(), poolclass=NullPool, future=True
    )
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False)
    try:
        async with AppSession() as s:
            # No SET LOCAL — policy filters everything because
            # current_setting('app.current_tenant', true) is NULL
            # and (tenant_id = NULL::uuid) is NULL → row excluded.
            res = await s.execute(
                text(
                    "SELECT count(*)::int FROM paperless_webhook_secrets "
                    "WHERE tenant_id = :tid"
                ).bindparams(tid=tenant_id)
            )
            assert res.scalar_one() == 0, (
                "FORCE-RLS is inert under saebooks_app — row visible "
                "without app.current_tenant. Lane 4 P0-1 still open."
            )

            # Now set the GUC and expect the row back. This is what
            # the webhook handler does.
            await s.execute(
                text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
            )
            res = await s.execute(
                text(
                    "SELECT count(*)::int FROM paperless_webhook_secrets "
                    "WHERE tenant_id = :tid"
                ).bindparams(tid=tenant_id)
            )
            assert res.scalar_one() == 1, (
                "SET LOCAL did not surface the seeded row — fix-F "
                "is not closing the SET-LOCAL gap."
            )
    finally:
        await app_engine.dispose()

    # 3. Clean up via owner role.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM paperless_webhook_secrets WHERE tenant_id = :tid"
            ).bindparams(tid=tenant_id)
        )
        await session.execute(
            text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=tenant_id)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# End-to-end — paperless webhook 200 under the app role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_paperless_webhook_succeeds_with_real_db_and_app_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hit the webhook against a real DB row, with the runtime engine
    pointed at the saebooks_app role.

    This is the symptom-level proof that fix-F closes Lane 5 P0-005:
    if the SET LOCAL in the handler did not fire, this test would
    404 (row filtered by FORCE-RLS) and never reach the HMAC step.
    """
    tenant_id = uuid.uuid4()
    secret = "rls-e2e-paperless-secret"
    label = f"rls-e2e-{uuid.uuid4().hex[:8]}"
    payload = b'{"type":"document_added","document_id":4242}'

    # 1. Seed tenant + paperless secret as the owner role.
    #    We store the literal-bytes ciphertext that decrypt_field will
    #    return, then monkeypatch decrypt_field so the test does not
    #    require SAEBOOKS_FIELD_ENCRYPTION_KEY to be set in CI.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (:id, :name, :slug) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=tenant_id, name=label, slug=label),
        )
        await session.execute(
            text(
                "INSERT INTO paperless_webhook_secrets "
                "(id, tenant_id, label, secret_ciphertext, created_at) "
                "VALUES (gen_random_uuid(), :tid, :lab, :ct, now())"
            ).bindparams(tid=tenant_id, lab=label, ct=secret.encode("utf-8")),
        )
        await session.commit()

    # 2. Stub decrypt_field to be the identity function (the seeded
    #    "ciphertext" is just the plaintext bytes — keeps the test off
    #    Fernet key plumbing). The HMAC step still runs for real.
    import saebooks.api.v1.integrations as integ_mod

    monkeypatch.setattr(
        integ_mod,
        "decrypt_field",
        lambda blob: blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else blob,
    )

    # 3. Repoint AsyncSessionLocal at a fresh engine bound to the
    #    saebooks_app role for the duration of the request. The
    #    handler imports AsyncSessionLocal from saebooks.db AT MODULE
    #    LOAD TIME, so we patch the symbol it actually uses in
    #    integrations.py — that's the same symbol the production
    #    behaviour will resolve at runtime once SAEBOOKS_APP_DATABASE_URL
    #    is set on the compose stack.
    app_engine = create_async_engine(
        _app_role_url(), poolclass=NullPool, future=True
    )
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False)
    monkeypatch.setattr(integ_mod, "AsyncSessionLocal", AppSession)

    sig = _sign(payload, secret)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/integrations/paperless/webhook",
                content=payload,
                headers={
                    "X-Tenant-Id": str(tenant_id),
                    "X-Paperless-Signature": sig,
                },
            )
    finally:
        await app_engine.dispose()
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "DELETE FROM paperless_webhook_secrets WHERE tenant_id = :tid"
                ).bindparams(tid=tenant_id)
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=tenant_id)
            )
            await session.commit()

    # 200 only reachable if SET LOCAL fired, the row was selected past
    # FORCE-RLS, decrypt_field was called, and the HMAC matched.
    assert resp.status_code == 200, (
        f"webhook expected 200 but got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert data["received"] is True
    assert data["tenant_id"] == str(tenant_id)
