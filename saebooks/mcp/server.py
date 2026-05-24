"""SAE Books MCP server — exposes the REST API as native AI-agent tools.

Runs over HTTP (SSE / Streamable-HTTP) so a single deployment serves
every Claude Code session, scheduled agent, and remote client. Auth is
Bearer with a SAE Books API token (``saebk_*``); see
``services/api_tokens.py`` for issuance.

Per-tenant scope: each MCP client supplies their own API token via
``Authorization: Bearer saebk_*``. The token resolves to a (user,
company) and every downstream call inherits that scope. There is no
shared admin token — multi-tenant safety is achieved through the
saebooks API itself, not through MCP.

Why not stdio: stdio MCP servers are per-session subprocesses, which
means every Claude Code session would need the saebooks-mcp binary
installed locally + its own token. HTTP gives us a single
deployment, a single token-per-user, and remote access from any
machine. Latency cost is one extra network hop; acceptable for the
~50 ms human-perceivable bound on these calls.

Safety levels
-------------

Every tool is annotated with one of four safety levels. The
``SAEBOOKS_MCP_MAX_SAFETY`` env var caps which tools are registered
at startup:

  - ``safe``         — read-only (list/get/reports/search/whoami)
  - ``mutation``     — create/update DRAFT entities, master data,
                       record payments, allocate bank lines, post
                       drafts (which are reversible via /void)
  - ``void``         — void POSTED entries, archive master data,
                       soft-delete master data (reversible)
  - ``destructive``  — hard-delete (irreversible), reverse posted
                       journals, close period, ATO lodge, Xero push

Default is ``destructive`` (no cap). Set to ``void`` for the community
edition, ``mutation`` for typical user-facing deployments, ``safe``
for a read-only AI assistant.

The API server enforces role-based authorization independently; the
safety cap exists so the MCP tool surface itself doesn't tempt a
casual user (or an over-eager agent) into operations they shouldn't
attempt.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

from saebooks import __version__

logger = logging.getLogger("saebooks.mcp")
logging.basicConfig(level=os.getenv("SAEBOOKS_MCP_LOG_LEVEL", "INFO"))

_DEFAULT_API_BASE = "http://127.0.0.1:8000"
# In-tree mount runs inside the saebooks API process, so tools loop
# back to the local uvicorn listener (port 8000 — see entrypoint.sh).
# The standalone container deployment overrides this with
# SAEBOOKS_API_URL=http://api:8000 to reach the API across the docker
# network.
API_BASE = os.getenv("SAEBOOKS_API_URL", _DEFAULT_API_BASE).rstrip("/")
SHARED_API_TOKEN = os.getenv("SAEBOOKS_API_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# Safety levels — gate tool registration at import time
# ---------------------------------------------------------------------------

_SAFETY_ORDER = {"safe": 0, "mutation": 1, "void": 2, "destructive": 3}
_MAX_SAFETY = os.getenv("SAEBOOKS_MCP_MAX_SAFETY", "destructive").strip().lower()
if _MAX_SAFETY not in _SAFETY_ORDER:
    logger.warning(
        "Unknown SAEBOOKS_MCP_MAX_SAFETY=%r — defaulting to 'destructive'",
        _MAX_SAFETY,
    )
    _MAX_SAFETY = "destructive"
_MAX_SAFETY_RANK = _SAFETY_ORDER[_MAX_SAFETY]

logger.info("MCP safety cap: %s (rank %d)", _MAX_SAFETY, _MAX_SAFETY_RANK)


mcp = FastMCP(
    "saebooks",
    instructions=(
        "SAE Books — self-hosted double-entry accounting. "
        "Tools cover invoices, bills, expenses, journals, contacts, "
        "bank reconciliation, projects, accounts, time tracking, and "
        "reports. Every tool talks to a SAE Books instance over REST; "
        "the user's API token (issued at /admin/api-tokens) "
        "authenticates the call. "
        "Default currency AUD. Dates are ISO-8601 (YYYY-MM-DD). "
        "Mutating tools that change POSTED entities use If-Match: "
        "<version> for optimistic locking — fetch the entity first "
        "to read its current version."
    ),
)
# Expose the application version via MCP initialize serverInfo.
# FastMCP does not accept a version= kwarg; we set it on the
# underlying low-level server directly after construction.
mcp._mcp_server.version = "0.5.0"  # beta — MCP integration is more mature than the alpha-tier product API


def _register(safety: str = "safe"):
    """Decorator that registers a tool only when its safety level is
    at or below the configured cap. Use as::

        @_register(safety="mutation")
        @mcp.tool()
        async def update_invoice(...): ...

    Order matters — the @_register must wrap @mcp.tool so we can
    decide whether to actually call mcp.tool's decorator.
    """
    if safety not in _SAFETY_ORDER:
        raise ValueError(f"unknown safety level: {safety!r}")
    allowed = _SAFETY_ORDER[safety] <= _MAX_SAFETY_RANK

    def deco(fn):
        if not allowed:
            logger.debug("skip register %s (safety=%s > %s)", fn.__name__, safety, _MAX_SAFETY)
            return fn
        # The wrapped @mcp.tool() decorator has already run by the time
        # we see fn, so registration is in effect; return fn unchanged.
        # Set an attribute so introspection can see the level.
        fn.__mcp_safety__ = safety
        return fn

    return deco


def _gated_tool(safety: str, **tool_kwargs):
    """Combine @_register + @mcp.tool into one decorator that ONLY calls
    mcp.tool() when the safety level passes the cap. Cleaner than
    stacking decorators on every function.
    """
    if safety not in _SAFETY_ORDER:
        raise ValueError(f"unknown safety level: {safety!r}")
    allowed = _SAFETY_ORDER[safety] <= _MAX_SAFETY_RANK

    def deco(fn):
        if not allowed:
            logger.debug("skip %s (safety=%s > %s)", fn.__name__, safety, _MAX_SAFETY)
            return fn
        fn.__mcp_safety__ = safety
        return mcp.tool(**tool_kwargs)(fn)

    return deco


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _client_for(ctx: Context | None) -> httpx.AsyncClient:
    """Return an httpx client with auth headers set.

    Token resolution order:
    1. ``Authorization`` header on the underlying HTTP request
       (in-tree mount + standalone HTTP transports — the per-client
       token path; preferred in production).
    2. ``_meta.authorization`` on the JSON-RPC request (stdio /
       legacy clients that promote auth into the MCP meta field).
    3. ``SAEBOOKS_API_TOKEN`` env (single-tenant deployments).
    4. No auth (will 401 from saebooks).
    """
    token: str | None = None
    if ctx is not None:
        request_ctx = getattr(ctx, "request_context", None)
        if request_ctx is not None:
            # Path 1: HTTP transport — read the Starlette request's
            # Authorization header directly. This is the in-tree
            # production path: the MCP transport carries an
            # ``Authorization: Bearer saebk_*`` and we forward it
            # verbatim to the loopback REST API.
            req = getattr(request_ctx, "request", None)
            if req is not None:
                headers_attr = getattr(req, "headers", None)
                auth_header: str | None = None
                if headers_attr is not None:
                    # Starlette Headers: case-insensitive .get()
                    try:
                        auth_header = headers_attr.get("authorization")
                    except (AttributeError, TypeError):
                        # Plain dict fallback
                        auth_header = (
                            headers_attr.get("authorization")
                            if hasattr(headers_attr, "get")
                            else None
                        )
                if auth_header:
                    if auth_header.lower().startswith("bearer "):
                        token = auth_header.split(None, 1)[1].strip()
                    else:
                        token = auth_header.strip()

            # Path 2: fall back to MCP _meta field for stdio clients
            # or anything else that doesn't carry HTTP headers.
            if not token:
                meta = getattr(request_ctx, "meta", None)
                if meta is None:
                    meta_dict: dict = {}
                elif isinstance(meta, dict):
                    meta_dict = meta
                elif hasattr(meta, "model_dump"):
                    meta_dict = meta.model_dump()
                else:
                    meta_dict = dict(meta.__dict__)
                token = meta_dict.get("authorization") or meta_dict.get("Authorization")
                if token and token.lower().startswith("bearer "):
                    token = token.split(None, 1)[1].strip()
    if not token:
        token = SHARED_API_TOKEN or None

    headers = {"User-Agent": "saebooks-mcp/0.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return httpx.AsyncClient(
        base_url=API_BASE,
        headers=headers,
        timeout=30.0,
    )


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop empty-string / None values so they're not sent as filters."""
    return {k: v for k, v in params.items() if v not in (None, "")}


async def _get(ctx: Context | None, path: str, **params: Any) -> Any:
    async with _client_for(ctx) as client:
        resp = await client.get(path, params=_clean_params(params))
        resp.raise_for_status()
        return resp.json()


async def _post(
    ctx: Context | None,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    if_match: str | int | None = None,
    idempotency_key: str | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    if idempotency_key is not None:
        headers["X-Idempotency-Key"] = idempotency_key
    async with _client_for(ctx) as client:
        resp = await client.post(path, json=body or {}, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


async def _patch(
    ctx: Context | None,
    path: str,
    body: dict[str, Any],
    *,
    if_match: str | int | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    async with _client_for(ctx) as client:
        resp = await client.patch(path, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _put(
    ctx: Context | None,
    path: str,
    body: dict[str, Any],
    *,
    if_match: str | int | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    async with _client_for(ctx) as client:
        resp = await client.put(path, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _delete(
    ctx: Context | None,
    path: str,
    *,
    if_match: str | int | None = None,
) -> Any:
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    async with _client_for(ctx) as client:
        resp = await client.delete(path, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Strip keys whose value is None or empty string — for PATCH bodies."""
    return {k: v for k, v in d.items() if v not in (None, "")}


# ===========================================================================
# Utility tools (safe)
# ===========================================================================


@_gated_tool(safety="safe")
async def whoami(ctx: Context) -> dict[str, Any]:
    """Identify the SAE Books user/company this MCP session is acting as.

    Useful as a first call to confirm the token works and to see which
    company's data you're querying.
    """
    return await _get(ctx, "/api/v1/companies")


@_gated_tool(safety="safe")
async def get_openapi_schema(ctx: Context) -> dict[str, Any]:
    """Fetch the SAE Books REST OpenAPI schema.

    Use this when you need to call an endpoint that has no dedicated
    MCP tool — combine with ``call_api`` to issue the request.
    """
    return await _get(ctx, "/openapi.json")


@_gated_tool(safety="destructive")
async def call_api(
    ctx: Context,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    if_match: str | int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Raw API escape hatch — call any SAE Books endpoint.

    Marked ``destructive`` because it bypasses all safety classification;
    a caller with this tool can do anything the underlying token can do.

    Args:
        method: HTTP method (GET, POST, PATCH, PUT, DELETE).
        path:   API path including /api/v1/ prefix.
        body:   JSON body for POST/PATCH/PUT.
        params: query-string parameters.
        if_match: optimistic-lock version for mutation endpoints.
        idempotency_key: retry-safe key for POST endpoints.

    Returns the JSON response (or {"ok": True} if empty).
    """
    method = method.upper()
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    if idempotency_key is not None:
        headers["X-Idempotency-Key"] = idempotency_key
    async with _client_for(ctx) as client:
        resp = await client.request(
            method,
            path,
            json=body if method in ("POST", "PATCH", "PUT") else None,
            params=_clean_params(params or {}),
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


@_gated_tool(safety="safe")
async def search(ctx: Context, query: str, limit: int = 20) -> dict[str, Any]:
    """Global search across invoices, contacts, accounts, journal entries.

    Args:
        query: free-text search string.
        limit: max hits per category.
    """
    return await _get(ctx, "/api/v1/search", q=query, limit=limit)


@_gated_tool(safety="safe")
async def recent_changes(
    ctx: Context,
    entity_type: str = "",
    since: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Recent change-log entries — what changed when, by whom.

    Args:
        entity_type: filter to one entity (invoice, bill, contact, etc.).
        since: ISO datetime lower bound.
        limit: max rows.
    """
    return await _get(
        ctx,
        "/api/v1/changes",
        entity_type=entity_type,
        since=since,
        limit=limit,
    )


# ===========================================================================
# Contacts (customers / suppliers)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_contacts(
    ctx: Context,
    search: str = "",
    contact_type: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List contacts (customers / suppliers / both).

    Args:
        search: free-text match against name/email.
        contact_type: CUSTOMER | SUPPLIER | BOTH | BENEFICIARY.
        limit: page size (max 200).
        page: 1-indexed page number.
    """
    return await _get(
        ctx,
        "/api/v1/contacts",
        search=search,
        contact_type=contact_type,
        limit=limit,
        page=page,
    )


@_gated_tool(safety="safe")
async def get_contact(ctx: Context, contact_id: str) -> dict[str, Any]:
    """Fetch a single contact by id."""
    return await _get(ctx, f"/api/v1/contacts/{contact_id}")


@_gated_tool(safety="mutation")
async def create_contact(
    ctx: Context,
    name: str,
    contact_type: str = "CUSTOMER",
    email: str = "",
    phone: str = "",
    abn: str = "",
    address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a contact.

    Args:
        name: required, e.g. "Acme Pty Ltd".
        contact_type: CUSTOMER (default), SUPPLIER, BOTH, BENEFICIARY.
        email: optional.
        phone: optional.
        abn: optional 11-digit ABN (Australian Business Number).
        address: optional {line1, line2, city, state, postcode, country}.
    """
    body: dict[str, Any] = {"name": name, "contact_type": contact_type}
    if email:
        body["email"] = email
    if phone:
        body["phone"] = phone
    if abn:
        body["abn"] = abn
    if address:
        body["address"] = address
    return await _post(ctx, "/api/v1/contacts", body)


@_gated_tool(safety="mutation")
async def update_contact(
    ctx: Context,
    contact_id: str,
    version: int,
    name: str = "",
    email: str = "",
    phone: str = "",
    abn: str = "",
    address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a contact. Empty fields are left unchanged."""
    body = _drop_empty({
        "name": name,
        "email": email,
        "phone": phone,
        "abn": abn,
        "address": address,
    })
    return await _patch(
        ctx, f"/api/v1/contacts/{contact_id}", body, if_match=version
    )


@_gated_tool(safety="void")
async def archive_contact(ctx: Context, contact_id: str, version: int) -> dict[str, Any]:
    """Soft-delete a contact (sets archived_at).

    Reversible — the contact is hidden from lists but historical
    transactions referencing it are unaffected. Use
    ``hard_delete_contact`` only if you genuinely need it gone.
    """
    return await _delete(ctx, f"/api/v1/contacts/{contact_id}", if_match=version)


@_gated_tool(safety="destructive")
async def hard_delete_contact(ctx: Context, contact_id: str, version: int) -> dict[str, Any]:
    """Permanently delete a contact. Irreversible.

    Most workflows want ``archive_contact`` instead — hard-delete
    fails if the contact has posted transactions referring to it.
    """
    return await _delete(
        ctx,
        f"/api/v1/contacts/{contact_id}?hard=true",
        if_match=version,
    )


# ===========================================================================
# Invoices (accounts receivable)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_invoices(
    ctx: Context,
    status: str = "",
    contact_id: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List invoices.

    Args:
        status: DRAFT | AWAITING_PAYMENT | PAID | VOIDED — empty for all.
        contact_id: filter to one customer.
        from_date: ISO date YYYY-MM-DD invoice-date lower bound.
        to_date: ISO date YYYY-MM-DD upper bound.
    """
    return await _get(
        ctx, "/api/v1/invoices",
        status=status, contact_id=contact_id,
        from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_invoice(ctx: Context, invoice_id: str) -> dict[str, Any]:
    """Fetch a single invoice with its lines."""
    return await _get(ctx, f"/api/v1/invoices/{invoice_id}")


@_gated_tool(safety="mutation")
async def create_invoice(
    ctx: Context,
    contact_id: str,
    issue_date: str,
    due_date: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    currency: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT invoice.

    Args:
        contact_id: customer UUID.
        issue_date: ISO date YYYY-MM-DD.
        due_date: ISO date. Empty defaults to the customer's payment terms.
        reference: free-text reference shown on the invoice.
        lines: list of {description, quantity, unit_price, account_id,
            tax_code_id, project_id?, item_id?}.
        currency: 3-letter ISO; defaults to company base currency.
        notes: optional internal notes.

    The invoice is DRAFT — call ``post_invoice`` to finalise.
    """
    body: dict[str, Any] = {
        "contact_id": contact_id,
        "issue_date": issue_date,
        "lines": lines or [],
    }
    if due_date:
        body["due_date"] = due_date
    if reference:
        body["reference"] = reference
    if currency:
        body["currency"] = currency
    if notes:
        body["notes"] = notes
    return await _post(ctx, "/api/v1/invoices", body)


@_gated_tool(safety="mutation")
async def update_invoice(
    ctx: Context,
    invoice_id: str,
    version: int,
    contact_id: str = "",
    issue_date: str = "",
    due_date: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT invoice. Empty fields are left unchanged."""
    body = _drop_empty({
        "contact_id": contact_id,
        "issue_date": issue_date,
        "due_date": due_date,
        "reference": reference,
        "lines": lines,
        "notes": notes,
    })
    return await _patch(
        ctx, f"/api/v1/invoices/{invoice_id}", body, if_match=version
    )


@_gated_tool(safety="mutation")
async def post_invoice(ctx: Context, invoice_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → AWAITING_PAYMENT and write the journal entries."""
    return await _post(
        ctx, f"/api/v1/invoices/{invoice_id}/post", if_match=version
    )


@_gated_tool(safety="mutation")
async def create_invoice_stripe_payment_link(ctx: Context, invoice_id: str) -> dict[str, Any]:
    """Generate a Stripe Checkout payment link for an invoice."""
    return await _post(ctx, f"/api/v1/invoices/{invoice_id}/stripe-payment-link")


@_gated_tool(safety="void")
async def void_invoice(ctx: Context, invoice_id: str, version: int) -> dict[str, Any]:
    """Void a POSTED invoice — writes a reversing JE and marks it VOIDED."""
    return await _post(
        ctx, f"/api/v1/invoices/{invoice_id}/void", if_match=version
    )


@_gated_tool(safety="destructive")
async def delete_invoice(ctx: Context, invoice_id: str, version: int) -> dict[str, Any]:
    """Delete an invoice. DRAFT is soft-archived; POSTED is HARD-DELETED.

    Irreversible for posted invoices. Most workflows want ``void_invoice``
    instead — see [[saebooks-hard-delete-policy]].
    """
    return await _delete(
        ctx, f"/api/v1/invoices/{invoice_id}", if_match=version
    )


# ===========================================================================
# Bills (accounts payable)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_bills(
    ctx: Context,
    status: str = "",
    contact_id: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List supplier bills (accounts payable).

    Args:
        status: DRAFT | AWAITING_PAYMENT | PAID | VOIDED — empty for all.
        contact_id: filter to one supplier.
    """
    return await _get(
        ctx, "/api/v1/bills",
        status=status, contact_id=contact_id,
        from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_bill(ctx: Context, bill_id: str) -> dict[str, Any]:
    """Fetch a single bill with its lines."""
    return await _get(ctx, f"/api/v1/bills/{bill_id}")


@_gated_tool(safety="mutation")
async def create_bill(
    ctx: Context,
    contact_id: str,
    issue_date: str,
    due_date: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    currency: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT supplier bill.

    Args:
        contact_id: supplier UUID.
        issue_date: ISO date.
        due_date: ISO date. Empty defaults to supplier payment terms.
        reference: supplier's invoice/reference number.
        lines: [{description, quantity, unit_price, account_id, tax_code_id, ...}].
        currency: 3-letter ISO; defaults to base currency.
        notes: internal notes.
    """
    body: dict[str, Any] = {
        "contact_id": contact_id,
        "issue_date": issue_date,
        "lines": lines or [],
    }
    for k, v in (("due_date", due_date), ("reference", reference),
                 ("currency", currency), ("notes", notes)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/bills", body)


@_gated_tool(safety="mutation")
async def update_bill(
    ctx: Context,
    bill_id: str,
    version: int,
    contact_id: str = "",
    issue_date: str = "",
    due_date: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT bill."""
    body = _drop_empty({
        "contact_id": contact_id, "issue_date": issue_date,
        "due_date": due_date, "reference": reference,
        "lines": lines, "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/bills/{bill_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_bill(ctx: Context, bill_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → AWAITING_PAYMENT (writes journal entries)."""
    return await _post(ctx, f"/api/v1/bills/{bill_id}/post", if_match=version)


@_gated_tool(safety="void")
async def void_bill(ctx: Context, bill_id: str, version: int) -> dict[str, Any]:
    """Void a POSTED bill — writes reversing JE, marks VOIDED."""
    return await _post(ctx, f"/api/v1/bills/{bill_id}/void", if_match=version)


@_gated_tool(safety="destructive")
async def delete_bill(ctx: Context, bill_id: str, version: int) -> dict[str, Any]:
    """Delete a bill. DRAFT soft-archived; POSTED HARD-DELETED. Irreversible."""
    return await _delete(ctx, f"/api/v1/bills/{bill_id}", if_match=version)


# ===========================================================================
# Expenses (paid-at-checkout supplier expense, simpler than bills)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_expenses(
    ctx: Context,
    status: str = "",
    contact_id: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List expenses (paid at point of purchase, no AP tracking)."""
    return await _get(
        ctx, "/api/v1/expenses",
        status=status, contact_id=contact_id,
        from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_expense(ctx: Context, expense_id: str) -> dict[str, Any]:
    """Fetch a single expense."""
    return await _get(ctx, f"/api/v1/expenses/{expense_id}")


@_gated_tool(safety="mutation")
async def create_expense(
    ctx: Context,
    issue_date: str,
    bank_account_id: str,
    lines: list[dict[str, Any]],
    contact_id: str = "",
    reference: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT expense (e.g. cash receipt or card purchase).

    Args:
        issue_date: ISO date.
        bank_account_id: UUID of the account that paid (chequing, card, etc.).
        lines: [{description, amount, account_id, tax_code_id, ...}].
        contact_id: supplier UUID (optional for cash receipts).
        reference: receipt number / memo.
        notes: internal notes.
    """
    body: dict[str, Any] = {
        "issue_date": issue_date,
        "bank_account_id": bank_account_id,
        "lines": lines,
    }
    for k, v in (("contact_id", contact_id), ("reference", reference), ("notes", notes)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/expenses", body)


@_gated_tool(safety="mutation")
async def update_expense(
    ctx: Context,
    expense_id: str,
    version: int,
    issue_date: str = "",
    bank_account_id: str = "",
    contact_id: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT expense."""
    body = _drop_empty({
        "issue_date": issue_date, "bank_account_id": bank_account_id,
        "contact_id": contact_id, "reference": reference,
        "lines": lines, "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/expenses/{expense_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_expense(ctx: Context, expense_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → POSTED."""
    return await _post(ctx, f"/api/v1/expenses/{expense_id}/post", if_match=version)


@_gated_tool(safety="void")
async def void_expense(ctx: Context, expense_id: str, version: int) -> dict[str, Any]:
    """Void a POSTED expense."""
    return await _post(ctx, f"/api/v1/expenses/{expense_id}/void", if_match=version)


@_gated_tool(safety="destructive")
async def delete_expense(ctx: Context, expense_id: str, version: int) -> dict[str, Any]:
    """Delete an expense. DRAFT soft-archived; POSTED HARD-DELETED."""
    return await _delete(ctx, f"/api/v1/expenses/{expense_id}", if_match=version)


# ===========================================================================
# Journal entries
# ===========================================================================


@_gated_tool(safety="safe")
async def list_journal_entries(
    ctx: Context,
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List journal entries.

    Args:
        status: DRAFT | POSTED | VOIDED — empty for all.
        from_date, to_date: ISO date bounds on the entry date.
    """
    return await _get(
        ctx, "/api/v1/journal_entries",
        status=status, from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_journal_entry(ctx: Context, entry_id: str) -> dict[str, Any]:
    """Fetch a single JE with its lines."""
    return await _get(ctx, f"/api/v1/journal_entries/{entry_id}")


@_gated_tool(safety="mutation")
async def create_journal_entry(
    ctx: Context,
    entry_date: str,
    lines: list[dict[str, Any]],
    description: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """Create a DRAFT journal entry.

    Args:
        entry_date: ISO date.
        lines: [{account_id, debit, credit, description?, tax_code_id?, ...}].
            Sum of debits MUST equal sum of credits.
        description: header description.
        reference: external reference.
    """
    body: dict[str, Any] = {
        "entry_date": entry_date,
        "lines": lines,
    }
    if description:
        body["description"] = description
    if reference:
        body["reference"] = reference
    return await _post(ctx, "/api/v1/journal_entries", body)


@_gated_tool(safety="mutation")
async def update_journal_entry(
    ctx: Context,
    entry_id: str,
    version: int,
    entry_date: str = "",
    lines: list[dict[str, Any]] | None = None,
    description: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT journal entry."""
    body = _drop_empty({
        "entry_date": entry_date, "lines": lines,
        "description": description, "reference": reference,
    })
    return await _patch(ctx, f"/api/v1/journal_entries/{entry_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_journal_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → POSTED."""
    return await _post(ctx, f"/api/v1/journal_entries/{entry_id}/post", if_match=version)


@_gated_tool(safety="destructive")
async def reverse_journal_entry(
    ctx: Context, entry_id: str, version: int, reverse_date: str = ""
) -> dict[str, Any]:
    """Post a reversing JE for a previously POSTED entry.

    Creates a new entry with debits/credits swapped, dated reverse_date
    (defaults to today). Both entries remain in the ledger — this is
    the correct way to "undo" a posted journal without breaking audit.
    """
    body: dict[str, Any] = {}
    if reverse_date:
        body["reverse_date"] = reverse_date
    return await _post(
        ctx, f"/api/v1/journal_entries/{entry_id}/reverse", body, if_match=version
    )


@_gated_tool(safety="destructive")
async def delete_journal_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Delete a journal entry. DRAFT soft-archived; POSTED HARD-DELETED.

    For posted entries, prefer ``reverse_journal_entry`` — hard-delete
    of posted journals is allowed for admins (saebooks-hard-delete-policy)
    but obliterates audit trail.
    """
    return await _delete(ctx, f"/api/v1/journal_entries/{entry_id}", if_match=version)


# ===========================================================================
# Credit notes
# ===========================================================================


@_gated_tool(safety="safe")
async def list_credit_notes(
    ctx: Context, status: str = "", contact_id: str = "",
    limit: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List credit notes."""
    return await _get(
        ctx, "/api/v1/credit_notes",
        status=status, contact_id=contact_id, limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_credit_note(ctx: Context, credit_note_id: str) -> dict[str, Any]:
    """Fetch a single credit note."""
    return await _get(ctx, f"/api/v1/credit_notes/{credit_note_id}")


@_gated_tool(safety="mutation")
async def create_credit_note(
    ctx: Context, contact_id: str, issue_date: str,
    lines: list[dict[str, Any]],
    reference: str = "", notes: str = "",
    invoice_id: str = "",
) -> dict[str, Any]:
    """Create a DRAFT credit note.

    Args:
        contact_id: customer/supplier UUID.
        issue_date: ISO date.
        lines: [{description, quantity, unit_price, account_id, tax_code_id}].
        invoice_id: link to original invoice this credits (optional).
    """
    body: dict[str, Any] = {
        "contact_id": contact_id, "issue_date": issue_date, "lines": lines,
    }
    for k, v in (("reference", reference), ("notes", notes), ("invoice_id", invoice_id)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/credit_notes", body)


@_gated_tool(safety="mutation")
async def update_credit_note(
    ctx: Context, credit_note_id: str, version: int,
    contact_id: str = "", issue_date: str = "",
    lines: list[dict[str, Any]] | None = None,
    reference: str = "", notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT credit note."""
    body = _drop_empty({
        "contact_id": contact_id, "issue_date": issue_date, "lines": lines,
        "reference": reference, "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/credit_notes/{credit_note_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_credit_note(ctx: Context, credit_note_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → POSTED."""
    return await _post(ctx, f"/api/v1/credit_notes/{credit_note_id}/post", if_match=version)


@_gated_tool(safety="void")
async def void_credit_note(ctx: Context, credit_note_id: str, version: int) -> dict[str, Any]:
    """Void a POSTED credit note."""
    return await _post(ctx, f"/api/v1/credit_notes/{credit_note_id}/void", if_match=version)


@_gated_tool(safety="destructive")
async def delete_credit_note(ctx: Context, credit_note_id: str, version: int) -> dict[str, Any]:
    """Delete a credit note. DRAFT soft-archived; POSTED HARD-DELETED."""
    return await _delete(ctx, f"/api/v1/credit_notes/{credit_note_id}", if_match=version)


# ===========================================================================
# Quotes
# ===========================================================================


@_gated_tool(safety="safe")
async def list_quotes(
    ctx: Context, status: str = "", contact_id: str = "",
    limit: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List quotes / estimates.

    Args:
        status: DRAFT | SENT | ACCEPTED | DECLINED | ARCHIVED.
    """
    return await _get(
        ctx, "/api/v1/quotes",
        status=status, contact_id=contact_id, limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_quote(ctx: Context, quote_id: str) -> dict[str, Any]:
    """Fetch a single quote with its lines."""
    return await _get(ctx, f"/api/v1/quotes/{quote_id}")


@_gated_tool(safety="mutation")
async def create_quote(
    ctx: Context, contact_id: str, issue_date: str,
    lines: list[dict[str, Any]],
    expires_at: str = "", reference: str = "", notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT quote."""
    body: dict[str, Any] = {
        "contact_id": contact_id, "issue_date": issue_date, "lines": lines,
    }
    for k, v in (("expires_at", expires_at), ("reference", reference), ("notes", notes)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/quotes", body)


@_gated_tool(safety="mutation")
async def update_quote(
    ctx: Context, quote_id: str, version: int,
    contact_id: str = "", issue_date: str = "", expires_at: str = "",
    lines: list[dict[str, Any]] | None = None,
    reference: str = "", notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT quote."""
    body = _drop_empty({
        "contact_id": contact_id, "issue_date": issue_date,
        "expires_at": expires_at, "lines": lines,
        "reference": reference, "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/quotes/{quote_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def send_quote(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Email a quote to the customer (DRAFT → SENT)."""
    return await _post(ctx, f"/api/v1/quotes/{quote_id}/send", if_match=version)


@_gated_tool(safety="mutation")
async def accept_quote(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Mark a quote as ACCEPTED by the customer."""
    return await _post(ctx, f"/api/v1/quotes/{quote_id}/accept", if_match=version)


@_gated_tool(safety="mutation")
async def decline_quote(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Mark a quote as DECLINED."""
    return await _post(ctx, f"/api/v1/quotes/{quote_id}/decline", if_match=version)


@_gated_tool(safety="void")
async def archive_quote(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Archive a quote (hides from default list)."""
    return await _post(ctx, f"/api/v1/quotes/{quote_id}/archive", if_match=version)


@_gated_tool(safety="mutation")
async def convert_quote_to_invoice(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Create a DRAFT invoice from an accepted quote."""
    return await _post(ctx, f"/api/v1/quotes/{quote_id}/convert-to-invoice", if_match=version)


@_gated_tool(safety="destructive")
async def delete_quote(ctx: Context, quote_id: str, version: int) -> dict[str, Any]:
    """Delete a quote permanently."""
    return await _delete(ctx, f"/api/v1/quotes/{quote_id}", if_match=version)


# ===========================================================================
# Payments
# ===========================================================================


@_gated_tool(safety="safe")
async def list_payments(
    ctx: Context, contact_id: str = "",
    from_date: str = "", to_date: str = "",
    limit: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List payments (received and made)."""
    return await _get(
        ctx, "/api/v1/payments",
        contact_id=contact_id, from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_payment(ctx: Context, payment_id: str) -> dict[str, Any]:
    """Fetch a single payment."""
    return await _get(ctx, f"/api/v1/payments/{payment_id}")


@_gated_tool(safety="mutation")
async def create_payment(
    ctx: Context,
    payment_date: str,
    bank_account_id: str,
    amount: float,
    direction: str,
    contact_id: str = "",
    allocations: list[dict[str, Any]] | None = None,
    reference: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Record a payment received or made.

    Args:
        payment_date: ISO date.
        bank_account_id: account that received/sent the money.
        amount: positive number.
        direction: "RECEIVED" (from customer) or "MADE" (to supplier).
        contact_id: customer or supplier UUID.
        allocations: [{invoice_id|bill_id, amount}] — how the payment
            applies to outstanding invoices/bills. If empty, payment
            sits unallocated on the contact's account.
        reference: bank-statement reference.
    """
    body: dict[str, Any] = {
        "payment_date": payment_date,
        "bank_account_id": bank_account_id,
        "amount": amount,
        "direction": direction,
    }
    if contact_id:
        body["contact_id"] = contact_id
    if allocations:
        body["allocations"] = allocations
    if reference:
        body["reference"] = reference
    if notes:
        body["notes"] = notes
    return await _post(ctx, "/api/v1/payments", body)


@_gated_tool(safety="mutation")
async def update_payment(
    ctx: Context, payment_id: str, version: int,
    payment_date: str = "",
    bank_account_id: str = "",
    amount: float | None = None,
    contact_id: str = "",
    allocations: list[dict[str, Any]] | None = None,
    reference: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Edit a payment."""
    body = _drop_empty({
        "payment_date": payment_date,
        "bank_account_id": bank_account_id,
        "amount": amount,
        "contact_id": contact_id,
        "allocations": allocations,
        "reference": reference,
        "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/payments/{payment_id}", body, if_match=version)


@_gated_tool(safety="void")
async def delete_payment(ctx: Context, payment_id: str, version: int) -> dict[str, Any]:
    """Delete a payment (un-allocates any matched invoices/bills)."""
    return await _delete(ctx, f"/api/v1/payments/{payment_id}", if_match=version)


# ===========================================================================
# Bank statement lines + reconciliation
# ===========================================================================


@_gated_tool(safety="safe")
async def list_bank_statement_lines(
    ctx: Context,
    bank_account_id: str = "",
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """List bank statement lines (the raw rows from a bank feed / CSV).

    Args:
        bank_account_id: filter to one account.
        status: UNMATCHED | MATCHED | RECONCILED | IGNORED.
    """
    return await _get(
        ctx, "/api/v1/bank_statement_lines",
        bank_account_id=bank_account_id, status=status,
        from_date=from_date, to_date=to_date,
        limit=limit, page=page,
    )


@_gated_tool(safety="safe")
async def get_bank_statement_line(ctx: Context, line_id: str) -> dict[str, Any]:
    """Fetch a single statement line."""
    return await _get(ctx, f"/api/v1/bank_statement_lines/{line_id}")


@_gated_tool(safety="mutation")
async def create_bank_statement_line(
    ctx: Context, bank_account_id: str, transaction_date: str,
    amount: float, description: str, reference: str = "",
) -> dict[str, Any]:
    """Create a single statement line (use ``imports`` for bulk CSV upload)."""
    body: dict[str, Any] = {
        "bank_account_id": bank_account_id,
        "transaction_date": transaction_date,
        "amount": amount,
        "description": description,
    }
    if reference:
        body["reference"] = reference
    return await _post(ctx, "/api/v1/bank_statement_lines", body)


@_gated_tool(safety="mutation")
async def update_bank_statement_line(
    ctx: Context, line_id: str, version: int,
    description: str = "", reference: str = "", amount: float | None = None,
) -> dict[str, Any]:
    """Edit a statement line (description / reference / amount)."""
    body = _drop_empty({
        "description": description, "reference": reference, "amount": amount,
    })
    return await _patch(ctx, f"/api/v1/bank_statement_lines/{line_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def match_bank_statement_line(
    ctx: Context, line_id: str,
    target_type: str, target_id: str,
) -> dict[str, Any]:
    """Match a statement line to an invoice / bill / expense / JE.

    Args:
        line_id: bank statement line UUID.
        target_type: invoice | bill | expense | journal_entry | payment.
        target_id: UUID of the target entity.
    """
    return await _post(
        ctx, f"/api/v1/bank_statement_lines/{line_id}/match",
        {"target_type": target_type, "target_id": target_id},
    )


@_gated_tool(safety="mutation")
async def split_match_bank_statement_line(
    ctx: Context, line_id: str, allocations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Split-match a statement line across multiple targets.

    Args:
        allocations: [{target_type, target_id, amount}].
    """
    return await _post(
        ctx, f"/api/v1/bank_statement_lines/{line_id}/split_match",
        {"allocations": allocations},
    )


@_gated_tool(safety="mutation")
async def unmatch_bank_statement_line(ctx: Context, line_id: str) -> dict[str, Any]:
    """Remove an existing match — returns the line to UNMATCHED."""
    return await _post(ctx, f"/api/v1/bank_statement_lines/{line_id}/unmatch")


@_gated_tool(safety="destructive")
async def delete_bank_statement_line(ctx: Context, line_id: str, version: int) -> dict[str, Any]:
    """Delete a bank statement line. Irreversible."""
    return await _delete(ctx, f"/api/v1/bank_statement_lines/{line_id}", if_match=version)


@_gated_tool(safety="safe")
async def list_reconciliation_accounts(ctx: Context) -> dict[str, Any]:
    """List bank accounts with reconciliation status (unmatched count, last reconciled)."""
    return await _get(ctx, "/api/v1/reconciliation/accounts")


@_gated_tool(safety="safe")
async def list_unmatched(ctx: Context, bank_account_id: str = "") -> dict[str, Any]:
    """List statement lines awaiting reconciliation."""
    return await _get(ctx, "/api/v1/reconciliation/unmatched", bank_account_id=bank_account_id)


@_gated_tool(safety="safe")
async def suggest_match(ctx: Context, bsl_id: str) -> dict[str, Any]:
    """Get suggested matches for an unmatched statement line."""
    return await _get(ctx, f"/api/v1/reconciliation/suggest/{bsl_id}")


@_gated_tool(safety="mutation")
async def reconciliation_match(
    ctx: Context, bsl_id: str, target_type: str, target_id: str,
) -> dict[str, Any]:
    """Match a statement line via the reconciliation endpoint."""
    return await _post(
        ctx, "/api/v1/reconciliation/match",
        {"bsl_id": bsl_id, "target_type": target_type, "target_id": target_id},
    )


@_gated_tool(safety="mutation")
async def reconciliation_auto_match(ctx: Context, bank_account_id: str) -> dict[str, Any]:
    """Run auto-matching across all unmatched lines for an account.

    Applies bank-rules + heuristics. Returns counts of matches made.
    """
    return await _post(ctx, "/api/v1/reconciliation/auto_match", {"bank_account_id": bank_account_id})


@_gated_tool(safety="mutation")
async def reconciliation_unmatch(ctx: Context, bsl_id: str) -> dict[str, Any]:
    """Unmatch a statement line via the reconciliation endpoint."""
    return await _post(ctx, f"/api/v1/reconciliation/unmatch/{bsl_id}")


# ===========================================================================
# Projects / departments / cost centres
# ===========================================================================


@_gated_tool(safety="safe")
async def list_projects(ctx: Context, archived: bool = False, limit: int = 100) -> dict[str, Any]:
    """List projects."""
    return await _get(ctx, "/api/v1/projects", archived=str(archived).lower(), limit=limit)


@_gated_tool(safety="safe")
async def get_project(ctx: Context, project_id: str) -> dict[str, Any]:
    """Fetch a single project."""
    return await _get(ctx, f"/api/v1/projects/{project_id}")


@_gated_tool(safety="mutation")
async def create_project(
    ctx: Context, name: str, code: str = "",
    contact_id: str = "", description: str = "",
    default_billable: bool = False, default_rate: float | None = None,
) -> dict[str, Any]:
    """Create a project."""
    body: dict[str, Any] = {"name": name, "default_billable": default_billable}
    for k, v in (("code", code), ("contact_id", contact_id), ("description", description)):
        if v:
            body[k] = v
    if default_rate is not None:
        body["default_rate"] = default_rate
    return await _post(ctx, "/api/v1/projects", body)


@_gated_tool(safety="mutation")
async def update_project(
    ctx: Context, project_id: str, version: int,
    name: str = "", code: str = "", contact_id: str = "",
    description: str = "", default_billable: bool | None = None,
    default_rate: float | None = None,
) -> dict[str, Any]:
    """Edit a project."""
    body = _drop_empty({
        "name": name, "code": code, "contact_id": contact_id,
        "description": description, "default_billable": default_billable,
        "default_rate": default_rate,
    })
    return await _patch(ctx, f"/api/v1/projects/{project_id}", body, if_match=version)


@_gated_tool(safety="void")
async def archive_project(ctx: Context, project_id: str, version: int) -> dict[str, Any]:
    """Archive a project (hidden from default list)."""
    return await _delete(ctx, f"/api/v1/projects/{project_id}", if_match=version)


# ===========================================================================
# Chart of accounts
# ===========================================================================


@_gated_tool(safety="safe")
async def list_accounts(ctx: Context, account_type: str = "") -> dict[str, Any]:
    """List chart-of-accounts entries.

    Args:
        account_type: ASSET | LIABILITY | EQUITY | INCOME | EXPENSE.
    """
    return await _get(ctx, "/api/v1/accounts", account_type=account_type)


@_gated_tool(safety="safe")
async def get_account(ctx: Context, account_id: str) -> dict[str, Any]:
    """Fetch a single account."""
    return await _get(ctx, f"/api/v1/accounts/{account_id}")


@_gated_tool(safety="mutation")
async def create_account(
    ctx: Context, code: str, name: str, account_type: str,
    parent_id: str = "", description: str = "",
    tax_code_id: str = "",
) -> dict[str, Any]:
    """Add an account to the chart of accounts.

    Args:
        code: short code (e.g. "4-1100").
        name: human-readable name.
        account_type: ASSET | LIABILITY | EQUITY | INCOME | EXPENSE.
        parent_id: optional parent account UUID for nesting.
        tax_code_id: default tax code for transactions on this account.
    """
    body: dict[str, Any] = {"code": code, "name": name, "account_type": account_type}
    for k, v in (("parent_id", parent_id), ("description", description), ("tax_code_id", tax_code_id)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/accounts", body)


@_gated_tool(safety="mutation")
async def update_account(
    ctx: Context, account_id: str, version: int,
    code: str = "", name: str = "", description: str = "",
    tax_code_id: str = "",
) -> dict[str, Any]:
    """Edit an account."""
    body = _drop_empty({
        "code": code, "name": name, "description": description,
        "tax_code_id": tax_code_id,
    })
    return await _patch(ctx, f"/api/v1/accounts/{account_id}", body, if_match=version)


@_gated_tool(safety="void")
async def archive_account(ctx: Context, account_id: str, version: int) -> dict[str, Any]:
    """Archive an account (hidden, transactions preserved)."""
    return await _delete(ctx, f"/api/v1/accounts/{account_id}", if_match=version)


# ===========================================================================
# Items (products / services)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_items(ctx: Context, search: str = "", limit: int = 50) -> dict[str, Any]:
    """List products / services."""
    return await _get(ctx, "/api/v1/items", search=search, limit=limit)


@_gated_tool(safety="safe")
async def get_item(ctx: Context, item_id: str) -> dict[str, Any]:
    """Fetch a single item."""
    return await _get(ctx, f"/api/v1/items/{item_id}")


@_gated_tool(safety="safe")
async def get_item_stock(ctx: Context, item_id: str) -> dict[str, Any]:
    """Current stock-on-hand for a tracked item."""
    return await _get(ctx, f"/api/v1/items/{item_id}/stock")


@_gated_tool(safety="mutation")
async def create_item(
    ctx: Context, code: str, name: str,
    sale_price: float | None = None, sale_account_id: str = "",
    purchase_price: float | None = None, purchase_account_id: str = "",
    tax_code_id: str = "", tracked: bool = False,
) -> dict[str, Any]:
    """Create a product/service item."""
    body: dict[str, Any] = {"code": code, "name": name, "tracked": tracked}
    for k, v in (
        ("sale_price", sale_price), ("sale_account_id", sale_account_id),
        ("purchase_price", purchase_price), ("purchase_account_id", purchase_account_id),
        ("tax_code_id", tax_code_id),
    ):
        if v not in (None, ""):
            body[k] = v
    return await _post(ctx, "/api/v1/items", body)


@_gated_tool(safety="mutation")
async def update_item(
    ctx: Context, item_id: str, version: int,
    code: str = "", name: str = "",
    sale_price: float | None = None, purchase_price: float | None = None,
    tax_code_id: str = "",
) -> dict[str, Any]:
    """Edit an item."""
    body = _drop_empty({
        "code": code, "name": name,
        "sale_price": sale_price, "purchase_price": purchase_price,
        "tax_code_id": tax_code_id,
    })
    return await _patch(ctx, f"/api/v1/items/{item_id}", body, if_match=version)


@_gated_tool(safety="void")
async def archive_item(ctx: Context, item_id: str, version: int) -> dict[str, Any]:
    """Archive an item."""
    return await _delete(ctx, f"/api/v1/items/{item_id}", if_match=version)


# ===========================================================================
# Tax codes
# ===========================================================================


@_gated_tool(safety="safe")
async def list_tax_codes(ctx: Context) -> dict[str, Any]:
    """List tax codes (GST/VAT rates)."""
    return await _get(ctx, "/api/v1/tax_codes")


@_gated_tool(safety="safe")
async def get_tax_code(ctx: Context, tax_code_id: str) -> dict[str, Any]:
    """Fetch a tax code."""
    return await _get(ctx, f"/api/v1/tax_codes/{tax_code_id}")


@_gated_tool(safety="mutation")
async def create_tax_code(
    ctx: Context, code: str, name: str, rate: float,
    tax_account_id: str = "", description: str = "",
) -> dict[str, Any]:
    """Create a tax code (e.g. 10% GST)."""
    body: dict[str, Any] = {"code": code, "name": name, "rate": rate}
    for k, v in (("tax_account_id", tax_account_id), ("description", description)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/tax_codes", body)


@_gated_tool(safety="mutation")
async def update_tax_code(
    ctx: Context, tax_code_id: str, version: int,
    name: str = "", rate: float | None = None,
    description: str = "",
) -> dict[str, Any]:
    """Edit a tax code."""
    body = _drop_empty({"name": name, "rate": rate, "description": description})
    return await _patch(ctx, f"/api/v1/tax_codes/{tax_code_id}", body, if_match=version)


@_gated_tool(safety="void")
async def archive_tax_code(ctx: Context, tax_code_id: str, version: int) -> dict[str, Any]:
    """Archive a tax code."""
    return await _delete(ctx, f"/api/v1/tax_codes/{tax_code_id}", if_match=version)


# ===========================================================================
# Bank rules + allocation rules
# ===========================================================================


@_gated_tool(safety="safe")
async def list_bank_rules(ctx: Context, bank_account_id: str = "") -> dict[str, Any]:
    """List bank-feed auto-categorisation rules."""
    return await _get(ctx, "/api/v1/bank_rules", bank_account_id=bank_account_id)


@_gated_tool(safety="mutation")
async def create_bank_rule(
    ctx: Context, name: str,
    match_type: str, match_value: str,
    target_account_id: str, tax_code_id: str = "",
    contact_id: str = "", bank_account_id: str = "",
) -> dict[str, Any]:
    """Create a bank rule (auto-classify matching statement lines).

    Args:
        match_type: CONTAINS | STARTS_WITH | REGEX | EXACT.
        match_value: text or regex against statement-line description.
        target_account_id: where to post matches.
        tax_code_id: optional default tax code.
        contact_id: optional default contact.
        bank_account_id: scope to one bank account (else applies globally).
    """
    body: dict[str, Any] = {
        "name": name, "match_type": match_type, "match_value": match_value,
        "target_account_id": target_account_id,
    }
    for k, v in (
        ("tax_code_id", tax_code_id), ("contact_id", contact_id),
        ("bank_account_id", bank_account_id),
    ):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/bank_rules", body)


@_gated_tool(safety="mutation")
async def update_bank_rule(
    ctx: Context, rule_id: str, version: int,
    name: str = "", match_type: str = "", match_value: str = "",
    target_account_id: str = "", tax_code_id: str = "", contact_id: str = "",
) -> dict[str, Any]:
    """Edit a bank rule."""
    body = _drop_empty({
        "name": name, "match_type": match_type, "match_value": match_value,
        "target_account_id": target_account_id, "tax_code_id": tax_code_id,
        "contact_id": contact_id,
    })
    return await _patch(ctx, f"/api/v1/bank_rules/{rule_id}", body, if_match=version)


@_gated_tool(safety="void")
async def archive_bank_rule(ctx: Context, rule_id: str, version: int) -> dict[str, Any]:
    """Archive a bank rule."""
    return await _delete(ctx, f"/api/v1/bank_rules/{rule_id}", if_match=version)


@_gated_tool(safety="safe")
async def list_allocation_rules(ctx: Context) -> dict[str, Any]:
    """List journal-allocation rules."""
    return await _get(ctx, "/api/v1/allocation_rules")


@_gated_tool(safety="mutation")
async def create_allocation_rule(
    ctx: Context, name: str,
    source_account_id: str, splits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create an allocation rule (split costs across accounts/projects).

    Args:
        splits: [{account_id|project_id, percentage}] — must sum to 100.
    """
    return await _post(ctx, "/api/v1/allocation_rules", {
        "name": name, "source_account_id": source_account_id, "splits": splits,
    })


# ===========================================================================
# Attachments (receipts, supporting docs)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_attachments(
    ctx: Context, entity_type: str = "", entity_id: str = "",
) -> dict[str, Any]:
    """List uploaded files, optionally scoped to one entity."""
    return await _get(
        ctx, "/api/v1/attachments",
        entity_type=entity_type, entity_id=entity_id,
    )


@_gated_tool(safety="safe")
async def get_attachment(ctx: Context, file_id: str) -> dict[str, Any]:
    """Fetch attachment metadata."""
    return await _get(ctx, f"/api/v1/attachments/{file_id}")


@_gated_tool(safety="mutation")
async def upload_attachment(
    ctx: Context, entity_type: str, entity_id: str,
    filename: str, content_base64: str, content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload a file (PDF receipt, photo, etc.) attached to an entity.

    Args:
        entity_type: invoice | bill | expense | journal_entry | contact.
        entity_id: UUID of the target.
        filename: name to store as.
        content_base64: file bytes encoded as base64.
        content_type: MIME type.
    """
    return await _post(ctx, "/api/v1/attachments", {
        "entity_type": entity_type, "entity_id": entity_id,
        "filename": filename, "content_base64": content_base64,
        "content_type": content_type,
    })


@_gated_tool(safety="void")
async def delete_attachment(ctx: Context, file_id: str) -> dict[str, Any]:
    """Remove an attachment (the file is purged from storage)."""
    return await _delete(ctx, f"/api/v1/attachments/{file_id}")


# ===========================================================================
# Companies (multi-company / settings)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_companies(ctx: Context) -> dict[str, Any]:
    """List companies in this tenant."""
    return await _get(ctx, "/api/v1/companies")


@_gated_tool(safety="safe")
async def get_company(ctx: Context, company_id: str) -> dict[str, Any]:
    """Fetch one company's settings."""
    return await _get(ctx, f"/api/v1/companies/{company_id}")


@_gated_tool(safety="mutation")
async def update_company(
    ctx: Context, company_id: str, version: int,
    name: str = "", legal_name: str = "", trading_name: str = "",
    abn: str = "", acn: str = "",
    gst_registered: bool | None = None, gst_effective_date: str = "",
    bookkeeping_mode: str = "",
    address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit company settings (GST registration, address, mode, ...)."""
    body = _drop_empty({
        "name": name, "legal_name": legal_name, "trading_name": trading_name,
        "abn": abn, "acn": acn,
        "gst_registered": gst_registered, "gst_effective_date": gst_effective_date,
        "bookkeeping_mode": bookkeeping_mode,
        "address": address,
    })
    return await _patch(ctx, f"/api/v1/companies/{company_id}", body, if_match=version)


@_gated_tool(safety="safe")
async def gst_backdate_preview(ctx: Context, company_id: str, new_effective_date: str) -> dict[str, Any]:
    """Preview the effect of backdating GST registration on past entries."""
    return await _get(
        ctx, f"/api/v1/companies/{company_id}/gst-backdate-preview",
        new_effective_date=new_effective_date,
    )


@_gated_tool(safety="destructive")
async def delete_company(ctx: Context, company_id: str, version: int) -> dict[str, Any]:
    """Delete a company — irreversible, all data goes with it.

    Almost certainly not what you want. Archive the company in the
    UI instead.
    """
    return await _delete(ctx, f"/api/v1/companies/{company_id}", if_match=version)


# ===========================================================================
# Time entries (carried over from v0.1 — now with safety annotations)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_time_entries(
    ctx: Context, user_id: str = "", project_id: str = "",
    contact_id: str = "", approval_status: str = "", billable: str = "",
    from_date: str = "", to_date: str = "",
    limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """List time entries with filters."""
    return await _get(
        ctx, "/api/v1/time-entries",
        user_id=user_id, project_id=project_id, contact_id=contact_id,
        approval_status=approval_status, billable=billable,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )


@_gated_tool(safety="safe")
async def get_time_entry(ctx: Context, entry_id: str) -> dict[str, Any]:
    """Fetch a single time entry by id."""
    return await _get(ctx, f"/api/v1/time-entries/{entry_id}")


@_gated_tool(safety="safe")
async def weekly_time_grid(
    ctx: Context, week_start: str, user_id: str = "",
) -> dict[str, Any]:
    """Fetch a Mon–Sun weekly grid of time entries.

    Args:
        week_start: Monday of the target week, ISO date YYYY-MM-DD.
        user_id: optional worker UUID — defaults to the authenticated user.
    """
    return await _get(
        ctx, "/api/v1/time-entries/week",
        week_start=week_start, user_id=user_id,
    )


@_gated_tool(safety="mutation")
async def create_time_entry(
    ctx: Context, work_date: str, hours: float,
    description: str = "", user_id: str = "",
    project_id: str = "", contact_id: str = "",
    department_id: str = "", cost_centre_id: str = "",
    start_time: str = "", end_time: str = "",
    break_minutes: int = 0,
    billable: bool = False, rate: float | None = None,
) -> dict[str, Any]:
    """Log time worked on a given day."""
    body: dict[str, Any] = {
        "work_date": work_date, "hours": hours,
        "break_minutes": break_minutes, "billable": billable,
    }
    for k, v in (
        ("description", description), ("user_id", user_id),
        ("project_id", project_id), ("contact_id", contact_id),
        ("department_id", department_id), ("cost_centre_id", cost_centre_id),
        ("start_time", start_time), ("end_time", end_time),
    ):
        if v:
            body[k] = v
    if rate is not None:
        body["rate"] = rate
    return await _post(ctx, "/api/v1/time-entries", body)


@_gated_tool(safety="mutation")
async def update_time_entry(
    ctx: Context, entry_id: str, version: int,
    work_date: str = "", hours: float | None = None,
    description: str = "", project_id: str = "", contact_id: str = "",
    billable: bool | None = None, rate: float | None = None,
) -> dict[str, Any]:
    """Edit a DRAFT or REJECTED time entry."""
    body = _drop_empty({
        "work_date": work_date, "hours": hours, "description": description,
        "project_id": project_id, "contact_id": contact_id,
        "billable": billable, "rate": rate,
    })
    return await _patch(ctx, f"/api/v1/time-entries/{entry_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def submit_time_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → SUBMITTED."""
    return await _post(ctx, f"/api/v1/time-entries/{entry_id}/submit", if_match=version)


@_gated_tool(safety="mutation")
async def approve_time_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Transition SUBMITTED → APPROVED (admin only)."""
    return await _post(ctx, f"/api/v1/time-entries/{entry_id}/approve", if_match=version)


@_gated_tool(safety="mutation")
async def reject_time_entry(
    ctx: Context, entry_id: str, version: int, reason: str = "",
) -> dict[str, Any]:
    """Transition SUBMITTED → REJECTED with an optional reason."""
    body: dict[str, Any] = {}
    if reason:
        body["reason"] = reason
    return await _post(
        ctx, f"/api/v1/time-entries/{entry_id}/reject", body, if_match=version,
    )


@_gated_tool(safety="mutation")
async def convert_time_entries_to_invoice(
    ctx: Context, entry_ids: list[str], contact_id: str = "",
) -> dict[str, Any]:
    """Bundle N billable time entries into one DRAFT invoice line."""
    return await _post(ctx, "/api/v1/time-entries/convert-to-invoice", {
        "entry_ids": entry_ids, "contact_id": contact_id,
    })


@_gated_tool(safety="void")
async def archive_time_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Soft-delete a time entry (only allowed in DRAFT/REJECTED)."""
    return await _delete(ctx, f"/api/v1/time-entries/{entry_id}", if_match=version)


# ===========================================================================
# Reports (all safe — read-only)
# ===========================================================================


@_gated_tool(safety="safe")
async def trial_balance(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Trial balance as at a date."""
    return await _get(ctx, "/api/v1/reports/trial_balance", as_at=as_at)


@_gated_tool(safety="safe")
async def profit_and_loss(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """P&L between two dates (inclusive)."""
    return await _get(
        ctx, "/api/v1/reports/profit_loss", from_date=from_date, to_date=to_date
    )


@_gated_tool(safety="safe")
async def balance_sheet(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Balance sheet as at a date."""
    return await _get(ctx, "/api/v1/reports/balance_sheet", as_at=as_at)


@_gated_tool(safety="safe")
async def aged_receivables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Aged receivables (AR aging)."""
    return await _get(ctx, "/api/v1/reports/aged_receivables", as_at=as_at)


@_gated_tool(safety="safe")
async def aged_payables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Aged payables (AP aging)."""
    return await _get(ctx, "/api/v1/reports/aged_payables", as_at=as_at)


@_gated_tool(safety="safe")
async def cash_flow(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """Cash flow statement."""
    return await _get(ctx, "/api/v1/reports/cashflow", from_date=from_date, to_date=to_date)


@_gated_tool(safety="safe")
async def bas_summary(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """BAS summary (G1/1A/W1/W2/etc.) for an AU GST period."""
    return await _get(
        ctx, "/api/v1/reports/bas_summary", from_date=from_date, to_date=to_date,
    )


@_gated_tool(safety="safe")
async def budget_vs_actual(ctx: Context, from_date: str, to_date: str, budget_id: str = "") -> dict[str, Any]:
    """Budget vs actual report."""
    return await _get(
        ctx, "/api/v1/reports/budget_vs_actual",
        from_date=from_date, to_date=to_date, budget_id=budget_id,
    )


@_gated_tool(safety="safe")
async def depreciation_schedule(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Fixed-asset depreciation schedule."""
    return await _get(ctx, "/api/v1/reports/depreciation_schedule", as_at=as_at)


@_gated_tool(safety="safe")
async def fx_revaluation(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """FX revaluation report (unrealised gains/losses)."""
    return await _get(ctx, "/api/v1/reports/fx_revaluation", as_at=as_at)


@_gated_tool(safety="safe")
async def pl_by_segment(
    ctx: Context, from_date: str, to_date: str,
    segment_by: str = "project",
) -> dict[str, Any]:
    """P&L sliced by project / department / cost-centre.

    Args:
        segment_by: project | department | cost_centre.
    """
    return await _get(
        ctx, "/api/v1/reports/pl_by_segment",
        from_date=from_date, to_date=to_date, segment_by=segment_by,
    )


@_gated_tool(safety="safe")
async def revenue_by_customer(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """Revenue grouped by customer."""
    return await _get(
        ctx, "/api/v1/reports/revenue_by_customer",
        from_date=from_date, to_date=to_date,
    )


@_gated_tool(safety="safe")
async def ytd_turnover(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Year-to-date turnover (useful for GST threshold checks)."""
    return await _get(ctx, "/api/v1/reports/ytd_turnover", as_at=as_at)


# ===========================================================================
# ATO SBR (BAS / STP lodgement — destructive)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_ato_sbr_configs(ctx: Context) -> dict[str, Any]:
    """List ATO SBR machine-credential configurations."""
    return await _get(ctx, "/api/v1/ato_sbr")


@_gated_tool(safety="destructive")
async def prepare_bas(
    ctx: Context, period_from: str, period_to: str,
) -> dict[str, Any]:
    """Prepare a BAS draft for lodgement to the ATO.

    Generates the SBR payload from the BAS summary report. Review the
    output carefully before calling ``lodge_bas`` — lodgement is
    irrevocable.
    """
    return await _post(
        ctx, "/api/v1/ato_sbr/prepare-bas",
        {"period_from": period_from, "period_to": period_to},
    )


@_gated_tool(safety="destructive")
async def lodge_bas(ctx: Context, bas_draft_id: str) -> dict[str, Any]:
    """Lodge a prepared BAS with the ATO. Irreversible.

    ⚠ Lodging to the ATO is a legal filing — only call this after
    confirming the BAS draft is correct.
    """
    return await _post(ctx, "/api/v1/ato_sbr/lodge-bas", {"bas_draft_id": bas_draft_id})


# ===========================================================================
# Integrations (Xero / Stripe / Companies House / LEI / Paperless)
# ===========================================================================


@_gated_tool(safety="mutation")
async def ato_prefill(ctx: Context, identifier: str) -> dict[str, Any]:
    """ATO pre-fill lookup (e.g. ABN → company details)."""
    return await _post(ctx, "/api/v1/integrations/ato/prefill", {"identifier": identifier})


@_gated_tool(safety="mutation")
async def companies_house_search(ctx: Context, query: str) -> dict[str, Any]:
    """UK Companies House search."""
    return await _post(ctx, "/api/v1/integrations/companies-house/search", {"query": query})


@_gated_tool(safety="mutation")
async def lei_lookup(ctx: Context, lei: str) -> dict[str, Any]:
    """Global LEI (Legal Entity Identifier) lookup."""
    return await _post(ctx, "/api/v1/integrations/lei/lookup", {"lei": lei})


@_gated_tool(safety="safe")
async def stripe_customer_info(ctx: Context, contact_id: str = "") -> dict[str, Any]:
    """Get Stripe customer info linked to a SAE Books contact."""
    return await _get(ctx, "/api/v1/integrations/stripe/customer", contact_id=contact_id)


@_gated_tool(safety="mutation")
async def stripe_connect_customer(
    ctx: Context, contact_id: str, stripe_customer_id: str,
) -> dict[str, Any]:
    """Link an existing Stripe customer to a SAE Books contact."""
    return await _post(
        ctx, "/api/v1/integrations/stripe/customer/connect",
        {"contact_id": contact_id, "stripe_customer_id": stripe_customer_id},
    )


# ===========================================================================
# Imports (CSV bulk upload)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_imports(ctx: Context) -> dict[str, Any]:
    """List past CSV/JSON imports."""
    return await _get(ctx, "/api/v1/imports")


@_gated_tool(safety="mutation")
async def start_import(
    ctx: Context, import_type: str, filename: str,
    content_base64: str, options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a bulk import.

    Args:
        import_type: contacts | bank_statement | journal_entries | invoices | bills.
        filename: source filename.
        content_base64: CSV/JSON bytes encoded as base64.
        options: import-type-specific config (delimiter, date format, etc.).
    """
    body: dict[str, Any] = {
        "import_type": import_type, "filename": filename,
        "content_base64": content_base64,
    }
    if options:
        body["options"] = options
    return await _post(ctx, "/api/v1/imports", body)


def streamable_http_asgi_app():
    """Return a Starlette ASGI app serving the Streamable-HTTP MCP transport.

    The saebooks FastAPI app mounts this at /mcp so every instance
    speaks MCP natively at ``https://books.<tenant>/mcp`` without a
    separate container. Auth: the MCP transport's ``Authorization:
    Bearer saebk_*`` header is forwarded to the loopback REST API by
    ``_client_for(ctx)``; ``require_bearer`` then resolves the token
    to a user + tenant_id and binds RLS for the duration of the call.

    Sets ``streamable_http_path`` to ``/`` so the inner Starlette
    route serves at the mount root — full external path stays ``/mcp``
    (or ``/mcp/`` after the implicit trailing-slash redirect) rather
    than the awkward ``/mcp/mcp/`` you'd get with the SDK's default.
    """
    if os.getenv("SAEBOOKS_MCP_DISABLE_HOST_CHECK", "1") == "1":
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.streamable_http_path = "/"
    return mcp.streamable_http_app()


def sse_asgi_app():
    """SSE transport variant for clients that prefer SSE over Streamable-HTTP."""
    if os.getenv("SAEBOOKS_MCP_DISABLE_HOST_CHECK", "1") == "1":
        mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.sse_path = "/"
    mcp.settings.message_path = "/messages/"
    return mcp.sse_app()


def main() -> None:
    transport = os.getenv("SAEBOOKS_MCP_TRANSPORT", "sse").lower()
    if transport == "stdio":
        mcp.run("stdio")
    elif transport in ("sse", "http", "streamable-http"):
        host = os.getenv("SAEBOOKS_MCP_HOST", "0.0.0.0")
        port = int(os.getenv("SAEBOOKS_MCP_PORT", "8000"))
        logger.info(
            "saebooks-mcp serving %s on %s:%d (api=%s, max_safety=%s)",
            transport, host, port, API_BASE, _MAX_SAFETY,
        )
        mcp.settings.host = host
        mcp.settings.port = port
        if os.getenv("SAEBOOKS_MCP_DISABLE_HOST_CHECK", "1") == "1":
            mcp.settings.transport_security.enable_dns_rebinding_protection = False
        mcp.run(transport)
    else:
        raise SystemExit(f"unknown transport: {transport}")


if __name__ == "__main__":
    main()
