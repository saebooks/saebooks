"""Thin wrappers around the Xero v2 endpoints we use.

These are pure transport — they translate ``(path, params, body)`` into
a ``XeroClient`` call and return the raw Xero shape. Mapping to/from
SAE Books shapes is done in ``mappers.py``; the orchestrators in
``pull.py`` and ``push.py`` glue the two together.

Pagination
----------
Xero pages at 100 records per response on Contacts and Invoices; the
``page`` query parameter is 1-based. ``iter_*`` helpers walk pagination
transparently. Callers that want raw pages use ``list_*``.

If-Modified-Since
-----------------
Xero accepts ``If-Modified-Since`` on Contacts and Invoices. Format is
``YYYY-MM-DDTHH:MM:SS`` (no fractional seconds, no offset — Xero docs
say "expressed in UTC"). 304 returns an empty body. ``ifms()`` formats
a ``datetime`` correctly.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from saebooks.services.sync.xero.client import XeroClient

# Xero will return up to 100 records per page on both Contacts and
# Invoices. The constant is documented for clarity; we don't need to
# pass it as a query parameter — it's the upstream default.
_PAGE_SIZE = 100


def ifms(dt: datetime) -> str:
    """Format a ``datetime`` as Xero's If-Modified-Since string.

    Xero requires UTC, no fractional seconds, no offset suffix. Pass a
    naive UTC datetime or one with ``tzinfo=UTC``; tz-aware non-UTC
    datetimes are converted to UTC first.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------- #
# Connections (initial post-OAuth call to discover available orgs)        #
# ---------------------------------------------------------------------- #


async def list_connections(client: XeroClient) -> list[dict[str, Any]]:
    """List all Xero orgs the access-token grant covers.

    This endpoint lives at a different base
    (``https://api.xero.com/connections``) — outside the per-org API
    surface — because at the moment it's called we may not yet have
    chosen which org the connection refers to.

    Xero's response is a *bare list* (not the usual ``{"items": [...]}``
    object). The wrapper accommodates that by issuing the GET against
    a dedicated path and parsing the body explicitly.
    """
    # Use the public path off the API base. We strip the trailing
    # ``api.xro/2.0/`` and call ``../../connections`` once. Practical:
    # we just construct a separate one-off httpx request with the same
    # bearer; sharing the client's auth is enough.
    raise NotImplementedError(
        "list_connections is implemented inline in the OAuth callback "
        "handler in saebooks/api/v1/sync_xero.py (the module-private "
        "_list_xero_connections helper) — it runs *before* an org is "
        "selected, so it doesn't fit the per-org XeroClient shape."
    )


# ---------------------------------------------------------------------- #
# Contacts                                                                #
# ---------------------------------------------------------------------- #


async def list_contacts_page(
    client: XeroClient,
    *,
    page: int = 1,
    if_modified_since: datetime | None = None,
    include_archived: bool = True,
) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch one page of Contacts.

    Returns ``(contacts, next_page_or_None)``. When the page is shorter
    than ``_PAGE_SIZE``, ``next_page_or_None`` is ``None``.
    """
    params: dict[str, Any] = {"page": page}
    if include_archived:
        params["includeArchived"] = "true"
    body, _headers = await client.get(
        "Contacts",
        params=params,
        if_modified_since=ifms(if_modified_since) if if_modified_since else None,
    )
    contacts = body.get("Contacts", []) if body else []
    next_page = page + 1 if len(contacts) >= _PAGE_SIZE else None
    return contacts, next_page


async def iter_contacts(
    client: XeroClient,
    *,
    if_modified_since: datetime | None = None,
    include_archived: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Walk all Contacts on the connected org. One row per yield."""
    page: int | None = 1
    while page is not None:
        rows, next_page = await list_contacts_page(
            client,
            page=page,
            if_modified_since=if_modified_since,
            include_archived=include_archived,
        )
        for row in rows:
            yield row
        page = next_page


async def post_contacts(
    client: XeroClient,
    contacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create or update one or more Contacts via ``POST /Contacts``.

    Xero's create-or-update semantics: ``ContactID`` present means
    update, absent means create. Validation errors return 400 with a
    ``ValidationErrors`` array — surfaces as ``SyncValidationError``.

    Returns the full Contact rows as Xero echoes them back (with new
    ``ContactID`` for creates and refreshed ``UpdatedDateUTC`` /
    ``ETag`` headers for updates).
    """
    body, _headers = await client.post(
        "Contacts",
        json={"Contacts": contacts},
    )
    return list(body.get("Contacts") or [])


# ---------------------------------------------------------------------- #
# Invoices (ACCREC = customer invoices, ACCPAY = supplier bills)          #
# ---------------------------------------------------------------------- #


async def list_invoices_page(
    client: XeroClient,
    *,
    page: int = 1,
    if_modified_since: datetime | None = None,
    where: str | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Fetch one page of Invoices.

    ``where`` is a Xero filter expression (e.g.
    ``'Type=="ACCREC" AND Status!="DELETED"'``). The orchestrator
    typically scopes by Type to fetch ACCREC and ACCPAY in two passes.
    """
    params: dict[str, Any] = {"page": page}
    if where is not None:
        params["where"] = where
    body, _headers = await client.get(
        "Invoices",
        params=params,
        if_modified_since=ifms(if_modified_since) if if_modified_since else None,
    )
    invoices = body.get("Invoices", []) if body else []
    next_page = page + 1 if len(invoices) >= _PAGE_SIZE else None
    return invoices, next_page


async def iter_invoices(
    client: XeroClient,
    *,
    if_modified_since: datetime | None = None,
    invoice_type: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Walk Invoices, optionally scoped by ``invoice_type`` ("ACCREC" / "ACCPAY")."""
    where = None
    if invoice_type is not None:
        where = f'Type=="{invoice_type}"'
    page: int | None = 1
    while page is not None:
        rows, next_page = await list_invoices_page(
            client,
            page=page,
            if_modified_since=if_modified_since,
            where=where,
        )
        for row in rows:
            yield row
        page = next_page


async def get_invoice(
    client: XeroClient,
    *,
    invoice_id: str,
) -> dict[str, Any]:
    """Fetch one full invoice by ``InvoiceID`` (UUID).

    The list endpoint returns a summary shape; line items are only
    populated on the per-id GET. The orchestrator calls this lazily
    when it needs lines.
    """
    body, _headers = await client.get(f"Invoices/{invoice_id}")
    items = body.get("Invoices") or []
    if not items:
        # Xero returned 200 with no Invoices entry — treat as missing.
        from saebooks.services.sync.errors import SyncValidationError
        raise SyncValidationError(
            f"Xero returned no invoice for InvoiceID={invoice_id}",
            http_status=200,
        )
    item: dict[str, Any] = items[0]
    return item


async def post_invoices(
    client: XeroClient,
    invoices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create or update one or more Invoices.

    ``InvoiceID`` present means update, absent means create.
    """
    body, _headers = await client.post(
        "Invoices",
        json={"Invoices": invoices},
    )
    return list(body.get("Invoices") or [])


# ---------------------------------------------------------------------- #
# Manual Journals                                                         #
# ---------------------------------------------------------------------- #


async def post_manual_journals(
    client: XeroClient,
    journals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create one or more Manual Journals via ``POST /ManualJournals``.

    We never *pull* manual journals — accountant-side adjustments come
    back to us as journals, but we treat the GL itself as our source
    of truth (pull-direction journals would be noise). Push-only.
    """
    body, _headers = await client.post(
        "ManualJournals",
        json={"ManualJournals": journals},
    )
    return list(body.get("ManualJournals") or [])


# ---------------------------------------------------------------------- #
# Chart of accounts (read-only on first-connect, never written)           #
# ---------------------------------------------------------------------- #


async def list_accounts(client: XeroClient) -> list[dict[str, Any]]:
    """Fetch the full Xero CoA. No pagination — Xero returns everything.

    Read-only on first connect. We never push CoA changes to Xero —
    that's the accountant's domain.
    """
    body, _headers = await client.get("Accounts")
    return list(body.get("Accounts") or [])
