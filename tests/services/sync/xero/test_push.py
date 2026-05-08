"""End-to-end push tests.

Mocks Xero HTTP via respx and exercises ``push_contacts``: creates a
local contact with no external_id, pushes it, verifies the resulting
``external_id`` is persisted and a ``sync_state`` row is created with
``last_pushed_version``.
"""
from __future__ import annotations

import uuid

import httpx
import respx
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.sync import (
    SyncConnection,
    SyncConnectionStatus,
    SyncProvider,
    SyncState,
)
from saebooks.services.sync.xero.client import XERO_API_BASE, XeroClient
from saebooks.services.sync.xero.push import (
    detect_conflict,
    push_contacts,
)
from saebooks.services.sync.xero.token import XERO_TOKEN_URL, XeroTokenCache


def _ok_refresh() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ACCESS",
            "refresh_token": "ROTATED",
            "expires_in": 1800,
        },
    )


def _make_client() -> XeroClient:
    return XeroClient(
        token_cache=XeroTokenCache(
            client_id="cid",
            client_secret="secret",
            refresh_token="OLD",
        ),
        xero_tenant_id="TEN",
    )


async def _seed_connection_and_contact() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a connection + a brand-new contact (no external_id).

    Returns ``(tenant, conn_id, contact_id)``.
    """
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
        contact = Contact(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            company_id=company.id,
            name=f"Push Co {uuid.uuid4().hex[:6]}",
            contact_type=ContactType.CUSTOMER,
        )
        session.add(conn)
        session.add(contact)
        await session.commit()
        return tenant_id, conn.id, contact.id


@respx.mock
async def test_push_contacts_creates_remote_and_records_external_id() -> None:
    tenant_id, conn_id, contact_id = await _seed_connection_and_contact()

    # Echo a unique ContactID derived from the request body so multiple
    # candidates (test pollution) don't collide on the partial unique
    # index ``(company_id, external_source, external_id) WHERE
    # external_id IS NOT NULL``.
    def _echo(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        outs = []
        for c in body.get("Contacts", []):
            outs.append(
                {
                    "ContactID": f"XC-{uuid.uuid4().hex[:12]}",
                    "Name": c.get("Name"),
                    "UpdatedDateUTC": "2026-04-20T08:00:00",
                }
            )
        return httpx.Response(200, json={"Contacts": outs})

    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.post(XERO_API_BASE + "Contacts").mock(side_effect=_echo)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        client = _make_client()
        try:
            stats = await push_contacts(session, connection=conn, client=client)
        finally:
            await client.aclose()
        await session.commit()

    assert stats.candidates >= 1
    assert stats.pushed >= 1

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        c = await session.get(Contact, contact_id)
        assert c is not None
        assert c.external_id is not None
        assert c.external_id.startswith("XC-")
        assert c.external_source == "xero"

        state = (
            await session.execute(
                select(SyncState).where(SyncState.local_id == c.id)
            )
        ).scalar_one_or_none()
        assert state is not None
        assert state.last_pushed_version == c.version
        assert state.local_id == c.id
        assert state.external_id == c.external_id


def test_detect_conflict_pure_function() -> None:
    """``detect_conflict`` returns True only when both sides moved."""
    from saebooks.models.sync import SyncState as _SS

    s = _SS(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        object_type="invoice",
        external_id="X",
        last_pulled_etag="ETAG-1",
        last_pushed_version=1,
    )
    # Both moved -> conflict.
    assert detect_conflict(state=s, local_version=2, current_remote_etag="ETAG-2") is True
    # Only remote moved.
    assert detect_conflict(state=s, local_version=1, current_remote_etag="ETAG-2") is False
    # Only local moved.
    assert detect_conflict(state=s, local_version=2, current_remote_etag="ETAG-1") is False
    # No state at all.
    assert detect_conflict(state=None, local_version=2, current_remote_etag="X") is False
