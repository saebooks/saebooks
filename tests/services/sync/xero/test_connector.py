"""End-to-end smoke test for ``connector.sync_xero``.

The connector wires pull and push together. This test mocks Xero
HTTP via respx and proves the round-trip:

    seed connection (with encrypted client_id/secret/refresh) +
    seed one local contact with no external_id, on a brand-new tenant
    -> sync_xero()
    -> Xero Contacts GET returns one new remote contact (pull)
    -> Xero Contacts POST echoes the local contact's payload (push)
    -> assertions:
       * the remote contact is now in our Contact table
       * the local contact now has an external_id
       * a sync_state row exists for both, with the right ``origin``:
         pulled row -> 'remote'; pushed row -> 'synced'
       * connection.last_pulled_at / last_pushed_at advanced
       * refresh token was rotated and re-encrypted on the connection
       * sync_audit_log carries a "sync run complete" row
       * the just-pulled contact is NOT re-pushed (regression guard;
         the push selector is now origin-aware so rows with
         origin='remote' AND version=1 are excluded by construction)

This is the cheapest test that proves the full happy path works.

Uses an isolated tenant_id (random UUID) and its own Company so the
seed-company's pre-existing POSTED invoices don't get swept into the
push pass and slow the test to a crawl.

Postgres-only: ``connector.sync_xero`` runs ``SHOW app.current_tenant``
and this test uses ``SET LOCAL app.current_tenant`` (RLS GUC), neither
of which SQLite supports — see ``tests/conftest.py``'s
``postgres_only`` marker.
"""
from __future__ import annotations

import json as _json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.sync import (
    SyncAuditLog,
    SyncConnection,
    SyncConnectionStatus,
    SyncProvider,
    SyncState,
    SyncStateOrigin,
)
from saebooks.models.tenant import Tenant
from saebooks.services.crypto import decrypt_field, encrypt_field
from saebooks.services.sync.xero.client import XERO_API_BASE
from saebooks.services.sync.xero.connector import sync_xero
from saebooks.services.sync.xero.token import XERO_TOKEN_URL

pytestmark = pytest.mark.postgres_only


def _ok_refresh() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ACCESS",
            "refresh_token": "ROTATED-REFRESH",
            "expires_in": 1800,
        },
    )


async def _seed_isolated() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed (tenant_id, company_id, conn_id, local_contact_id) on a fresh tenant.

    A random tenant_id keeps this test out of the seed company's
    POSTED-invoice backlog (otherwise push_invoices iterates ~200 rows).
    """
    tenant_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        # RLS policies require the GUC for inserts on tenant-scoped tables.
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        # Companies have a FK to tenants; seed the tenants row first.
        tenant = Tenant(
            id=tenant_id,
            name=f"Sync Test Tenant {uuid.uuid4().hex[:6]}",
            slug=f"sync-test-{uuid.uuid4().hex[:8]}",
            edition="enterprise",
        )
        session.add(tenant)
        await session.flush()
        company = Company(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=f"Sync Test Co {uuid.uuid4().hex[:6]}",
            base_currency="AUD",
            fin_year_start_month=7,
            version=1,
        )
        session.add(company)
        await session.flush()

        conn = SyncConnection(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            provider=SyncProvider.XERO.value,
            external_tenant_id=f"TEN-{uuid.uuid4().hex[:6]}",
            oauth_client_id_ciphertext=encrypt_field("cid").encode("ascii"),
            oauth_client_secret_ciphertext=encrypt_field("csecret").encode("ascii"),
            oauth_refresh_token_ciphertext=encrypt_field("OLD-REFRESH").encode("ascii"),
            status=SyncConnectionStatus.ACTIVE.value,
        )
        contact = Contact(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            company_id=company.id,
            name=f"Local Push Co {uuid.uuid4().hex[:6]}",
            contact_type=ContactType.CUSTOMER,
        )
        session.add(conn)
        session.add(contact)
        await session.commit()
        return tenant_id, company.id, conn.id, contact.id


@respx.mock
async def test_sync_xero_round_trips_contact_pull_and_push() -> None:
    tenant_id, company_id, conn_id, local_contact_id = await _seed_isolated()

    pulled_external_id = f"XC-PULL-{uuid.uuid4().hex[:8]}"

    # Token refresh.
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())

    # Contacts GET (pull) — return one new upstream row.
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": pulled_external_id,
                        "Name": "Pulled From Xero",
                        "EmailAddress": "ar@pulled.example",
                        "IsCustomer": True,
                        "IsSupplier": False,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-15T12:00:00",
                    }
                ]
            },
        )
    )

    # Invoices GET (pull) — empty result for both ACCREC and ACCPAY.
    respx.get(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )

    # Contacts POST (push) — count the calls + echo back a fresh ContactID.
    push_call_count = {"n": 0}

    def _echo_contacts(request: httpx.Request) -> httpx.Response:
        push_call_count["n"] += 1
        body = _json.loads(request.content)
        outs = []
        for c in body.get("Contacts", []):
            outs.append(
                {
                    "ContactID": f"XC-PUSH-{uuid.uuid4().hex[:12]}",
                    "Name": c.get("Name"),
                    "UpdatedDateUTC": "2026-04-20T08:00:00",
                }
            )
        return httpx.Response(200, json={"Contacts": outs})

    respx.post(XERO_API_BASE + "Contacts").mock(side_effect=_echo_contacts)

    # Invoices POST — should not be hit (no POSTED invoices on this tenant).
    inv_route = respx.post(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        report = await sync_xero(session, connection=conn, company_id=company_id)
        await session.commit()

    # Sanity on the report.
    assert report.error is None, report.error
    assert report.contacts_pull.fetched == 1
    assert report.contacts_pull.upserted == 1
    assert report.contacts_push.candidates >= 1
    assert report.contacts_push.pushed >= 1
    # Regression guard: only the *new local* contact should be pushed,
    # not the just-pulled one. Push call count must equal pushed count.
    assert push_call_count["n"] == report.contacts_push.pushed
    # No invoices on this isolated tenant -> no invoice pushes.
    assert inv_route.call_count == 0

    # Verify DB state.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )

        # Pulled contact landed and KEPT its external_id (not overwritten
        # by a subsequent push).
        pulled = (
            await session.execute(
                select(Contact).where(Contact.external_id == pulled_external_id)
            )
        ).scalar_one()
        assert pulled.name == "Pulled From Xero"
        assert pulled.external_source == "xero"

        # Pushed contact got a real external_id.
        local = await session.get(Contact, local_contact_id)
        assert local is not None
        assert local.external_id is not None
        assert local.external_id.startswith("XC-PUSH-")
        assert local.external_source == "xero"

        # sync_state rows for both, with proper version stamping.
        states = list(
            (
                await session.execute(
                    select(SyncState).where(SyncState.connection_id == conn_id)
                )
            ).scalars()
        )
        ext_ids = {s.external_id for s in states}
        assert pulled_external_id in ext_ids
        assert local.external_id in ext_ids
        # Pulled contact's sync_state must be origin='remote' with
        # last_pushed_version=NULL — the push selector excludes
        # (origin='remote' AND version=1) by construction, which is
        # what stops the re-push (no version-stamping workaround).
        pulled_state = next(
            s for s in states if s.external_id == pulled_external_id
        )
        assert pulled_state.origin == SyncStateOrigin.REMOTE.value
        assert pulled_state.last_pushed_version is None
        # Locally-created contact got pushed -> origin='synced',
        # last_pushed_version=local.version.
        local_state = next(
            s for s in states if s.external_id == local.external_id
        )
        assert local_state.origin == SyncStateOrigin.SYNCED.value
        assert local_state.last_pushed_version == local.version

        # Connection: refresh rotated, watermarks advanced, status ACTIVE.
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        assert conn.status == SyncConnectionStatus.ACTIVE.value
        assert conn.last_pulled_at is not None
        assert conn.last_pushed_at is not None
        assert conn.last_error is None
        assert conn.oauth_refresh_token_ciphertext is not None
        rotated_plain = decrypt_field(
            conn.oauth_refresh_token_ciphertext.decode("ascii")
        )
        assert rotated_plain == "ROTATED-REFRESH"

        # Audit log: a per-run summary row + per-object rows.
        audit = list(
            (
                await session.execute(
                    select(SyncAuditLog).where(SyncAuditLog.connection_id == conn_id)
                )
            ).scalars()
        )
        assert any(a.message == "sync run complete" for a in audit)
        assert any(a.outcome == "ok" for a in audit)


@respx.mock
async def test_sync_xero_does_not_repush_freshly_pulled_contact() -> None:
    """Tight regression test: pull a contact, then run a SECOND cycle
    with no upstream changes. The freshly-pulled contact must NOT be
    re-pushed (otherwise its external_id gets overwritten and the link
    breaks).
    """
    tenant_id, company_id, conn_id, _local_contact_id = await _seed_isolated()
    pulled_external_id = f"XC-PULL-{uuid.uuid4().hex[:8]}"

    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": pulled_external_id,
                        "Name": "Stable Pulled",
                        "IsCustomer": True,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-15T12:00:00",
                    }
                ]
            },
        )
    )
    respx.get(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )
    push_calls = {"n": 0}

    def _echo_contacts(request: httpx.Request) -> httpx.Response:
        push_calls["n"] += 1
        body = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": f"XC-PUSH-{uuid.uuid4().hex[:12]}",
                        "Name": c.get("Name"),
                        "UpdatedDateUTC": "2026-04-20T08:00:00",
                    }
                    for c in body.get("Contacts", [])
                ]
            },
        )

    respx.post(XERO_API_BASE + "Contacts").mock(side_effect=_echo_contacts)
    respx.post(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        await sync_xero(session, connection=conn, company_id=company_id)
        await session.commit()

    pushes_after_first = push_calls["n"]

    # Second cycle — no upstream changes (same Contacts response). The
    # freshly-pulled contact should NOT be in the push candidates.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        report = await sync_xero(session, connection=conn, company_id=company_id)
        await session.commit()

    pushes_after_second = push_calls["n"]
    # Strict: zero NEW push calls in the second cycle (the local-only
    # contact was already pushed in the first cycle, and the
    # just-pulled contact must not be re-pushed).
    assert pushes_after_second == pushes_after_first
    assert report.contacts_push.pushed == 0


@respx.mock
async def test_origin_state_transitions_explicit() -> None:
    """Pin the SyncStateOrigin state machine end-to-end.

    Three rows go through the connector and we assert the canonical
    end-state of each:

    * a locally-created contact with no external_id          -> 'synced'
      (after push, the new sync_state row carries SYNCED)
    * a remote contact arriving via pull                     -> 'remote'
      (insert; last_pushed_version stays NULL — no workaround)
    * a contact that was pulled, then has its name edited
      locally, then re-syncs                                 -> 'synced'
      (REMOTE -> SYNCED transition fires on first push)
    """
    tenant_id, company_id, conn_id, local_contact_id = await _seed_isolated()
    pulled_external_id = f"XC-PULL-{uuid.uuid4().hex[:8]}"

    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": pulled_external_id,
                        "Name": "Pulled Then Edited",
                        "IsCustomer": True,
                        "ContactStatus": "ACTIVE",
                        "UpdatedDateUTC": "2026-04-15T12:00:00",
                    }
                ]
            },
        )
    )
    respx.get(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )

    def _echo(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "Contacts": [
                    {
                        "ContactID": f"XC-PUSH-{uuid.uuid4().hex[:12]}",
                        "Name": c.get("Name"),
                        "UpdatedDateUTC": "2026-04-20T08:00:00",
                    }
                    for c in body.get("Contacts", [])
                ]
            },
        )

    respx.post(XERO_API_BASE + "Contacts").mock(side_effect=_echo)
    respx.post(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []})
    )

    # First cycle: local contact gets pushed; remote contact gets pulled.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        await sync_xero(session, connection=conn, company_id=company_id)
        await session.commit()

    # After cycle 1: pulled is REMOTE, local is SYNCED.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        states = list(
            (
                await session.execute(
                    select(SyncState).where(SyncState.connection_id == conn_id)
                )
            ).scalars()
        )
        pulled_state = next(
            s for s in states if s.external_id == pulled_external_id
        )
        assert pulled_state.origin == SyncStateOrigin.REMOTE.value
        assert pulled_state.last_pushed_version is None

        local = await session.get(Contact, local_contact_id)
        assert local is not None and local.external_id is not None
        local_state = next(
            s for s in states if s.external_id == local.external_id
        )
        assert local_state.origin == SyncStateOrigin.SYNCED.value
        assert local_state.last_pushed_version == local.version

        # Now bump the pulled contact's local version (simulating an
        # operator edit). Going through the service layer would also
        # write a change_log row; for this test we mutate directly and
        # bump the version counter ourselves.
        pulled = (
            await session.execute(
                select(Contact).where(Contact.external_id == pulled_external_id)
            )
        ).scalar_one()
        pulled.name = "Pulled Then Edited LOCAL"
        pulled.version = pulled.version + 1
        await session.commit()

    # Second cycle: the edited remote-origin contact should now be
    # picked up by the push selector via (origin='remote' AND version > 1).
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        conn = await session.get(SyncConnection, conn_id)
        assert conn is not None
        report = await sync_xero(session, connection=conn, company_id=company_id)
        await session.commit()

    # The pulled contact should have flipped REMOTE -> SYNCED on push.
    # Note: the push echo handler returns a fresh ContactID, so the
    # contact's external_id changes; we look it up by local_id.
    assert report.contacts_push.pushed >= 1
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        pulled = (
            await session.execute(
                select(Contact).where(
                    Contact.name == "Pulled Then Edited LOCAL",
                    Contact.tenant_id == tenant_id,
                )
            )
        ).scalar_one()
        # Find the state row by local_id (external_id was rewritten by
        # the echo'd push response).
        post_state = (
            await session.execute(
                select(SyncState).where(
                    SyncState.connection_id == conn_id,
                    SyncState.local_id == pulled.id,
                    SyncState.external_id == pulled.external_id,
                )
            )
        ).scalar_one()
        assert post_state.origin == SyncStateOrigin.SYNCED.value
        assert post_state.last_pushed_version == pulled.version
