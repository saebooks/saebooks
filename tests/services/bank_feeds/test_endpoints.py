"""Tests for saebooks.services.bank_feeds.endpoints.

Uses respx to mock SISS HTTP responses. Covers URL construction, param
propagation, envelope return shape, and the ``iter_transactions``
pagination walk.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx

from saebooks.services.bank_feeds import endpoints
from saebooks.services.bank_feeds.client import SissClient
from saebooks.services.bank_feeds.token import TokenCache

TOKEN_URL = "https://auth.example/oauth/token"
API_BASE = "https://api.example/cdr-au/v1/"


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "TOK", "expires_in": 3600})


async def _make_client() -> SissClient:
    return SissClient(
        api_base=API_BASE,
        subscription_key="APIM-KEY",
        token_cache=TokenCache(
            client_id="cid",
            client_secret="secret",
            token_url=TOKEN_URL,
        ),
    )


# ---------------------------------------------------------------------- #
# list_clients / get_client                                              #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_list_clients_returns_envelope_and_forwards_paging() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"clients": [{"sdsClientId": "abc"}]},
                "links": {"self": "..."},
                "meta": {"totalRecords": 1},
            },
        )
    )
    client = await _make_client()
    async with client:
        body = await endpoints.list_clients(client, page=2, page_size=50)
    assert body["data"]["clients"][0]["sdsClientId"] == "abc"
    req = route.calls[0].request
    assert "page=2" in str(req.url)
    assert "page-size=50" in str(req.url)


@respx.mock
async def test_get_client_hits_expected_path() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc-123").mock(
        return_value=httpx.Response(
            200, json={"data": {"client": {"sdsClientId": "abc-123"}}}
        )
    )
    client = await _make_client()
    async with client:
        body = await endpoints.get_client(client, sds_client_id="abc-123")
    assert body["data"]["client"]["sdsClientId"] == "abc-123"
    assert route.called


# ---------------------------------------------------------------------- #
# list_accounts                                                          #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_list_accounts_forwards_filters() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/accounts").mock(
        return_value=httpx.Response(200, json={"data": {"accounts": []}})
    )
    client = await _make_client()
    async with client:
        await endpoints.list_accounts(
            client,
            sds_client_id="abc",
            product_category="TRANS_AND_SAVINGS_ACCOUNTS",
            open_status="OPEN",
            is_owned=True,
        )
    req = route.calls[0].request
    url = str(req.url)
    assert "product-category=TRANS_AND_SAVINGS_ACCOUNTS" in url
    assert "open-status=OPEN" in url
    assert "is-owned=true" in url


@respx.mock
async def test_list_accounts_drops_none_filters() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/accounts").mock(
        return_value=httpx.Response(200, json={"data": {"accounts": []}})
    )
    client = await _make_client()
    async with client:
        await endpoints.list_accounts(client, sds_client_id="abc")
    req = route.calls[0].request
    url = str(req.url)
    assert "product-category" not in url
    assert "is-owned" not in url
    # But default paging should still be present.
    assert "page=1" in url
    assert "page-size=100" in url


# ---------------------------------------------------------------------- #
# get_account_detail                                                     #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_get_account_detail_path() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/accounts/acct-1").mock(
        return_value=httpx.Response(
            200, json={"data": {"account": {"accountId": "acct-1"}}}
        )
    )
    client = await _make_client()
    async with client:
        body = await endpoints.get_account_detail(
            client, sds_client_id="abc", account_id="acct-1"
        )
    assert body["data"]["account"]["accountId"] == "acct-1"
    assert route.called


# ---------------------------------------------------------------------- #
# list_transactions                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_list_transactions_forwards_all_params() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    client = await _make_client()
    oldest = datetime(2026, 1, 1, tzinfo=UTC)
    newest = datetime(2026, 4, 1, tzinfo=UTC)
    async with client:
        await endpoints.list_transactions(
            client,
            sds_client_id="abc",
            from_transaction_id="txn-last",
            from_transaction_id_is_inclusive=False,
            oldest_time=oldest,
            newest_time=newest,
            exclude_balancing_transactions=True,
            page_size=25,
        )
    req = route.calls[0].request
    url = str(req.url)
    assert "fromTransactionId=txn-last" in url
    assert "fromTransactionIdIsInclusive=false" in url
    assert "oldest-time=2026-01-01" in url
    assert "newest-time=2026-04-01" in url
    assert "excludeBalancingTransactions=true" in url
    assert "page-size=25" in url


# ---------------------------------------------------------------------- #
# iter_transactions                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_iter_transactions_walks_pages_until_no_next() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/clients/abc/transactions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "transactions": [
                            {"transactionId": "t1"},
                            {"transactionId": "t2"},
                        ]
                    },
                    "links": {"next": "https://example/page/2"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {"transactions": [{"transactionId": "t3"}]},
                    "links": {"next": "https://example/page/3"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {"transactions": [{"transactionId": "t4"}]},
                    "links": {},  # no next — stop
                },
            ),
        ]
    )
    client = await _make_client()
    async with client:
        collected = [
            t async for t in endpoints.iter_transactions(
                client, sds_client_id="abc", page_size=50
            )
        ]
    assert [t["transactionId"] for t in collected] == ["t1", "t2", "t3", "t4"]
    # Three page requests made, each with a different page number.
    pages = [str(call.request.url) for call in route.calls]
    assert any("page=1" in u for u in pages)
    assert any("page=2" in u for u in pages)
    assert any("page=3" in u for u in pages)


@respx.mock
async def test_iter_transactions_stops_on_empty_page() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(API_BASE + "sds/clients/abc/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"transactions": []},
                "links": {"next": "https://example/page/2"},
            },
        )
    )
    client = await _make_client()
    async with client:
        collected = [
            t async for t in endpoints.iter_transactions(
                client, sds_client_id="abc"
            )
        ]
    assert collected == []


# ---------------------------------------------------------------------- #
# list_feed_issues                                                       #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_list_feed_issues_forwards_window() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    route = respx.get(API_BASE + "sds/feedissues").mock(
        return_value=httpx.Response(200, json={"data": {"feedIssue": []}})
    )
    client = await _make_client()
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 17, tzinfo=UTC)
    async with client:
        await endpoints.list_feed_issues(client, start_time=start, end_time=end)
    req = route.calls[0].request
    url = str(req.url)
    assert "start-time=2026-04-01" in url
    assert "end-time=2026-04-17" in url
