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
