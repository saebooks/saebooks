"""SAE Books gRPC server — standalone runnable.

Implements the ``SAEBooks`` protobuf service defined in
``saebooks/proto/saebooks.proto``.  Delegates to the same service
layer used by the FastAPI REST routers so there is a single source
of truth for business logic.

Usage::

    python -m saebooks.grpc_server             # default port 50051
    python -m saebooks.grpc_server --port 50052

The server is intentionally standalone so it can be started without
FastAPI (useful for desktop integration tests).  ``saebooks/main.py``
will start it inside the FastAPI lifespan in a later cycle.

Auth: reads the ``authorization`` gRPC metadata header and passes it
through for logging.  Full JWT verification will be wired in a
follow-up once the portal JWT JWKS is in scope.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import grpc
from grpc import aio

from saebooks.db import AsyncSessionLocal
from saebooks.grpc_gen import saebooks_pb2, saebooks_pb2_grpc
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import contacts as contact_svc
from saebooks.services import change_log as change_log_svc
from sqlalchemy import select

logger = logging.getLogger("saebooks.grpc_server")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: Any) -> uuid.UUID:
    """Community edition: single-company — pick the first active company."""
    result = await session.execute(
        select(Company)
        .where(Company.archived_at.is_(None))
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise RuntimeError("No active company in database")
    return company.id


def _contact_to_proto(c: Contact) -> saebooks_pb2.ContactRecord:
    return saebooks_pb2.ContactRecord(
        id=str(c.id),
        name=c.name or "",
        email=c.email or "",
        phone=c.phone or "",
        version=c.version or 0,
        updated_at=c.updated_at.isoformat() if c.updated_at else "",
    )


def _extract_actor(context: aio.ServicerContext) -> str:
    """Pull the ``authorization`` metadata value for audit logging."""
    for key, value in context.invocation_metadata():
        if key == "authorization":
            return value[:80]  # truncate for log safety
    return "grpc-anonymous"


# ---------------------------------------------------------------------------
# Servicer
# ---------------------------------------------------------------------------


class SAEBooksServicer(saebooks_pb2_grpc.SAEBooksServicer):
    """Async gRPC servicer — delegates to services/contacts.py."""

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    async def ListContacts(
        self,
        request: saebooks_pb2.ListContactsRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ListContactsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ListContactsResponse()

            rows = await contact_svc.list_active(
                session,
                company_id,
                search=request.search or None,
                limit=page_size,
                offset=offset,
            )

        contacts = [_contact_to_proto(c) for c in rows]
        page_info = saebooks_pb2.PageInfo(
            total=len(contacts),  # approximate; full count query deferred
            page=page_num,
            page_size=page_size,
        )
        return saebooks_pb2.ListContactsResponse(
            contacts=contacts,
            page_info=page_info,
        )

    async def GetContact(
        self,
        request: saebooks_pb2.GetContactRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.ContactResponse()

        async with AsyncSessionLocal() as session:
            contact = await contact_svc.get(session, contact_id)

        if contact is None or contact.archived_at is not None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Contact {request.id} not found")
            return saebooks_pb2.ContactResponse()

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def CreateContact(
        self,
        request: saebooks_pb2.CreateContactRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ContactResponse:
        actor = _extract_actor(context)
        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ContactResponse()

            try:
                contact = await contact_svc.create(
                    session,
                    company_id,
                    actor=actor,
                    name=request.name,
                    contact_type=ContactType.BOTH,
                    email=request.email or None,
                    phone=request.phone or None,
                )
            except Exception as exc:  # noqa: BLE001
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                return saebooks_pb2.ContactResponse()

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def UpdateContact(
        self,
        request: saebooks_pb2.UpdateContactRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.ContactResponse()

        actor = _extract_actor(context)
        kwargs: dict[str, Any] = {}
        if request.name:
            kwargs["name"] = request.name
        if request.email:
            kwargs["email"] = request.email
        if request.phone:
            kwargs["phone"] = request.phone

        async with AsyncSessionLocal() as session:
            try:
                contact = await contact_svc.update(
                    session,
                    contact_id,
                    actor=actor,
                    expected_version=request.if_match_version if request.if_match_version else None,
                    **kwargs,
                )
            except contact_svc.VersionConflict:
                await context.abort(
                    grpc.StatusCode.ABORTED,
                    f"Version conflict: contact {request.id} has been modified",
                )
                return saebooks_pb2.ContactResponse()
            except ValueError as exc:
                await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
                return saebooks_pb2.ContactResponse()

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def ArchiveContact(
        self,
        request: saebooks_pb2.ArchiveContactRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.ContactResponse()

        actor = _extract_actor(context)
        async with AsyncSessionLocal() as session:
            try:
                contact = await contact_svc.archive(
                    session,
                    contact_id,
                    actor=actor,
                    expected_version=request.if_match_version if request.if_match_version else None,
                )
            except contact_svc.VersionConflict:
                await context.abort(
                    grpc.StatusCode.ABORTED,
                    f"Version conflict: contact {request.id} has been modified",
                )
                return saebooks_pb2.ContactResponse()

        if contact is None:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Contact {request.id} not found"
            )
            return saebooks_pb2.ContactResponse()

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    # ------------------------------------------------------------------
    # Change stream
    # ------------------------------------------------------------------

    async def WatchChanges(
        self,
        request: saebooks_pb2.WatchChangesRequest,
        context: aio.ServicerContext,
    ) -> AsyncIterator[saebooks_pb2.ChangeEvent]:
        """Stream change_log rows from ``cursor`` onward, polling every 2 s."""
        cursor = request.cursor
        poll_interval = 2.0
        batch_size = 50

        while not context.cancelled():
            async with AsyncSessionLocal() as session:
                rows = await change_log_svc.since(
                    session, cursor=cursor, limit=batch_size
                )

            for row in rows:
                cursor = row.id
                yield saebooks_pb2.ChangeEvent(
                    entity=row.entity,
                    entity_id=str(row.entity_id),
                    op=row.op,
                    cursor=row.id,
                    payload_json=json.dumps(row.payload),
                    version=row.version,
                )

            if not rows:
                try:
                    await asyncio.sleep(poll_interval)
                except asyncio.CancelledError:
                    break

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def Heartbeat(
        self,
        request: saebooks_pb2.HeartbeatRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.HeartbeatResponse:
        """Basic liveness ping — returns status=ok. JWT refresh deferred."""
        return saebooks_pb2.HeartbeatResponse(status="ok", fresh_jwt="")

    # ------------------------------------------------------------------
    # Presence / locking — UNIMPLEMENTED stubs
    # ------------------------------------------------------------------

    # WatchPresence, AcquireLock, ReleaseLock are not in the proto yet;
    # they will be added in a follow-up cycle once the presence schema
    # is defined.


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def serve(port: int = 50051) -> aio.Server:
    """Create and start the async gRPC server.

    Returns the server instance so the caller can await ``server.wait_for_termination()``.
    """
    server = aio.server()
    saebooks_pb2_grpc.add_SAEBooksServicer_to_server(SAEBooksServicer(), server)
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    logger.info("gRPC server listening on %s", listen_addr)
    return server


async def _main(port: int = 50051) -> None:
    server = await serve(port)
    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop(grace=5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SAE Books gRPC server")
    parser.add_argument("--port", type=int, default=50051, help="Port to listen on")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(_main(args.port))
