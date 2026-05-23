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

import contextvars
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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
# Auth context
# ---------------------------------------------------------------------------
#
# Connecpy doesn't expose a per-request "state" slot on RequestContext, so
# the auth interceptor stamps these contextvars and handlers read them.
# ContextVars are async-task-local and properly isolated per request under
# uvicorn's asyncio runtime.

_current_user_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "saebooks_connect_user_id", default=None
)
_current_tenant_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar(
    "saebooks_connect_tenant_id", default=_DEFAULT_TENANT
)
_current_company_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "saebooks_connect_company_id", default=None
)
_auth_is_api_token: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "saebooks_connect_is_api_token", default=False
)


def current_tenant_id() -> uuid.UUID:
    """Return the active tenant for this request. Default tenant if unauth."""
    return _current_tenant_id.get()


def current_company_id() -> uuid.UUID | None:
    """Return the active company stamped by the interceptor, or None."""
    return _current_company_id.get()


def current_user_id() -> uuid.UUID | None:
    """Return the authenticated user_id, or None for unauthenticated calls."""
    return _current_user_id.get()


# ---------------------------------------------------------------------------
# Auth interceptor — mirrors api/v1/auth.require_bearer for Connect calls
# ---------------------------------------------------------------------------


class BearerAuthInterceptor:
    """Verifies ``Authorization: Bearer <jwt | saebk_*>`` on every Connect
    call. Stamps tenant / company / user into contextvars for handler use.

    Behaviour mirrors ``saebooks.api.v1.auth.require_bearer``:

    1. Try JWT decode — stamp tenant_id/company_id from claims, sub→user
    2. Try ``saebk_*`` token — DB lookup + bcrypt verify
    3. Fall back to the static dev bearer (``SAEBOOKS_DEV_API_TOKEN``)
       so scripts / tests can poke the Connect surface without minting
       a real token
    4. Else raise UNAUTHENTICATED

    The Heartbeat RPC is exempt from auth (uptime probes shouldn't need
    a token). Everything else is gated.

    Implements both unary and server-stream interceptor protocols so
    the same auth logic covers WatchChanges / WatchPresence.
    """

    # Methods that bypass the bearer check. Add sparingly — every entry
    # here is a route that any internet endpoint can hit without proof.
    _OPEN_METHODS: frozenset[str] = frozenset({"Heartbeat"})

    async def intercept_unary(
        self,
        call_next: Callable[[Any, Any], Awaitable[Any]],
        request: Any,
        ctx: Any,
    ) -> Any:
        await self._authenticate(ctx)
        return await call_next(request, ctx)

    def intercept_server_stream(
        self,
        call_next: Callable[[Any, Any], AsyncIterator[Any]],
        request: Any,
        ctx: Any,
    ) -> AsyncIterator[Any]:
        # Server-stream interceptors return an async iterator. We wrap
        # the auth check + downstream stream into a single generator.
        async def gen() -> AsyncIterator[Any]:
            await self._authenticate(ctx)
            async for item in call_next(request, ctx):
                yield item

        return gen()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _authenticate(self, ctx: Any) -> None:
        method_name = ctx.method().name if hasattr(ctx, "method") else ""
        if method_name in self._OPEN_METHODS:
            return

        bearer = self._extract_bearer(ctx)
        if not bearer:
            raise ConnecpyException(
                Code.UNAUTHENTICATED, "missing bearer token"
            )

        # 1. JWT branch
        from saebooks.services.jwt_tokens import (  # noqa: PLC0415
            JWTError,
            decode_access_token,
        )

        try:
            claims = decode_access_token(bearer)
        except JWTError:
            claims = None

        if claims is not None:
            self._stamp_from_jwt(claims)
            return

        # 2. saebk_ branch
        from saebooks.services.api_tokens import (  # noqa: PLC0415
            TOKEN_PREFIX_HEADER,
            TokenVerifyError,
            verify as verify_api_token,
        )

        if bearer.startswith(TOKEN_PREFIX_HEADER):
            try:
                async with AsyncSessionLocal() as session:
                    token_row = await verify_api_token(session, bearer)
                    await session.commit()
            except TokenVerifyError as exc:
                logger.info("connect api_token rejected: %s", exc)
                raise ConnecpyException(
                    Code.UNAUTHENTICATED, "invalid api token"
                ) from exc

            _current_user_id.set(token_row.user_id)
            _current_tenant_id.set(token_row.tenant_id)
            _current_company_id.set(token_row.company_id)
            _auth_is_api_token.set(True)
            return

        # 3. Dev static bearer — only honoured when explicitly set
        import os  # noqa: PLC0415
        dev_token = os.environ.get("SAEBOOKS_DEV_API_TOKEN", "").strip()
        if dev_token and secrets.compare_digest(bearer, dev_token):
            # Dev fallback: no user, default tenant. Useful for scripts
            # and pytest. Production deploys leave SAEBOOKS_DEV_API_TOKEN
            # unset.
            _current_tenant_id.set(_DEFAULT_TENANT)
            return

        raise ConnecpyException(Code.UNAUTHENTICATED, "invalid bearer token")

    @staticmethod
    def _extract_bearer(ctx: Any) -> str | None:
        """Pull ``Bearer <token>`` from request headers; returns the raw
        token portion or None."""
        try:
            headers = ctx.request_headers()
        except Exception:  # noqa: BLE001
            return None
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip() or None

    @staticmethod
    def _stamp_from_jwt(claims: dict[str, Any]) -> None:
        """Set contextvars from a verified JWT's claims."""
        sub = claims.get("sub")
        if sub:
            try:
                _current_user_id.set(uuid.UUID(str(sub)))
            except (ValueError, TypeError):
                pass

        tenant_claim = claims.get("tenant_id")
        if tenant_claim:
            try:
                _current_tenant_id.set(uuid.UUID(str(tenant_claim)))
            except (ValueError, TypeError):
                pass

        company_claim = claims.get("company_id")
        if company_claim:
            try:
                _current_company_id.set(uuid.UUID(str(company_claim)))
            except (ValueError, TypeError):
                pass


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


async def _bind_request_tenant(session: Any) -> None:
    """Bind ``app.current_tenant`` to whatever the auth interceptor stamped
    (default tenant on fully-anonymous calls). RLS-scoped tables need
    this set before any SELECT or they return zero rows."""
    from sqlalchemy import text  # noqa: PLC0415

    await session.execute(
        text(f"SET LOCAL app.current_tenant = '{current_tenant_id()}'")
    )


async def _resolve_company_id(session: Any) -> uuid.UUID:
    """Return the company the current Connect request should act in.

    Order:
      1. Company stamped by the auth interceptor (JWT claim, API token row)
      2. First active company under the request's tenant (single-company
         fallback — matches the existing grpcio :50051 behaviour)

    Always binds the tenant on the session first so the SELECT in step 2
    actually returns rows.
    """
    await _bind_request_tenant(session)

    stamped = current_company_id()
    if stamped is not None:
        return stamped

    from saebooks.models.company import Company  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    result = await session.execute(
        select(Company)
        .where(Company.archived_at.is_(None))
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise ConnecpyException(Code.INTERNAL, "No active company in database")
    return company.id


# Backwards-compat alias — internal callers still use the old name.
_first_company_id = _resolve_company_id


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

    # ------------------------------------------------------------------
    # Change feed (server-streaming)
    # ------------------------------------------------------------------

    async def watch_changes(
        self,
        request: saebooks_pb2.WatchChangesRequest,
        ctx: Any,
    ) -> AsyncIterator[saebooks_pb2.ChangeEvent]:
        """Yield ChangeLog rows from ``cursor`` onward, polling every 2 s.

        Mirrors the grpcio :50051 ``WatchChanges`` handler. Connecpy
        translates async-generator returns into Connect server-streaming
        envelopes automatically; cancellation propagates as
        ``asyncio.CancelledError`` from the generator's caller.
        """
        import asyncio  # noqa: PLC0415
        import json  # noqa: PLC0415

        from saebooks.services import change_log as change_log_svc  # noqa: PLC0415

        cursor = request.cursor
        poll_interval = 2.0
        batch_size = 50

        while True:
            async with AsyncSessionLocal() as session:
                await _bind_request_tenant(session)
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
    # Collaborative presence + locks (in-memory store shared with grpcio)
    # ------------------------------------------------------------------

    async def watch_presence(
        self,
        request: saebooks_pb2.PresenceRequest,
        ctx: Any,
    ) -> AsyncIterator[saebooks_pb2.PresenceEvent]:
        """Stream presence events for a tenant+entity scope.

        Shares the in-memory ``_presence_store`` + ``_presence_queues``
        with the grpcio :50051 servicer — both surfaces see the same
        viewers in real time. This is deliberate: a desktop client on
        gRPC and a browser on Connect should not be mutually invisible.
        """
        import asyncio  # noqa: PLC0415

        from saebooks.grpc_server import (  # noqa: PLC0415
            _now_utc,
            _presence_queues,
            _presence_store,
            _scope_key,
        )

        tenant_id = request.tenant_id
        user_id = request.user_id
        entity_type = request.entity_type
        entity_id = request.entity_id
        scope = _scope_key(entity_type, entity_id)
        timestamp = _now_utc().isoformat()

        _presence_store.setdefault(tenant_id, {}).setdefault(scope, {})[
            user_id
        ] = user_id

        queue: asyncio.Queue = asyncio.Queue()
        _presence_queues.setdefault(tenant_id, []).append(queue)

        def _fan_out(event: saebooks_pb2.PresenceEvent) -> None:
            for q in list(_presence_queues.get(tenant_id, [])):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        try:
            for existing_uid in list(
                _presence_store.get(tenant_id, {}).get(scope, {}).keys()
            ):
                yield saebooks_pb2.PresenceEvent(
                    user_id=existing_uid,
                    user_name=existing_uid,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="viewing",
                    timestamp=timestamp,
                )

            _fan_out(
                saebooks_pb2.PresenceEvent(
                    user_id=user_id,
                    user_name=user_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="viewing",
                    timestamp=timestamp,
                )
            )

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    if (
                        event.entity_type == entity_type
                        and event.entity_id == entity_id
                    ):
                        yield event
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            scope_map = _presence_store.get(tenant_id, {}).get(scope, {})
            scope_map.pop(user_id, None)

            queues = _presence_queues.get(tenant_id, [])
            if queue in queues:
                queues.remove(queue)

            _fan_out(
                saebooks_pb2.PresenceEvent(
                    user_id=user_id,
                    user_name=user_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="left",
                    timestamp=_now_utc().isoformat(),
                )
            )

    async def acquire_lock(
        self,
        request: saebooks_pb2.AcquireLockRequest,
        ctx: Any,
    ) -> saebooks_pb2.LockResponse:
        """Acquire an entity lock with lazy expiry. Shares ``_lock_store``
        with the grpcio :50051 servicer so locks are honoured across
        both transports."""
        import datetime as _dt  # noqa: PLC0415

        from saebooks.grpc_server import _lock_store, _now_utc  # noqa: PLC0415

        tenant_id = request.tenant_id
        entity_type = request.entity_type
        entity_id = request.entity_id
        user_id = request.user_id
        ttl = request.ttl_seconds if request.ttl_seconds > 0 else 30

        lock_key = (tenant_id, entity_type, entity_id)
        now = _now_utc()
        expires_at = now + _dt.timedelta(seconds=ttl)

        existing = _lock_store.get(lock_key)
        if existing is not None:
            existing_user, existing_expires = existing
            if existing_expires <= now:
                del _lock_store[lock_key]
            elif existing_user != user_id:
                return saebooks_pb2.LockResponse(
                    acquired=False,
                    locked_by=existing_user,
                    expires_at=existing_expires.isoformat(),
                )

        _lock_store[lock_key] = (user_id, expires_at)
        return saebooks_pb2.LockResponse(
            acquired=True,
            locked_by=user_id,
            expires_at=expires_at.isoformat(),
        )

    async def release_lock(
        self,
        request: saebooks_pb2.ReleaseLockRequest,
        ctx: Any,
    ) -> saebooks_pb2.ReleaseLockResponse:
        """Release a lock if it belongs to the requesting user."""
        from saebooks.grpc_server import _lock_store  # noqa: PLC0415

        lock_key = (request.tenant_id, request.entity_type, request.entity_id)
        existing = _lock_store.get(lock_key)
        if existing is not None and existing[0] == request.user_id:
            del _lock_store[lock_key]
            return saebooks_pb2.ReleaseLockResponse(released=True)
        return saebooks_pb2.ReleaseLockResponse(released=False)


# ---------------------------------------------------------------------------
# ASGI factory + dispatch middleware
# ---------------------------------------------------------------------------


def build_connect_app() -> saebooks_connecpy.SAEBooksASGIApplication:
    """Construct the connecpy ASGI application with the bearer-auth
    interceptor wired in. Called once at import time from ``main.py``
    and wrapped with the dispatch middleware below."""
    return saebooks_connecpy.SAEBooksASGIApplication(
        SAEBooksConnectImpl(),
        interceptors=(BearerAuthInterceptor(),),
    )


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

    @property
    def state(self) -> Any:
        """Proxy through to the wrapped FastAPI's app.state.

        Code that does from saebooks.main import app gets THIS
        middleware, not the inner FastAPI. Tests and middleware-tweak
        code that touch app.state.<...> would otherwise hit
        AttributeError. Proxying keeps the API surface compatible.
        """
        return self._fastapi.state

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith(
            CONNECT_PATH_PREFIX
        ):
            await self._connect(scope, receive, send)
            return
        await self._fastapi(scope, receive, send)


__all__ = [
    "BearerAuthInterceptor",
    "CONNECT_PATH_PREFIX",
    "ConnectDispatchMiddleware",
    "SAEBooksConnectImpl",
    "build_connect_app",
    "current_company_id",
    "current_tenant_id",
    "current_user_id",
]
