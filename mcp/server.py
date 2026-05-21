"""SAE Books MCP server — exposes the API as native AI-agent tools.

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
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

logger = logging.getLogger("saebooks.mcp")
logging.basicConfig(level=os.getenv("SAEBOOKS_MCP_LOG_LEVEL", "INFO"))

# The saebooks REST API base URL — defaults to the in-container service
# name on r420's bosun compose network so the MCP container can reach
# the api service without going through the public Caddy edge.
API_BASE = os.getenv("SAEBOOKS_API_URL", "http://api:8000").rstrip("/")

# Optional shared bearer for development. In production, every request
# carries the user's own ``Authorization: Bearer saebk_*`` token via
# the MCP transport layer — we forward it. The shared fallback is
# disabled when empty.
SHARED_API_TOKEN = os.getenv("SAEBOOKS_API_TOKEN", "").strip()


mcp = FastMCP(
    "saebooks",
    instructions=(
        "SAE Books — self-hosted double-entry accounting. "
        "Tools cover invoices, customers, vendors, journal entries, "
        "and reports. Every tool talks to a saebooks instance over "
        "REST; the user's API token (issued at /api/v1/api-tokens) "
        "authenticates the call. "
        "Default currency AUD. Dates are ISO-8601 (YYYY-MM-DD)."
    ),
)


def _client_for(ctx: Context | None) -> httpx.AsyncClient:
    """Return an httpx client with auth headers set.

    Token resolution order:
    1. Per-call Authorization header forwarded by the MCP transport
       (production path — every client uses their own token).
    2. SAEBOOKS_API_TOKEN env (dev / single-tenant deployments).
    3. No auth (will 401 from saebooks).
    """
    token: str | None = None
    if ctx is not None:
        request_ctx = getattr(ctx, "request_context", None)
        if request_ctx is not None:
            meta = getattr(request_ctx, "meta", None) or {}
            token = meta.get("authorization") or meta.get("Authorization")
            if token and token.lower().startswith("bearer "):
                token = token.split(None, 1)[1].strip()
    if not token:
        token = SHARED_API_TOKEN or None

    headers = {"User-Agent": "saebooks-mcp/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return httpx.AsyncClient(
        base_url=API_BASE,
        headers=headers,
        timeout=30.0,
    )


async def _get(ctx: Context | None, path: str, **params: Any) -> Any:
    async with _client_for(ctx) as client:
        resp = await client.get(path, params={k: v for k, v in params.items() if v not in (None, "")})
        resp.raise_for_status()
        return resp.json()


async def _post(ctx: Context | None, path: str, body: dict[str, Any]) -> Any:
    async with _client_for(ctx) as client:
        resp = await client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()


async def _delete(ctx: Context | None, path: str) -> Any:
    async with _client_for(ctx) as client:
        resp = await client.delete(path)
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def whoami(ctx: Context) -> dict[str, Any]:
    """Identify the SAE Books user/company this MCP session is acting as.

    Useful as a first call to confirm the token works and to see which
    company's data you're querying.
    """
    return await _get(ctx, "/api/v1/companies/active")


@mcp.tool()
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
        contact_type: one of CUSTOMER, SUPPLIER, BOTH, BENEFICIARY.
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


@mcp.tool()
async def get_contact(ctx: Context, contact_id: str) -> dict[str, Any]:
    """Fetch a single contact by id."""
    return await _get(ctx, f"/api/v1/contacts/{contact_id}")


@mcp.tool()
async def create_contact(
    ctx: Context,
    name: str,
    contact_type: str = "CUSTOMER",
    email: str = "",
    phone: str = "",
    abn: str = "",
) -> dict[str, Any]:
    """Create a contact.

    Args:
        name: required, e.g. "Acme Pty Ltd".
        contact_type: CUSTOMER (default), SUPPLIER, BOTH, BENEFICIARY.
        email: optional.
        phone: optional.
        abn: optional Australian Business Number (11 digits).
    """
    body: dict[str, Any] = {"name": name, "contact_type": contact_type}
    if email:
        body["email"] = email
    if phone:
        body["phone"] = phone
    if abn:
        body["abn"] = abn
    return await _post(ctx, "/api/v1/contacts", body)


@mcp.tool()
async def list_invoices(
    ctx: Context,
    status: str = "",
    contact_id: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List invoices.

    Args:
        status: DRAFT, AWAITING_PAYMENT, PAID, VOIDED — empty for all.
        contact_id: filter to one customer.
        limit: page size (max 200).
        page: 1-indexed.
    """
    return await _get(
        ctx,
        "/api/v1/invoices",
        status=status,
        contact_id=contact_id,
        limit=limit,
        page=page,
    )


@mcp.tool()
async def get_invoice(ctx: Context, invoice_id: str) -> dict[str, Any]:
    """Fetch a single invoice with its lines."""
    return await _get(ctx, f"/api/v1/invoices/{invoice_id}")


@mcp.tool()
async def list_bills(
    ctx: Context,
    status: str = "",
    contact_id: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List supplier bills (accounts payable).

    Args:
        status: DRAFT, AWAITING_PAYMENT, PAID, VOIDED — empty for all.
        contact_id: filter to one supplier.
    """
    return await _get(
        ctx,
        "/api/v1/bills",
        status=status,
        contact_id=contact_id,
        limit=limit,
        page=page,
    )


@mcp.tool()
async def list_journal_entries(
    ctx: Context,
    status: str = "",
    limit: int = 50,
    page: int = 1,
) -> dict[str, Any]:
    """List journal entries (manual + system-posted).

    Args:
        status: DRAFT, POSTED, REVERSED — empty for all.
    """
    return await _get(
        ctx,
        "/api/v1/journal_entries",
        status=status,
        limit=limit,
        page=page,
    )


@mcp.tool()
async def get_journal_entry(ctx: Context, entry_id: str) -> dict[str, Any]:
    """Fetch a single journal entry with its lines."""
    return await _get(ctx, f"/api/v1/journal_entries/{entry_id}")


@mcp.tool()
async def list_accounts(ctx: Context, account_type: str = "") -> dict[str, Any]:
    """List the chart of accounts.

    Args:
        account_type: ASSET, LIABILITY, EQUITY, INCOME, EXPENSE,
            OTHER_INCOME, OTHER_EXPENSE, COST_OF_SALES — empty for all.
    """
    return await _get(ctx, "/api/v1/accounts", account_type=account_type)


@mcp.tool()
async def trial_balance(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Run the trial balance report.

    Args:
        as_at: ISO date (YYYY-MM-DD). Empty for today.
    """
    return await _get(ctx, "/api/v1/reports/trial_balance", as_at=as_at)


@mcp.tool()
async def profit_and_loss(
    ctx: Context, from_date: str, to_date: str
) -> dict[str, Any]:
    """Run the P&L report between two dates (inclusive).

    Args:
        from_date: ISO date YYYY-MM-DD.
        to_date: ISO date YYYY-MM-DD.
    """
    return await _get(
        ctx, "/api/v1/reports/pnl", from_date=from_date, to_date=to_date
    )


@mcp.tool()
async def balance_sheet(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Run the balance sheet report at a given date.

    Args:
        as_at: ISO date YYYY-MM-DD. Empty for today.
    """
    return await _get(ctx, "/api/v1/reports/balance_sheet", as_at=as_at)


@mcp.tool()
async def aged_receivables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Run aged receivables (AR aging) as at a date."""
    return await _get(ctx, "/api/v1/reports/aged_receivables", as_at=as_at)


@mcp.tool()
async def aged_payables(ctx: Context, as_at: str = "") -> dict[str, Any]:
    """Run aged payables (AP aging) as at a date."""
    return await _get(ctx, "/api/v1/reports/aged_payables", as_at=as_at)


@mcp.tool()
async def search(ctx: Context, query: str, limit: int = 20) -> dict[str, Any]:
    """Global search across invoices, contacts, accounts, journal entries.

    Args:
        query: free-text search string.
        limit: max hits per category.
    """
    return await _get(ctx, "/api/v1/search", q=query, limit=limit)


def main() -> None:
    transport = os.getenv("SAEBOOKS_MCP_TRANSPORT", "sse").lower()
    if transport == "stdio":
        mcp.run("stdio")
    elif transport in ("sse", "http", "streamable-http"):
        # FastMCP serves SSE on :8000 by default; the bind host is
        # controlled via uvicorn-style env vars.
        host = os.getenv("SAEBOOKS_MCP_HOST", "0.0.0.0")
        port = int(os.getenv("SAEBOOKS_MCP_PORT", "8000"))
        logger.info("saebooks-mcp serving %s on %s:%d (api=%s)", transport, host, port, API_BASE)
        # FastMCP's settings live on mcp.settings — set host/port before run.
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport)
    else:
        raise SystemExit(f"unknown transport: {transport}")


if __name__ == "__main__":
    main()
