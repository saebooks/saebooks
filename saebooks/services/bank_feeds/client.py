"""Async HTTP client for the SISS Aggregation / Banking / Common / Discovery APIs.

Phase 1 surface: a thin typed wrapper over ``httpx.AsyncClient`` that

1. attaches OAuth bearer + APIM-subscription-key auth to every request,
2. injects CDR / FAPI correlation headers (``x-v``, ``x-fapi-interaction-id``),
3. retries on 429 honouring ``Retry-After``,
4. maps non-2xx responses to the typed exceptions in ``errors.py``.

Higher-level wrappers (onboarding, sync) build on this in later phases.

No business logic lives here — treat this class as a boring plumbing
layer. Keep it that way.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import TracebackType
from typing import Any

import httpx

from saebooks.services.bank_feeds.errors import (
    SissError,
    SissRateLimitError,
)
from saebooks.services.bank_feeds.token import TokenCache

logger = logging.getLogger(__name__)

# How many times we retry a 429 before giving up. Each retry sleeps for
# ``Retry-After`` seconds if present, else exponential backoff.
_MAX_RATE_LIMIT_RETRIES = 3
_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0

# CDR API version header value. Bump when SISS rolls out a new schema.
_CDR_X_V = "1"


class SissClient:
    """Low-level HTTP client for SISS.

    Usage::

        client = SissClient(
            api_base="https://api.sissdata.com.au/cdr-au/v1/",
            subscription_key="...",
            token_cache=TokenCache(...),
        )
        async with client:
            data = await client.get("sds/clients")
    """

    def __init__(
        self,
        *,
        api_base: str,
        subscription_key: str,
        token_cache: TokenCache,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_base.endswith("/"):
            api_base = api_base + "/"
        self._api_base = api_base
        self._subscription_key = subscription_key
        self._token_cache = token_cache
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http = http_client is None

    # ------------------------------------------------------------------ #
    # Context manager                                                    #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> SissClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        await self._token_cache.aclose()

    # ------------------------------------------------------------------ #
    # Convenience verbs                                                  #
    # ------------------------------------------------------------------ #

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("POST", path, json=json, params=params)

    async def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("DELETE", path, params=params)

    async def patch(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("PATCH", path, json=json, params=params)

    # ------------------------------------------------------------------ #
    # Core request loop                                                  #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue one SISS request with retry/backoff for 429 and one-shot
        token refresh for 401.
        """
        url = self._api_base + path.lstrip("/")
        interaction_id = str(uuid.uuid4())
        attempt = 0
        refreshed_token = False
        while True:
            attempt += 1
            token = await self._token_cache.get()
            headers = _headers(
                token=token,
                subscription_key=self._subscription_key,
                interaction_id=interaction_id,
            )
            started = time.monotonic()
            try:
                resp = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "SISS transport error",
                    extra={
                        "method": method,
                        "path": path,
                        "x-fapi-interaction-id": interaction_id,
                        "error": str(exc),
                    },
                )
                raise SissError(
                    f"SISS transport error: {exc}",
                    http_status=0,
                    interaction_id=interaction_id,
                ) from exc
            latency_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "SISS request",
                extra={
                    "method": method,
                    "path": path,
                    "status": resp.status_code,
                    "latency_ms": latency_ms,
                    "x-fapi-interaction-id": interaction_id,
                    "attempt": attempt,
                },
            )
            # Happy path
            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    # 2xx with non-JSON body — return raw text
                    return resp.text
            # One-shot token refresh on 401
            if resp.status_code == 401 and not refreshed_token:
                await self._token_cache.invalidate()
                refreshed_token = True
                continue
            # 429 → honour Retry-After if present
            if resp.status_code == 429 and attempt <= _MAX_RATE_LIMIT_RETRIES:
                sleep = _retry_after_seconds(resp, attempt)
                logger.info(
                    "SISS 429 backoff",
                    extra={
                        "sleep_seconds": sleep,
                        "attempt": attempt,
                        "x-fapi-interaction-id": interaction_id,
                    },
                )
                await asyncio.sleep(sleep)
                continue
            # Map to typed exception
            raise _error_from_response(resp, interaction_id)


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


def _headers(
    *,
    token: str,
    subscription_key: str,
    interaction_id: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Ocp-Apim-Subscription-Key": subscription_key,
        "x-v": _CDR_X_V,
        "x-fapi-interaction-id": interaction_id,
        "Accept": "application/json",
    }


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Parse ``Retry-After`` header, falling back to exponential backoff."""
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            # RFC 7231 also permits an HTTP-date here, but SISS returns
            # seconds in practice. Fall through to backoff.
            parsed = None
        if parsed is not None:
            return max(0.0, parsed)
    # Exponential with cap, no jitter — we're the only caller, so lock-
    # stepping is fine.
    sleep: float = _BACKOFF_INITIAL_SECONDS * float(2 ** (attempt - 1))
    return min(sleep, _BACKOFF_MAX_SECONDS)


def _error_from_response(resp: httpx.Response, interaction_id: str) -> SissError:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.status_code == 429:
        retry_after = _retry_after_seconds(resp, attempt=1)
        # Build the RateLimitError directly so we can pin retry_after
        # (from_payload doesn't know about Retry-After).
        err = SissError.from_payload(
            http_status=resp.status_code,
            payload=payload,
            interaction_id=interaction_id,
        )
        if isinstance(err, SissRateLimitError):
            err.retry_after_seconds = retry_after
        return err
    return SissError.from_payload(
        http_status=resp.status_code,
        payload=payload,
        interaction_id=interaction_id,
    )
