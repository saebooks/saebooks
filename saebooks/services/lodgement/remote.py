"""HTTP client implementation of ``LodgementService``.

Talks to ``lodge.saebooks.com.au`` per
``~/.claude/plans/saebooks-lodge-server-contract.md``. The contract
locks the request shape, the response shape, and the status-code
semantics — change either side without changing the other and you
break production.

Wire format reminder
--------------------

The envelope ships as base64 of the raw bytes plus a sha256 hex
digest. The server recomputes the hash on its side and rejects
(400) if the two don't match. We compute both here in one place so
every call site uses the same hash discipline.

Authentication
--------------

The licence JWT is the only auth — no API key, no mTLS. The server
verifies the signature against the embedded portal pubkey. We pull
the raw token from ``LicenseService.current_token()`` per request
(not at construction time) so a ``LicenseService.reload()`` is
picked up on the next call without restarting.

Error mapping
-------------

The HTTP-status → exception mapping is the contract surface. Adding
a new code goes here, in ``_raise_for_status``, and is documented
inline so future changes have a single grep target.

Network timeouts
----------------

A 60-second total timeout is generous for ATO SBR — the relay sits
between us and an upstream that can take 20s on a bad day. Going
shorter risks false 504s during BAS-day load.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime
from typing import Any

import httpx

from saebooks.services.licence import LicenseService
from saebooks.services.lodgement.base import (
    LodgementResult,
    LodgementService,
    LodgementStatus,
)
from saebooks.services.lodgement.exceptions import (
    LodgementAuthError,
    LodgementEditionError,
    LodgementRejected,
    LodgementUpstreamUnavailable,
    LodgementValidationError,
)

log = logging.getLogger(__name__)


DEFAULT_LODGE_SERVER_URL = "https://lodge.saebooks.com.au"
DEFAULT_TIMEOUT_SECONDS = 60.0


def _envelope_payload(envelope: bytes) -> tuple[str, str]:
    """Return ``(b64, sha256_hex)`` for an envelope blob.

    Centralised so the hash and the b64 are always derived from
    *the same byte buffer*. Computing them in two places risks
    sneaking in a normalisation step (eg encoding-conversion) on
    one side and not the other, which would manifest as a 400
    "envelope hash mismatch" with no obvious cause.
    """
    sha = hashlib.sha256(envelope).hexdigest()
    b64 = base64.b64encode(envelope).decode("ascii")
    return b64, sha


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; returns None on garbage input.

    The contract specifies ``ato_timestamp`` as ISO with a Z suffix.
    We tolerate the +00:00 form too (different Python json encoders
    emit different forms). On parse failure we return None and let
    the caller persist the raw_response — losing the timestamp is
    not worth raising for.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python's fromisoformat doesn't accept the Z suffix until
        # 3.11+, but we're on 3.12 — Z works directly.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("could not parse ato_timestamp=%r", value)
        return None


class RemoteLodgementService(LodgementService):
    """HTTP client for ``lodge.saebooks.com.au``.

    Constructor arguments are all optional — the no-arg default is
    the production setup. Tests inject an ``httpx.AsyncClient`` with
    a ``MockTransport`` (or use ``respx``) and an explicit
    ``base_url``.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        submitter_abn: str | None = None,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.environ.get("LODGE_SERVER_URL", DEFAULT_LODGE_SERVER_URL)
        ).rstrip("/")
        self._timeout = timeout
        self._client = client
        self._submitter_abn = submitter_abn

    # ------------------------------------------------------------------ #
    # Public methods (one per route)                                     #
    # ------------------------------------------------------------------ #

    async def lodge_stp(
        self,
        envelope: bytes,
        payevent_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        body = self._build_envelope_body(envelope, payevent_id, metadata)
        return await self._post_envelope("/api/v1/stp/lodge", body)

    async def lodge_bas(
        self,
        envelope: bytes,
        period_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        body = self._build_envelope_body(envelope, period_id, metadata)
        return await self._post_envelope("/api/v1/bas/lodge", body)

    async def lodge_tpar(
        self,
        envelope: bytes,
        year_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        body = self._build_envelope_body(envelope, year_id, metadata)
        return await self._post_envelope("/api/v1/tpar/lodge", body)

    async def send_superstream(
        self,
        message: bytes,
        message_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        body = self._build_envelope_body(message, message_id, metadata)
        return await self._post_envelope("/api/v1/superstream/send", body)

    async def lookup_abr(self, abn: str) -> dict[str, Any]:
        url = f"{self._base_url}/api/v1/abr/lookup"
        async with self._http() as client:
            try:
                resp = await client.post(url, json={"abn": abn})
            except httpx.HTTPError as exc:
                raise LodgementUpstreamUnavailable(
                    status=None,
                    detail=f"ABR lookup transport error: {exc}",
                ) from exc
        if resp.status_code != 200:
            self._raise_for_status(resp)
        return resp.json()

    async def my_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        url = f"{self._base_url}/api/v1/audit/me"
        async with self._http() as client:
            try:
                resp = await client.get(url, params={"limit": limit})
            except httpx.HTTPError as exc:
                raise LodgementUpstreamUnavailable(
                    status=None,
                    detail=f"Audit fetch transport error: {exc}",
                ) from exc
        if resp.status_code != 200:
            self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _http(self) -> _ClientCtx:
        """Return an async-context manager that yields the client.

        When the constructor was given an ``httpx.AsyncClient``
        (test injection), reuse it without closing — the test owns
        the lifecycle. Otherwise spin up a new one per call so we
        don't leak connections between callers (and the relay is
        infrequent enough that pooling isn't worth the complexity).
        """
        if self._client is not None:
            return _ClientCtx(self._client, owned=False)
        return _ClientCtx(
            httpx.AsyncClient(timeout=self._timeout, headers=self._headers()),
            owned=True,
        )

    def _headers(self) -> dict[str, str]:
        token = LicenseService.current_token()
        if not token:
            # Treat a missing token as a 401-equivalent up front —
            # no point waking the server to be told the same thing.
            raise LodgementAuthError(
                "No licence token available — cannot authenticate to lodge-server"
            )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "saebooks-lodgement-client/1",
        }

    def _build_envelope_body(
        self,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the contract-shaped JSON body.

        Note ``payevent_id`` is the field name in the contract for
        every envelope route, not just STP. We pass through whatever
        identity the caller provided (BAS uses ``period_id``, TPAR
        uses ``year_id``) under that contract field name.
        """
        b64, sha = _envelope_payload(envelope)
        return {
            "envelope_xml": b64,
            "envelope_hash": sha,
            "submitter_abn": self._submitter_abn or "",
            "payevent_id": idempotency_id,
            "metadata": metadata,
        }

    async def _post_envelope(
        self, path: str, body: dict[str, Any]
    ) -> LodgementResult:
        url = f"{self._base_url}{path}"
        async with self._http() as client:
            try:
                resp = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                raise LodgementUpstreamUnavailable(
                    status=None,
                    detail=f"Lodge-server transport error: {exc}",
                ) from exc

        return self._interpret_envelope_response(resp)

    def _interpret_envelope_response(
        self, resp: httpx.Response
    ) -> LodgementResult:
        """Map HTTP status → LodgementResult or raise.

        Status code semantics (mirror the contract):

        * 200 — ATO accepted; receipt + timestamp present.
        * 202 — queued (deferred receipt); ato_receipt_id may be
          None until the next poll.
        * 501 — STUB mode; treated as queued-success, not failure.
        * 400 / 401 / 403 / 422 — typed exceptions.
        * 5xx (and transport errors) — UpstreamUnavailable.
        """
        sc = resp.status_code

        if sc in (200, 202, 501):
            data = self._safe_json(resp)
            return self._success_to_result(sc, data)

        # Hard failures — try to extract a useful detail string.
        self._raise_for_status(resp)
        # Unreachable — _raise_for_status always raises on non-2xx/501,
        # but mypy can't see that.
        raise RuntimeError("unreachable")  # pragma: no cover

    def _success_to_result(
        self, sc: int, data: dict[str, Any]
    ) -> LodgementResult:
        if sc == 200:
            status = LodgementStatus.ACCEPTED
            receipt = data.get("ato_receipt_id")
        elif sc == 202:
            status = LodgementStatus.QUEUED
            receipt = data.get("ato_receipt_id")
        else:
            # 501 — stub mode. Surface stub_receipt_id as the
            # receipt so audit rows have something to anchor on.
            status = LodgementStatus.STUB
            receipt = data.get("stub_receipt_id") or data.get("ato_receipt_id")

        ts = _parse_iso(data.get("ato_timestamp"))
        warnings = data.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        return LodgementResult(
            status=status,
            ato_receipt_id=receipt if isinstance(receipt, str) else None,
            ato_timestamp=ts,
            warnings=[str(w) for w in warnings],
            raw_response=data,
        )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        sc = resp.status_code
        data = self._safe_json(resp)
        detail = self._extract_detail(resp, data)

        if sc == 400:
            raise LodgementValidationError(detail)
        if sc == 401:
            raise LodgementAuthError(detail)
        if sc == 403:
            raise LodgementEditionError(detail)
        if sc == 422:
            ato_errors = data.get("ato_errors") if isinstance(data, dict) else None
            raise LodgementRejected(
                detail=detail,
                ato_errors=ato_errors if isinstance(ato_errors, list) else [],
                raw_response=data if isinstance(data, dict) else {},
            )
        if 500 <= sc <= 599:
            raise LodgementUpstreamUnavailable(status=sc, detail=detail)

        # Unexpected status — treat as upstream problem rather than
        # leaking an httpx exception. The lodge-server contract
        # enumerates every code we expect; anything else is a bug
        # one side or the other.
        raise LodgementUpstreamUnavailable(
            status=sc,
            detail=f"Unexpected lodge-server status {sc}: {detail}",
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

    Tests inject a long-lived ``AsyncClient`` and expect the suite,
    not the service, to close it. Production calls let the service
    spin up a per-call client and clean it up on exit.
    """

    def __init__(self, client: httpx.AsyncClient, *, owned: bool) -> None:
        self._client = client
        self._owned = owned

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        if self._owned:
            await self._client.aclose()
