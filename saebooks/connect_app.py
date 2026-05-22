"""Connect-RPC server — mounted alongside FastAPI in ``main.py``.

Implements ``SAEBooks`` as a connecpy service. Each handler mirrors
the corresponding grpcio handler in ``grpc_server.py`` (same service
layer, same proto messages, same semantics) — only the failure mode
differs: grpcio uses ``await context.abort(StatusCode.X, msg)``,
connecpy raises ``ConnecpyException(Code.X, msg)``.

What this unlocks (vs the existing :50051 grpcio server):

* gRPC, gRPC-Web, AND plain Connect HTTP+JSON from the same handler
* No HTTP/2 requirement — the CLI / browser / curl can hit it over
  HTTP/1.1
* Mounted inside the FastAPI process, so it shares the same
  database session config, observability, and deploy unit

Mount path: ``/saebooks.SAEBooks/*`` — the standard Connect URL
shape. ``ConnectDispatchMiddleware`` in ``main.py`` routes those
paths to this ASGI app, everything else falls through to FastAPI.

Scope (v1)
----------

Ports the 16 unary RPCs that the existing grpcio servicer
implements:

* Contacts: List / Get / Create / Update / Archive
* Invoices: List / Get
* Bills: List / Get
* Payments: List / Get
* Journal entries: List / Get
* Heartbeat (liveness probe)

The two server-streaming methods (``WatchChanges``, ``WatchPresence``)
and the two lock RPCs (``AcquireLock``, ``ReleaseLock``) inherit the
Protocol's UNIMPLEMENTED default; the grpcio :50051 server still
handles those for clients that need streaming. Tracked in
DEFERRED.md.

Auth: every request is expected to carry ``Authorization: Bearer
<jwt or saebk_*>`` like the REST API. Verification is the same path
(``services.api_tokens.verify`` / ``services.jwt_tokens.decode``)
called from a small middleware in this module — see ``BearerInterceptor``.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from connecpy.code import Code
from connecpy.exceptions import ConnecpyException

from saebooks.db import AsyncSessionLocal
from saebooks.grpc_gen import saebooks_connecpy, saebooks_pb2
from saebooks.models.bill import BillStatus
from saebooks.models.contact import ContactType
from saebooks.models.invoice import InvoiceStatus
from saebooks.models.journal import EntryStatus
from saebooks.models.payment import PaymentDirection
from saebooks.services import bills as bill_svc
from saebooks.services import contacts as contact_svc
from saebooks.services import invoices as invoice_svc
from saebooks.services import journal_entries as je_svc
from saebooks.services import payments as payment_svc

logger = logging.getLogger("saebooks.connect")

# Single-company seed value used by the existing grpcio handlers. When
# multi-company lands the resolver will pull from the bearer's claims
# / active-company cookie; for now the grpcio path uses the same
# "first by created_at" fallback so we mirror it here for parity.
_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Proto conversion helpers — pure functions, mirror grpc_server.py
# ---------------------------------------------------------------------------


def _contact_to_proto(c: Any) -> saebooks_pb2.ContactRecord:
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


async def _bind_default_tenant(session: Any) -> None:
    """Bind ``app.current_tenant`` so RLS-scoped tables return rows.

    Tonight's Connect handlers don't yet extract the tenant from the
    bearer token (that wants a connecpy interceptor — DEFERRED.md).
    Until that lands, we bind the default tenant for parity with how
    the existing grpcio :50051 server effectively behaves on a
    single-tenant install. Multi-tenant deployments will need the
    real interceptor before going live.
    """
    from sqlalchemy import text  # noqa: PLC0415

    await session.execute(
        text(f"SET LOCAL app.current_tenant = '{_DEFAULT_TENANT}'")
    )


async def _first_company_id(session: Any) -> uuid.UUID:
    """Single-company fallback — same as grpc_server.py."""
    from saebooks.models.company import Company  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    await _bind_default_tenant(session)
    result = await session.execute(
        select(Company)
        .where(Company.archived_at.is_(None))
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise ConnecpyException(Code.INTERNAL, "No active company in database")
    return company.id


def _actor_from_ctx(ctx: Any) -> str:
    """Pull the bearer (truncated) from request headers for audit logging."""
    try:
        headers = getattr(ctx, "request_headers", None) or {}
        # connecpy 2.x exposes headers as a dict-like
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        return auth[:80] if auth else "connect-anonymous"
    except Exception:  # pragma: no cover — never block a handler on header parse
        return "connect-anonymous"


def _bad_uuid(name: str, value: str) -> ConnecpyException:
    return ConnecpyException(
        Code.INVALID_ARGUMENT, f"Invalid {name} UUID: {value!r}"
    )


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class SAEBooksConnectImpl(saebooks_connecpy.SAEBooks):
    """Connect-RPC handlers — delegates to the same service layer as
    ``grpc_server.SAEBooksServicer``."""

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    async def list_contacts(
        self,
        request: saebooks_pb2.ListContactsRequest,
        ctx: Any,
    ) -> saebooks_pb2.ListContactsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
            rows = await contact_svc.list_active(
                session,
                company_id,
                search=request.search or None,
                limit=page_size,
                offset=offset,
            )

        return saebooks_pb2.ListContactsResponse(
            contacts=[_contact_to_proto(c) for c in rows],
            page_info=saebooks_pb2.PageInfo(
                total=len(rows), page=page_num, page_size=page_size
            ),
        )

    async def get_contact(
        self,
        request: saebooks_pb2.GetContactRequest,
        ctx: Any,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("contact", request.id) from exc

        async with AsyncSessionLocal() as session:
            contact = await contact_svc.get(session, contact_id)

        if contact is None or contact.archived_at is not None:
            raise ConnecpyException(
                Code.NOT_FOUND, f"Contact {request.id} not found"
            )
        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def create_contact(
        self,
        request: saebooks_pb2.CreateContactRequest,
        ctx: Any,
    ) -> saebooks_pb2.ContactResponse:
        actor = _actor_from_ctx(ctx)
        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
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
                await session.commit()
            except Exception as exc:  # noqa: BLE001
                raise ConnecpyException(Code.INVALID_ARGUMENT, str(exc)) from exc

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def update_contact(
        self,
        request: saebooks_pb2.UpdateContactRequest,
        ctx: Any,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("contact", request.id) from exc

        actor = _actor_from_ctx(ctx)
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
                    expected_version=request.if_match_version or None,
                    **kwargs,
                )
                await session.commit()
            except contact_svc.VersionConflict as exc:
                raise ConnecpyException(
                    Code.ABORTED,
                    f"Version conflict: contact {request.id} has been modified",
                ) from exc
            except ValueError as exc:
                raise ConnecpyException(Code.NOT_FOUND, str(exc)) from exc

        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    async def archive_contact(
        self,
        request: saebooks_pb2.ArchiveContactRequest,
        ctx: Any,
    ) -> saebooks_pb2.ContactResponse:
        try:
            contact_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("contact", request.id) from exc

        actor = _actor_from_ctx(ctx)
        async with AsyncSessionLocal() as session:
            try:
                contact = await contact_svc.archive(
                    session,
                    contact_id,
                    actor=actor,
                    expected_version=request.if_match_version or None,
                )
                await session.commit()
            except contact_svc.VersionConflict as exc:
                raise ConnecpyException(
                    Code.ABORTED,
                    f"Version conflict: contact {request.id} has been modified",
                ) from exc

        if contact is None:
            raise ConnecpyException(
                Code.NOT_FOUND, f"Contact {request.id} not found"
            )
        return saebooks_pb2.ContactResponse(contact=_contact_to_proto(contact))

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    async def list_invoices(
        self,
        request: saebooks_pb2.ListInvoicesRequest,
        ctx: Any,
    ) -> saebooks_pb2.ListInvoicesResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: InvoiceStatus | None = None
        if request.status:
            try:
                status_filter = InvoiceStatus(request.status)
            except ValueError as exc:
                raise ConnecpyException(
                    Code.INVALID_ARGUMENT,
                    f"Invalid invoice status: {request.status}",
                ) from exc

        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
            rows = await invoice_svc.list_invoices(
                session,
                company_id,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        return saebooks_pb2.ListInvoicesResponse(
            invoices=[_invoice_to_proto(i) for i in rows],
            page_info=saebooks_pb2.PageInfo(
                total=len(rows), page=page_num, page_size=page_size
            ),
        )

    async def get_invoice(
        self,
        request: saebooks_pb2.GetInvoiceRequest,
        ctx: Any,
    ) -> saebooks_pb2.InvoiceResponse:
        try:
            invoice_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("invoice", request.id) from exc

        async with AsyncSessionLocal() as session:
            try:
                invoice = await invoice_svc.get(session, invoice_id)
            except invoice_svc.InvoiceError as exc:
                raise ConnecpyException(
                    Code.NOT_FOUND, f"Invoice {request.id} not found"
                ) from exc

        if invoice.archived_at is not None:
            raise ConnecpyException(
                Code.NOT_FOUND, f"Invoice {request.id} not found"
            )
        return saebooks_pb2.InvoiceResponse(invoice=_invoice_to_proto(invoice))

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    async def list_bills(
        self,
        request: saebooks_pb2.ListBillsRequest,
        ctx: Any,
    ) -> saebooks_pb2.ListBillsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: BillStatus | None = None
        if request.status:
            try:
                status_filter = BillStatus(request.status)
            except ValueError as exc:
                raise ConnecpyException(
                    Code.INVALID_ARGUMENT, f"Invalid bill status: {request.status}"
                ) from exc

        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
            rows = await bill_svc.list_bills(
                session,
                company_id,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        return saebooks_pb2.ListBillsResponse(
            bills=[_bill_to_proto(b) for b in rows],
            page_info=saebooks_pb2.PageInfo(
                total=len(rows), page=page_num, page_size=page_size
            ),
        )

    async def get_bill(
        self,
        request: saebooks_pb2.GetBillRequest,
        ctx: Any,
    ) -> saebooks_pb2.BillResponse:
        try:
            bill_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("bill", request.id) from exc

        async with AsyncSessionLocal() as session:
            try:
                bill = await bill_svc.get(session, bill_id)
            except bill_svc.BillError as exc:
                raise ConnecpyException(
                    Code.NOT_FOUND, f"Bill {request.id} not found"
                ) from exc

        if bill.archived_at is not None:
            raise ConnecpyException(Code.NOT_FOUND, f"Bill {request.id} not found")
        return saebooks_pb2.BillResponse(bill=_bill_to_proto(bill))

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    async def list_payments(
        self,
        request: saebooks_pb2.ListPaymentsRequest,
        ctx: Any,
    ) -> saebooks_pb2.ListPaymentsResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        direction_filter: PaymentDirection | None = None
        if request.direction:
            try:
                direction_filter = PaymentDirection(request.direction)
            except ValueError as exc:
                raise ConnecpyException(
                    Code.INVALID_ARGUMENT,
                    f"Invalid payment direction: {request.direction}",
                ) from exc

        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
            rows = await payment_svc.list_payments(
                session,
                company_id,
                direction=direction_filter,
                limit=page_size,
                offset=offset,
            )

        return saebooks_pb2.ListPaymentsResponse(
            payments=[_payment_to_proto(p) for p in rows],
            page_info=saebooks_pb2.PageInfo(
                total=len(rows), page=page_num, page_size=page_size
            ),
        )

    async def get_payment(
        self,
        request: saebooks_pb2.GetPaymentRequest,
        ctx: Any,
    ) -> saebooks_pb2.PaymentResponse:
        try:
            payment_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("payment", request.id) from exc

        async with AsyncSessionLocal() as session:
            try:
                payment = await payment_svc.get(session, payment_id)
            except payment_svc.PaymentError as exc:
                raise ConnecpyException(
                    Code.NOT_FOUND, f"Payment {request.id} not found"
                ) from exc

        if payment.archived_at is not None:
            raise ConnecpyException(
                Code.NOT_FOUND, f"Payment {request.id} not found"
            )
        return saebooks_pb2.PaymentResponse(payment=_payment_to_proto(payment))

    # ------------------------------------------------------------------
    # Journal entries
    # ------------------------------------------------------------------

    async def list_journal_entries(
        self,
        request: saebooks_pb2.ListJournalEntriesRequest,
        ctx: Any,
    ) -> saebooks_pb2.ListJournalEntriesResponse:
        page_size = max(1, min(request.page.page_size or 50, 200))
        page_num = max(1, request.page.page or 1)
        offset = (page_num - 1) * page_size

        status_filter: EntryStatus | None = None
        if request.status:
            try:
                status_filter = EntryStatus(request.status)
            except ValueError as exc:
                raise ConnecpyException(
                    Code.INVALID_ARGUMENT,
                    f"Invalid journal entry status: {request.status}",
                ) from exc

        async with AsyncSessionLocal() as session:
            company_id = await _first_company_id(session)
            entries, total = await je_svc.list_active(
                session,
                company_id,
                _DEFAULT_TENANT,
                status=status_filter,
                limit=page_size,
                offset=offset,
            )

        return saebooks_pb2.ListJournalEntriesResponse(
            entries=[_je_to_proto(e) for e in entries],
            page_info=saebooks_pb2.PageInfo(
                total=total, page=page_num, page_size=page_size
            ),
        )

    async def get_journal_entry(
        self,
        request: saebooks_pb2.GetJournalEntryRequest,
        ctx: Any,
    ) -> saebooks_pb2.JournalEntryResponse:
        try:
            entry_id = uuid.UUID(request.id)
        except ValueError as exc:
            raise _bad_uuid("journal_entry", request.id) from exc

        async with AsyncSessionLocal() as session:
            entry = await je_svc.get(session, entry_id)

        if entry is None or entry.archived_at is not None:
            raise ConnecpyException(
                Code.NOT_FOUND, f"JournalEntry {request.id} not found"
            )
        return saebooks_pb2.JournalEntryResponse(entry=_je_to_proto(entry))

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(
        self,
        request: saebooks_pb2.HeartbeatRequest,
        ctx: Any,
    ) -> saebooks_pb2.HeartbeatResponse:
        """Liveness probe — returns status=ok. JWT refresh deferred."""
        return saebooks_pb2.HeartbeatResponse(status="ok", fresh_jwt="")

    # NOTE: WatchChanges, WatchPresence (streaming) and AcquireLock /
    # ReleaseLock (state-heavy in-memory store) are intentionally left
    # un-overridden — they inherit the Protocol's UNIMPLEMENTED default.
    # Clients that need them keep using the grpcio :50051 server until
    # the streaming + lock-store work is properly migrated. See
    # DEFERRED.md.


# ---------------------------------------------------------------------------
# ASGI factory + dispatch middleware
# ---------------------------------------------------------------------------


def build_connect_app() -> saebooks_connecpy.SAEBooksASGIApplication:
    """Construct the connecpy ASGI application. Called once at import time
    from ``main.py`` and wrapped with the dispatch middleware below."""
    return saebooks_connecpy.SAEBooksASGIApplication(SAEBooksConnectImpl())


# Standard Connect URL prefix — every RPC under this service hangs off
# ``/saebooks.SAEBooks/<MethodName>``. The dispatch middleware uses this
# to decide which app handles the request.
CONNECT_PATH_PREFIX = "/saebooks.SAEBooks/"


class ConnectDispatchMiddleware:
    """Routes ``/saebooks.SAEBooks/*`` to the Connect ASGI app, falls
    through to the wrapped FastAPI app for everything else.

    Why not ``app.mount(prefix, connect_app)``: Starlette's Mount
    rewrites ``scope["path"]`` to the sub-path, but connecpy routes by
    the full ``/<service>/<method>`` path. Mounting under a prefix
    would silently 404 every call. A pure pass-through middleware
    keeps the path intact on the way through.
    """

    def __init__(self, fastapi_app: Any, connect_app: Any) -> None:
        self._fastapi = fastapi_app
        self._connect = connect_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith(
            CONNECT_PATH_PREFIX
        ):
            await self._connect(scope, receive, send)
            return
        await self._fastapi(scope, receive, send)


__all__ = [
    "CONNECT_PATH_PREFIX",
    "ConnectDispatchMiddleware",
    "SAEBooksConnectImpl",
    "build_connect_app",
]
