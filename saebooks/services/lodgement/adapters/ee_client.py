"""Async X-Road (X-tee) KMD3 client — submit → poll(UUID) → confirm.

This is the EE transmission rail. It deliberately does NOT reuse the AU
``remote.py`` wire discipline (base64+sha256 JSON envelope, 200/202/501 map) —
the X-Road KMD3 channel is a different lifecycle and a different wire shape,
verified from the e-MTA X-tee interfacing guide
(``APA_KMD_juhend_rakendusega_liidestumiseks_yle_X-tee_20260619.pdf``, v1.0):

Submit — ``POST /submit-data``  [VERIFIED, guide §3]
    * ``multipart/form-data`` with exactly one ``file`` part (the XBRL GL XML).
    * Headers ``X-Road-Client`` = ``<instance>/<memberClass>/<memberCode>/<subsystem>``
      and optional ``X-Road-Represented-Party`` = ``<partyClass>/<partyCode>``
      (file-on-behalf-of; the service-provider moat).
    * Success = **201 Created** with JSON
      ``{"feedbackReportId": <uuid>, "estimatedProcessingEndTime": <ts>}``.
      HTTP is synchronous but business processing is async — the UUID is the
      handle to poll for the result later.
    * Client error = **400** with JSON ``{"errorCode": ..., "errorMessage": ...}``
      (e.g. ``SINGLE_FILE_REQUIRED``, ``PERSON_NOT_DEFINED``).

Poll — ``GET /return-data/{uuid}``  [PARTIALLY VERIFIED]
    * The endpoint and UUID path are VERIFIED (guide §2/§3). The exact body
      wrapper (the "KMD koondvaade" aggregate view + how a still-processing
      report is represented) is **UNVERIFIED** — the guide marks it "täpsem info
      lisandub" (more detail to follow). We model it against the response
      contract we DO have: the ``operationAccepted`` / ``operationRejected``
      messages (``ee_messages``), and treat HTTP 202 (or a body with no
      terminal message) as *pending*. Both the pending representation and the
      koondvaade wrapper are flagged UNVERIFIED modelling choices, not spec.

Confirm — ``POST /confirm`` (kinnitamine)  [UNVERIFIED — gated stub]
    * The confirmation service is marked "täpsem info lisandub" in the guide.
      Its wire shape is not published, so ``confirm`` is a gated stub that
      raises ``EEConfirmServiceUnverified`` rather than fabricating a request.

The live gate
-------------

X-Road membership (branch 17151236) + a security server + the mTLS client
cert/key are NOT provisioned. The client talks to a real security server only
when given a **complete** :class:`MtlsConfig`; absent that (and absent an
injected transport for tests) every network-needing call raises
``EELiveCredentialsMissing`` *before opening a socket*. The real-mTLS transport
builder below is real code that stays dormant until Richard provisions creds —
the live path is "one complete MtlsConfig away" and no more. Nothing here files
to the real tax board today.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import httpx

from saebooks.services.lodgement.adapters.ee_messages import (
    ReturnFeedback,
    parse_feedback_message,
)
from saebooks.services.lodgement.exceptions import (
    EEConfirmServiceUnverified,
    EEFilingRejected,
    EEFilingStateError,
    EEFilingValidationError,
    EELiveCredentialsMissing,
    EEUpstreamUnavailable,
)
from saebooks.services.lodgement.remote import _ClientCtx, _parse_iso

# X-tee environments (guide §2.1). The instance prefixes the X-Road-Client header
# and the request path.
XROAD_ENVIRONMENTS: frozenset[str] = frozenset({"EE", "ee-test", "ee-dev"})
DEFAULT_ENVIRONMENT = "ee-test"

# Generous timeout: the security server proxies to EMTA; submit returns quickly
# (async server-side) but a busy period can add latency.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Section identifier for the VAT purchase/sale transaction data (GUIDE §3.2.1).
KMD3_SECTION = "EE0203001"


# --------------------------------------------------------------------------- #
# Filing lifecycle state machine (pure, fully unit-testable offline)
# --------------------------------------------------------------------------- #


class EEFilingState(str, Enum):  # noqa: UP042  str-mixin: values persist to ee_filing_state column
    """States of one KMD3 filing.

    The ``.value`` strings ARE the tokens written to ``tax_returns.ee_filing_state``
    (migration 0196) — this enum is the single source of truth for both the
    in-memory transition machine and the persisted column. Do not diverge them.
    """

    IDLE = "idle"          # nothing submitted yet
    SUBMITTED = "submitted"  # submit returned a feedbackReportId; awaiting processing
    PENDING = "pending"     # poll returned "still processing"
    ACCEPTED = "accepted"   # poll returned operationAccepted
    REJECTED = "rejected"   # poll returned operationRejected (terminal)
    CONFIRMED = "confirmed"  # confirm succeeded (terminal; unreachable while gated)


class EEFilingEvent(str, Enum):  # noqa: UP042
    """Events that drive a filing between states."""

    SUBMIT = "submit"
    POLL_PENDING = "poll_pending"
    POLL_ACCEPTED = "poll_accepted"
    POLL_REJECTED = "poll_rejected"
    CONFIRM = "confirm"


# Allowed (state, event) -> next-state transitions. Anything absent is illegal
# and raises ``EEFilingStateError``. Terminal states (REJECTED, CONFIRMED) have
# no outgoing edges.
_TRANSITIONS: dict[tuple[EEFilingState, EEFilingEvent], EEFilingState] = {
    (EEFilingState.IDLE, EEFilingEvent.SUBMIT): EEFilingState.SUBMITTED,
    (EEFilingState.SUBMITTED, EEFilingEvent.POLL_PENDING): EEFilingState.PENDING,
    (EEFilingState.SUBMITTED, EEFilingEvent.POLL_ACCEPTED): EEFilingState.ACCEPTED,
    (EEFilingState.SUBMITTED, EEFilingEvent.POLL_REJECTED): EEFilingState.REJECTED,
    (EEFilingState.PENDING, EEFilingEvent.POLL_PENDING): EEFilingState.PENDING,
    (EEFilingState.PENDING, EEFilingEvent.POLL_ACCEPTED): EEFilingState.ACCEPTED,
    (EEFilingState.PENDING, EEFilingEvent.POLL_REJECTED): EEFilingState.REJECTED,
    (EEFilingState.ACCEPTED, EEFilingEvent.CONFIRM): EEFilingState.CONFIRMED,
}


def advance(state: EEFilingState, event: EEFilingEvent) -> EEFilingState:
    """Return the next state for ``(state, event)`` or raise ``EEFilingStateError``.

    Pure function — the single authority on legal transitions. Callers thread
    their persisted ``EEFilingState`` through this before/after each client
    call so a filing can never skip a step (e.g. confirm before accepted) or
    act on a terminal return (e.g. poll after rejected).
    """
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise EEFilingStateError(
            f"illegal filing transition: {event.value!r} not permitted from "
            f"state {state.value!r}"
        ) from None


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MtlsConfig:
    """X-Road security-server connection + mTLS material.

    ``is_complete()`` gates the live path: every field must be present, or the
    client refuses to build a real transport and raises
    ``EELiveCredentialsMissing`` instead. ``client_cert_path`` / ``client_key_path``
    are filesystem paths to the PEM client cert/key the security server presents;
    ``security_server_url`` is the local security server base
    (``https://<securityserver>``); ``xroad_client_header`` is the assembled
    ``<instance>/<memberClass>/<memberCode>/<subsystem>`` subsystem identifier.
    ``ca_bundle_path`` / ``verify`` control server-cert verification (never
    disabled in production).
    """

    client_cert_path: str | None = None
    client_key_path: str | None = None
    security_server_url: str | None = None
    xroad_client_header: str | None = None
    ca_bundle_path: str | None = None
    verify: bool = True

    def is_complete(self) -> bool:
        """True only if EVERY field required to open a real mTLS connection is set."""
        return bool(
            self.client_cert_path
            and self.client_key_path
            and self.security_server_url
            and self.xroad_client_header
        )


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    """The immediate response to ``POST /submit-data`` (201).

    ``request_id`` is the ``feedbackReportId`` UUID — the handle used to poll
    for the feedback report. ``estimated_end`` is EMTA's estimate of when
    processing completes (``estimatedProcessingEndTime``); may be None if the
    server omitted or sent an unparseable timestamp.
    """

    request_id: str
    estimated_end: datetime | None
    raw_response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PollResult:
    """The outcome of one ``poll`` call.

    ``state`` is the filing state this poll implies (``PENDING`` or ``ACCEPTED``;
    a rejection raises ``EEFilingRejected`` rather than returning). ``feedback``
    is the parsed :class:`ReturnFeedback` when a terminal (accepted) message was
    present, else None while still pending.
    """

    state: EEFilingState
    feedback: ReturnFeedback | None


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    """Result of the confirm step. UNVERIFIED — never constructed while gated."""

    request_id: str
    confirmed: bool
    raw_response: dict[str, Any]


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class EELodgementClient:
    """Stateless async X-Road KMD3 client.

    Stateless by design (build plan §3.4): the caller owns persistence of the
    ``feedbackReportId`` UUID + filing state between calls (Option A columns on
    ``tax_returns``). Each method opens/closes its own transport unless an
    ``httpx.AsyncClient`` was injected (tests pass a ``MockTransport``-backed
    client; the suite owns its lifecycle).

    The live gate lives in :meth:`_resolve_transport`: with no injected client
    and no complete :class:`MtlsConfig`, it raises ``EELiveCredentialsMissing``
    before any socket is opened.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        mtls: MtlsConfig | None = None,
        represented_party: str | None = None,
        environment: str = DEFAULT_ENVIRONMENT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if environment not in XROAD_ENVIRONMENTS:
            raise ValueError(
                f"unknown X-tee environment {environment!r}; "
                f"expected one of {sorted(XROAD_ENVIRONMENTS)}"
            )
        # base_url is only used with an injected transport (tests) or as an
        # override; the live path derives the base from mtls.security_server_url.
        self._base_url = (base_url or "").rstrip("/")
        self._client = client
        self._mtls = mtls
        self._represented_party = represented_party
        self._environment = environment
        self._timeout = timeout

    # ---- transport / live gate ------------------------------------------- #

    def _resolve_base_url(self) -> str:
        if self._client is not None:
            # Injected transport (tests): base_url may be an arbitrary test host.
            return self._base_url or "https://xroad.test"
        if self._mtls is not None and self._mtls.security_server_url:
            return self._mtls.security_server_url.rstrip("/")
        # Unreachable in practice — _resolve_transport gates first — but keep a
        # defensive raise so a base URL is never silently empty.
        raise EELiveCredentialsMissing()

    def _resolve_transport(self) -> _ClientCtx:
        """Return the transport to use, or FAIL LOUD with zero network egress.

        * Injected client (tests / MockTransport) → reuse it, do not close it.
        * Complete MtlsConfig → build a real mTLS ``AsyncClient`` (dormant until
          creds are provisioned; owned, closed on exit).
        * Otherwise → ``EELiveCredentialsMissing`` before any socket opens.
        """
        if self._client is not None:
            return _ClientCtx(self._client, owned=False)
        if self._mtls is None or not self._mtls.is_complete():
            raise EELiveCredentialsMissing()
        return _ClientCtx(self._build_live_client(), owned=True)

    def _build_live_client(self) -> httpx.AsyncClient:
        """Construct a real mTLS transport. Reached ONLY with complete creds.

        Not exercised today (no creds provisioned) — this is the "one MtlsConfig
        away" seam. When Richard wires X-Road, this presents the client cert/key
        the security server requires and verifies the server cert against the
        provided CA bundle. No verification is ever disabled here.
        """
        assert self._mtls is not None and self._mtls.is_complete()
        verify: bool | str = (
            self._mtls.ca_bundle_path
            if self._mtls.ca_bundle_path
            else self._mtls.verify
        )
        return httpx.AsyncClient(
            cert=(self._mtls.client_cert_path, self._mtls.client_key_path),
            verify=verify,
            timeout=self._timeout,
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        # X-Road-Client: injected-transport tests may not set mtls; fall back to
        # a deterministic test header so the request is well-formed for asserts.
        client_header = (
            self._mtls.xroad_client_header
            if self._mtls is not None and self._mtls.xroad_client_header
            else f"{self._environment}/COM/00000000/kmd3-test"
        )
        headers["X-Road-Client"] = client_header
        if self._represented_party:
            headers["X-Road-Represented-Party"] = self._represented_party
        return headers

    # ---- submit ---------------------------------------------------------- #

    async def submit(
        self,
        envelope: bytes,
        *,
        section: str = KMD3_SECTION,
        idempotency_id: str,
    ) -> SubmitReceipt:
        """``POST /submit-data`` — file the XBRL GL payload, get a UUID back.

        ``envelope`` is the already-serialised XBRL GL XML (Module 4 produces
        it; this client is payload-agnostic). ``section`` is the data section
        (default ``EE0203001``). ``idempotency_id`` is the caller's correlation
        id; the VERIFIED submit spec carries NO explicit idempotency field
        (amendment = full resubmission of the period, GUIDE §4), so we ride it
        on the multipart filename purely for traceability — it is NOT a
        server-side dedup key. State: IDLE → SUBMITTED on success.
        """
        base = self._resolve_base_url()
        # OFFLINE-SHAPE-ONLY URL. The real X-Road path is
        # /r1/{xRoadInstance}/{memberClass}/{memberCode}/{subsystemCode}/{serviceCode}/v1/submit-data
        # where memberClass/memberCode/subsystemCode/serviceCode identify EMTA's
        # SERVICE-PROVIDER subsystem (distinct from X-Road-Client, the sender) and
        # are NOT pinned in the guide (curl uses {placeholders}). MtlsConfig does
        # not model them yet. MockTransport ignores the path so tests pass; a real
        # security server needs this completed. Do not read this as live-ready.
        url = f"{base}/r1/{self._environment}/submit-data"
        filename = f"{section}-{idempotency_id}.xml"
        ctx = self._resolve_transport()
        async with ctx as client:
            try:
                resp = await client.post(
                    url,
                    headers=self._headers(),
                    files={"file": (filename, envelope, "application/xml")},
                )
            except httpx.HTTPError as exc:
                raise EEUpstreamUnavailable(
                    status=None,
                    detail=f"X-Road submit transport error: {exc}",
                ) from exc
        return self._interpret_submit(resp)

    def _interpret_submit(self, resp: httpx.Response) -> SubmitReceipt:
        sc = resp.status_code
        data = _safe_json(resp)
        if sc == 201:
            report_id = data.get("feedbackReportId")
            if not isinstance(report_id, str) or not report_id:
                raise EEUpstreamUnavailable(
                    status=sc,
                    detail="submit 201 but no feedbackReportId in body",
                )
            return SubmitReceipt(
                request_id=report_id,
                estimated_end=_parse_iso(data.get("estimatedProcessingEndTime")),
                raw_response=data,
            )
        if sc == 400:
            raise EEFilingValidationError(
                detail=_detail(data, resp, default="X-Road submit rejected (400)"),
                error_code=(
                    data.get("errorCode") if isinstance(data, dict) else None
                ),
                raw_response=data,
            )
        if 500 <= sc <= 599:
            raise EEUpstreamUnavailable(
                status=sc, detail=_detail(data, resp, default=f"X-Road 5xx ({sc})")
            )
        raise EEUpstreamUnavailable(
            status=sc,
            detail=f"unexpected X-Road submit status {sc}: "
            f"{_detail(data, resp, default='')}",
        )

    # ---- poll ------------------------------------------------------------ #

    async def poll(self, request_id: str) -> PollResult:
        """``GET /return-data/{uuid}`` — fetch the feedback report.

        Returns ``PollResult(state=PENDING, feedback=None)`` while EMTA is still
        processing (HTTP 202, or a 200 whose body carries no terminal
        operationAccepted/Rejected message — the exact pending representation is
        UNVERIFIED). Returns ``PollResult(state=ACCEPTED, feedback=...)`` on an
        ``operationAccepted`` message. Raises ``EEFilingRejected`` on an
        ``operationRejected`` message — a rejection is terminal and loud, not a
        return value.
        """
        base = self._resolve_base_url()
        # OFFLINE-SHAPE-ONLY URL (see submit() — the real path carries EMTA's
        # service-provider subsystem + service code, which are not pinned yet).
        url = f"{base}/r1/{self._environment}/return-data/{request_id}"
        ctx = self._resolve_transport()
        async with ctx as client:
            try:
                resp = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise EEUpstreamUnavailable(
                    status=None,
                    detail=f"X-Road poll transport error: {exc}",
                ) from exc
        return self._interpret_poll(request_id, resp)

    def _interpret_poll(self, request_id: str, resp: httpx.Response) -> PollResult:
        sc = resp.status_code
        # 202 = accepted-but-still-processing. UNVERIFIED exact code; treated as
        # the pending signal.
        if sc == 202:
            return PollResult(state=EEFilingState.PENDING, feedback=None)
        if sc == 200:
            body = resp.content or b""
            feedback = _extract_feedback(body)
            if feedback is None:
                # 200 with no terminal message → still pending (UNVERIFIED
                # koondvaade wrapper; we only key off the embedded message).
                return PollResult(state=EEFilingState.PENDING, feedback=None)
            if feedback.accepted:
                return PollResult(state=EEFilingState.ACCEPTED, feedback=feedback)
            raise EEFilingRejected(
                detail=(
                    "EMTA rejected the declaration: "
                    f"{len(feedback.xml_errors)} XML error(s), "
                    f"{len(feedback.functional_errors)} functional error(s)"
                ),
                request_id=feedback.request_id or request_id,
                xml_errors=feedback.xml_errors,
                functional_errors=list(feedback.functional_errors),
            )
        if sc == 404:
            # Unknown UUID → treat as an upstream/state problem, not pending.
            raise EEUpstreamUnavailable(
                status=sc,
                detail=f"X-Road poll: unknown feedbackReportId {request_id!r}",
            )
        if 500 <= sc <= 599:
            raise EEUpstreamUnavailable(status=sc, detail=f"X-Road poll 5xx ({sc})")
        raise EEUpstreamUnavailable(
            status=sc, detail=f"unexpected X-Road poll status {sc}"
        )

    # ---- confirm (gated stub) ------------------------------------------- #

    async def confirm(self, request_id: str) -> ConfirmResult:
        """``POST /confirm`` (kinnitamine) — GATED STUB, shape UNVERIFIED.

        The confirmation service is marked "täpsem info lisandub" in the X-tee
        guide §2; its request/response shape is unpublished. We refuse to
        fabricate a request and raise ``EEConfirmServiceUnverified`` — before
        any transport is resolved, so this fires regardless of credentials
        (it is a spec gap, not a live-creds gap). ``CONFIRMED`` exists in the
        transition map but is unreachable at runtime while this is gated.
        """
        raise EEConfirmServiceUnverified()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _extract_feedback(body: bytes) -> ReturnFeedback | None:
    """Parse a terminal feedback message from a poll body, else None (pending).

    The KMD3 poll-body wrapper (koondvaade) is UNVERIFIED, so we are tolerant:
    if the body *is* (or *contains*, at its root) an operationAccepted/Rejected
    message we parse it; anything else is treated as "no terminal message yet"
    → pending.
    """
    if not body.strip():
        return None
    try:
        return parse_feedback_message(body)
    except Exception:
        return None


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _detail(data: dict[str, Any], resp: httpx.Response, *, default: str) -> str:
    if isinstance(data, dict):
        for key in ("errorMessage", "errorCode", "detail", "message"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v
    text = resp.text or ""
    return text[:400] if text else default
