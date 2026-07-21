"""HTTP client to the saebooks-group broker (originator side, Phase 3c).

The originator's outbox dispatcher uses this to POST a signed relay message to
the broker ``/ic/relay``. Mirrors the structure of
``saebooks.services.bank_feeds.remote`` deliberately so a developer who knows
one knows the other: an optional injected ``httpx.AsyncClient`` for tests, a
tight total timeout, and a typed exception surface.

Auth to the broker = the per-edge scoped bearer token (``icrl_…``) presented as
``Authorization: Bearer <token>``. The signature in the body is the primary
trust anchor; the token is belt-and-braces (two independent secrets, §4.4).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("saebooks.services.ic_relay.broker_client")

DEFAULT_TIMEOUT_SECONDS = 30.0


class BrokerError(Exception):
    """Base error talking to the broker. ``status`` is None on transport error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BrokerUnavailable(BrokerError):
    """Transport-level failure (DNS/connect/timeout) — retryable."""


class BrokerRejected(BrokerError):
    """Broker returned a 4xx/5xx — body carried a reason. Retryability varies."""


class BrokerClient:
    """Thin async client for the broker relay endpoint.

    Usage::

        client = BrokerClient(base_url=settings.ic_broker_url)
        ack = await client.relay(payload=payload, signature_b64=sig_b64,
                                 token="icrl_…")
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def relay(
        self,
        *,
        payload: dict[str, Any],
        signature_b64: str,
        token: str,
    ) -> dict[str, Any]:
        """POST a signed message to ``/ic/relay``. Returns the broker ack dict.

        Body shape (the broker forwards the SAME envelope to the partner):
            {"payload": <canonical dict>, "signature": "<base64>"}
        The per-edge token is the Authorization bearer. On a non-2xx the broker's
        reason is surfaced as ``BrokerRejected``; a transport error as
        ``BrokerUnavailable`` (retryable). The originator's local leg is ALREADY
        posted by the time this runs — a failure here only delays delivery, it
        never rolls back the local books.
        """
        url = f"{self._base_url}/ic/relay"
        body = {"payload": payload, "signature": signature_b64}
        headers = {"Authorization": f"Bearer {token}"}
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BrokerUnavailable(
                    f"broker transport error: {exc}", status=None
                ) from exc
        finally:
            if owned:
                await client.aclose()

        if resp.status_code // 100 == 2:
            try:
                return resp.json()
            except ValueError:
                return {}
        # Non-2xx: surface the broker's reason without leaking internals.
        detail = ""
        try:
            detail = str(resp.json())
        except ValueError:
            detail = resp.text[:200]
        raise BrokerRejected(
            f"broker rejected relay ({resp.status_code}): {detail}",
            status=resp.status_code,
        )
