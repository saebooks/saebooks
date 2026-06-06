"""Migration 0159 — principal_webauthn_lookup_credential SECURITY DEFINER fn.

Proves the credential resolver added by 0159:
  * exists and is SECURITY DEFINER with a pinned search_path (anti-hijack);
  * returns the owning principal id for a known credential id;
  * returns nothing for an unknown credential id.

This is the function the login ceremony uses to derive the principal id from
the assertion's credential — so its correctness underpins the whole feature's
"principal_id is server-derived" invariant.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-mig-0159")

from saebooks.db import engine as _owner_engine
from saebooks.models.principal import Principal, PrincipalFido2Credential


@pytest_asyncio.fixture
async def owner_sessionmaker() -> AsyncIterator[Any]:
    yield async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )


async def test_function_is_security_definer_with_pinned_search_path(
    owner_sessionmaker: Any,
) -> None:
    async with owner_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT p.prosecdef, p.proconfig "
                    "FROM pg_proc p "
                    "WHERE p.proname = 'principal_webauthn_lookup_credential'"
                )
            )
        ).first()
    assert row is not None, "function principal_webauthn_lookup_credential missing"
    assert row.prosecdef is True, "function must be SECURITY DEFINER"
    config = row.proconfig or []
    assert any(
        c.startswith("search_path=") for c in config
    ), f"search_path must be pinned, got {config}"
    joined = ",".join(config)
    assert "pg_catalog" in joined and "public" in joined


async def test_lookup_returns_owner_for_known_credential(
    owner_sessionmaker: Any,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    cred_id = b"mig-cred-" + suffix.encode()
    async with owner_sessionmaker() as s:
        p = Principal(
            id=uuid.uuid4(), display_name="Mig Test",
            username=f"mig-{suffix}",
        )
        s.add(p)
        await s.flush()
        s.add(
            PrincipalFido2Credential(
                principal_id=p.id,
                credential_id=cred_id,
                public_key=b"pubkey-bytes",
                sign_count=7,
            )
        )
        await s.commit()
        pid = p.id
    try:
        async with owner_sessionmaker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT principal_id, public_key, sign_count "
                        "FROM principal_webauthn_lookup_credential(:c)"
                    ),
                    {"c": cred_id},
                )
            ).first()
        assert row is not None
        assert str(row.principal_id) == str(pid)
        assert bytes(row.public_key) == b"pubkey-bytes"
        assert row.sign_count == 7
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(
                text("DELETE FROM principal_fido2_credentials WHERE principal_id=:p"),
                {"p": str(pid)},
            )
            await s.execute(
                text("DELETE FROM principals WHERE id=:p"), {"p": str(pid)}
            )
            await s.commit()


async def test_lookup_returns_nothing_for_unknown_credential(
    owner_sessionmaker: Any,
) -> None:
    async with owner_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT principal_id "
                    "FROM principal_webauthn_lookup_credential(:c)"
                ),
                {"c": b"definitely-not-a-real-credential"},
            )
        ).all()
    assert rows == []
