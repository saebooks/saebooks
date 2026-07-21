"""Exception hierarchy for accounting-package sync.

Mirrors the shape of ``services/bank_feeds/errors.py``. ``sync_xero``'s
router (``saebooks.api.v1.sync_xero``) maps each subclass to a specific
HTTP status.
"""
from __future__ import annotations


class SyncError(Exception):
    """Base class for every sync-package error surfaced to callers."""


class SyncNotConfiguredError(SyncError):
    """Raised when ``SAEBOOKS_FIELD_ENCRYPTION_KEY`` is unset.

    The OAuth refresh token is encrypted at rest, so the encryption
    key is a hard prerequisite. The connector refuses to ``connect()``
    or ``sync()`` when this is missing — surfaces in the UI as
    "Encryption key not configured" rather than a 500.
    """


class SyncAuthError(SyncError):
    """OAuth token refresh failed and is unrecoverable.

    The connection is marked ``revoked`` — only an explicit
    operator re-OAuth can recover. Refresh tokens that are rejected
    do not come back; the upstream has invalidated them.
    """

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class SyncRateLimited(SyncError):
    """Upstream returned 429.

    ``retry_after`` carries the value of the ``Retry-After`` header in
    seconds, or ``None`` when absent. The client wrapper backs off
    transparently; this exception only escapes when the request had
    already used up its retry budget.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SyncUpstreamError(SyncError):
    """Upstream returned 5xx.

    Treated as transient — a future worker would log and retry on the
    next scheduled poll. Connection status stays ``active``.
    """

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class SyncValidationError(SyncError):
    """Upstream returned 4xx (other than 401/429).

    The offending object is quarantined. The connection stays
    ``active`` so other objects continue to sync.
    """

    def __init__(self, message: str, *, http_status: int = 0, payload: object = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload


class SyncConflictError(SyncError):
    """Both sides moved on the same object since last sync.

    A ``sync_audit_log`` row with ``direction='conflict'`` and the diff
    payload is logged. The connection's ``status`` flips to ``error``
    and the operator must resolve via the Settings -> Sync UI (re-push
    or re-pull). LWW is the default for the unambiguous case (only one
    side moved); this class fires only when both moved.
    """
