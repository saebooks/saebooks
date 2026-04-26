"""RFC 7807 Problem Details error registry and FastAPI exception handlers.

All non-2xx JSON API responses from ``/api/v1/*`` endpoints are normalised
to the RFC 7807 ``application/problem+json`` shape when the caller
includes ``Accept: application/json`` (or ``application/problem+json``).

Schema
------
All problem detail objects carry the mandatory RFC 7807 fields plus a
stable machine-readable ``code`` that integrators can key on without
parsing ``detail`` prose::

    {
        "type":   "https://saebooks.io/problems/contact_not_found",
        "title":  "Contact Not Found",
        "status": 404,
        "detail": "No contact with that ID exists in this tenant.",
        "code":   "contact_not_found"
    }

``type`` is a stable URI for each code.  It is NOT required to be
dereferenceable — it is an identifier.  The base URI is
``https://saebooks.io/problems/`` + code.

``title`` is a short, human-readable summary that does NOT change between
occurrences.  ``detail`` may include per-request context (e.g. the bad
UUID).

Error codes
-----------
The registry below maps every stable machine-readable code to its default
HTTP status code and title.  Handlers that raise these can override
``detail`` for request-specific context.

HTML routes are unaffected — they return Jinja2-rendered templates as
before.  Only requests with ``Accept: application/json`` (or
``application/problem+json``) are converted to problem+json.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

# ---------------------------------------------------------------------------
# Error code registry
# ---------------------------------------------------------------------------

_BASE_URI = "https://saebooks.io/problems/"


class _Code:
    """Lightweight registry entry."""

    __slots__ = ("code", "status", "title")

    def __init__(self, code: str, status: int, title: str) -> None:
        self.code = code
        self.status = status
        self.title = title

    @property
    def type_uri(self) -> str:
        return f"{_BASE_URI}{self.code}"


# Registry — extend as new error surfaces are stabilised.
_REGISTRY: dict[str, _Code] = {}


def _reg(code: str, status: int, title: str) -> _Code:
    entry = _Code(code, status, title)
    _REGISTRY[code] = entry
    return entry


# Cross-tenant / permissions
CROSS_TENANT_FORBIDDEN = _reg("cross_tenant_forbidden", 403, "Cross-Tenant Access Forbidden")
INSUFFICIENT_PERMISSIONS = _reg("insufficient_permissions", 403, "Insufficient Permissions")

# Resource lifecycle
CONTACT_NOT_FOUND = _reg("contact_not_found", 404, "Contact Not Found")
INVOICE_NOT_FOUND = _reg("invoice_not_found", 404, "Invoice Not Found")
BILL_NOT_FOUND = _reg("bill_not_found", 404, "Bill Not Found")
PAYMENT_NOT_FOUND = _reg("payment_not_found", 404, "Payment Not Found")
ACCOUNT_NOT_FOUND = _reg("account_not_found", 404, "Account Not Found")
COMPANY_NOT_FOUND = _reg("company_not_found", 404, "Company Not Found")
TENANT_NOT_FOUND = _reg("tenant_not_found", 404, "Tenant Not Found")
RESOURCE_NOT_FOUND = _reg("resource_not_found", 404, "Resource Not Found")
RESOURCE_ARCHIVED = _reg("resource_archived", 410, "Resource Has Been Archived")

# Idempotency
IDEMPOTENCY_KEY_CONFLICT = _reg("idempotency_key_conflict", 409, "Idempotency Key Conflict")
VERSION_MISMATCH = _reg("version_mismatch", 409, "Version Mismatch")

# Validation
VALIDATION_FAILED = _reg("validation_failed", 422, "Validation Failed")
PRECONDITION_REQUIRED = _reg("precondition_required", 428, "Precondition Required")

# Auth
AUTHENTICATION_REQUIRED = _reg("authentication_required", 401, "Authentication Required")
INVALID_CREDENTIALS = _reg("invalid_credentials", 401, "Invalid Credentials")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wants_json(request: Request) -> bool:
    """True when the caller signals it can accept JSON / problem+json.

    Checks the ``Accept`` header for ``application/json``,
    ``application/problem+json``, or ``*/*``.  HTML routes should never
    trigger this because their Accept header will be ``text/html`` or the
    caller comes from a browser.
    """
    accept = request.headers.get("accept", "")
    return any(
        token in accept
        for token in (
            "application/json",
            "application/problem+json",
            "*/*",
        )
    )


def _problem(
    *,
    status: int,
    code: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    type_uri: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build an ``application/problem+json`` response."""
    entry = _REGISTRY.get(code or "") if code else None
    resolved_title = title or (entry.title if entry else "Error")
    resolved_type = type_uri or (entry.type_uri if entry else f"{_BASE_URI}error")
    resolved_code = code or "error"

    body: dict[str, Any] = {
        "type": resolved_type,
        "title": resolved_title,
        "status": status,
        "code": resolved_code,
    }
    if detail:
        body["detail"] = detail
    if extra:
        body.update(extra)

    return JSONResponse(
        content=body,
        status_code=status,
        media_type="application/problem+json",
    )


# ---------------------------------------------------------------------------
# Exception handlers — registered on the FastAPI app in main.py
# ---------------------------------------------------------------------------


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """Convert ``starlette.HTTPException`` to RFC 7807 problem+json.

    Only fires when the caller's ``Accept`` header includes
    ``application/json`` or ``application/problem+json``.  For
    HTML/browser callers FastAPI's default handler takes over.

    The ``detail`` field from the original exception is forwarded as
    ``detail`` in the problem body.  If the caller has set
    ``exc.headers``, those headers are propagated to the response.
    """
    if not _wants_json(request):
        # Fall back to FastAPI's built-in handler (plain JSON ``{"detail": ...}``).
        # Import here to avoid circular at module load.
        from fastapi.exception_handlers import http_exception_handler as _default

        return await _default(request, exc)  # type: ignore[return-value]

    # Map known HTTP status codes to stable problem codes when possible.
    _STATUS_CODE_MAP: dict[int, _Code] = {
        401: _REGISTRY["authentication_required"],
        403: _REGISTRY["insufficient_permissions"],
        404: _REGISTRY["resource_not_found"],
        409: _REGISTRY["version_mismatch"],
        422: _REGISTRY["validation_failed"],
        428: _REGISTRY["precondition_required"],
    }
    entry = _STATUS_CODE_MAP.get(exc.status_code)

    detail_str = str(exc.detail) if exc.detail is not None else None
    resp = _problem(
        status=exc.status_code,
        code=entry.code if entry else None,
        title=entry.title if entry else None,
        detail=detail_str,
    )
    if exc.headers:
        resp.headers.update(exc.headers)
    return resp


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Convert pydantic ``RequestValidationError`` to RFC 7807 problem+json.

    Bundles the raw validation errors into the ``errors`` field so
    callers can enumerate which fields failed without parsing ``detail``
    prose.
    """
    if not _wants_json(request):
        from fastapi.exception_handlers import request_validation_exception_handler as _default

        return await _default(request, exc)

    return _problem(
        status=422,
        code="validation_failed",
        detail="Request body or query parameters failed validation.",
        extra={"errors": exc.errors()},
    )


def register_handlers(app: Any) -> None:
    """Install both handlers on a FastAPI ``app`` instance."""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
