"""Typed exceptions raised by ``RemoteBankFeedsService``.

These mirror the lodgement counterparts (``services/lodgement/exceptions.py``)
deliberately — same shape, same shallow hierarchy, same one-class-per-status
discipline — so a developer who has worked on the lodgement client recognises
the layout immediately. The HTTP status -> exception map is the contract
surface; it lives in ``remote.py:_raise_for_status`` and is the single grep
target for "what does the server tell us when X?".

Distinct from the legacy ``saebooks.services.bank_feeds.errors`` hierarchy
(SISS-direct), which is unaffected by this module: those errors describe
SISS-API failures the relay-server now absorbs. The exceptions here describe
failures of the **relay** itself — the typed layer the saebooks-api code
above us cares about.

Why we don't re-use the lodgement exception classes
---------------------------------------------------
Different domains, different upstream surfaces, different recovery hints.
A 403 from feeds-server says "feeds_enabled=false, upgrade for bank-feeds";
a 403 from lodge-server says "ato_sbr disabled, upgrade for STP/BAS". The
UI message is different and tying them via inheritance bites every time we
want to render that message. Keep them parallel; never share the parent.
"""
from __future__ import annotations


class FeedsError(Exception):
    """Base class. Catch this to mop up every relay-related failure."""


class FeedsAuthError(FeedsError):
    """Server returned 401 — licence token missing, malformed, or expired.

    Most common cause: the licence cache file is stale and the user has
    not refreshed since their last renewal. Calling
    ``LicenseService.reload()`` (exposed on the admin licence page) usually
    clears it. The relay never re-issues tokens — it only verifies them.
    """

    def __init__(
        self, detail: str = "Feeds-server rejected licence token"
    ) -> None:
        super().__init__(detail)
        self.detail = detail


class FeedsEditionError(FeedsError):
    """Server returned 403 — the licence does not permit bank-feeds.

    The relay reads ``feeds_enabled`` off the licence JWT (or falls back
    to the edition gate when missing). Community / offline editions get
    this. Should be impossible past the route-level ``require_feature``
    gate; raised here as a backstop in case someone bypasses the gate
    (eg directly hitting the API with a Community-tier token).
    """

    def __init__(
        self,
        detail: str = "Licence edition does not include bank-feeds",
    ) -> None:
        super().__init__(detail)
        self.detail = detail


class FeedsIdempotencyConflict(FeedsError):
    """Server returned 409 — same idempotency key, different body hash.

    Per the contract: ``(license_id, route, idempotency_key)`` is the
    unique index, and a second request under the same key with a
    different ``sha256(canonical_json(body))`` is a contract violation.
    Almost always a client bug (retried with a freshly-built body). The
    server returns the prior request's hash + this request's hash so
    operators can diff them.
    """

    def __init__(
        self,
        detail: str = "Idempotency-Key reused with different request body",
        first_request_hash: str | None = None,
        this_request_hash: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.first_request_hash = first_request_hash
        self.this_request_hash = this_request_hash


class FeedsStubError(FeedsError):
    """Server returned 501 — relay is in stub mode for this route.

    Build-stub of feeds-server returns 501 from every business route except
    ``GET /connections`` (which returns an empty list). The body shape is
    deterministic: ``{"status": "stub", "would_have_<verb>": true,
    "stub_<noun>_id": "stub_<noun>_<uuid>", "comment": "..."}``.

    The router layer treats this as a known state — surface a "feeds-server
    stubbed" banner rather than 500-ing.
    """

    def __init__(
        self, detail: str = "Feeds-server is in stub mode", body: dict | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.body: dict = body or {}


class FeedsUpstreamError(FeedsError):
    """Server returned 502 — SISS upstream returned 5xx / refused the request.

    The relay reached SISS but SISS itself was unhappy. Retry policy is
    caller-driven; the consent flow can usually be retried after a few
    seconds, sync ops should back off longer.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class FeedsUpstreamUnavailable(FeedsError):
    """Server returned 503 (or transport-errored) — SISS unreachable.

    Fully transient class — the relay couldn't even talk to SISS. Treat
    the same as a network blip; admin retry from the UI is the user-
    facing recovery.
    """

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FeedsValidationError(FeedsError):
    """Server returned 400 — malformed body / unknown institution_id.

    Almost always a client bug (we sent a body that doesn't match the
    contract). The detail string carries the server's explanation; we
    surface it verbatim in the audit log and in admin-tier error UI.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class FeedsNotFoundError(FeedsError):
    """Server returned 404 — connection not owned by this licence (or absent).

    Per the contract, the server deliberately conflates not-found with
    not-owned-by-you to avoid licence-fishing. So this exception covers
    both cases; the caller should treat them the same way (404 to the
    end user).
    """

    def __init__(self, detail: str = "Connection not found") -> None:
        super().__init__(detail)
        self.detail = detail


class FeedsUnsupportedEdition(FeedsError):
    """Raised by the offline / community fallback when bank-feeds is gated.

    Mirrors ``LodgementUnsupportedEdition`` — the route gate normally
    short-circuits with a 403 well before any service call, but this is
    the belt-and-braces variant when something bypasses the gate.
    """

    def __init__(
        self, required_edition: str = "business", flag: str = "bank_feeds"
    ) -> None:
        msg = (
            f"Bank feeds requires the '{flag}' feature, available in the "
            f"{required_edition.capitalize()} edition or higher. "
            f"Upgrade your licence at https://saebooks.com.au/buy."
        )
        super().__init__(msg)
        self.required_edition = required_edition
        self.flag = flag
