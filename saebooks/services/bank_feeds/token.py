"""OAuth2 Client-Credentials token cache for the SISS API.

SISS issues bearer tokens via the standard OAuth2 ``client_credentials``
grant at ``https://auth.sissdata.com.au/oauth/token``. Tokens are
short-lived (typically 1 hour). We cache the current token in memory
and refresh *ahead* of expiry so the hot path (request-issuing) never
blocks on a token fetch.

The cache is process-local. Cross-process/cross-instance sharing is
unnecessary at v1.1 (single-install, §6 of the integration brief); if
that changes, swap ``TokenCache`` for a Redis-backed equivalent.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from saebooks.services.bank_feeds.errors import SissAuthError

# Refresh when less than 20 % of the token's TTL remains.
_REFRESH_AHEAD_FRACTION = 0.20

# Minimum safety margin in seconds; never hand out a token closer than
# this to expiry even if the 20 % fraction would allow it.
_MIN_MARGIN_SECONDS = 30.0


@dataclass
class _CachedToken:
    """In-memory state for one active token."""

    value: str
    issued_at: float  # monotonic
    ttl_seconds: float

    @property
    def refresh_at(self) -> float:
        """Monotonic time at which we should request a fresh token."""
        margin = max(_MIN_MARGIN_SECONDS, self.ttl_seconds * _REFRESH_AHEAD_FRACTION)
        return self.issued_at + self.ttl_seconds - margin


class TokenCache:
    """Fetches and caches a SISS OAuth bearer token.

    Thread/task safety is provided by an ``asyncio.Lock`` — concurrent
    callers race to take the lock, but only one HTTP round-trip to the
    token endpoint runs at a time.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_url: str,
        scopes: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scopes = scopes or []
        self._http = http_client
        self._owns_http = http_client is None
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """Return a valid bearer token value, fetching if necessary."""
        now = time.monotonic()
        if self._cached is not None and now < self._cached.refresh_at:
            return self._cached.value
        async with self._lock:
            # Re-check after acquiring the lock: another coroutine may
            # have already refreshed while we were waiting.
            now = time.monotonic()
            if self._cached is not None and now < self._cached.refresh_at:
                return self._cached.value
            self._cached = await self._fetch()
            return self._cached.value

    async def invalidate(self) -> None:
        """Drop the cached token. Next ``get()`` will re-fetch."""
        async with self._lock:
            self._cached = None

    async def aclose(self) -> None:
        """Close the internally-owned HTTP client, if any."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _fetch(self) -> _CachedToken:
        http = self._http or httpx.AsyncClient(timeout=30.0)
        self._http = http
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scopes:
            data["scope"] = " ".join(self._scopes)
        issued_at = time.monotonic()
        try:
            resp = await http.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise SissAuthError(
                f"SISS token endpoint unreachable: {exc}",
                http_status=0,
            ) from exc
        if resp.status_code != 200:
            raise SissAuthError(
                f"SISS token endpoint returned {resp.status_code}",
                http_status=resp.status_code,
            )
        body = resp.json()
        token_value = body.get("access_token")
        if not isinstance(token_value, str) or not token_value:
            raise SissAuthError(
                "SISS token response missing access_token",
                http_status=resp.status_code,
            )
        expires_in = body.get("expires_in")
        if not isinstance(expires_in, int) or expires_in <= 0:
            # Fall back to 1-hour default per OAuth2 convention if SISS
            # omits the field.
            expires_in = 3600
        return _CachedToken(
            value=token_value,
            issued_at=issued_at,
            ttl_seconds=float(expires_in),
        )
