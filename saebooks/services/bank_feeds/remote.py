"""HTTP client for ``feeds.saebooks.com.au`` — the bank-feeds relay.

Talks to the relay per
``~/.claude/plans/saebooks-feeds-server-contract.md``. The contract locks
the request shape, the response shape, and the status-code semantics.
Change either side without changing the other and you break production.

This is the bank-feeds analogue of ``saebooks.services.lodgement.remote``;
the two were deliberately built with byte-identical structure so a
developer who knows one knows the other. If you find yourself adding
something here that lodgement's remote already has, copy the lodgement
shape verbatim.

Authentication
--------------

The licence JWT is the only auth — no API key, no mTLS. The relay
verifies the signature against the embedded portal pubkey. We pull the
raw token from ``LicenseService.current_token()`` per request (not at
construction time) so a ``LicenseService.reload()`` is picked up on the
next call without restarting.

Error mapping
-------------

The HTTP-status -> exception mapping is the contract surface. Adding a
new code goes here, in ``_raise_for_status``, and is documented inline
so future changes have a single grep target.

* 400 -> ``FeedsValidationError``
* 401 -> ``FeedsAuthError``
* 403 -> ``FeedsEditionError``
* 404 -> ``FeedsNotFoundError``
* 409 -> ``FeedsIdempotencyConflict``
* 501 -> ``FeedsStubError``
* 502 -> ``FeedsUpstreamError``
* 503 -> ``FeedsUpstreamUnavailable``

Network timeouts
----------------

A 30-second total timeout is a tight default for what is mostly a
relay (no human-touch on the upstream). SISS itself can be slow during
consent flows, but that latency happens server-side; the relay returns
its 5xx-class status quickly.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from saebooks.services.bank_feeds.exceptions import (
    FeedsAuthError,
    FeedsEditionError,
    FeedsError,
    FeedsIdempotencyConflict,
    FeedsNotFoundError,
    FeedsStubError,
    FeedsUpstreamError,
    FeedsUpstreamUnavailable,
    FeedsValidationError,
)
from saebooks.services.licence import LicenseService

log = logging.getLogger(__name__)


DEFAULT_FEEDS_SERVER_URL = "https://feeds.saebooks.com.au"
DEFAULT_TIMEOUT_SECONDS = 30.0


class RemoteBankFeedsService:
    """HTTP client for ``feeds.saebooks.com.au``.

    Constructor arguments are all optional — the no-arg default is the
    production setup. Tests inject an ``httpx.AsyncClient`` (with a
    ``MockTransport`` or via ``respx``) and an explicit ``base_url``.

    Usage::

        svc = RemoteBankFeedsService()
        row = await svc.create_connection(
            bank="AU000001",
            account_label="Acme Pty Ltd — Operating",
            idempotency_key=str(uuid.uuid4()),
        )
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.environ.get("FEEDS_SERVER_URL", DEFAULT_FEEDS_SERVER_URL)
        ).rstrip("/")
        self._timeout = timeout
        self._client = client

    # ------------------------------------------------------------------ #
    # Public methods (one per contract route)                            #
    # ------------------------------------------------------------------ #

    async def create_connection(
        self,
        *,
        bank: str,
        account_label: str,
        idempotency_key: str,
        ledger_id: str | None = None,
        redirect_uri: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/connections`` — start a consent flow.

        Returns the parsed response body verbatim. Live-mode 201 carries
        ``connection_id`` + ``consent_url``; stub-mode 501 raises
        ``FeedsStubError`` with the deterministic stub body attached so
        the caller can persist a placeholder if desired.

        ``bank`` maps to the contract's ``institution_id``;
        ``account_label`` and any extra fields are passed through under
        ``metadata`` per the contract's passthrough policy.
        """
        meta = dict(metadata or {})
        # account_label is the user-facing tag for this enrolment.
        # The contract documents it under metadata.ledger_label, but
        # we also stash account_label so server-side audit can render
        # whichever the operator finds clearer.
        meta.setdefault("account_label", account_label)
        body: dict[str, Any] = {
            "institution_id": bank,
            "metadata": meta,
        }
        if ledger_id is not None:
            body["ledger_id"] = ledger_id
        if redirect_uri is not None:
            body["redirect_uri"] = redirect_uri
        return await self._request(
            "POST",
            "/api/v1/connections",
            json_body=body,
            idempotency_key=idempotency_key,
            success_codes=(200, 201),
        )

    async def list_connections(
        self,
        *,
        ledger_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/connections`` — list this licence's connections.

        Returns the ``rows`` array directly (the contract wraps it in a
        body with a ``license_id`` echo; callers don't typically need
        that, so we unwrap here). Stub-mode returns an empty list per
        the contract — that's a 200 from the server, not a 501.
        """
        params: dict[str, Any] = {}
        if ledger_id is not None:
            params["ledger_id"] = ledger_id
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit

        body = await self._request(
            "GET",
            "/api/v1/connections",
            params=params or None,
            success_codes=(200,),
        )
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    async def get_connection(self, connection_id: str) -> dict[str, Any]:
        """``GET /api/v1/connections/{id}`` — fetch one row.

        Stub-mode returns 501 here (not an empty list, per the contract:
        callers reaching for a specific connection need to know the
        difference between "doesn't exist" and "stub mode"). 404 is
        deliberately conflated with "not owned by you" so we surface it
        as ``FeedsNotFoundError``.
        """
        return await self._request(
            "GET",
            f"/api/v1/connections/{connection_id}",
            success_codes=(200,),
        )

    async def delete_connection(
        self,
        connection_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """``DELETE /api/v1/connections/{id}`` — revoke a connection.

        Returns ``None`` on success — the body carries ``status: revoked``
        + ``revoked_at`` but callers don't need it: we set the local row
        status from this side. Stub-mode raises ``FeedsStubError`` per
        the contract.
        """
        await self._request(
            "DELETE",
            f"/api/v1/connections/{connection_id}",
            idempotency_key=idempotency_key,
            success_codes=(200, 204),
        )

    async def sync_transactions(
        self,
        connection_id: str | None,
        since_cursor: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """``POST /api/v1/transactions/sync`` — pull new transactions.

        ``connection_id`` ``None`` → the relay fans out across all the
        licence's active connections (per contract). ``since_cursor``
        ``None`` → the relay uses the last persisted cursor on the
        connection row.

        Returns the response body verbatim. Live mode shape::

            {
              "connection_id": "conn_<uuid>",
              "transactions": [...],
              "next_cursor": "...",
              "has_more": false,
            }

        Stub mode raises ``FeedsStubError`` with the stub body attached.
        """
        body: dict[str, Any] = {}
        if connection_id is not None:
            body["connection_id"] = connection_id
        if since_cursor is not None:
            body["since_cursor"] = since_cursor
        return await self._request(
            "POST",
            "/api/v1/transactions/sync",
            json_body=body,
            idempotency_key=idempotency_key,
            success_codes=(200,),
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _http(self) -> _ClientCtx:
        """Async-context manager that yields the HTTP client.

        When the constructor was given an ``httpx.AsyncClient`` (test
        injection), reuse it without closing — the test owns the
        lifecycle. Otherwise spin up a new one per call so we don't
        leak connections between callers (the relay is infrequent
        enough that pooling isn't worth the complexity).
        """
        if self._client is not None:
            return _ClientCtx(self._client, owned=False)
        return _ClientCtx(
            httpx.AsyncClient(timeout=self._timeout, headers=self._headers()),
            owned=True,
        )

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        token = LicenseService.current_token()
        if not token:
            # Treat a missing token as a 401-equivalent up front — no
            # point waking the relay to be told the same thing.
            raise FeedsAuthError(
                "No licence token available — cannot authenticate to feeds-server"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "saebooks-bank-feeds-client/1",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        success_codes: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        """Issue one feeds-server request and decode the body.

        On success: returns the parsed dict (or ``{}`` for empty bodies).
        On failure: dispatches to ``_raise_for_status`` which raises a
        typed ``FeedsError`` subclass. Transport errors map to
        ``FeedsUpstreamUnavailable(status=None, ...)`` so callers don't
        have to catch ``httpx.HTTPError`` separately.

        Header lifecycle: when the constructor was given an injected
        client (test mode) we ship the per-call headers explicitly,
        because the injected client may have been built without our
        defaults. When we own the client we pre-bake the headers at
        construction; the per-call ``Idempotency-Key`` is still merged
        in here so it always reflects this call's value.
        """
        url = f"{self._base_url}{path}"
        per_call_headers = self._headers(idempotency_key=idempotency_key)
        async with self._http() as client:
            try:
                resp = await client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=per_call_headers,
                )
            except httpx.HTTPError as exc:
                raise FeedsUpstreamUnavailable(
                    status=None,
                    detail=f"Feeds-server transport error: {exc}",
                ) from exc

        if resp.status_code in success_codes:
            return self._safe_json(resp)

        # _raise_for_status always raises on the contract's documented
        # non-success codes; an unexpected code falls through to the
        # generic "treat as upstream problem" path so we never leak an
        # untyped failure to the caller.
        self._raise_for_status(resp)
        # Unreachable — _raise_for_status always raises. mypy can't see
        # that; a sentinel raise here keeps the type checker quiet
        # without affecting runtime behaviour.
        raise RuntimeError("unreachable")  # pragma: no cover

    def _raise_for_status(self, resp: httpx.Response) -> None:
        sc = resp.status_code
        data = self._safe_json(resp)
        detail = self._extract_detail(resp, data)

        if sc == 400:
            raise FeedsValidationError(detail)
        if sc == 401:
            raise FeedsAuthError(detail)
        if sc == 403:
            raise FeedsEditionError(detail)
        if sc == 404:
            raise FeedsNotFoundError(detail)
        if sc == 409:
            raise FeedsIdempotencyConflict(
                detail=detail,
                first_request_hash=(
                    data.get("first_request_hash")
                    if isinstance(data, dict)
                    else None
                ),
                this_request_hash=(
                    data.get("this_request_hash")
                    if isinstance(data, dict)
                    else None
                ),
            )
        if sc == 501:
            raise FeedsStubError(detail=detail, body=data)
        if sc == 502:
            raise FeedsUpstreamError(detail)
        if sc == 503:
            raise FeedsUpstreamUnavailable(status=sc, detail=detail)
        if 500 <= sc <= 599:
            # Any other 5xx — collapse onto upstream-unavailable. The
            # relay only documents 502 and 503; a 500 is a relay bug
            # and should be treated like a transient outage from our
            # caller's perspective.
            raise FeedsUpstreamUnavailable(status=sc, detail=detail)

        # Unexpected status — treat as upstream problem rather than
        # leaking an httpx exception. The contract enumerates every
        # code we expect; anything else is a bug one side or the other.
        raise FeedsError(
            f"Unexpected feeds-server status {sc}: {detail}"
        )

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_detail(resp: httpx.Response, data: dict[str, Any]) -> str:
        if isinstance(data, dict):
            for key in ("detail", "message", "error", "comment"):
                v = data.get(key)
                if isinstance(v, str) and v:
                    return v
        # Truncate long bodies — error messages bubble up to logs and
        # the audit row, no benefit to dumping multi-KB payloads.
        text = resp.text or ""
        return text[:400] if text else f"HTTP {resp.status_code}"


class _ClientCtx:
    """Async-context wrapper that closes only the clients we own.

    Tests inject a long-lived ``AsyncClient`` and expect the suite, not
    the service, to close it. Production calls let the service spin up
    a per-call client and clean it up on exit.
    """

    def __init__(self, client: httpx.AsyncClient, *, owned: bool) -> None:
        self._client = client
        self._owned = owned

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        if self._owned:
            await self._client.aclose()
