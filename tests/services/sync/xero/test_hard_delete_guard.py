"""Test the sync-aware hard-delete guard.

Admins MUST be able to hard-delete synced rows; the guard's job is to
surface a clear warning, not to refuse outright.
``force_sync_override=True`` proceeds.

Postgres-only: uses ``SET LOCAL app.current_tenant`` (RLS GUC), which
SQLite does not support — see ``tests/conftest.py``'s
``postgres_only`` marker.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
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
from saebooks.services.hard_delete import (
    HardDeleteSyncedError,
    check_sync_state_or_force,
    hard_delete_with_audit,
)

pytestmark = pytest.mark.postgres_only


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a synced contact + connection. Returns (tenant, company, contact_id).

    Uses a per-call random external_id so tests don't collide on the
    ``(company_id, external_source, external_id)`` partial unique index.
    """
    external_id = f"C-XERO-{uuid.uuid4().hex[:8]}"

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
        # Set the tenant GUC for RLS-aware writes.
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )

        contact = Contact(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            company_id=company.id,
            name="Synced Contact",
            contact_type=ContactType.CUSTOMER,
            external_id=external_id,
            external_source="xero",
            external_etag="ETAG",
        )
        connection = SyncConnection(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider=SyncProvider.XERO.value,
            external_tenant_id=f"TEN-{uuid.uuid4().hex[:6]}",
            status=SyncConnectionStatus.ACTIVE.value,
        )
        session.add(contact)
        session.add(connection)
        await session.flush()
        state = SyncState(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            connection_id=connection.id,
            object_type="contact",
            external_id=external_id,
            local_id=contact.id,
            last_pulled_etag="ETAG",
            last_pulled_at=datetime.now(UTC),
        )
        session.add(state)
        await session.commit()
        return tenant_id, company.id, contact.id


async def test_check_sync_state_blocks_when_linked_and_no_force() -> None:
    tenant_id, _company_id, contact_id = await _seed()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        with pytest.raises(HardDeleteSyncedError) as exc:
            await check_sync_state_or_force(
                session, contact, table_name="contacts", force=False,
            )
        assert "xero" in exc.value.providers
        # No DB mutations from the guard.
        await session.rollback()


async def test_check_sync_state_passes_when_force_is_true() -> None:
    tenant_id, _company_id, contact_id = await _seed()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        # Should not raise.
        await check_sync_state_or_force(
            session, contact, table_name="contacts", force=True,
        )
        await session.rollback()


async def test_check_sync_state_ignores_non_synced_tables() -> None:
    """``users`` etc. are never sync-eligible; guard must no-op."""
    tenant_id, _company_id, contact_id = await _seed()
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = await session.get(Contact, contact_id)
        # Pretend we're deleting from a non-sync table.
        await check_sync_state_or_force(
            session, contact, table_name="users", force=False,
        )
        await session.rollback()


async def test_hard_delete_with_audit_propagates_force() -> None:
    """End-to-end: hard_delete_with_audit refuses without force, succeeds with."""
    tenant_id, _company_id, contact_id = await _seed()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = await session.get(Contact, contact_id)
        with pytest.raises(HardDeleteSyncedError):
            await hard_delete_with_audit(
                session, contact, "contacts", current_user=None,
                force_sync_override=False,
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        await hard_delete_with_audit(
            session, contact, "contacts", current_user=None,
            force_sync_override=True,
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        gone = await session.get(Contact, contact_id)
        assert gone is None
