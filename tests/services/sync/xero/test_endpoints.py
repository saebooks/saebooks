"""Tests for ``saebooks.services.sync.xero.endpoints``.

URL construction, pagination, and If-Modified-Since formatting.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import respx

from saebooks.services.sync.xero import endpoints
from saebooks.services.sync.xero.client import XERO_API_BASE, XeroClient
from saebooks.services.sync.xero.token import XERO_TOKEN_URL, XeroTokenCache


def _ok_refresh() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ACCESS",
            "refresh_token": "ROTATED",
            "expires_in": 1800,
        },
    )


def _make_client() -> XeroClient:
    return XeroClient(
        token_cache=XeroTokenCache(
            client_id="cid",
            client_secret="secret",
            refresh_token="OLD",
        ),
        xero_tenant_id="TEN",
    )


def test_ifms_strips_microseconds_and_offset() -> None:
    dt = datetime(2026, 4, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
    assert endpoints.ifms(dt) == "2026-04-01T12:30:45"


def test_ifms_naive_datetime_treated_as_utc() -> None:
    dt = datetime(2026, 4, 1, 12, 30, 45)
    assert endpoints.ifms(dt) == "2026-04-01T12:30:45"


@respx.mock
async def test_list_contacts_page_sends_includeArchived() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    route = respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={"Contacts": [{"ContactID": "1"}]},
        )
    )
    async with _make_client() as client:
        contacts, next_page = await endpoints.list_contacts_page(client, page=1)
    assert len(contacts) == 1
    assert next_page is None
    req = route.calls[0].request
    assert "includeArchived=true" in str(req.url)


@respx.mock
async def test_list_contacts_page_returns_next_when_full_page() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    full_page = [{"ContactID": str(i)} for i in range(100)]
    respx.get(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(200, json={"Contacts": full_page}),
    )
    async with _make_client() as client:
        _, next_page = await endpoints.list_contacts_page(client)
    assert next_page == 2


@respx.mock
async def test_iter_contacts_walks_pagination() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    page1 = [{"ContactID": str(i)} for i in range(100)]
    page2 = [{"ContactID": "X"}]
    respx.get(XERO_API_BASE + "Contacts").mock(
        side_effect=[
            httpx.Response(200, json={"Contacts": page1}),
            httpx.Response(200, json={"Contacts": page2}),
        ]
    )
    seen: list[str] = []
    async with _make_client() as client:
        async for row in endpoints.iter_contacts(client):
            seen.append(row["ContactID"])
    assert seen == [str(i) for i in range(100)] + ["X"]


@respx.mock
async def test_iter_invoices_scopes_by_type() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    route = respx.get(XERO_API_BASE + "Invoices").mock(
        return_value=httpx.Response(200, json={"Invoices": []}),
    )
    async with _make_client() as client:
        rows = [r async for r in endpoints.iter_invoices(client, invoice_type="ACCPAY")]
    assert rows == []
    req = route.calls[0].request
    assert 'where=Type%3D%3D%22ACCPAY%22' in str(req.url) or 'Type=="ACCPAY"' in str(req.url)


@respx.mock
async def test_get_invoice_returns_first_item() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Invoices/I-1").mock(
        return_value=httpx.Response(
            200,
            json={"Invoices": [{"InvoiceID": "I-1", "Status": "AUTHORISED"}]},
        )
    )
    async with _make_client() as client:
        body = await endpoints.get_invoice(client, invoice_id="I-1")
    assert body["InvoiceID"] == "I-1"


@respx.mock
async def test_post_contacts_returns_echoed_rows() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.post(XERO_API_BASE + "Contacts").mock(
        return_value=httpx.Response(
            200,
            json={"Contacts": [{"ContactID": "NEW", "Name": "Acme"}]},
        )
    )
    async with _make_client() as client:
        rows = await endpoints.post_contacts(client, [{"Name": "Acme"}])
    assert rows[0]["ContactID"] == "NEW"


@respx.mock
async def test_list_accounts_no_pagination() -> None:
    respx.post(XERO_TOKEN_URL).mock(return_value=_ok_refresh())
    respx.get(XERO_API_BASE + "Accounts").mock(
        return_value=httpx.Response(200, json={"Accounts": [{"Code": "200"}]}),
    )
    async with _make_client() as client:
        accounts = await endpoints.list_accounts(client)
    assert accounts[0]["Code"] == "200"
