"""Xero v2 API HTTP wrapper.

Wraps ``httpx.AsyncClient`` with three behaviours the raw client doesn't
provide:

1. **Bearer token injection.** Every request is decorated with
   ``Authorization: Bearer <access>``, where ``access`` is fetched
   from a ``XeroTokenCache``. If the request returns 401, the wrapper
   forces a token refresh and retries once. If the second 401 still
   fails, the connection's refresh token is dead — surface
   ``SyncAuthError`` to the caller.

2. **429 retry with Retry-After.** Xero returns 429 on rate-limit hits
   with ``Retry-After`` in seconds. The wrapper sleeps for that long
   and retries up to ``_MAX_429_RETRIES`` times (3). After the final
   attempt it surfaces ``SyncRateLimited`` with the last
   ``Retry-After`` so the caller can sleep until the next scheduled
   poll.

3. **Xero-tenant-id header.** Xero's multi-org pattern threads the
   active org via ``Xero-tenant-id: <tenantId>``. The wrapper reads
   this from the ``XeroClient(xero_tenant_id=...)`` constructor and
   injects it on every request. (This is the *Xero* tenant — i.e.
   the customer's Xero org — distinct from our SAE Books
   ``app.current_tenant`` GUC.)

The wrapper is the only place that knows how to talk to Xero. The
endpoint helpers (``endpoints.py``) and the pull/push orchestrators
all go through it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from saebooks.services.sync.errors import (
    SyncAuthError,
    SyncRateLimited,
    SyncUpstreamError,
    SyncValidationError,
)
from saebooks.services.sync.xero.token import XeroTokenCache

log = logging.getLogger(__name__)

XERO_API_BASE = "https://api.xero.com/api.xro/2.0/"

_MAX_429_RETRIES = 3
_DEFAULT_TIMEOUT = 30.0


class XeroClient:
    """Async HTTP client for Xero v2 API endpoints.

    Each instance binds to ONE Xero org (via ``xero_tenant_id``). To
    fan out to multiple orgs on the same connection, build one client
    per org. (In practice one client is built per ``sync_connections``
    row, and each row already maps to a single ``external_tenant_id``.)
    """

    def __init__(
        self,
        *,
        token_cache: XeroTokenCache,
        xero_tenant_id: str,
        api_base: str = XERO_API_BASE,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._token_cache = token_cache
        self._xero_tenant_id = xero_tenant_id
        self._api_base = api_base.rstrip("/") + "/"
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> XeroClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    # ---- Public surface ------------------------------------------------ #

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        if_modified_since: str | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        """``GET <api_base>/<path>`` returning ``(json_body, headers)``.

        ``if_modified_since`` is sent as ``If-Modified-Since`` in the
        Xero-required ISO-8601-without-microseconds form (caller
        formats it). 304 responses surface as ``({}, headers)`` —
        callers check ``not body`` to detect "no changes".
        """
        return await self._request(
            "GET",
            path,
            params=params,
            if_modified_since=if_modified_since,
        )

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | list[Any],
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        """``POST <api_base>/<path>`` with a JSON body.

        Xero's "create or update" endpoints all accept a top-level
        list under the singular noun (e.g. ``{"Contacts": [...]}``),
        but the wrapper does not impose that shape — callers pass
        whatever Xero expects for the specific endpoint.
        """
        return await self._request("POST", path, json=json, params=params)

    async def put(
        self,
        path: str,
        *,
        json: dict[str, Any] | list[Any],
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        """``PUT <api_base>/<path>`` with a JSON body."""
        return await self._request("PUT", path, json=json, params=params)

    # ---- Internal ------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: object = None,
        if_modified_since: str | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        url = self._api_base + path.lstrip("/")
        # Token + headers
        access = await self._token_cache.get()
        headers = self._build_headers(access, if_modified_since)
        # 401-once-then-refresh-and-retry, plus 429 retry loop.
        retried_401 = False
        attempts_429 = 0
        while True:
            resp = await self._http.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            )
            status = resp.status_code
            if status == 401 and not retried_401:
                # Force refresh and retry once.
                retried_401 = True
                log.warning("xero: 401 on %s, refreshing token", path)
                new_access = await self._token_cache.force_refresh()
                headers = self._build_headers(new_access, if_modified_since)
                continue
            if status == 401:
                raise SyncAuthError(
                    f"Xero rejected refreshed token on {method} {path}",
                    http_status=status,
                )
            if status == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                attempts_429 += 1
                if attempts_429 > _MAX_429_RETRIES:
                    raise SyncRateLimited(
                        f"Xero rate-limit on {method} {path} "
                        f"after {attempts_429 - 1} retries",
                        retry_after=retry_after,
                    )
                # Honour Retry-After, default 60s.
                sleep_for = retry_after if retry_after is not None else 60.0
                log.info(
                    "xero: 429 on %s, sleeping %.1fs (attempt %d)",
                    path,
                    sleep_for,
                    attempts_429,
                )
                await asyncio.sleep(sleep_for)
                continue
            if 500 <= status < 600:
                raise SyncUpstreamError(
                    f"Xero {status} on {method} {path}: {resp.text[:200]}",
                    http_status=status,
                )
            if status == 304:
                return {}, resp.headers
            if 400 <= status < 500:
                # Includes 400 (validation), 403 (forbidden / scope),
                # 404 (object missing). Surface as SyncValidationError;
                # the orchestrator decides whether to quarantine the
                # offending object.
                payload: object
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                raise SyncValidationError(
                    f"Xero {status} on {method} {path}",
                    http_status=status,
                    payload=payload,
                )
            # 200/201/etc.
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                # Xero always returns a JSON object at the top level
                # for the endpoints we care about. If we ever see a
                # bare list or scalar, it is an upstream contract
                # violation and the caller should know.
                raise SyncUpstreamError(
                    f"Xero {method} {path} returned non-object body",
                    http_status=status,
                )
            return body, resp.headers
        # Unreachable — every branch returns or raises.

    def _build_headers(
        self,
        access: str,
        if_modified_since: str | None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access}",
            "Xero-tenant-id": self._xero_tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if if_modified_since is not None:
            headers["If-Modified-Since"] = if_modified_since
        return headers


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header. Xero only sends seconds-as-int."""
    if value is None:
        return None
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None
