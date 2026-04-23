"""Tests for the standalone gRPC server (SAEBooksServicer).

Uses a real grpc.aio.server started on a random port in each test,
with mocked service layer calls so no live database is needed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, UTC
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest
from grpc import aio

from saebooks.grpc_gen import saebooks_pb2, saebooks_pb2_grpc
from saebooks.grpc_server import SAEBooksServicer, serve


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_contact(
    *,
    name: str = "Test Contact",
    email: str = "test@example.com",
    phone: str = "0400000000",
    version: int = 1,
) -> MagicMock:
    """Return a mock Contact ORM object."""
    c = MagicMock()
    c.id = uuid.uuid4()
    c.name = name
    c.email = email
    c.phone = phone
    c.version = version
    c.archived_at = None
    c.updated_at = datetime.now(UTC)
    return c


def _make_change_log_row(*, cursor: int = 1, entity: str = "contact") -> MagicMock:
    row = MagicMock()
    row.id = cursor
    row.entity = entity
    row.entity_id = uuid.uuid4()
    row.op = "create"
    row.version = 1
    row.payload = {"id": str(uuid.uuid4()), "name": "Test", "version": 1}
    return row


@pytest.fixture
async def grpc_channel():
    """Start a real aio.server with SAEBooksServicer and yield a stub channel.

    The server binds to a random port on localhost.
    """
    server = aio.server()
    saebooks_pb2_grpc.add_SAEBooksServicer_to_server(SAEBooksServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = aio.insecure_channel(f"127.0.0.1:{port}")
    stub = saebooks_pb2_grpc.SAEBooksStub(channel)

    yield stub

    await channel.close()
    await server.stop(grace=0)


# ---------------------------------------------------------------------------
# Helper: mock the DB session context manager used by the servicer
# ---------------------------------------------------------------------------


def _patch_session(company_id: uuid.UUID | None = None, **_kwargs: Any):
    """Patch AsyncSessionLocal so no real DB connection is attempted."""

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            result = MagicMock()
            if company_id is not None:
                company = MagicMock()
                company.id = company_id
                company.archived_at = None
                result.scalars.return_value.first.return_value = company
            else:
                result.scalars.return_value.first.return_value = None
            return result

    return patch("saebooks.grpc_server.AsyncSessionLocal", return_value=_FakeSession())


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def test_heartbeat(grpc_channel: saebooks_pb2_grpc.SAEBooksStub) -> None:
    """Heartbeat always returns status='ok'."""
    response = await grpc_channel.Heartbeat(
        saebooks_pb2.HeartbeatRequest(licence_jwt="irrelevant")
    )
    assert response.status == "ok"


# ---------------------------------------------------------------------------
# ListContacts
# ---------------------------------------------------------------------------


async def test_list_contacts_returns_data(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """ListContacts returns a list of ContactRecords (may be empty on no DB)."""
    cid = uuid.uuid4()
    contacts = [_make_contact(name="Alice"), _make_contact(name="Bob")]

    with (
        _patch_session(company_id=cid),
        patch(
            "saebooks.grpc_server.contact_svc.list_active",
            new=AsyncMock(return_value=contacts),
        ),
    ):
        response = await grpc_channel.ListContacts(
            saebooks_pb2.ListContactsRequest(
                page=saebooks_pb2.PageRequest(page=1, page_size=10)
            )
        )

    assert len(response.contacts) == 2
    names = {c.name for c in response.contacts}
    assert names == {"Alice", "Bob"}
    assert response.page_info.page == 1


async def test_list_contacts_empty(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """ListContacts with no rows returns empty list."""
    cid = uuid.uuid4()
    with (
        _patch_session(company_id=cid),
        patch(
            "saebooks.grpc_server.contact_svc.list_active",
            new=AsyncMock(return_value=[]),
        ),
    ):
        response = await grpc_channel.ListContacts(
            saebooks_pb2.ListContactsRequest()
        )
    assert response.contacts == []


async def test_list_contacts_no_company(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """ListContacts returns INTERNAL when no company exists."""
    with _patch_session(company_id=None):
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await grpc_channel.ListContacts(saebooks_pb2.ListContactsRequest())
    assert exc_info.value.code() == grpc.StatusCode.INTERNAL


# ---------------------------------------------------------------------------
# GetContact
# ---------------------------------------------------------------------------


async def test_get_contact_found(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    contact = _make_contact(name="Charlie", email="charlie@example.com")
    with patch(
        "saebooks.grpc_server.contact_svc.get",
        new=AsyncMock(return_value=contact),
    ):
        response = await grpc_channel.GetContact(
            saebooks_pb2.GetContactRequest(id=str(contact.id))
        )
    assert response.contact.name == "Charlie"
    assert response.contact.id == str(contact.id)


async def test_get_contact_not_found(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    with patch(
        "saebooks.grpc_server.contact_svc.get",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await grpc_channel.GetContact(
                saebooks_pb2.GetContactRequest(id=str(uuid.uuid4()))
            )
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


async def test_get_contact_invalid_uuid(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await grpc_channel.GetContact(
            saebooks_pb2.GetContactRequest(id="not-a-uuid")
        )
    assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# CreateContact
# ---------------------------------------------------------------------------


async def test_create_contact(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    cid = uuid.uuid4()
    created = _make_contact(name="Dave", email="dave@example.com")
    with (
        _patch_session(company_id=cid),
        patch(
            "saebooks.grpc_server.contact_svc.create",
            new=AsyncMock(return_value=created),
        ),
    ):
        response = await grpc_channel.CreateContact(
            saebooks_pb2.CreateContactRequest(
                name="Dave", email="dave@example.com", phone="0411000000"
            )
        )
    assert response.contact.name == "Dave"
    assert response.contact.id != ""


# ---------------------------------------------------------------------------
# UpdateContact
# ---------------------------------------------------------------------------


async def test_update_contact_ok(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    original = _make_contact(name="Eve", version=1)
    updated = _make_contact(name="Eve Updated", version=2)
    updated.id = original.id
    with patch(
        "saebooks.grpc_server.contact_svc.update",
        new=AsyncMock(return_value=updated),
    ):
        response = await grpc_channel.UpdateContact(
            saebooks_pb2.UpdateContactRequest(
                id=str(original.id), name="Eve Updated", if_match_version=1
            )
        )
    assert response.contact.name == "Eve Updated"
    assert response.contact.version == 2


async def test_update_contact_version_conflict(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """UpdateContact returns ABORTED on version conflict."""
    from saebooks.services.contacts import VersionConflict

    stale_contact = _make_contact(version=5)
    with patch(
        "saebooks.grpc_server.contact_svc.update",
        new=AsyncMock(side_effect=VersionConflict(stale_contact)),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await grpc_channel.UpdateContact(
                saebooks_pb2.UpdateContactRequest(
                    id=str(uuid.uuid4()), name="X", if_match_version=1
                )
            )
    assert exc_info.value.code() == grpc.StatusCode.ABORTED


# ---------------------------------------------------------------------------
# ArchiveContact
# ---------------------------------------------------------------------------


async def test_archive_contact_ok(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    c = _make_contact(version=2)
    c.archived_at = datetime.now(UTC)
    with patch(
        "saebooks.grpc_server.contact_svc.archive",
        new=AsyncMock(return_value=c),
    ):
        response = await grpc_channel.ArchiveContact(
            saebooks_pb2.ArchiveContactRequest(id=str(c.id), if_match_version=2)
        )
    assert response.contact.id == str(c.id)


async def test_archive_contact_not_found(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    with patch(
        "saebooks.grpc_server.contact_svc.archive",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await grpc_channel.ArchiveContact(
                saebooks_pb2.ArchiveContactRequest(id=str(uuid.uuid4()), if_match_version=1)
            )
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


# ---------------------------------------------------------------------------
# WatchChanges
# ---------------------------------------------------------------------------


async def test_watch_changes_streams_events(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """WatchChanges yields ChangeEvent rows and then hangs (we cancel after 1)."""
    rows = [_make_change_log_row(cursor=1), _make_change_log_row(cursor=2)]
    call_count = 0

    async def _mock_since(session, *, cursor, limit, entity=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [r for r in rows if r.id > cursor]
        # Second poll returns empty — but we'll have cancelled by then
        raise asyncio.CancelledError()

    with patch(
        "saebooks.grpc_server.change_log_svc.since",
        side_effect=_mock_since,
    ):
        events = []
        call = grpc_channel.WatchChanges(
            saebooks_pb2.WatchChangesRequest(cursor=0)
        )
        try:
            async for event in call:
                events.append(event)
                if len(events) >= 2:
                    call.cancel()
                    break
        except grpc.aio.AioRpcError:
            pass  # cancelled — expected

    assert len(events) == 2
    assert events[0].cursor == 1
    assert events[1].cursor == 2
    assert events[0].entity == "contact"
    assert events[0].op == "create"
    # payload_json should be valid JSON
    payload = json.loads(events[0].payload_json)
    assert "name" in payload


async def test_watch_changes_empty_poll(
    grpc_channel: saebooks_pb2_grpc.SAEBooksStub,
) -> None:
    """WatchChanges with no rows waits then yields when rows appear."""
    poll_count = 0

    async def _mock_since(session, *, cursor, limit, entity=None):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return []  # first poll: nothing
        # After sleep, produce one event
        return [_make_change_log_row(cursor=10)]

    with (
        patch("saebooks.grpc_server.change_log_svc.since", side_effect=_mock_since),
        patch("saebooks.grpc_server.asyncio.sleep", new=AsyncMock()),  # skip real sleep
    ):
        events = []
        call = grpc_channel.WatchChanges(
            saebooks_pb2.WatchChangesRequest(cursor=0)
        )
        try:
            async for event in call:
                events.append(event)
                call.cancel()
                break
        except grpc.aio.AioRpcError:
            pass

    assert len(events) == 1
    assert events[0].cursor == 10
