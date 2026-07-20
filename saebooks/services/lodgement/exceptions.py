"""Typed exceptions raised by ``RemoteLodgementService`` and ``NullLodgementService``.

The hierarchy is deliberately shallow — every class extends
``LodgementError`` so callers can ``except LodgementError`` to handle
all server-rejected cases generically (rare; the STP UI usually wants
to discriminate so it can render the right user message).

Why not return error codes
--------------------------

A ``LodgementResult`` always represents a successful relay; making
errors explicit exceptions keeps the happy-path call site clean.
The HTTP status → exception mapping lives in ``remote.py`` and is
a single switch — add a status code there, not here.
"""
from __future__ import annotations

from typing import Any


class LodgementError(Exception):
    """Base class. Catch this to mop up every lodgement-related failure."""


class LodgementValidationError(LodgementError):
    """Server returned 400 — the envelope hash mismatched or was malformed.

    Almost always a client bug (we sent the wrong hash for the bytes,
    or shipped non-XML where XML was required). The lodge-server
    detail is in ``detail`` for inclusion in error UI.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class LodgementAuthError(LodgementError):
    """Server returned 401 — licence token missing, malformed, or expired.

    Usually means the licence cache file is stale and the user needs
    to refresh. The /admin/license refresh button calls into
    ``LicenseService.reload()`` which fixes it.
    """

    def __init__(self, detail: str = "Lodge-server rejected licence token") -> None:
        super().__init__(detail)
        self.detail = detail


class LodgementEditionError(LodgementError):
    """Server returned 403 — the licence edition does not include ``ato_sbr``.

    Should be impossible if the factory + UI gating is correct (the
    Pro/Enterprise check happens client-side first). Raised here as
    a backstop in case someone bypasses the gate.
    """

    def __init__(self, detail: str = "Licence edition does not permit lodgement") -> None:
        super().__init__(detail)
        self.detail = detail


class LodgementRejected(LodgementError):
    """Server returned 422 — the ATO rejected the envelope.

    The ATO error list is in ``ato_errors`` for the UI to surface
    verbatim (e.g. invalid TFN format, employee BMS ID changed
    between payevents, etc.). The full response body is preserved
    in ``raw_response``.
    """

    def __init__(
        self,
        detail: str,
        ato_errors: list[dict[str, Any]] | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.ato_errors: list[dict[str, Any]] = ato_errors or []
        self.raw_response: dict[str, Any] = raw_response or {}


class LodgementUpstreamUnavailable(LodgementError):
    """Server returned 5xx (or transport errored) — try again later.

    Caller decides retry policy. STP has no statutory retry deadline
    short of the pay-cycle so a 30-min backoff is fine; BAS deadlines
    are firmer and the UI should warn at the 24h mark.
    """

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class LodgementUnsupportedEdition(LodgementError):
    """Raised by ``NullLodgementService`` when called.

    This is what ``require_feature("ato_sbr")`` falls back to if the
    user somehow bypasses the route-level gate. The message names
    the required edition so the UI can prompt for an upgrade.
    """

    def __init__(
        self, required_edition: str = "pro", flag: str = "ato_sbr"
    ) -> None:
        msg = (
            f"Lodgement requires the '{flag}' feature, available in the "
            f"{required_edition.capitalize()} edition or higher. "
            f"Upgrade your licence at https://saebooks.com.au/buy."
        )
        super().__init__(msg)
        self.required_edition = required_edition
        self.flag = flag


# --------------------------------------------------------------------------- #
# EE (Estonia / X-Road KMD3) exceptions — M3.
#
# The EE lodgement rail is a different lifecycle from the AU relay (async
# submit→poll→confirm over X-Road, not fire-and-forget POST). It gets its own
# exception family, all under ``EELodgementError`` (itself a ``LodgementError``
# so ``except LodgementError`` still mops up every jurisdiction).
# --------------------------------------------------------------------------- #


class EELodgementError(LodgementError):
    """Base for every EE (X-Road / e-MTA KMD3) lodgement failure."""


class EELiveCredentialsMissing(EELodgementError):
    """No X-Road mTLS client cert / security-server config is present.

    THE LOUD LIVE GATE. Raised before any socket is opened when the
    :class:`EELodgementClient` is asked to talk to a real security server but
    has neither an injected transport (tests / ``MockTransport``) nor a
    complete :class:`~saebooks.services.lodgement.adapters.ee_client.MtlsConfig`.

    X-Road membership (branch 17151236) + a security server + the mTLS client
    cert/key are NOT provisioned. Nothing in SAE Books files to the real tax
    board until Richard wires those — the live path is a single complete
    ``MtlsConfig`` away and stays loudly gated until then. This exception is
    the guarantee that a mis-wired call fails fast with zero network egress
    rather than silently attempting a connection to EMTA.
    """

    def __init__(
        self,
        detail: str = (
            "EE X-Road live filing is not configured — no mTLS client "
            "cert/key + security-server config present (X-Road membership "
            "branch 17151236 unwired). Refusing to open a connection to the "
            "tax board. Inject a transport for offline tests, or provision a "
            "complete MtlsConfig to go live."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


class EEFilingValidationError(EELodgementError):
    """X-Road ``POST /submit-data`` returned 400 with an error body.

    The KMD3 submit service returns ``{"errorCode": ..., "errorMessage": ...}``
    on a malformed request (verified from the X-tee interfacing guide §3.2,
    e.g. ``SINGLE_FILE_REQUIRED`` / ``PERSON_NOT_DEFINED``). This is a
    client-side / structural problem — distinct from a business rejection of
    an accepted-then-processed declaration (see :class:`EEFilingRejected`).
    """

    def __init__(
        self,
        detail: str,
        error_code: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.error_code = error_code
        self.raw_response: dict[str, Any] = raw_response or {}


class EEFilingRejected(EELodgementError):
    """The feedback report carried an ``operationRejected`` message.

    The declaration's data processing failed: XML-structure errors and/or
    business-rule (functional) errors. Parsed from the in-tree
    ``operationrejected.xsd`` shape. The two error lists are surfaced verbatim
    for the UI. ``request_id`` correlates back to the feedback report UUID.
    """

    def __init__(
        self,
        detail: str,
        *,
        request_id: str | None = None,
        xml_errors: list[str] | None = None,
        functional_errors: list[Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.request_id = request_id
        self.xml_errors: list[str] = xml_errors or []
        self.functional_errors: list[Any] = functional_errors or []


class EEUpstreamUnavailable(EELodgementError):
    """Security server / X-Road / EMTA returned 5xx or the transport errored.

    Caller decides retry. KMD3 submit is asynchronous server-side, so a 5xx on
    submit means the file was never queued — safe to retry with the same
    payload (amendment is full resubmission of the period anyway, GUIDE §4).
    """

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class EEFilingStateError(EELodgementError):
    """An illegal filing-lifecycle transition was requested.

    e.g. ``confirm`` before the return reached ``ACCEPTED``, or ``poll`` after
    a terminal ``REJECTED``. Guards the submit→poll→confirm state machine so a
    caller cannot skip a step or act on a terminal return.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- #
# NZ (Inland Revenue gateway services) exceptions — NZ jurisdiction module.
#
# The NZ rail (when built) is IR "gateway services": SOAP + mTLS (IR-issued
# X.509 client cert) + OAuth2/JWT, plus the separate MBIE NZBN REST API.
# No transport exists yet — the adapter is shaped (targets validated,
# payloads accepted) but every network-needing call fails loudly before
# any socket, same pattern as the EE live gate above.
# --------------------------------------------------------------------------- #


class NZLodgementError(LodgementError):
    """Base for every NZ (IR gateway services / NZBN) lodgement failure."""


class NZLiveCredentialsMissing(NZLodgementError):
    """No IR gateway credentials / NZBN API key are present.

    THE LOUD LIVE GATE (mirrors :class:`EELiveCredentialsMissing`).
    Raised before any socket is opened when the NZ adapter is asked to
    talk to Inland Revenue's gateway services (SOAP + mTLS + OAuth2) or
    the MBIE NZBN API without a complete credential set. Nothing is
    provisioned today — IR digital-service-provider onboarding (DSP due
    diligence + OSF cycle) and the SPS 21/02 offshore-record-storage
    authorisation are prerequisites that have not been started. This
    exception is the guarantee that a mis-wired call fails fast with
    zero network egress rather than silently attempting a connection to
    Inland Revenue.
    """

    def __init__(
        self,
        detail: str = (
            "NZ Inland Revenue live filing is not configured — no IR "
            "gateway credential set (X.509 client cert/key + OAuth2 "
            "client) or NZBN API key is present, and the SOAP gateway "
            "transport is a later phase. Refusing to open a connection "
            "to Inland Revenue."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


class EEConfirmServiceUnverified(EELodgementError):
    """The KMD3 confirmation ("kinnitamine") service shape is UNVERIFIED.

    The X-tee guide §2 marks the confirm service "täpsem info lisandub" (more
    detail to follow) — its request/response wire shape is not published. Per
    the "never fabricate an unpinned contract" discipline (mirrors
    ``RemoteLodgementService.poll_status``'s ``NotImplementedError`` gate), we
    fail loudly rather than invent a request. This is a GATED STUB, not a live
    gate: it fires regardless of credentials because the spec itself is absent.
    """

    def __init__(
        self,
        detail: str = (
            "EE KMD3 confirmation service is UNVERIFIED (X-tee guide §2: "
            "'täpsem info lisandub'). Its request/response shape is not "
            "published; refusing to fabricate one. confirm() is a gated stub "
            "until EMTA publishes the schema."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- #
# UK (HMRC MTD / GovTalk) exceptions — UK jurisdiction module.
#
# Same shape as the EE family: a per-jurisdiction base under
# ``LodgementError`` plus THE LOUD LIVE GATE, raised before any socket.
# --------------------------------------------------------------------------- #


class UKLodgementError(LodgementError):
    """Base for every UK (HMRC / Companies House) lodgement failure."""


class UKLiveCredentialsMissing(UKLodgementError):
    """No HMRC application credentials / OAuth grant is present.

    THE LOUD LIVE GATE (the ``EELiveCredentialsMissing`` pattern). Raised
    before any socket is opened when the UK adapter is asked to submit to a
    real HMRC endpoint. No HMRC developer-hub application, OAuth 2.0 client,
    fraud-prevention-header middleware or Companies House presenter account
    is provisioned — and the transport itself is deliberately not built in
    this wave (see ``adapters/uk.py``'s docstring). This exception is the
    guarantee that a mis-wired call fails fast with zero network egress
    rather than silently attempting a connection to HMRC.
    """

    def __init__(
        self,
        detail: str = (
            "UK HMRC live filing is not configured — no MTD application "
            "credentials / OAuth grant present, and the UK transport "
            "(OAuth client + Gov-Client-* fraud-prevention-header "
            "middleware) is a later phase. Refusing to open a connection "
            "to HMRC."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- #
# LT (VMI / i.MAS / Sodra) exceptions — LT jurisdiction module.
# LV (VID EDS) exceptions — LV jurisdiction module.
#
# Same shape as the EE/NZ/UK families: a per-jurisdiction base under
# ``LodgementError`` plus THE LOUD LIVE GATE, raised before any socket.
# --------------------------------------------------------------------------- #


class LTLodgementError(LodgementError):
    """Base for every LT (VMI / i.MAS / Sodra) lodgement failure."""


class LTLiveCredentialsMissing(LTLodgementError):
    """No VMI / i.MAS API credentials are present.

    THE LOUD LIVE GATE (the ``EELiveCredentialsMissing`` pattern). Raised
    before any socket is opened when the LT adapter is asked to submit to
    a real VMI endpoint (EDS declaration filing, the i.MAS/i.SAF web
    services) or to Sodra's EDAS. No i.MAS web-service credential set,
    EDS integration or Sodra EDAS access is provisioned — and the LT
    transport itself is deliberately not built in this wave (see
    ``adapters/lt.py``'s docstring). This exception is the guarantee that
    a mis-wired call fails fast with zero network egress rather than
    silently attempting a connection to the Lithuanian tax authority.
    """

    def __init__(
        self,
        detail: str = (
            "LT VMI live filing is not configured — no i.MAS/EDS "
            "credential set (or Sodra EDAS access) is present, and the "
            "LT transport is a later phase. Refusing to open a "
            "connection to VMI/Sodra."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail


class LVLodgementError(LodgementError):
    """Base for every LV (VID EDS) lodgement failure."""


class LVLiveCredentialsMissing(LVLodgementError):
    """No VID EDS credentials are present.

    THE LOUD LIVE GATE (the ``EELiveCredentialsMissing`` pattern). Raised
    before any socket is opened when the LV adapter is asked to submit a
    PVN declaration, employer report or UIN return to VID's Electronic
    Declaration System. Nothing is provisioned today — no EDS API
    credential set exists and the transport itself is deliberately not
    built in this wave (see ``adapters/lv.py``'s docstring). This
    exception is the guarantee that a mis-wired call fails fast with zero
    network egress rather than silently attempting a connection to VID.
    """

    def __init__(
        self,
        detail: str = (
            "LV VID EDS live filing is not configured — no EDS credential "
            "set is present, and the LV transport is a later phase. "
            "Refusing to open a connection to VID."
        ),
    ) -> None:
        super().__init__(detail)
        self.detail = detail
