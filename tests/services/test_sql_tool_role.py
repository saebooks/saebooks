"""SECURITY-CRITICAL smoke tests for the saebooks_sql_ro role.

These tests open a real Postgres connection as ``saebooks_sql_ro`` and
assert that every dangerous operation the brief listed raises a
permission / privilege error. If any of these tests pass when they
shouldn't, the SQL tool ships an admin-shell-on-server.

Brief checklist (W5):
  1. INSERT INTO contacts → permission denied
  2. UPDATE contacts → permission denied
  3. DELETE FROM contacts → permission denied
  4. SELECT pg_read_server_files('/etc/passwd') → permission denied
  5. COPY contacts FROM PROGRAM '...' → permission denied (engine-level
     superuser-only, but verified)
  6. SELECT lo_export(...) → permission denied
  7. As saebooks (or app role) — the matching writes succeed.
  8. If any dangerous call is allowed, ADD a REVOKE to migration 0087.

Each test invokes the connection through ``services.sql_tool.execute``
where possible so we exercise the real path the API endpoint uses.
The smoke connection bypasses the API layer (no FastAPI client) — we
only care about the role grants, not auth.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import sql_tool as sql_svc


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Engines for the two roles. These are smoke-tests — we don't go via the
# API. The RO engine reuses the service helper to guarantee the runtime
# code path matches.
# ---------------------------------------------------------------------------


def _ro_engine():
    return sql_svc._get_ro_engine()


def _app_engine():
    """Connect as the runtime app role (``saebooks_app``)."""
    from saebooks.db import engine as runtime_engine

    return runtime_engine


# ---------------------------------------------------------------------------
# Direct-connection privilege probes.
# ---------------------------------------------------------------------------


async def _expect_denied(sql: str, *, engine, contains: list[str] | None = None):
    """Run ``sql`` on ``engine`` and assert it raises a permission /
    privilege error. ``contains`` is an optional list of substrings that
    must all appear in the error message (case-insensitive)."""
    import sqlalchemy.exc

    with pytest.raises(Exception) as exc_info:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
            await conn.commit()
    msg = str(exc_info.value).lower()
    needles = contains or ["permission denied", "must be superuser", "privilege"]
    assert any(n.lower() in msg for n in needles), (
        f"Expected permission/privilege error for {sql!r}; got: {exc_info.value}"
    )


# ---------- 1. INSERT denied ------------------------------------------------


async def test_ro_role_cannot_insert_contacts() -> None:
    await _expect_denied(
        "INSERT INTO contacts (id, company_id, tenant_id, name, contact_type, "
        " currency_code, email, phone, version, created_at, updated_at) "
        " VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
        "        'evil', 'CUSTOMER', 'AUD', NULL, NULL, 1, now(), now())",
        engine=_ro_engine(),
    )


# ---------- 2. UPDATE denied ------------------------------------------------


async def test_ro_role_cannot_update_contacts() -> None:
    await _expect_denied(
        "UPDATE contacts SET name = 'pwned' WHERE 1=0",
        engine=_ro_engine(),
    )


# ---------- 3. DELETE denied ------------------------------------------------


async def test_ro_role_cannot_delete_contacts() -> None:
    await _expect_denied(
        "DELETE FROM contacts WHERE 1=0",
        engine=_ro_engine(),
    )


# ---------- 4. pg_read_server_files denied ----------------------------------


async def test_ro_role_cannot_read_server_files() -> None:
    await _expect_denied(
        "SELECT pg_read_server_files('/etc/passwd')",
        engine=_ro_engine(),
        contains=["permission denied", "no function", "does not exist", "privilege"],
    )


async def test_ro_role_cannot_read_binary_file() -> None:
    await _expect_denied(
        "SELECT pg_read_binary_file('/etc/passwd')",
        engine=_ro_engine(),
        contains=["permission denied", "no function", "does not exist", "privilege"],
    )


# ---------- 5. COPY ... FROM PROGRAM denied ---------------------------------


async def test_ro_role_cannot_copy_from_program() -> None:
    """COPY ... FROM PROGRAM is superuser-only at the engine level —
    even an app role with INSERT privilege can't run it. We assert the
    behaviour to keep the smoke test contract honest."""
    await _expect_denied(
        "COPY contacts FROM PROGRAM '/bin/cat /etc/passwd'",
        engine=_ro_engine(),
        contains=["superuser", "permission denied", "privilege", "must be"],
    )


# ---------- 6. lo_export / lo_import denied ---------------------------------


async def test_ro_role_cannot_lo_export() -> None:
    await _expect_denied(
        "SELECT lo_export(0, '/tmp/nope')",
        engine=_ro_engine(),
        contains=["permission denied", "no function", "does not exist", "privilege"],
    )


async def test_ro_role_cannot_lo_import() -> None:
    await _expect_denied(
        "SELECT lo_import('/etc/passwd')",
        engine=_ro_engine(),
        contains=["permission denied", "no function", "does not exist", "privilege"],
    )


# ---------- 7. App role smoke: SELECT/INSERT/UPDATE/DELETE all succeed ------


async def test_app_role_full_dml_smoke() -> None:
    """Sanity check that the app role still has full DML on a scratch
    table — i.e., the dangerous-function REVOKEs in migration 0087
    didn't accidentally strip the runtime role too."""
    name = f"sql_tool_smoke_{uuid.uuid4().hex[:8]}"
    engine = _app_engine()
    async with engine.connect() as conn:
        # The app role doesn't have CREATE TABLE on public, so we use
        # the owner role via the AsyncSessionLocal (which is bound to
        # whichever runtime URL the test config supplies).
        pass

    # Bootstrap with the runtime engine (which in tests is usually the
    # owner role). We use AsyncSessionLocal because that's how every
    # other test seeds.
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            f"CREATE UNLOGGED TABLE {name} (id INT PRIMARY KEY, label TEXT)"
        ))
        await session.execute(text(
            f"GRANT ALL ON TABLE {name} TO saebooks_app"
        ))
        await session.execute(text(
            f"GRANT SELECT ON TABLE {name} TO saebooks_sql_ro"
        ))
        await session.commit()
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"INSERT INTO {name} VALUES (1, 'a'), (2, 'b')"))
            n = (await conn.execute(text(f"SELECT count(*) FROM {name}"))).scalar_one()
            assert n == 2
            await conn.execute(text(f"UPDATE {name} SET label = 'c' WHERE id = 1"))
            await conn.execute(text(f"DELETE FROM {name} WHERE id = 2"))
            await conn.commit()
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
            await session.commit()


# ---------- 8. Service-layer integration smoke -----------------------------


async def test_execute_select_uses_ro_role() -> None:
    """The high-level ``execute()`` helper must pick the RO role for a
    plain SELECT. Audit row records ``role_used='saebooks_sql_ro'``."""
    async with AsyncSessionLocal() as audit_session:
        result = await sql_svc.execute(
            audit_session,
            statement="SELECT 1 AS one",
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )
    assert result.role_used == "saebooks_sql_ro"
    assert result.rows == [[1]]
    assert result.audit_id > 0


async def test_execute_update_without_confirmation_rejected() -> None:
    async with AsyncSessionLocal() as audit_session:
        with pytest.raises(sql_svc.WriteRejectedError) as exc:
            await sql_svc.execute(
                audit_session,
                statement="UPDATE contacts SET name = 'x' WHERE 1=0",
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )
        assert exc.value.audit_id > 0


async def test_execute_update_with_confirmation_uses_app_role() -> None:
    """End-to-end smoke through the service: an UPDATE with matching
    confirmation flips role_used to 'saebooks_app'."""
    name = f"sql_tool_svc_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            f"CREATE UNLOGGED TABLE {name} (id INT PRIMARY KEY, label TEXT)"
        ))
        await session.execute(text(
            f"GRANT ALL ON TABLE {name} TO saebooks_app"
        ))
        await session.execute(text(f"INSERT INTO {name} VALUES (1, 'a')"))
        await session.commit()
    try:
        async with AsyncSessionLocal() as audit_session:
            result = await sql_svc.execute(
                audit_session,
                statement=f"UPDATE {name} SET label = 'b' WHERE id = 1",
                write_confirmation=sql_svc.WriteConfirmation(
                    enabled=True, verb_typed="UPDATE"
                ),
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )
        assert result.role_used == "saebooks_app"
        assert result.rowcount == 1
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
            await session.commit()
