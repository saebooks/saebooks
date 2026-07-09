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

import base64
import json
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
        "bank reconciliation, projects, accounts, time tracking, "
        "reports, and the document inbox (capture a receipt/invoice → "
        "AI extraction → human review → publish as a DRAFT record). "
        "Every tool talks to a SAE Books instance over REST; "
        "the user's API token (issued at /admin/api-tokens) "
        "authenticates the call. "
        "Default currency AUD. Dates are ISO-8601 (YYYY-MM-DD). "
        "Mutating tools that change POSTED entities use If-Match: "
        "<version> for optimistic locking — fetch the entity first "
        "to read its current version. "
        "\n\n"
        "GOLDEN RULE — NEVER author a manual journal entry as a shortcut. "
        "The ledger is DERIVED from real business records. To record an "
        "economic event, use the record-type tool that models it and let "
        "the engine post the journal: "
        "create_invoice (sales / money owed to you), "
        "create_credit_note (refund / reduction of an invoice or bill), "
        "create_bill (supplier purchases on terms), "
        "create_expense (cash / card spend), "
        "create_payment (money in or out, with allocations), "
        "plus the item / depreciation / fixed-asset and bank-reconciliation "
        "tools for their respective events. These produce a real record "
        "with provenance (origin=INVOICE/BILL/… + source_type/source_id) and "
        "a proper audit trail. "
        "create_journal_entry is the EXCEPTION PATH — use it ONLY for a "
        "genuine adjustment / correction that no record-type tool can "
        "express (e.g. an accountant's year-end reclassification). A manual "
        "JE stamps origin=MANUAL: it is a visible exception, weaker audit "
        "practice, and must carry a written reason. If you find yourself "
        "reaching for create_journal_entry to record a normal transaction, "
        "STOP and use the correct record type instead — if no tool fits the "
        "event, that is a missing record type to flag, not a manual JE to "
        "write. To move money between two of your own accounts use "
        "create_transfer (origin=TRANSFER), not a manual JE; for a "
        "reciprocal posting between two of your companies use "
        "intercompany_post (origin=INTERCOMPANY), never two hand-balanced "
        "manual JEs."
    ),
)
# Expose the application version via MCP initialize serverInfo.
# FastMCP does not accept a version= kwarg; we set it on the
# underlying low-level server directly after construction. Mirror
# saebooks.__version__ so the three surfaces (OpenAPI /openapi.json,
# /api/v1/version, and MCP initialize) all agree — see
# tests/test_version_unification.py for the asserts.
mcp._mcp_server.version = __version__


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

    headers = {"User-Agent": f"saebooks-mcp/{__version__}"}
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


_ADDRESS_FIELD_MAP = {
    "line1": "address_line1",
    "line2": "address_line2",
    "city": "city",
    "state": "state",
    "postcode": "postcode",
    "country": "country",
}


def _flatten_address(address: dict[str, Any]) -> dict[str, Any]:
    """Map the MCP-facing nested {line1, line2, city, state, postcode,
    country} shape onto the flat address_line1/address_line2/city/state/
    postcode/country fields Contact{Create,Update} actually expect.
    Also passes through already-flat keys unchanged (callers may supply
    either shape).
    """
    out: dict[str, Any] = {}
    for k, v in address.items():
        if v in (None, ""):
            continue
        out[_ADDRESS_FIELD_MAP.get(k, k)] = v
    return out


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
async def search(ctx: Context, query: str) -> dict[str, Any]:
    """Global search across contacts, invoices, bills, accounts.

    Args:
        query: free-text search string.

    Returns up to 10 hits per entity type (40 total) — the API has no
    per-call limit override.
    """
    return await _get(ctx, "/api/v1/search", q=query)


@_gated_tool(safety="safe")
async def recent_changes(
    ctx: Context,
    entity: str = "",
    since: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Recent change-log entries — what changed when, by whom.

    Args:
        entity: filter to one entity (invoice, bill, contact, etc.).
        since: change-log cursor (row id) lower bound — 0 for the start.
            Use the ``next_cursor`` from a previous call to page forward.
        limit: max rows (1-5000).

    The underlying endpoint streams NDJSON (one JSON object per line),
    not a single JSON document — this tool parses that for you and
    returns ``{"items": [...], "next_cursor": <int|None>}``.
    """
    async with _client_for(ctx) as client:
        resp = await client.get(
            "/api/v1/changes",
            params=_clean_params({"entity": entity, "since": since, "limit": limit}),
        )
        resp.raise_for_status()
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        items = [json.loads(ln) for ln in lines]
        next_cursor = resp.headers.get("X-Cursor-Next")
        return {
            "items": items,
            "next_cursor": int(next_cursor) if next_cursor is not None else None,
        }


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
        contact_type: CUSTOMER | SUPPLIER | CONTRACTOR | SUB_CONTRACTOR | BOTH | BENEFICIARY.
        limit: page size (max 500).
        page: 1-indexed page number (converted to an offset — the API
            has no page-number param).
    """
    offset = max(page - 1, 0) * limit
    return await _get(
        ctx,
        "/api/v1/contacts",
        q=search,
        type=contact_type,
        limit=limit,
        offset=offset,
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
        contact_type: CUSTOMER (default), SUPPLIER, CONTRACTOR,
            SUB_CONTRACTOR, BOTH, BENEFICIARY.
            CONTRACTOR = higher-tier entity delivering a whole section of a
              job (spend is COST OF SALES, recommend 5-2000 Contractor
              Costs; NOT TPAR-reportable — ATO materials-incidental
              exemption, Richard’s informed call; is_tpar_supplier=False).
            SUB_CONTRACTOR = middle-tier labour-services payee under a
              contractor (spend is EXPENSE; TPAR-reportable — set
              is_tpar_supplier=True). Both are payable like a SUPPLIER.
        email: optional.
        phone: optional.
        abn: optional 11-digit ABN (Australian Business Number).
        address: optional {line1, line2, city, state, postcode, country} —
            flattened onto address_line1/address_line2/city/state/postcode/
            country before sending (the API has no nested 'address' field).
    """
    body: dict[str, Any] = {"name": name, "contact_type": contact_type}
    if email:
        body["email"] = email
    if phone:
        body["phone"] = phone
    if abn:
        body["abn"] = abn
    if address:
        body.update(_flatten_address(address))
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
    })
    if address:
        body.update(_flatten_address(address))
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
    date_from: str = "",
    date_to: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List invoices.

    Args:
        status: DRAFT | POSTED | VOIDED | WRITTEN_OFF — empty for all.
        contact_id: filter to one customer.
        date_from: ISO date YYYY-MM-DD invoice-date lower bound.
        date_to: ISO date YYYY-MM-DD upper bound.
        page_size: page size (max 500).
    """
    return await _get(
        ctx, "/api/v1/invoices",
        status=status, contact_id=contact_id,
        date_from=date_from, date_to=date_to,
        page_size=page_size, page=page,
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
    due_date: str,
    lines: list[dict[str, Any]] | None = None,
    currency: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT invoice.

    Args:
        contact_id: customer UUID.
        issue_date: ISO date YYYY-MM-DD.
        due_date: ISO date — REQUIRED, no server-side default.
        lines: list of {description, quantity, unit_price, account_id,
            tax_code_id, project_id?, item_id?, discount_pct?}.
        currency: 3-letter ISO; defaults to company base currency.
        notes: optional internal notes.

    There is no ``reference`` field on invoices — the API has none.

    The invoice is DRAFT — call ``post_invoice`` to finalise.

    Use this (not a manual journal entry) to record a sale or money a
    customer owes you. Posting derives the AR + revenue + GST journal for
    you with origin=INVOICE provenance.
    """
    body: dict[str, Any] = {
        "contact_id": contact_id,
        "issue_date": issue_date,
        "due_date": due_date,
        "lines": lines or [],
    }
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
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT invoice. Empty fields are left unchanged.

    There is no ``reference`` field on invoices — the API has none.
    """
    body = _drop_empty({
        "contact_id": contact_id,
        "issue_date": issue_date,
        "due_date": due_date,
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
    """Delete an invoice. DRAFT is soft-archived; POSTED is VOIDED with a
    reversing journal entry (NOT hard-deleted — this MCP tool never sends
    the hard-delete admin gate). Both outcomes are reversible/auditable.
    Prefer ``void_invoice`` for explicit intent.
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
    date_from: str = "",
    date_to: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List supplier bills (accounts payable).

    Args:
        status: DRAFT | POSTED | VOIDED — empty for all.
        contact_id: filter to one supplier.
        date_from, date_to: ISO date bounds on the bill's issue date.
        page_size: page size (max 500).
    """
    return await _get(
        ctx, "/api/v1/bills",
        status=status, contact_id=contact_id,
        date_from=date_from, date_to=date_to,
        page_size=page_size, page=page,
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

    Use this (not a manual journal entry) to record a supplier purchase on
    terms. Posting derives the AP + expense/asset + GST journal with
    origin=BILL provenance.
    """
    body: dict[str, Any] = {
        "contact_id": contact_id,
        "issue_date": issue_date,
        "lines": lines or [],
    }
    # The bill create/update API field is ``supplier_reference`` (the
    # supplier's own invoice number), NOT ``reference`` — map it so the
    # MCP-facing ``reference`` arg actually lands on the bill.
    for k, v in (("due_date", due_date), ("supplier_reference", reference),
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
        "due_date": due_date, "supplier_reference": reference,
        "lines": lines, "notes": notes,
    })
    return await _patch(ctx, f"/api/v1/bills/{bill_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_bill(ctx: Context, bill_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → POSTED (writes journal entries)."""
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
    date_from: str = "",
    date_to: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List expenses (paid at point of purchase, no AP tracking)."""
    return await _get(
        ctx, "/api/v1/expenses",
        status=status, contact_id=contact_id,
        date_from=date_from, date_to=date_to,
        page_size=page_size, page=page,
    )


@_gated_tool(safety="safe")
async def get_expense(ctx: Context, expense_id: str) -> dict[str, Any]:
    """Fetch a single expense."""
    return await _get(ctx, f"/api/v1/expenses/{expense_id}")


@_gated_tool(safety="mutation")
async def create_expense(
    ctx: Context,
    expense_date: str,
    payment_account_id: str,
    lines: list[dict[str, Any]],
    contact_id: str = "",
    reference: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT expense (e.g. cash receipt or card purchase).

    Args:
        expense_date: ISO date.
        payment_account_id: UUID of the account that paid (chequing, card, etc.).
        lines: [{description, account_id, tax_code_id?, quantity?, unit_price?,
            discount_pct?, project_id?}] — quantity/unit_price, NOT 'amount'.
        contact_id: supplier UUID (optional for cash receipts).
        reference: receipt number / memo.
        notes: internal notes.

    Use this (not a manual journal entry) to record cash or card spend.
    Posting derives the bank/credit + expense + GST journal with
    origin=EXPENSE provenance.
    """
    body: dict[str, Any] = {
        "expense_date": expense_date,
        "payment_account_id": payment_account_id,
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
    expense_date: str = "",
    payment_account_id: str = "",
    contact_id: str = "",
    reference: str = "",
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT expense."""
    body = _drop_empty({
        "expense_date": expense_date, "payment_account_id": payment_account_id,
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
    date_from: str = "",
    date_to: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List journal entries.

    Args:
        status: DRAFT | POSTED | VOIDED — empty for all.
        date_from, date_to: ISO date bounds on the entry date.
        page_size: page size (max 500).
    """
    return await _get(
        ctx, "/api/v1/journal_entries",
        status=status, date_from=date_from, date_to=date_to,
        page_size=page_size, page=page,
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
    reason: str,
    description: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """EXCEPTION PATH — create a DRAFT manual journal entry.

    Use this ONLY when NO record-type tool fits the economic event — i.e. a
    genuine adjustment or correction (e.g. an accountant's year-end
    reclassification, an opening-balance setup, a write-off with no invoice).
    Prefer the proper record type for everything else:

      - money owed to you / a sale            -> create_invoice
      - a refund / reduction of an invoice    -> create_credit_note
      - a supplier purchase on terms          -> create_bill
      - cash or card spend                    -> create_expense
      - money received or paid (+allocations) -> create_payment
      - moving money between your own accounts -> create_transfer
      - a reciprocal posting between two of your companies -> intercompany_post

    Those tools create a real record with provenance (origin=INVOICE/BILL/…
    + source_type/source_id) and a full audit trail. A manual JE is BAD
    ACCOUNTING PRACTICE: it is stamped origin=MANUAL (a visible exception),
    carries a weaker audit trail than a record-derived posting, and is the
    shortcut this tool deliberately makes harder to reach. If no tool fits
    the event, FLAG the missing record type rather than hand-writing a JE.

    Args:
        entry_date: ISO date.
        lines: [{account_id, debit, credit, description?, tax_code_id?, ...}].
            Sum of debits MUST equal sum of credits.
        reason: REQUIRED — why a manual JE is justified here and why no
            record-type tool fits. Persisted into the entry narration so the
            manual exception is self-documenting in the ledger.
        description: optional header narration (appended after the reason).
        reference: external reference.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "create_journal_entry requires a non-empty 'reason' explaining why "
            "a manual journal entry is justified and why no record-type tool "
            "(invoice/credit_note/bill/expense/payment/transfer/intercompany) "
            "fits this event. Prefer the record type."
        )
    narration = f"[MANUAL JE] {reason.strip()}"
    if description and description.strip():
        narration = f"{narration} — {description.strip()}"
    body: dict[str, Any] = {
        "entry_date": entry_date,
        "lines": lines,
        "narration": narration,
    }
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
    narration: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT journal entry."""
    body = _drop_empty({
        "entry_date": entry_date, "lines": lines,
        "narration": narration, "reference": reference,
    })
    return await _patch(ctx, f"/api/v1/journal_entries/{entry_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def post_journal_entry(ctx: Context, entry_id: str, version: int) -> dict[str, Any]:
    """Transition DRAFT → POSTED."""
    return await _post(ctx, f"/api/v1/journal_entries/{entry_id}/post", if_match=version)


@_gated_tool(safety="destructive")
async def reverse_journal_entry(
    ctx: Context, entry_id: str, version: int, reversal_date: str = ""
) -> dict[str, Any]:
    """Post a reversing JE for a previously POSTED entry.

    Creates a new entry with debits/credits swapped, dated reversal_date
    (defaults to the ORIGINAL entry's date when omitted — NOT today).
    Both entries remain in the ledger — this is the correct way to "undo"
    a posted journal without breaking audit.
    """
    body: dict[str, Any] = {}
    if reversal_date:
        body["reversal_date"] = reversal_date
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
# Transfers — account-to-account money movement (origin=TRANSFER)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_transfers(
    ctx: Context,
    account_id: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List transfers for the active company, newest first.

    Args:
        account_id: filter to transfers touching this account (either side).
        date_from, date_to: ISO date bounds on the transfer date.
    """
    return await _get(
        ctx, "/api/v1/transfers",
        account_id=account_id, date_from=date_from, date_to=date_to,
        page=page, page_size=page_size,
    )


@_gated_tool(safety="safe")
async def get_transfer(ctx: Context, transfer_id: str) -> dict[str, Any]:
    """Fetch a single transfer."""
    return await _get(ctx, f"/api/v1/transfers/{transfer_id}")


@_gated_tool(safety="mutation")
async def create_transfer(
    ctx: Context,
    from_account_id: str,
    to_account_id: str,
    amount: float,
    transfer_date: str,
    description: str = "",
    reference: str = "",
) -> dict[str, Any]:
    """Move money between two of your OWN accounts.

    Use this (not a manual journal entry) to move money between two of your own
    accounts — bank->bank, credit-card paydown, director-loan repayment,
    bank/loan transfer. Money LEAVES ``from_account_id`` and ARRIVES at
    ``to_account_id``; both must be balance-sheet accounts (asset / liability /
    equity) of the active company. No GST. Posting derives ONE balanced
    balance-sheet journal (Dr to / Cr from) with origin=TRANSFER provenance and
    creates+posts the transfer in one step.

    Args:
        from_account_id: UUID of the account money leaves (e.g. the bank).
        to_account_id: UUID of the account money arrives at (e.g. the card
            liability or the director-loan account).
        amount: positive number.
        transfer_date: ISO date.
        description: memo carried onto the journal lines.
        reference: external reference (e.g. bank-statement ref).
    """
    body: dict[str, Any] = {
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": amount,
        "transfer_date": transfer_date,
    }
    if description:
        body["description"] = description
    if reference:
        body["reference"] = reference
    return await _post(ctx, "/api/v1/transfers", body)


@_gated_tool(safety="void")
async def reverse_transfer(
    ctx: Context, transfer_id: str, reversal_date: str = ""
) -> dict[str, Any]:
    """Reverse a POSTED transfer.

    Posts the swapped mirror journal and flips the transfer to REVERSED. This
    is the correct way to undo a transfer without breaking audit.

    Args:
        transfer_id: the transfer to reverse.
        reversal_date: ISO date for the reversal (defaults to the engine's
            choice when omitted).
    """
    body: dict[str, Any] = {}
    if reversal_date:
        body["reversal_date"] = reversal_date
    return await _post(ctx, f"/api/v1/transfers/{transfer_id}/reverse", body)


# ===========================================================================
# Intercompany — reciprocal posting between two of your companies
# (origin=INTERCOMPANY)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_intercompany(
    ctx: Context, page: int = 1, page_size: int = 50
) -> dict[str, Any]:
    """List intercompany transactions touching the active company."""
    return await _get(
        ctx, "/api/v1/intercompany", page=page, page_size=page_size
    )


@_gated_tool(safety="safe")
async def get_intercompany(ctx: Context, ic_txn_id: str) -> dict[str, Any]:
    """Fetch a single intercompany transaction with its two legs."""
    return await _get(ctx, f"/api/v1/intercompany/{ic_txn_id}")


@_gated_tool(safety="mutation")
async def intercompany_post(
    ctx: Context,
    originator_company_id: str,
    counterparty_company_id: str,
    amount: float,
    entry_date: str,
    originator_contra_account_id: str,
    counterparty_contra_account_id: str,
    description: str = "",
) -> dict[str, Any]:
    """Post a reciprocal pair between two of YOUR companies.

    Use this (not two hand-balanced manual JEs) for a reciprocal posting
    between two companies you own that share one tenant — e.g. one company
    funding another, an intercompany loan or settlement. The engine posts a
    LINKED pair of journals (one per company) inside one transaction with
    origin=INTERCOMPANY provenance: if either leg fails, neither posts (no
    half-pair). The "Due to/from" control account on each side comes from the
    pre-declared intercompany edge, NOT from you.

    Sign convention (fixed): the originator's control account is DEBITED (a
    receivable / "due from") and its contra credited; the counterparty's
    control account is CREDITED (an obligation / "due to") and its contra
    debited. To post the opposite economic direction, swap which company is the
    originator.

    Args:
        originator_company_id: UUID of the company that originates the event
            (the one with the receivable / "due from").
        counterparty_company_id: UUID of the partner company (the obligation /
            "due to" side). Both companies must be in your tenant and a
            reciprocal intercompany edge pair must already exist.
        amount: positive number.
        entry_date: ISO date for both legs.
        originator_contra_account_id: the originator's contra account (e.g. its
            bank) — must belong to the originator company.
        counterparty_contra_account_id: the counterparty's contra account (e.g.
            its bank) — must belong to the counterparty company.
        description: memo carried onto both legs and the shared txn.
    """
    body: dict[str, Any] = {
        "originator_company_id": originator_company_id,
        "counterparty_company_id": counterparty_company_id,
        "amount": amount,
        "entry_date": entry_date,
        "originator_contra_account_id": originator_contra_account_id,
        "counterparty_contra_account_id": counterparty_contra_account_id,
    }
    if description:
        body["description"] = description
    return await _post(ctx, "/api/v1/intercompany", body)


@_gated_tool(safety="void")
async def reverse_intercompany(
    ctx: Context, ic_txn_id: str, reversal_date: str = ""
) -> dict[str, Any]:
    """Reverse an intercompany transaction by reversing BOTH legs.

    Posts the swapped mirror of each leg, links them to the same intercompany
    txn, and flips it to REVERSED. The correct way to unwind an intercompany
    posting without breaking audit.

    Args:
        ic_txn_id: the intercompany transaction to reverse.
        reversal_date: ISO date for the reversals (defaults to the engine's
            choice when omitted).
    """
    body: dict[str, Any] = {}
    if reversal_date:
        body["reversal_date"] = reversal_date
    return await _post(ctx, f"/api/v1/intercompany/{ic_txn_id}/reverse", body)


# ===========================================================================
# Credit notes
# ===========================================================================


@_gated_tool(safety="safe")
async def list_credit_notes(
    ctx: Context, status: str = "", contact_id: str = "",
    page_size: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List credit notes."""
    return await _get(
        ctx, "/api/v1/credit_notes",
        status=status, contact_id=contact_id, page_size=page_size, page=page,
    )


@_gated_tool(safety="safe")
async def get_credit_note(ctx: Context, credit_note_id: str) -> dict[str, Any]:
    """Fetch a single credit note."""
    return await _get(ctx, f"/api/v1/credit_notes/{credit_note_id}")


@_gated_tool(safety="mutation")
async def create_credit_note(
    ctx: Context, contact_id: str, issue_date: str,
    lines: list[dict[str, Any]],
    reason: str = "", notes: str = "",
    original_invoice_id: str = "",
) -> dict[str, Any]:
    """Create a DRAFT credit note.

    Args:
        contact_id: customer/supplier UUID.
        issue_date: ISO date.
        lines: [{description, quantity, unit_price, account_id, tax_code_id}].
        reason: optional free-text reason for the credit (there is no
            'reference' field on credit notes).
        original_invoice_id: link to the original invoice this credits
            (optional).

    Use this (not a manual journal entry) to record a refund or a
    reduction of an invoice/bill. Posting derives the reversing AR/AP +
    revenue + GST journal with origin=CREDIT_NOTE provenance.
    """
    body: dict[str, Any] = {
        "contact_id": contact_id, "issue_date": issue_date, "lines": lines,
    }
    for k, v in (
        ("reason", reason), ("notes", notes),
        ("original_invoice_id", original_invoice_id),
    ):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/credit_notes", body)


@_gated_tool(safety="mutation")
async def update_credit_note(
    ctx: Context, credit_note_id: str, version: int,
    contact_id: str = "", issue_date: str = "",
    lines: list[dict[str, Any]] | None = None,
    reason: str = "", notes: str = "",
    original_invoice_id: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT credit note. There is no 'reference' field."""
    body = _drop_empty({
        "contact_id": contact_id, "issue_date": issue_date, "lines": lines,
        "reason": reason, "notes": notes,
        "original_invoice_id": original_invoice_id,
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
    """Delete a credit note — voids it (POSTED) or archives it (DRAFT).
    This MCP tool never sends the hard-delete admin gate, so the credit
    note is never permanently removed."""
    return await _delete(ctx, f"/api/v1/credit_notes/{credit_note_id}", if_match=version)


# ===========================================================================
# Quotes
# ===========================================================================


@_gated_tool(safety="safe")
async def list_quotes(
    ctx: Context, status: str = "", customer_id: str = "",
    page_size: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List quotes / estimates.

    Args:
        status: DRAFT | SENT | ACCEPTED | DECLINED | ARCHIVED.
        customer_id: filter to one customer.
    """
    return await _get(
        ctx, "/api/v1/quotes",
        status=status, customer_id=customer_id, page_size=page_size, page=page,
    )


@_gated_tool(safety="safe")
async def get_quote(ctx: Context, quote_id: str) -> dict[str, Any]:
    """Fetch a single quote with its lines."""
    return await _get(ctx, f"/api/v1/quotes/{quote_id}")


@_gated_tool(safety="mutation")
async def create_quote(
    ctx: Context, customer_id: str, issue_date: str,
    lines: list[dict[str, Any]],
    expiry_date: str = "", notes: str = "",
) -> dict[str, Any]:
    """Create a DRAFT quote.

    Args:
        customer_id: customer UUID.
        issue_date: ISO date.
        lines: [{description, quantity?, unit_price?, tax_code_id?,
            account_id?, section_label?, material?, length_note?,
            drawing_ref?}].
        expiry_date: ISO date the quote lapses (there is no 'reference'
            field on quotes).
        notes: optional internal notes.
    """
    body: dict[str, Any] = {
        "customer_id": customer_id, "issue_date": issue_date, "lines": lines,
    }
    for k, v in (("expiry_date", expiry_date), ("notes", notes)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/quotes", body)


@_gated_tool(safety="mutation")
async def update_quote(
    ctx: Context, quote_id: str, version: int,
    customer_id: str = "", issue_date: str = "", expiry_date: str = "",
    lines: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Edit a DRAFT quote. There is no 'reference' field."""
    body = _drop_empty({
        "customer_id": customer_id, "issue_date": issue_date,
        "expiry_date": expiry_date, "lines": lines,
        "notes": notes,
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
    date_from: str = "", date_to: str = "",
    page_size: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List payments (received and made)."""
    return await _get(
        ctx, "/api/v1/payments",
        contact_id=contact_id, date_from=date_from, date_to=date_to,
        page_size=page_size, page=page,
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
    contact_id: str,
    allocations: list[dict[str, Any]] | None = None,
    reference: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Record a payment received or made.

    Args:
        payment_date: ISO date.
        bank_account_id: account that received/sent the money.
        amount: positive number.
        direction: "INCOMING" (from customer) or "OUTGOING" (to supplier).
        contact_id: customer or supplier UUID — REQUIRED (no unallocated,
            contact-less payments; use allocations=[] for an unallocated
            payment that still names a contact).
        allocations: [{invoice_id|bill_id|credit_note_id, amount}] — how
            the payment applies to outstanding invoices/bills/credit
            notes. If empty, payment sits unallocated on the contact's
            account.
        reference: bank-statement reference.

    Use this (not a manual journal entry) to record money in or out and to
    apply it against invoices/bills. Posting derives the bank + AR/AP
    journal with origin=PAYMENT provenance. To move money between two of
    your OWN accounts, use create_transfer rather than a payment or a
    manual JE.
    """
    body: dict[str, Any] = {
        "payment_date": payment_date,
        "bank_account_id": bank_account_id,
        "amount": amount,
        "direction": direction,
        "contact_id": contact_id,
    }
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
    """Void a payment (un-allocates any matched invoices/bills). This
    MCP tool never sends the hard-delete admin gate, so the payment
    record itself is not removed — it's marked VOIDED."""
    return await _delete(ctx, f"/api/v1/payments/{payment_id}", if_match=version)


# ===========================================================================
# Bank statement lines + reconciliation
# ===========================================================================


@_gated_tool(safety="safe")
async def list_bank_statement_lines(
    ctx: Context,
    bank_account_id: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List bank statement lines (the raw rows from a bank feed / CSV).

    Args:
        bank_account_id: filter to one account.
        status: UNMATCHED | PARTIAL | MATCHED | IGNORED.
        date_from, date_to: ISO date bounds on the transaction date.
        offset: 0-indexed row offset (there is no page-number param).
    """
    return await _get(
        ctx, "/api/v1/bank_statement_lines",
        bank_account_id=bank_account_id, status=status,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


@_gated_tool(safety="safe")
async def get_bank_statement_line(ctx: Context, line_id: str) -> dict[str, Any]:
    """Fetch a single statement line."""
    return await _get(ctx, f"/api/v1/bank_statement_lines/{line_id}")


@_gated_tool(safety="mutation")
async def create_bank_statement_line(
    ctx: Context, account_id: str, txn_date: str,
    amount: float, description: str = "", reference: str = "",
) -> dict[str, Any]:
    """Create a single statement line (use ``imports`` for bulk CSV upload).

    Args:
        account_id: bank/cash account UUID this line belongs to.
        txn_date: ISO date of the transaction.
        amount: signed amount (positive = deposit, negative = withdrawal).
        description: statement line description.
        reference: optional bank reference.
    """
    body: dict[str, Any] = {
        "account_id": account_id,
        "txn_date": txn_date,
        "amount": amount,
    }
    if description:
        body["description"] = description
    if reference:
        body["reference"] = reference
    return await _post(ctx, "/api/v1/bank_statement_lines", body)


@_gated_tool(safety="mutation")
async def update_bank_statement_line(
    ctx: Context, line_id: str, version: int,
    description: str = "", reference: str = "",
) -> dict[str, Any]:
    """Edit a statement line (description / reference).

    There is no editable 'amount' field — statement-line amounts are
    immutable once imported.
    """
    body = _drop_empty({
        "description": description, "reference": reference,
    })
    return await _patch(ctx, f"/api/v1/bank_statement_lines/{line_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def match_bank_statement_line(
    ctx: Context, line_id: str,
    matched_to_type: str, matched_to_id: str,
) -> dict[str, Any]:
    """Match a statement line to a payment or journal entry.

    Args:
        line_id: bank statement line UUID.
        matched_to_type: PAYMENT or JOURNAL_ENTRY (only these two kinds).
        matched_to_id: UUID of the matching payment or journal entry.
    """
    return await _post(
        ctx, f"/api/v1/bank_statement_lines/{line_id}/match",
        {"matched_to_type": matched_to_type, "matched_to_id": matched_to_id},
    )


@_gated_tool(safety="mutation")
async def split_match_bank_statement_line(
    ctx: Context, line_id: str, allocations: list[dict[str, Any]],
    entry_date: str = "", description: str = "",
) -> dict[str, Any]:
    """Split-match a statement line across multiple GL accounts.

    Posts a journal entry whose non-bank-account side is built from
    ``allocations``; the bank-account side is auto-generated from the
    statement line's amount.

    Args:
        allocations: [{account_id, debit?, credit?, description?,
            tax_code_id?}] — each row needs a non-zero debit OR credit.
            sum(credit) - sum(debit) across allocations must equal the
            statement line's amount.
        entry_date: ISO date for the journal entry (defaults to the
            statement line's transaction date).
        description: header narration for the journal entry.
    """
    body: dict[str, Any] = {"allocations": allocations}
    if entry_date:
        body["entry_date"] = entry_date
    if description:
        body["description"] = description
    return await _post(
        ctx, f"/api/v1/bank_statement_lines/{line_id}/split_match", body,
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
async def list_unmatched(ctx: Context, account_id: str) -> dict[str, Any]:
    """List statement lines awaiting reconciliation for one bank account.

    Args:
        account_id: bank/cash account UUID — REQUIRED.
    """
    return await _get(ctx, "/api/v1/reconciliation/unmatched", account_id=account_id)


@_gated_tool(safety="safe")
async def suggest_match(ctx: Context, bsl_id: str) -> dict[str, Any]:
    """Get suggested matches for an unmatched statement line."""
    return await _get(ctx, f"/api/v1/reconciliation/suggest/{bsl_id}")


@_gated_tool(safety="mutation")
async def reconciliation_match(
    ctx: Context, bsl_id: str, entry_id: str,
) -> dict[str, Any]:
    """Match a statement line to a posted journal entry via the
    reconciliation endpoint (exact-amount matching only).

    Args:
        bsl_id: bank statement line UUID.
        entry_id: posted journal entry UUID to match it to.
    """
    return await _post(
        ctx, "/api/v1/reconciliation/match",
        {"bsl_id": bsl_id, "entry_id": entry_id},
    )


@_gated_tool(safety="mutation")
async def reconciliation_auto_match(ctx: Context, account_id: str) -> dict[str, Any]:
    """Run auto-matching (exact-amount, first candidate) across all
    unmatched lines for an account.

    Args:
        account_id: bank/cash account UUID — REQUIRED query param (the
            endpoint takes no request body).

    Returns ``{"matched": N}``.
    """
    async with _client_for(ctx) as client:
        resp = await client.post(
            "/api/v1/reconciliation/auto_match",
            params={"account_id": account_id},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


@_gated_tool(safety="mutation")
async def reconciliation_unmatch(ctx: Context, bsl_id: str) -> dict[str, Any]:
    """Unmatch a statement line via the reconciliation endpoint."""
    return await _post(ctx, f"/api/v1/reconciliation/unmatch/{bsl_id}")


# ===========================================================================
# Projects / departments / cost centres
# ===========================================================================


@_gated_tool(safety="safe")
async def list_projects(
    ctx: Context, status: str = "", archived: bool = False,
    page_size: int = 50, page: int = 1,
) -> dict[str, Any]:
    """List projects.

    Args:
        status: filter by project status (e.g. ACTIVE) — empty for all.
        archived: include archived projects.
        page_size: page size (max 500).
    """
    return await _get(
        ctx, "/api/v1/projects",
        status=status, archived=str(archived).lower(),
        page_size=page_size, page=page,
    )


@_gated_tool(safety="safe")
async def get_project(ctx: Context, project_id: str) -> dict[str, Any]:
    """Fetch a single project."""
    return await _get(ctx, f"/api/v1/projects/{project_id}")


@_gated_tool(safety="mutation")
async def create_project(
    ctx: Context, code: str, name: str,
    status: str = "", start_date: str = "", end_date: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create a project.

    Args:
        code: short project code — REQUIRED.
        name: project name — REQUIRED.
        status: defaults to ACTIVE.
        start_date, end_date: ISO dates.
        notes: optional notes.

    Projects have no contact_id / description / default_billable /
    default_rate fields — those job-costing concepts don't exist on
    this entity yet.
    """
    body: dict[str, Any] = {"code": code, "name": name}
    for k, v in (
        ("status", status), ("start_date", start_date),
        ("end_date", end_date), ("notes", notes),
    ):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/projects", body)


@_gated_tool(safety="mutation")
async def update_project(
    ctx: Context, project_id: str, version: int,
    name: str = "", code: str = "",
    status: str = "", start_date: str = "", end_date: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Edit a project."""
    body = _drop_empty({
        "name": name, "code": code, "status": status,
        "start_date": start_date, "end_date": end_date, "notes": notes,
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
    parent_id: str = "", tax_code_default: str = "",
    is_header: bool = False, reconcile: bool = False,
) -> dict[str, Any]:
    """Add an account to the chart of accounts.

    Args:
        code: short code (e.g. "4-1100").
        name: human-readable name.
        account_type: ASSET | LIABILITY | EQUITY | INCOME | EXPENSE.
        parent_id: optional parent account UUID for nesting.
        tax_code_default: default tax code (a code string, e.g. "GST",
            NOT a tax-code UUID) for transactions on this account.
        is_header: True for a non-postable grouping/summary account.
        reconcile: True to mark this a reconcilable bank/cash account.

    Accounts have no 'description' field.
    """
    body: dict[str, Any] = {
        "code": code, "name": name, "account_type": account_type,
        "is_header": is_header, "reconcile": reconcile,
    }
    for k, v in (("parent_id", parent_id), ("tax_code_default", tax_code_default)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/accounts", body)


@_gated_tool(safety="mutation")
async def update_account(
    ctx: Context, account_id: str, version: int,
    code: str = "", name: str = "", tax_code_default: str = "",
    is_header: bool | None = None, reconcile: bool | None = None,
) -> dict[str, Any]:
    """Edit an account. There is no 'description' field."""
    body = _drop_empty({
        "code": code, "name": name, "tax_code_default": tax_code_default,
        "is_header": is_header, "reconcile": reconcile,
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
async def list_items(ctx: Context, item_type: str = "", limit: int = 50) -> dict[str, Any]:
    """List products / services.

    Args:
        item_type: inventory | service — empty for all. There is no
            free-text search filter on this endpoint.
        limit: page size (max 1000).
    """
    return await _get(ctx, "/api/v1/items", item_type=item_type, limit=limit)


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
    ctx: Context, sku: str, name: str,
    inventory_account_id: str, cogs_account_id: str, income_account_id: str,
    item_type: str = "inventory", description: str = "",
    cost_method: str = "WAC", default_sale_price: float | None = None,
    on_hand_qty: float | None = None, wac_cost: float | None = None,
) -> dict[str, Any]:
    """Create a product/service item.

    Args:
        sku: unique item code (the field is ``sku``, not ``code``).
        name: item name.
        inventory_account_id: balance-sheet asset account for on-hand
            value — REQUIRED even for ``item_type="service"``.
        cogs_account_id: cost-of-goods-sold expense account — REQUIRED.
        income_account_id: sales/income account — REQUIRED.
        item_type: inventory (default) | service.
        description: optional.
        cost_method: WAC (default) — weighted-average cost.
        default_sale_price: default unit sale price.
        on_hand_qty: opening stock quantity (inventory items).
        wac_cost: opening weighted-average unit cost (inventory items).
    """
    body: dict[str, Any] = {
        "sku": sku, "name": name,
        "inventory_account_id": inventory_account_id,
        "cogs_account_id": cogs_account_id,
        "income_account_id": income_account_id,
        "item_type": item_type,
        "cost_method": cost_method,
    }
    if description:
        body["description"] = description
    if default_sale_price is not None:
        body["default_sale_price"] = default_sale_price
    if on_hand_qty is not None:
        body["on_hand_qty"] = on_hand_qty
    if wac_cost is not None:
        body["wac_cost"] = wac_cost
    return await _post(ctx, "/api/v1/items", body)


@_gated_tool(safety="mutation")
async def update_item(
    ctx: Context, item_id: str, version: int,
    sku: str = "", name: str = "", description: str = "",
    default_sale_price: float | None = None,
    inventory_account_id: str = "", cogs_account_id: str = "",
    income_account_id: str = "",
) -> dict[str, Any]:
    """Edit an item. on_hand_qty/wac_cost/cost_method/item_type are not
    editable via PATCH."""
    body = _drop_empty({
        "sku": sku, "name": name, "description": description,
        "default_sale_price": default_sale_price,
        "inventory_account_id": inventory_account_id,
        "cogs_account_id": cogs_account_id,
        "income_account_id": income_account_id,
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
    tax_system: str = "", reporting_type: str = "", description: str = "",
) -> dict[str, Any]:
    """Create a tax code (e.g. 10% GST).

    There is no ``tax_account_id`` field — tax codes aren't linked to a
    GL account directly.

    Args:
        tax_system: defaults to "GST".
        reporting_type: defaults to "taxable".
    """
    body: dict[str, Any] = {"code": code, "name": name, "rate": rate}
    for k, v in (
        ("tax_system", tax_system), ("reporting_type", reporting_type),
        ("description", description),
    ):
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
async def list_bank_rules(
    ctx: Context, active_only: bool = False, limit: int = 200, offset: int = 0,
) -> dict[str, Any]:
    """List bank-feed auto-categorisation rules.

    Bank rules aren't scoped to a bank account — there is no
    bank_account_id filter.
    """
    return await _get(
        ctx, "/api/v1/bank_rules",
        active_only=active_only, limit=limit, offset=offset,
    )


@_gated_tool(safety="mutation")
async def create_bank_rule(
    ctx: Context, name: str,
    match_type: str, match_pattern: str,
    account_id: str, tax_code: str = "",
    contact_id: str = "",
) -> dict[str, Any]:
    """Create a bank rule (auto-classify matching statement lines).

    Args:
        match_type: CONTAINS | STARTS_WITH | REGEX | EXACT.
        match_pattern: text or regex against statement-line description.
        account_id: where to post matches.
        tax_code: optional default tax code — a code string (e.g. "GST"),
            NOT a tax-code UUID.
        contact_id: optional default contact.

    Bank rules aren't scoped to a bank account — there is no
    bank_account_id field.
    """
    body: dict[str, Any] = {
        "name": name, "match_type": match_type, "match_pattern": match_pattern,
        "account_id": account_id,
    }
    for k, v in (("tax_code", tax_code), ("contact_id", contact_id)):
        if v:
            body[k] = v
    return await _post(ctx, "/api/v1/bank_rules", body)


@_gated_tool(safety="mutation")
async def update_bank_rule(
    ctx: Context, rule_id: str, version: int,
    name: str = "", match_type: str = "", match_pattern: str = "",
    account_id: str = "", tax_code: str = "", contact_id: str = "",
) -> dict[str, Any]:
    """Edit a bank rule."""
    body = _drop_empty({
        "name": name, "match_type": match_type, "match_pattern": match_pattern,
        "account_id": account_id, "tax_code": tax_code,
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
    source_account_id: str, targets: list[dict[str, Any]],
    description: str = "",
) -> dict[str, Any]:
    """Create an allocation rule (split costs across accounts).

    Args:
        targets: [{account_id, label?, percentage}] — percentages must
            sum to 100. There is no project_id target — allocation
            targets are accounts only.
        description: optional.
    """
    body: dict[str, Any] = {
        "name": name, "source_account_id": source_account_id, "targets": targets,
    }
    if description:
        body["description"] = description
    return await _post(ctx, "/api/v1/allocation_rules", body)


# ===========================================================================
# Attachments (receipts, supporting docs)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_attachments(
    ctx: Context, entity_kind: str, entity_id: str,
) -> dict[str, Any]:
    """List uploaded files for one entity.

    Args:
        entity_kind: invoice | bill | payment | contact | expense |
            credit_note | journal_entry — REQUIRED (there is no
            unscoped listing).
        entity_id: UUID of the target — REQUIRED.
    """
    return await _get(
        ctx, "/api/v1/attachments",
        entity_kind=entity_kind, entity_id=entity_id,
    )


@_gated_tool(safety="safe")
async def get_attachment(ctx: Context, file_id: str) -> dict[str, Any]:
    """Fetch attachment metadata."""
    return await _get(ctx, f"/api/v1/attachments/{file_id}")


@_gated_tool(safety="mutation")
async def upload_attachment(
    ctx: Context, entity_kind: str, entity_id: str,
    filename: str, content_base64: str, content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload a file (PDF receipt, photo, etc.) attached to an entity.

    For a receipt that should become an expense/bill, prefer
    ``upload_inbox_document`` — the document inbox extracts, codes and
    publishes it as a DRAFT record with the file already attached.

    Args:
        entity_kind: invoice | bill | payment | contact | expense |
            credit_note | journal_entry.
        entity_id: UUID of the target.
        filename: name to store as.
        content_base64: file bytes encoded as base64 — decoded and sent
            as a multipart file upload (the endpoint has no JSON body
            variant).
        content_type: MIME type.
    """
    raw = base64.b64decode(content_base64)
    async with _client_for(ctx) as client:
        resp = await client.post(
            "/api/v1/attachments",
            data={"entity_kind": entity_kind, "entity_id": entity_id},
            files={"file": (filename, raw, content_type)},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


@_gated_tool(safety="void")
async def delete_attachment(ctx: Context, file_id: str) -> dict[str, Any]:
    """Soft-delete an attachment (sets archived_at — the file is NOT
    purged from storage). Idempotent."""
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
    address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit company settings (GST registration, address, ...).

    Bookkeeping mode is NOT editable here — use
    ``set_bookkeeping_mode`` instead (it goes through a dedicated
    upgrade/downgrade endpoint, not a plain field PATCH).
    """
    body = _drop_empty({
        "name": name, "legal_name": legal_name, "trading_name": trading_name,
        "abn": abn, "acn": acn,
        "gst_registered": gst_registered, "gst_effective_date": gst_effective_date,
        "address": address,
    })
    return await _patch(ctx, f"/api/v1/companies/{company_id}", body, if_match=version)


@_gated_tool(safety="mutation")
async def set_bookkeeping_mode(
    ctx: Context, company_id: str, mode: str, bank_account_id: str = "",
) -> dict[str, Any]:
    """Switch a company between 'cashbook' (single-entry) and 'full'
    (double-entry) bookkeeping mode.

    Args:
        mode: "cashbook" or "full".
        bank_account_id: required only when downgrading full -> cashbook
            AND the company has no pre-existing
            cashbook_default_bank_account_id. Ignored on upgrade.
    """
    body: dict[str, Any] = {"mode": mode}
    if bank_account_id:
        body["bank_account_id"] = bank_account_id
    return await _post(ctx, f"/api/v1/companies/{company_id}/bookkeeping-mode", body)


@_gated_tool(safety="safe")
async def gst_backdate_preview(ctx: Context, company_id: str, effective_date: str) -> dict[str, Any]:
    """Preview the effect of backdating GST registration on past entries."""
    return await _get(
        ctx, f"/api/v1/companies/{company_id}/gst-backdate-preview",
        effective_date=effective_date,
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
    contact_id: str = "", approval_status: str = "",
    billable_only: bool = False, uninvoiced_only: bool = False,
    date_from: str = "", date_to: str = "",
    limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """List time entries with filters."""
    return await _get(
        ctx, "/api/v1/time-entries",
        user_id=user_id, project_id=project_id, contact_id=contact_id,
        approval_status=approval_status, billable_only=billable_only,
        uninvoiced_only=uninvoiced_only,
        date_from=date_from, date_to=date_to,
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
    ctx: Context, entry_id: str, version: int, reason: str,
) -> dict[str, Any]:
    """Transition SUBMITTED → REJECTED.

    Args:
        reason: REQUIRED — the endpoint has no default.
    """
    return await _post(
        ctx, f"/api/v1/time-entries/{entry_id}/reject", {"reason": reason},
        if_match=version,
    )


@_gated_tool(safety="mutation")
async def convert_time_entries_to_invoice(
    ctx: Context, entry_ids: list[str],
    contact_id: str = "", invoice_id: str = "",
) -> dict[str, Any]:
    """Bundle N billable time entries into one invoice line.

    Args:
        entry_ids: time entry UUIDs to convert.
        contact_id: create a new DRAFT invoice against this customer.
        invoice_id: append to this existing DRAFT invoice instead.

    Supply exactly one of ``contact_id`` or ``invoice_id``.
    """
    body: dict[str, Any] = {"entry_ids": entry_ids}
    if contact_id:
        body["contact_id"] = contact_id
    if invoice_id:
        body["invoice_id"] = invoice_id
    return await _post(ctx, "/api/v1/time-entries/convert-to-invoice", body)


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
    return await _get(ctx, "/api/v1/reports/trial_balance", as_of_date=as_at)


@_gated_tool(safety="safe")
async def profit_and_loss(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """P&L between two dates (inclusive)."""
    return await _get(
        ctx, "/api/v1/reports/profit_loss", from_date=from_date, to_date=to_date
    )


@_gated_tool(safety="safe")
async def balance_sheet(ctx: Context, as_at: str) -> dict[str, Any]:
    """Balance sheet as at a date.

    Args:
        as_at: ISO date — REQUIRED, the endpoint has no default.
    """
    return await _get(ctx, "/api/v1/reports/balance_sheet", as_of_date=as_at)


@_gated_tool(safety="safe")
async def aged_receivables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Aged receivables (AR aging)."""
    return await _get(ctx, "/api/v1/reports/aged_receivables", as_of_date=as_at)


@_gated_tool(safety="safe")
async def aged_payables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Aged payables (AP aging)."""
    return await _get(ctx, "/api/v1/reports/aged_payables", as_of_date=as_at)


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
async def budget_vs_actual(ctx: Context, year: int, month: int | None = None) -> dict[str, Any]:
    """Budget vs actual report for a financial year, or one month within it.

    Args:
        year: REQUIRED.
        month: 1-12 — when omitted the full year is aggregated.
    """
    return await _get(
        ctx, "/api/v1/reports/budget_vs_actual",
        year=year, month=month,
    )


@_gated_tool(safety="safe")
async def depreciation_schedule(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Fixed-asset depreciation schedule."""
    return await _get(ctx, "/api/v1/reports/depreciation_schedule", as_of_date=as_at)


@_gated_tool(safety="safe")
async def fx_revaluation(ctx: Context, as_at: str, base_currency: str = "") -> dict[str, Any]:
    """FX revaluation report (unrealised gains/losses).

    Args:
        as_at: ISO date — REQUIRED, the endpoint has no default.
        base_currency: defaults to "AUD".
    """
    return await _get(
        ctx, "/api/v1/reports/fx_revaluation",
        as_of_date=as_at, base_currency=base_currency,
    )


@_gated_tool(safety="safe")
async def pl_by_segment(
    ctx: Context, from_date: str, to_date: str,
    segment_type: str = "project",
) -> dict[str, Any]:
    """P&L sliced by project / department / cost-centre.

    Args:
        segment_type: project | department | cost_centre.
    """
    return await _get(
        ctx, "/api/v1/reports/pl_by_segment",
        from_date=from_date, to_date=to_date, segment_type=segment_type,
    )


@_gated_tool(safety="safe")
async def revenue_by_customer(ctx: Context, from_date: str, to_date: str) -> dict[str, Any]:
    """Revenue grouped by customer."""
    return await _get(
        ctx, "/api/v1/reports/revenue_by_customer",
        from_date=from_date, to_date=to_date,
    )


@_gated_tool(safety="safe")
async def ytd_turnover(ctx: Context) -> dict[str, Any]:
    """Year-to-date turnover for the CURRENT Australian financial year
    (useful for GST threshold checks). The endpoint takes no query
    params — historical-FY queries aren't supported."""
    return await _get(ctx, "/api/v1/reports/ytd_turnover")


# ===========================================================================
# ATO SBR (BAS / STP lodgement — destructive)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_ato_sbr_configs(ctx: Context) -> dict[str, Any]:
    """List ATO SBR machine-credential configurations."""
    return await _get(ctx, "/api/v1/ato_sbr/keystore")


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
async def ato_prefill(ctx: Context, period_start: str, period_end: str) -> dict[str, Any]:
    """ATO BAS pre-fill lookup for a period (currently a stub — returns
    501 until Machine Credential onboarding is complete). This is a BAS
    prefill, NOT an ABN/company lookup.

    Args:
        period_start, period_end: ISO dates bounding the BAS period.
    """
    return await _post(
        ctx, "/api/v1/integrations/ato/prefill",
        {"period_start": period_start, "period_end": period_end},
    )


@_gated_tool(safety="mutation")
async def companies_house_search(ctx: Context, query: str) -> dict[str, Any]:
    """UK Companies House search."""
    return await _post(ctx, "/api/v1/integrations/companies-house/search", {"query": query})


@_gated_tool(safety="mutation")
async def lei_lookup(ctx: Context, search: str) -> dict[str, Any]:
    """Global LEI (Legal Entity Identifier) lookup from GLEIF.

    Args:
        search: LEI code or entity name to search for.
    """
    return await _post(ctx, "/api/v1/integrations/lei/lookup", {"search": search})


@_gated_tool(safety="safe")
async def stripe_customer_info(ctx: Context) -> dict[str, Any]:
    """Get the tenant-level Stripe Connect status (connected/account_id/
    charges_enabled/payouts_enabled/details_submitted).

    This is NOT per-contact — the endpoint takes no contact_id.
    """
    return await _get(ctx, "/api/v1/integrations/stripe/customer")


@_gated_tool(safety="mutation")
async def stripe_connect_customer(ctx: Context) -> dict[str, Any]:
    """Initiate the tenant-level Stripe Connect OAuth flow.

    Returns ``{"authorize_url": "<url>", "state": "<hex>"}`` — redirect
    the user's browser to authorize_url to complete the connection.
    This does NOT link a specific contact to a specific Stripe
    customer id; there is no such per-contact linking endpoint.
    """
    return await _post(ctx, "/api/v1/integrations/stripe/customer/connect")


# ===========================================================================
# Imports (CSV bulk upload)
# ===========================================================================


_IMPORT_KINDS = ("bank_csv", "bank_ofx", "coa", "bill_csv", "qbo")


@_gated_tool(safety="safe")
async def list_imports(ctx: Context, kind: str = "", limit: int = 50) -> dict[str, Any]:
    """List active import wizards and their current state.

    SAE Books imports are multi-step *wizards*, NOT one-shot uploads: you
    create a wizard, upload the file as a step, review the merged state,
    then commit it. This lists the wizards that are still alive (not past
    their TTL) for the current tenant, so you can resume or commit one.

    Args:
        kind: optional filter — one of bank_csv, bank_ofx, coa, bill_csv, qbo.
        limit: max wizards to return (1-500).

    Returns ``{"wizards": [{wizard_id, kind, step, state, expires_at}, ...],
    "total": N}``. Backed by ``GET /api/v1/imports/wizards``.
    """
    return await _get(ctx, "/api/v1/imports/wizards", kind=kind, limit=limit)


@_gated_tool(safety="mutation")
async def start_import(
    ctx: Context,
    kind: str,
    raw: str = "",
    account_id: str = "",
    contacts_raw: str = "",
    accounts_raw: str = "",
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """Start a multi-step import wizard and upload its payload.

    SAE Books imports are wizards, not one-shot uploads. This tool does the
    first two steps for you: it creates the wizard
    (``POST /api/v1/imports/wizards``) and, when you pass the file text,
    uploads it as step 0 (``POST /api/v1/imports/wizards/{id}/step``). It
    does NOT auto-commit — inspect the returned ``state``, then commit with
    ``call_api`` (``POST /api/v1/imports/wizards/{id}/commit``) once you're
    satisfied.

    Args:
        kind: bank_csv | bank_ofx | coa | bill_csv | qbo.
        raw: file text for bank_csv / bank_ofx / coa / bill_csv (the raw
            CSV or OFX body — plain text, NOT base64).
        account_id: REQUIRED for bank_csv / bank_ofx — the bank ledger
            account UUID the statement lines are posted against.
        contacts_raw: QBO contacts CSV text (kind=qbo).
        accounts_raw: QBO accounts / chart-of-accounts CSV text (kind=qbo).
        ttl_seconds: wizard lifetime in seconds (default 3600).

    Returns ``{wizard_id, kind, step, state, completed, next}`` — ``next``
    is the remaining action (the commit URL when the payload is uploaded).
    """
    if kind not in _IMPORT_KINDS:
        raise ValueError(
            f"Unknown import kind {kind!r}. Must be one of {list(_IMPORT_KINDS)}"
        )

    initial: dict[str, Any] = {}
    if account_id:
        initial["account_id"] = account_id

    created = await _post(
        ctx,
        "/api/v1/imports/wizards",
        {"kind": kind, "initial": initial, "ttl_seconds": ttl_seconds},
    )
    wizard_id = created.get("wizard_id")

    # Build the upload patch for this kind. bank/coa/bill_csv read a single
    # ``raw`` blob; qbo reads separate contacts_raw / accounts_raw blobs.
    patch: dict[str, Any] = {}
    if kind in ("bank_csv", "bank_ofx", "coa", "bill_csv"):
        if raw:
            patch["raw"] = raw
    elif kind == "qbo":
        if contacts_raw:
            patch["contacts_raw"] = contacts_raw
        if accounts_raw:
            patch["accounts_raw"] = accounts_raw

    step_state: dict[str, Any] = created
    if patch and wizard_id:
        step_state = await _post(
            ctx,
            f"/api/v1/imports/wizards/{wizard_id}/step",
            {"step": created.get("step", 0), "patch": patch},
        )

    if patch and wizard_id:
        next_action = f"POST /api/v1/imports/wizards/{wizard_id}/commit"
    else:
        next_action = (
            f"upload the file via POST /api/v1/imports/wizards/{wizard_id}/step "
            "then commit"
        )

    return {
        "wizard_id": wizard_id,
        "kind": kind,
        "step": step_state.get("step", created.get("step", 0)),
        "state": step_state.get("state", step_state),
        "completed": step_state.get("completed", False),
        "next": next_action,
    }


# ===========================================================================
# Document inbox (issue #33 — capture → review → publish as DRAFT)
# ===========================================================================


@_gated_tool(safety="safe")
async def list_inbox_documents(
    ctx: Context,
    status: str = "",
    source: str = "",
    company_id: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List document-inbox items (captured receipts/invoices awaiting
    review). Default view excludes terminal states (PUBLISHED, REJECTED,
    DUPLICATE) — pass ``status`` to see one explicitly.

    Args:
        status: RECEIVED | EXTRACTING | NEEDS_REVIEW | READY | FAILED |
            PUBLISHED | REJECTED | DUPLICATE (UPPERCASE).
        source: UPLOAD | EMAIL | API.
        company_id: filter to one company's documents.
        page_size / page: pagination (max 200 per page).

    404 = the document inbox is not enabled on this edition;
    503 = saebooks-vault is not configured on this deployment.
    """
    return await _get(
        ctx, "/api/v1/inbox/documents",
        status=status, source=source, company_id=company_id,
        page_size=page_size, page=page,
    )


@_gated_tool(safety="safe")
async def get_inbox_document(ctx: Context, document_id: str) -> dict[str, Any]:
    """Fetch one inbox document — extract (verbatim model output),
    extraction_override (reviewer edits), supplier-rule suggestions,
    version (needed for update/publish), plus ``advisory_duplicates``:
    open sibling documents that look like the same underlying invoice
    (same contact/vendor + invoice number — catches re-scans that
    byte-level dedupe cannot). Advisory only; nothing is blocked.
    """
    return await _get(ctx, f"/api/v1/inbox/documents/{document_id}")


@_gated_tool(safety="mutation")
async def upload_inbox_document(
    ctx: Context,
    filename: str,
    content_base64: str,
    content_type: str = "image/jpeg",
    company_id: str = "",
) -> dict[str, Any]:
    """Capture a document (receipt/invoice photo or PDF) into the inbox.

    The blob is stored durably in the vault first, then AI extraction
    runs synchronously when the edition has it (Business+) — the call
    can take single-digit seconds. Never blocks on the model: transport
    failure still returns the RECEIVED document.

    Args:
        filename: name to store as.
        content_base64: file bytes as base64 — decoded and sent as a
            multipart upload.
        content_type: image/jpeg | image/png | image/webp |
            application/pdf (10 MiB cap; HEIC is rejected).
        company_id: optional company routing.

    A byte-identical duplicate returns the EXISTING document with
    ``"duplicate": true`` (200, not an error).
    """
    raw = base64.b64decode(content_base64)
    data: dict[str, str] = {}
    if company_id:
        data["company_id"] = company_id
    async with _client_for(ctx) as client:
        resp = await client.post(
            "/api/v1/inbox/documents",
            data=data,
            files={"file": (filename, raw, content_type)},
            # Synchronous in-request extraction needs headroom beyond
            # the default 30 s (10 MiB PDF through the vision model).
            timeout=90.0,
        )
        resp.raise_for_status()
        return resp.json()


@_gated_tool(safety="mutation")
async def update_inbox_document(
    ctx: Context,
    document_id: str,
    version: int,
    extraction_override: dict[str, Any] | None = None,
    company_id: str = "",
    suggested_contact_id: str = "",
    suggested_account_id: str = "",
    suggested_tax_code_id: str = "",
) -> dict[str, Any]:
    """Save reviewer edits on an inbox document.

    ``extract`` (the verbatim model output) is immutable — corrections
    go in ``extraction_override``, whose keys win over the extract
    (``line_items`` replaces the extracted list wholesale; line shape:
    {description, quantity, unit_price, account_id, tax_code_id} with
    amounts as decimal strings, line amount = unit_price convention).

    Args:
        document_id: inbox document UUID.
        version: current document version (optimistic lock — fetch the
            document first; a stale version is a 409).
        extraction_override: reviewer-corrected fields (vendor_name,
            contact_id, date, invoice_number, total, line_items, …).
        company_id: company routing.
        suggested_contact_id / suggested_account_id /
            suggested_tax_code_id: correct a supplier-rule suggestion.

    Completeness is recomputed after the write (NEEDS_REVIEW ↔ READY).
    """
    body: dict[str, Any] = {"version": version}
    if extraction_override is not None:
        body["extraction_override"] = extraction_override
    for key, value in (
        ("company_id", company_id),
        ("suggested_contact_id", suggested_contact_id),
        ("suggested_account_id", suggested_account_id),
        ("suggested_tax_code_id", suggested_tax_code_id),
    ):
        if value:
            body[key] = value
    return await _patch(ctx, f"/api/v1/inbox/documents/{document_id}", body)


@_gated_tool(safety="mutation")
async def retry_inbox_extraction(ctx: Context, document_id: str) -> dict[str, Any]:
    """Re-run AI extraction on an inbox document now (resets the sweep
    attempt counters). Requires the AI-extraction feature (Business+;
    404 below that). Idempotent — a re-run replaces ``extract``
    wholesale and never touches ``extraction_override``. Terminal
    documents 409.
    """
    return await _post(ctx, f"/api/v1/inbox/documents/{document_id}/extract")


@_gated_tool(safety="mutation")
async def publish_inbox_document(
    ctx: Context,
    document_id: str,
    version: int,
    record_kind: str,
    company_id: str,
    contact_id: str,
    date: str,
    lines: list[dict[str, Any]],
    payment_account_id: str = "",
    due_date: str = "",
    reference: str = "",
    notes: str = "",
    learn_rule: bool = False,
    update_rule: bool = False,
) -> dict[str, Any]:
    """Publish a reviewed inbox document as a **DRAFT** record with the
    source document attached. Posting stays a deliberate second act on
    the record itself — this never auto-posts and never writes a manual
    journal entry.

    The idempotency key is self-generated from (document_id, version),
    so retrying the same call replays the original response instead of
    creating a second record.

    Args:
        document_id: inbox document UUID.
        version: current document version (fetch the document first).
        record_kind: EXPENSE | BILL | CREDIT_NOTE (UPPERCASE).
        company_id: REQUIRED — the company the record belongs to.
        contact_id: supplier contact UUID.
        date: ISO document date.
        lines: [{description, account_id, tax_code_id?, quantity?,
            unit_price?, project_id?}] — amounts as decimal strings,
            line amount = unit_price convention.
        payment_account_id: REQUIRED for EXPENSE (the bank/card/cash
            account that paid); ignored for BILL / CREDIT_NOTE.
        due_date: BILL only; omitted → derived from supplier terms.
        reference: receipt / supplier invoice number.
        notes: internal notes.
        learn_rule: upsert a LEARNED supplier rule from these confirmed
            values so the next document from this vendor pre-codes.
        update_rule: rewrite an existing rule's defaults to these values.

    Returns ``{document, record: {kind, id, status: "DRAFT"}}``.
    """
    body: dict[str, Any] = {
        "record_kind": record_kind,
        "company_id": company_id,
        "contact_id": contact_id,
        "date": date,
        "lines": lines,
        "learn_rule": learn_rule,
        "update_rule": update_rule,
    }
    for key, value in (
        ("payment_account_id", payment_account_id),
        ("due_date", due_date),
        ("reference", reference),
        ("notes", notes),
    ):
        if value:
            body[key] = value
    return await _post(
        ctx,
        f"/api/v1/inbox/documents/{document_id}/publish",
        body,
        idempotency_key=f"mcp-inbox-publish-{document_id}-v{version}",
    )


@_gated_tool(safety="void")
async def reject_inbox_document(
    ctx: Context, document_id: str, reason: str, note: str = "",
) -> dict[str, Any]:
    """Reject an inbox document (terminal). The blob is vault
    soft-deleted (archived, never purged) and the content hash frees up
    so a mistaken reject is recoverable by re-upload.

    Args:
        reason: DUPLICATE | NOT_A_DOCUMENT | PERSONAL | OTHER.
        note: optional free-text explanation.
    """
    body: dict[str, Any] = {"reason": reason}
    if note:
        body["note"] = note
    return await _post(ctx, f"/api/v1/inbox/documents/{document_id}/reject", body)


@_gated_tool(safety="safe")
async def list_supplier_rules(
    ctx: Context,
    include_inactive: bool = False,
    company_id: str = "",
    page_size: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List the tenant's supplier rules — deterministic, suggestion-only
    vendor coding for the document inbox (ABN-exact → vendor-name-exact,
    first match wins; suggestions never auto-publish). Soft-deleted
    (inactive) rules are hidden unless requested.
    """
    return await _get(
        ctx, "/api/v1/inbox/supplier-rules",
        include_inactive=include_inactive, company_id=company_id,
        page_size=page_size, page=page,
    )


@_gated_tool(safety="mutation")
async def create_supplier_rule(
    ctx: Context,
    vendor_name: str,
    contact_id: str,
    company_id: str = "",
    vendor_abn: str = "",
    account_id: str = "",
    tax_code_id: str = "",
    record_kind: str = "",
) -> dict[str, Any]:
    """Create a MANUAL supplier rule: documents whose extracted vendor
    matches (by 11-digit ABN, or the normalised vendor name) are
    pre-coded with these values as SUGGESTIONS for the reviewer.

    Args:
        vendor_name: vendor display name — normalised (lowercased,
            whitespace-collapsed) into the stored match key.
        contact_id: REQUIRED — the supplier contact to suggest.
        company_id: scope the rule to one company (omit = tenant-wide).
        vendor_abn: 11-digit ABN for the exact match tier.
        account_id / tax_code_id: default coding to suggest.
        record_kind: EXPENSE | BILL | CREDIT_NOTE default.

    One active rule per vendor per scope — a duplicate is a 409; edit
    or reactivate the existing rule instead (PATCH via ``call_api``).
    """
    body: dict[str, Any] = {
        "vendor_name": vendor_name,
        "contact_id": contact_id,
    }
    for key, value in (
        ("company_id", company_id),
        ("vendor_abn", vendor_abn),
        ("account_id", account_id),
        ("tax_code_id", tax_code_id),
        ("record_kind", record_kind),
    ):
        if value:
            body[key] = value
    return await _post(ctx, "/api/v1/inbox/supplier-rules", body)


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
