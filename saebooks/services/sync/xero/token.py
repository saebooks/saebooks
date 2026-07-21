"""Xero OAuth2 token refresh + cache.

Xero issues short-lived access tokens (30 min) via OAuth2
authorization-code-with-PKCE. Refresh tokens are 60 days; **rotation
is mandatory** — every refresh response carries a NEW refresh token,
and Xero invalidates the connection if it sees the same refresh token
used twice in a row.

Lifecycle::

    operator clicks Connect -> redirect to Xero with code_challenge
                            -> Xero redirects back with `code`
                            -> POST /connect/token (grant=authorization_code)
                               returns access_token + refresh_token
                            -> persist refresh_token (Fernet ciphertext)
                            -> hold access_token in TokenCache
    access expires          -> POST /connect/token (grant=refresh_token)
                               returns NEW access_token + NEW refresh_token
                            -> on_refresh_rotated() persists the new
                               refresh_token; cache holds the new access

This module does NOT talk to the database. Persistence is the caller's
job (``connector.sync_xero``'s per-connection client factory). We
expose an ``on_refresh_rotated`` callback that fires synchronously on
every refresh — the caller writes the new ciphertext under its own
transaction.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from saebooks.services.sync.errors import SyncAuthError

# Refresh when less than 20 % of the token's TTL remains. Same shape
# as ``services.bank_feeds.token`` — we keep the constant local so
# tweaking one provider does not affect the other.
_REFRESH_AHEAD_FRACTION = 0.20
_MIN_MARGIN_SECONDS = 30.0

# Xero token endpoint. Hardcoded — there is no sandbox/prod split for
# the OAuth endpoint itself (Xero only exposes one identity host;
# tenancy is per-org, not per-environment).
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"


@dataclass
class _CachedAccessToken:
    """In-memory state for one active access token."""

    value: str
    issued_at: float  # monotonic
    ttl_seconds: float

    @property
    def refresh_at(self) -> float:
        margin = max(
            _MIN_MARGIN_SECONDS,
            self.ttl_seconds * _REFRESH_AHEAD_FRACTION,
        )
        return self.issued_at + self.ttl_seconds - margin


# Callback signature: invoked synchronously after a successful refresh
# with the new refresh token. The caller persists the ciphertext under
# its own transaction. Errors raised by the callback propagate to the
# refresh caller — i.e. if persistence fails, ``get()`` fails too.
RefreshRotatedCallback = Callable[[str], Awaitable[None]]


class XeroTokenCache:
    """Holds the current Xero access token; refreshes when stale.

    Each ``XeroTokenCache`` corresponds to ONE ``sync_connections`` row
    (one tenant + one Xero org). ``connector.sync_xero`` builds a fresh
    cache for each run; the cache lives only as long as that run.

    Concurrency: ``asyncio.Lock`` serialises refreshes within a single
    process. Cross-process serialisation is not required because at
    most one run happens per connection at a time (the trigger
    endpoint takes a row lock on ``sync_connections`` before doing
    anything).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        on_refresh_rotated: RefreshRotatedCallback | None = None,
        token_url: str = XERO_TOKEN_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._on_refresh_rotated = on_refresh_rotated
        self._token_url = token_url
        self._http = http_client
        self._owns_http = http_client is None
        self._cached: _CachedAccessToken | None = None
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str:
        """Current refresh token (rotated on every refresh)."""
        return self._refresh_token

    async def get(self) -> str:
        """Return a valid access token value, refreshing if stale."""
        now = time.monotonic()
        if self._cached is not None and now < self._cached.refresh_at:
            return self._cached.value
        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and now < self._cached.refresh_at:
                return self._cached.value
            self._cached = await self._refresh()
            return self._cached.value

    async def invalidate(self) -> None:
        """Drop the cached access token. Next ``get()`` refreshes."""
        async with self._lock:
            self._cached = None

    async def force_refresh(self) -> str:
        """Refresh now and return the new access token.

        Used by the client wrapper on a 401 response — Xero may have
        rotated keys server-side, in which case our cached access
        token is rejected even though we believe it's not yet stale.
        """
        async with self._lock:
            self._cached = await self._refresh()
            return self._cached.value

    async def aclose(self) -> None:
        """Close the internally-owned HTTP client, if any."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _refresh(self) -> _CachedAccessToken:
        http = self._http or httpx.AsyncClient(timeout=30.0)
        self._http = http
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        issued_at = time.monotonic()
        try:
            resp = await http.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise SyncAuthError(
                f"Xero token endpoint unreachable: {exc}",
                http_status=0,
            ) from exc
        if resp.status_code == 400:
            # Xero returns 400 with body {"error":"invalid_grant"} when
            # the refresh token has been revoked / replayed / expired.
            # Surface as SyncAuthError so the caller marks the
            # connection ``revoked`` and stops trying.
            raise SyncAuthError(
                f"Xero refresh rejected (400): {resp.text}",
                http_status=resp.status_code,
            )
        if resp.status_code != 200:
            raise SyncAuthError(
                f"Xero token endpoint returned {resp.status_code}: {resp.text}",
                http_status=resp.status_code,
            )
        body = resp.json()
        access = body.get("access_token")
        new_refresh = body.get("refresh_token")
        expires_in = body.get("expires_in")
        if not isinstance(access, str) or not access:
            raise SyncAuthError(
                "Xero token response missing access_token",
                http_status=resp.status_code,
            )
        if not isinstance(new_refresh, str) or not new_refresh:
            raise SyncAuthError(
                "Xero token response missing refresh_token "
                "(rotation invariant violated)",
                http_status=resp.status_code,
            )
        if not isinstance(expires_in, int) or expires_in <= 0:
            expires_in = 1800  # Xero default (30 min)
        # Rotate.
        self._refresh_token = new_refresh
        if self._on_refresh_rotated is not None:
            await self._on_refresh_rotated(new_refresh)
        return _CachedAccessToken(
            value=access,
            issued_at=issued_at,
            ttl_seconds=float(expires_in),
        )


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str,
) -> str:
    """Build the Xero authorize URL for the consent redirect.

    The customer's API redirects the operator to this URL when they
    click "Connect Xero". After consent, Xero redirects back to
    ``redirect_uri`` with ``?code=...&state=...``. The state value
    must be CSRF-bound to the current session.

    PKCE is mandatory for Xero public apps as of 2024-12 (see Xero
    OAuth2 docs). ``code_challenge`` is ``S256(code_verifier)`` —
    callers generate the verifier, compute the challenge, store the
    verifier in the session, and pass the challenge here.
    """
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return "https://login.xero.com/identity/connect/authorize?" + urlencode(params)


async def exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    token_url: str = XERO_TOKEN_URL,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    """Exchange an authorization code for an access + refresh token pair.

    Returns the parsed JSON body from Xero's token endpoint:
    ``{"access_token", "refresh_token", "expires_in", "scope", "id_token"}``.
    Raises ``SyncAuthError`` on any non-200 response.
    """
    own = http_client is None
    http = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }
        resp = await http.post(token_url, data=data)
        if resp.status_code != 200:
            raise SyncAuthError(
                f"Xero authorization-code exchange failed "
                f"({resp.status_code}): {resp.text}",
                http_status=resp.status_code,
            )
        body: dict[str, object] = resp.json()
        for required in ("access_token", "refresh_token", "expires_in"):
            if required not in body:
                raise SyncAuthError(
                    f"Xero authorization-code response missing {required!r}",
                    http_status=resp.status_code,
                )
        return body
    finally:
        if own:
            await http.aclose()
