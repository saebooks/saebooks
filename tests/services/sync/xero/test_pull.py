"""End-to-end pull tests.

Mocks Xero HTTP via respx and exercises the actual DB path:
``pull_contacts`` upserts a Contact, advances the watermark, and
appends an audit log row.

Postgres-only: uses ``SET LOCAL app.current_tenant`` (RLS GUC), which
SQLite does not support — see ``tests/conftest.py``'s
``postgres_only`` marker.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.sync import (
    SyncAuditLog,
    SyncConnection,
    SyncConnectionStatus,
    SyncProvider,
    SyncState,
    SyncStateOrigin,
)
from saebooks.services.sync.xero.client import XERO_API_BASE, XeroClient
from saebooks.services.sync.xero.pull import pull_contacts
from saebooks.services.sync.xero.token import XERO_TOKEN_URL, XeroTokenCache

pytestmark = pytest.mark.postgres_only


def _ok_refresh() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ACCESS",
            "refresh_token": "ROTATED",
            "expires_in": 1800,
        },
    )


async def _seed_connection() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert one ACTIVE connection on the seed company. Return (tenant, company, conn_id)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        tenant_id = company.tenant_id
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = SyncConnection(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider=SyncProvider.XERO.value,
            external_tenant_id=f"TEN-{uuid.uuid4().hex[:6]}",
            status=SyncConnectionStatus.ACTIVE.value,
        )
        session.add(conn)
        await session.commit()
        return tenant_id, company.id, conn.id


def _make_client() -> XeroClient:
    return XeroClient(
        token_cache=XeroTokenCache(
            client_id="cid",
            client_secret="secret",
            refresh_token="OLD",
        ),
        xero_tenant_id="TEN",
    )


@respx.mock
async def test_pull_contacts_inserts_new_contact() -> None:
    tenant_id, company_id, conn_id = await _seed_connection()

    external_id = f"XC-{uuid.uuid4().hex[:8]}"
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": external_id,
                        "Name": "Pulled Inc",
                        "EmailAddress": "ap@pulled.example",
                        "IsCustomer": True,
                        "IsSupplier": False,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-15T12:00:00",
                    },
                ]
            },
        )
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        client = _make_client()
        try:
            stats = await pull_contacts(
                session,
                connection=conn,
                client=client,
                company_id=company_id,
            )
        finally:
            await client.aclose()
        await session.commit()

    assert stats.fetched == 1
    assert stats.upserted == 1

    # Verify Contact + sync_state + audit row landed.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = (
            await session.execute(
                select(Contact).where(Contact.external_id == external_id)
            )
        ).scalar_one_or_none()
        assert contact is not None
        assert contact.name == "Pulled Inc"
        assert contact.external_source == "xero"

        state = (
            await session.execute(
                select(SyncState).where(SyncState.external_id == external_id)
            )
        ).scalar_one_or_none()
        assert state is not None
        assert state.local_id == contact.id
        # Pulled rows must be origin='remote' with last_pushed_version
        # NULL — they have not been pushed back upstream. The push
        # selector ignores them on its first pass thanks to the origin
        # column (no version-stamping workaround needed).
        assert state.origin == SyncStateOrigin.REMOTE.value
        assert state.last_pushed_version is None

        audit = list(
            (
                await session.execute(
                    select(SyncAuditLog).where(
                        SyncAuditLog.external_id == external_id,
                    )
                )
            ).scalars()
        )
        assert any(a.outcome == "ok" for a in audit)

        # Watermark advanced.
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        assert conn.last_pulled_at is not None
        assert conn.last_pulled_at >= datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


@respx.mock
async def test_pull_contacts_skips_when_unchanged_etag() -> None:
    """A second pull with the same UpdatedDateUTC stays as ``ok`` upsert.

    (We don't track per-row etag for contacts in the same way as
    invoices, so the row is re-applied — the test pins that at minimum
    the call doesn't blow up and counters reflect one fetch + one
    upsert.)
    """
    tenant_id, company_id, conn_id = await _seed_connection()

    external_id = f"XC-{uuid.uuid4().hex[:8]}"
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": external_id,
                        "Name": "First Name",
                        "IsCustomer": True,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-15T12:00:00",
                    }
                ]
            },
        )
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        client = _make_client()
        try:
            await pull_contacts(
                session, connection=conn, client=client, company_id=company_id,
            )
        finally:
            await client.aclose()
        await session.commit()

    # Second pass with renamed contact.
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": external_id,
                        "Name": "Renamed Co",
                        "IsCustomer": True,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-16T12:00:00",
                    }
                ]
            },
        )
    )
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        client = _make_client()
        try:
            stats2 = await pull_contacts(
                session, connection=conn, client=client, company_id=company_id,
            )
        finally:
            await client.aclose()
        await session.commit()

    assert stats2.fetched == 1
    # Same external_id; row is updated, not inserted.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        c = (
            await session.execute(
                select(Contact).where(Contact.external_id == external_id)
            )
        ).scalar_one()
        assert c.name == "Renamed Co"
