"""Typed read-only endpoint wrappers over ``SissClient`` — Phase 2.

Each function here is a thin shim that knows the URL template, query-
string idiom and response envelope shape for one upstream GET. No
persistence, no consent logic, no pagination bookkeeping beyond what
``iter_transactions`` needs.

Response shape is the open-standard CDR envelope::

    {
        "data": { "<collection>": [...] },
        "links": { "self": "...", "next": "...", ... },
        "meta": { "totalRecords": 123, "totalPages": 5 }
    }

We return the parsed body *as-is* rather than unwrapping ``data`` so
callers can see pagination links/metadata if they want.

All filter parameters map straight through to the upstream API using the
kebab-case names that the spec defines (``product-category``,
``page-size`` etc.); the rarer camelCase params (``fromTransactionId``)
are preserved as-is.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from saebooks.services.bank_feeds.client import SissClient

# ---------------------------------------------------------------------- #
# /sds/clients                                                           #
# ---------------------------------------------------------------------- #


async def list_clients(
    client: SissClient,
    *,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """GET ``/sds/clients``. Returns the full response envelope."""
    return _as_envelope(
        await client.get(
            "sds/clients",
            params=_params({"page": page, "page-size": page_size}),
        )
    )


async def get_client(
    client: SissClient,
    *,
    sds_client_id: str,
) -> dict[str, Any]:
    """GET ``/sds/clients/{sdsClientId}``. Returns the envelope."""
    return _as_envelope(await client.get(f"sds/clients/{sds_client_id}"))


# ---------------------------------------------------------------------- #
# /sds/clients/{id}/accounts                                              #
# ---------------------------------------------------------------------- #


async def list_accounts(
    client: SissClient,
    *,
    sds_client_id: str,
    product_category: str | None = None,
    open_status: str | None = None,
    is_owned: bool | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """GET ``/sds/clients/{sdsClientId}/accounts`` with CDR-standard filters."""
    return _as_envelope(
        await client.get(
            f"sds/clients/{sds_client_id}/accounts",
            params=_params(
                {
                    "product-category": product_category,
                    "open-status": open_status,
                    "is-owned": is_owned,
                    "page": page,
                    "page-size": page_size,
                }
            ),
        )
    )


async def get_account_detail(
    client: SissClient,
    *,
    sds_client_id: str,
    account_id: str,
) -> dict[str, Any]:
    """GET ``/sds/clients/{sdsClientId}/accounts/{accountId}``."""
    return _as_envelope(
        await client.get(f"sds/clients/{sds_client_id}/accounts/{account_id}")
    )


# ---------------------------------------------------------------------- #
# /sds/clients/{id}/transactions                                          #
# ---------------------------------------------------------------------- #


async def list_transactions(
    client: SissClient,
    *,
    sds_client_id: str,
    from_transaction_id: str | None = None,
    from_transaction_id_is_inclusive: bool | None = None,
    oldest_time: datetime | None = None,
    newest_time: datetime | None = None,
    product_category: str | None = None,
    open_status: str | None = None,
    is_owned: bool | None = None,
    exclude_balancing_transactions: bool | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """GET ``/sds/clients/{sdsClientId}/transactions``.

    All filter parameters are optional. When all are omitted the API
    returns whatever the default 90-day window yields.
    """
    return _as_envelope(
        await client.get(
            f"sds/clients/{sds_client_id}/transactions",
            params=_params(
                {
                    "fromTransactionId": from_transaction_id,
                    "fromTransactionIdIsInclusive": from_transaction_id_is_inclusive,
                    "oldest-time": _iso(oldest_time),
                    "newest-time": _iso(newest_time),
                    "product-category": product_category,
                    "open-status": open_status,
                    "is-owned": is_owned,
                    "excludeBalancingTransactions": exclude_balancing_transactions,
                    "page": page,
                    "page-size": page_size,
                }
            ),
        )
    )


async def iter_transactions(
    client: SissClient,
    *,
    sds_client_id: str,
    from_transaction_id: str | None = None,
    from_transaction_id_is_inclusive: bool | None = None,
    oldest_time: datetime | None = None,
    newest_time: datetime | None = None,
    product_category: str | None = None,
    exclude_balancing_transactions: bool | None = None,
    page_size: int = 100,
) -> AsyncIterator[dict[str, Any]]:
    """Yield each transaction dict across all pages.

    Pagination follows the CDR convention: keep incrementing ``page``
    until ``links.next`` is absent or the page carries no transactions.
    We use incrementing-page rather than ``links.next`` as a URL to
    keep the SissClient surface clean (it wants relative paths, not
    absolute URLs).
    """
    page = 1
    while True:
        envelope = await list_transactions(
            client,
            sds_client_id=sds_client_id,
            from_transaction_id=from_transaction_id,
            from_transaction_id_is_inclusive=from_transaction_id_is_inclusive,
            oldest_time=oldest_time,
            newest_time=newest_time,
            product_category=product_category,
            exclude_balancing_transactions=exclude_balancing_transactions,
            page=page,
            page_size=page_size,
        )
        data = envelope.get("data") or {}
        txns = data.get("transactions") or []
        for txn in txns:
            yield txn
        links = envelope.get("links") or {}
        if not links.get("next") or not txns:
            return
        page += 1


# ---------------------------------------------------------------------- #
# /sds/feedissues                                                         #
# ---------------------------------------------------------------------- #


async def list_feed_issues(
    client: SissClient,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """GET ``/sds/feedissues``. ``start-time`` / ``end-time`` are ISO 8601."""
    return _as_envelope(
        await client.get(
            "sds/feedissues",
            params=_params(
                {
                    "start-time": _iso(start_time),
                    "end-time": _iso(end_time),
                    "page": page,
                    "page-size": page_size,
                }
            ),
        )
    )


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


def _params(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None``; leave ``False`` + ``0`` untouched."""
    return {k: v for k, v in raw.items() if v is not None}


def _iso(value: datetime | None) -> str | None:
    """Format a datetime as RFC 3339 / ISO 8601, or return ``None``."""
    if value is None:
        return None
    return value.isoformat()


def _as_envelope(body: Any) -> dict[str, Any]:
    """Narrow the typing of a SissClient JSON body to a dict envelope."""
    if not isinstance(body, dict):
        raise TypeError(
            f"Expected JSON object from SISS, got {type(body).__name__}"
        )
    return body
