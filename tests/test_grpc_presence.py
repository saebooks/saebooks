"""Structural and direct-call tests for WatchPresence, AcquireLock, ReleaseLock.

No real gRPC port binding or DB connection is required.  The AcquireLock /
ReleaseLock tests call the servicer methods directly via asyncio (they are
plain async methods that only touch in-memory state).
"""
from __future__ import annotations

import asyncio
import datetime
import unittest.mock as mock


# ---------------------------------------------------------------------------
# Proto message presence
# ---------------------------------------------------------------------------


def test_presence_messages_in_proto() -> None:
    """PresenceRequest, PresenceEvent, AcquireLockRequest, LockResponse importable."""
    from saebooks.grpc_gen import saebooks_pb2  # noqa: PLC0415

    assert hasattr(saebooks_pb2, "PresenceRequest")
    assert hasattr(saebooks_pb2, "PresenceEvent")
    assert hasattr(saebooks_pb2, "AcquireLockRequest")
    assert hasattr(saebooks_pb2, "LockResponse")
    assert hasattr(saebooks_pb2, "ReleaseLockRequest")
    assert hasattr(saebooks_pb2, "ReleaseLockResponse")

    # Confirm they are constructible.
    assert saebooks_pb2.PresenceRequest() is not None
    assert saebooks_pb2.PresenceEvent() is not None
    assert saebooks_pb2.AcquireLockRequest() is not None
    assert saebooks_pb2.LockResponse() is not None
    assert saebooks_pb2.ReleaseLockRequest() is not None
    assert saebooks_pb2.ReleaseLockResponse() is not None


# ---------------------------------------------------------------------------
# Servicer method presence
# ---------------------------------------------------------------------------


def test_servicer_has_watch_presence() -> None:
    """SAEBooksServicer.WatchPresence is callable."""
    from saebooks.grpc_server import SAEBooksServicer  # noqa: PLC0415

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "WatchPresence", None))


def test_servicer_has_acquire_lock() -> None:
    """SAEBooksServicer.AcquireLock is callable."""
    from saebooks.grpc_server import SAEBooksServicer  # noqa: PLC0415

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "AcquireLock", None))


def test_servicer_has_release_lock() -> None:
    """SAEBooksServicer.ReleaseLock is callable."""
    from saebooks.grpc_server import SAEBooksServicer  # noqa: PLC0415

    svc = SAEBooksServicer()
    assert callable(getattr(svc, "ReleaseLock", None))


# ---------------------------------------------------------------------------
# Direct servicer call tests (in-memory, no port, no DB)
# ---------------------------------------------------------------------------


def _make_context() -> mock.MagicMock:
    """Return a minimal mock gRPC servicer context."""
    ctx = mock.MagicMock()
    ctx.cancelled.return_value = False
    return ctx


def test_acquire_lock_basic() -> None:
    """AcquireLock returns acquired=True for a fresh lock."""
    import saebooks.grpc_server as srv  # noqa: PLC0415
    from saebooks.grpc_gen import saebooks_pb2  # noqa: PLC0415

    # Isolate this test with a clean lock store.
    srv._lock_store.clear()

    svc = srv.SAEBooksServicer()
    req = saebooks_pb2.AcquireLockRequest(
        tenant_id="t1",
        entity_type="invoice",
        entity_id="inv-001",
        user_id="alice",
        ttl_seconds=30,
    )
    result = asyncio.get_event_loop().run_until_complete(
        svc.AcquireLock(req, _make_context())
    )
    assert isinstance(result, saebooks_pb2.LockResponse)
    assert result.acquired is True
    assert result.locked_by == "alice"
    assert result.expires_at != ""


def test_acquire_lock_conflict() -> None:
    """Second user cannot acquire a lock held by the first."""
    import saebooks.grpc_server as srv  # noqa: PLC0415
    from saebooks.grpc_gen import saebooks_pb2  # noqa: PLC0415

    srv._lock_store.clear()

    svc = srv.SAEBooksServicer()
    req_alice = saebooks_pb2.AcquireLockRequest(
        tenant_id="t2",
        entity_type="bill",
        entity_id="bill-007",
        user_id="alice",
        ttl_seconds=60,
    )
    req_bob = saebooks_pb2.AcquireLockRequest(
        tenant_id="t2",
        entity_type="bill",
        entity_id="bill-007",
        user_id="bob",
        ttl_seconds=60,
    )
    loop = asyncio.get_event_loop()
    r1 = loop.run_until_complete(svc.AcquireLock(req_alice, _make_context()))
    assert r1.acquired is True

    r2 = loop.run_until_complete(svc.AcquireLock(req_bob, _make_context()))
    assert r2.acquired is False
    assert r2.locked_by == "alice"


def test_release_lock() -> None:
    """Acquire, release, then re-acquire succeeds."""
    import saebooks.grpc_server as srv  # noqa: PLC0415
    from saebooks.grpc_gen import saebooks_pb2  # noqa: PLC0415

    srv._lock_store.clear()

    svc = srv.SAEBooksServicer()
    loop = asyncio.get_event_loop()

    acquire_req = saebooks_pb2.AcquireLockRequest(
        tenant_id="t3",
        entity_type="payment",
        entity_id="pay-42",
        user_id="carol",
        ttl_seconds=30,
    )
    release_req = saebooks_pb2.ReleaseLockRequest(
        tenant_id="t3",
        entity_type="payment",
        entity_id="pay-42",
        user_id="carol",
    )

    r1 = loop.run_until_complete(svc.AcquireLock(acquire_req, _make_context()))
    assert r1.acquired is True

    rel = loop.run_until_complete(svc.ReleaseLock(release_req, _make_context()))
    assert isinstance(rel, saebooks_pb2.ReleaseLockResponse)
    assert rel.released is True

    # Re-acquire after release.
    r2 = loop.run_until_complete(svc.AcquireLock(acquire_req, _make_context()))
    assert r2.acquired is True
