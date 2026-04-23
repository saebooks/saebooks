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
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.bill import BillStatus
from saebooks.models.payment import PaymentDirection
from saebooks.models.journal import EntryStatus
from saebooks.services import contacts as contact_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import invoices as invoice_svc
from saebooks.services import bills as bill_svc
from saebooks.services import payments as payment_svc
from saebooks.services import journal_entries as je_svc
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


def _invoice_to_proto(inv: Any) -> saebooks_pb2.InvoiceRecord:
    return saebooks_pb2.InvoiceRecord(
        id=str(inv.id),
        number=inv.number or "",
        contact_id=str(inv.contact_id),
        issue_date=inv.issue_date.isoformat() if inv.issue_date else "",
        due_date=inv.due_date.isoformat() if inv.due_date else "",
        status=str(inv.status.value) if inv.status else "",
        total=float(inv.total),
        amount_paid=float(inv.amount_paid),
        version=inv.version or 0,
    )


def _bill_to_proto(b: Any) -> saebooks_pb2.BillRecord:
    return saebooks_pb2.BillRecord(
        id=str(b.id),
        number=b.number or "",
        contact_id=str(b.contact_id),
        issue_date=b.issue_date.isoformat() if b.issue_date else "",
        due_date=b.due_date.isoformat() if b.due_date else "",
        status=str(b.status.value) if b.status else "",
        total=float(b.total),
        amount_paid=float(b.amount_paid),
        version=b.version or 0,
    )


def _payment_to_proto(p: Any) -> saebooks_pb2.PaymentRecord:
    return saebooks_pb2.PaymentRecord(
        id=str(p.id),
        contact_id=str(p.contact_id),
        payment_date=p.payment_date.isoformat() if p.payment_date else "",
        direction=str(p.direction.value) if p.direction else "",
        amount=float(p.amount),
        method=str(p.method.value) if p.method else "",
        reference=p.reference or "",
        version=p.version or 0,
    )


def _je_to_proto(e: Any) -> saebooks_pb2.JournalEntryRecord:
    return saebooks_pb2.JournalEntryRecord(
        id=str(e.id),
        ref=e.ref or "",
        entry_date=e.entry_date.isoformat() if e.entry_date else "",
        description=e.description or "",
        status=str(e.status.value) if e.status else "",
        version=e.version or 0,
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
    # Invoices
    # ------------------------------------------------------------------

    async def ListInvoices(
        self,
        request: saebooks_pb2.ListInvoicesRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ListInvoicesResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: InvoiceStatus | None = None
        if request.status:
            try:
                status_filter = InvoiceStatus(request.status)
            except ValueError:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid invoice status: {request.status}",
                )
                return saebooks_pb2.ListInvoicesResponse()

        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ListInvoicesResponse()

            rows = await invoice_svc.list_invoices(
                session,
                company_id,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        invoices = [_invoice_to_proto(inv) for inv in rows]
        page_info = saebooks_pb2.PageInfo(
            total=len(invoices),
            page=page_num,
            page_size=page_size,
        )
        return saebooks_pb2.ListInvoicesResponse(invoices=invoices, page_info=page_info)

    async def GetInvoice(
        self,
        request: saebooks_pb2.GetInvoiceRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.InvoiceResponse:
        try:
            invoice_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.InvoiceResponse()

        async with AsyncSessionLocal() as session:
            try:
                invoice = await invoice_svc.get(session, invoice_id)
            except invoice_svc.InvoiceError:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Invoice {request.id} not found")
                return saebooks_pb2.InvoiceResponse()

        if invoice.archived_at is not None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Invoice {request.id} not found")
            return saebooks_pb2.InvoiceResponse()

        return saebooks_pb2.InvoiceResponse(invoice=_invoice_to_proto(invoice))

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    async def ListBills(
        self,
        request: saebooks_pb2.ListBillsRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ListBillsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: BillStatus | None = None
        if request.status:
            try:
                status_filter = BillStatus(request.status)
            except ValueError:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid bill status: {request.status}",
                )
                return saebooks_pb2.ListBillsResponse()

        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ListBillsResponse()

            rows = await bill_svc.list_bills(
                session,
                company_id,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        bills = [_bill_to_proto(b) for b in rows]
        page_info = saebooks_pb2.PageInfo(
            total=len(bills),
            page=page_num,
            page_size=page_size,
        )
        return saebooks_pb2.ListBillsResponse(bills=bills, page_info=page_info)

    async def GetBill(
        self,
        request: saebooks_pb2.GetBillRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.BillResponse:
        try:
            bill_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.BillResponse()

        async with AsyncSessionLocal() as session:
            try:
                bill = await bill_svc.get(session, bill_id)
            except bill_svc.BillError:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Bill {request.id} not found")
                return saebooks_pb2.BillResponse()

        if bill.archived_at is not None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Bill {request.id} not found")
            return saebooks_pb2.BillResponse()

        return saebooks_pb2.BillResponse(bill=_bill_to_proto(bill))

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    async def ListPayments(
        self,
        request: saebooks_pb2.ListPaymentsRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ListPaymentsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        direction_filter: PaymentDirection | None = None
        if request.direction:
            try:
                direction_filter = PaymentDirection(request.direction)
            except ValueError:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid payment direction: {request.direction}",
                )
                return saebooks_pb2.ListPaymentsResponse()

        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ListPaymentsResponse()

            rows = await payment_svc.list_payments(
                session,
                company_id,
                direction=direction_filter,
                limit=page_size,
                offset=offset,
            )

        payments = [_payment_to_proto(p) for p in rows]
        page_info = saebooks_pb2.PageInfo(
            total=len(payments),
            page=page_num,
            page_size=page_size,
        )
        return saebooks_pb2.ListPaymentsResponse(payments=payments, page_info=page_info)

    async def GetPayment(
        self,
        request: saebooks_pb2.GetPaymentRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.PaymentResponse:
        try:
            payment_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.PaymentResponse()

        async with AsyncSessionLocal() as session:
            try:
                payment = await payment_svc.get(session, payment_id)
            except payment_svc.PaymentError:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Payment {request.id} not found")
                return saebooks_pb2.PaymentResponse()

        if payment.archived_at is not None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"Payment {request.id} not found")
            return saebooks_pb2.PaymentResponse()

        return saebooks_pb2.PaymentResponse(payment=_payment_to_proto(payment))

    # ------------------------------------------------------------------
    # Journal Entries
    # ------------------------------------------------------------------

    async def ListJournalEntries(
        self,
        request: saebooks_pb2.ListJournalEntriesRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.ListJournalEntriesResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: EntryStatus | None = None
        if request.status:
            try:
                status_filter = EntryStatus(request.status)
            except ValueError:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"Invalid journal entry status: {request.status}",
                )
                return saebooks_pb2.ListJournalEntriesResponse()

        async with AsyncSessionLocal() as session:
            try:
                company_id = await _first_company_id(session)
            except RuntimeError as exc:
                await context.abort(grpc.StatusCode.INTERNAL, str(exc))
                return saebooks_pb2.ListJournalEntriesResponse()

            _default_tenant = uuid.UUID("00000000-0000-0000-0000-000000000001")
            entries, total = await je_svc.list_active(
                session,
                company_id,
                _default_tenant,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        proto_entries = [_je_to_proto(e) for e in entries]
        page_info = saebooks_pb2.PageInfo(
            total=total,
            page=page_num,
            page_size=page_size,
        )
        return saebooks_pb2.ListJournalEntriesResponse(entries=proto_entries, page_info=page_info)

    async def GetJournalEntry(
        self,
        request: saebooks_pb2.GetJournalEntryRequest,
        context: aio.ServicerContext,
    ) -> saebooks_pb2.JournalEntryResponse:
        try:
            entry_id = uuid.UUID(request.id)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid UUID")
            return saebooks_pb2.JournalEntryResponse()

        async with AsyncSessionLocal() as session:
            entry = await je_svc.get(session, entry_id)

        if entry is None or entry.archived_at is not None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"JournalEntry {request.id} not found")
            return saebooks_pb2.JournalEntryResponse()

        return saebooks_pb2.JournalEntryResponse(entry=_je_to_proto(entry))

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
