"""Typed exception hierarchy for the SISS HTTP client.

Every non-2xx response from SISS is turned into one of these. The HTTP
status code + parsed CDR-standard ``ResponseErrorList`` payload are
preserved so handlers upstream can make decisions (retry, surface to
admin UI, log-and-move-on) without re-parsing the raw response.

CDR-standard error payload shape::

    {
      "errors": [
        {
          "code": "0001",
          "title": "Invalid field",
          "detail": "accountId is required",
          "meta": {...}
        }
      ]
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SissErrorDetail:
    """One entry in a CDR ``ResponseErrorList.errors`` array."""

    code: str
    title: str
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class SissError(Exception):
    """Base class for all SISS API failures.

    Attributes mirror what a caller needs for logging / retry / UI:

    - ``http_status``  — the raw HTTP status code (e.g. ``401``, ``429``).
    - ``errors``       — parsed CDR errors (may be empty if the response
                         didn't include a ``ResponseErrorList`` body).
    - ``interaction_id`` — the ``x-fapi-interaction-id`` we used for the
                         request, for correlation in SISS support tickets.
    """

    http_status: int
    errors: list[SissErrorDetail]
    interaction_id: str | None

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        errors: list[SissErrorDetail] | None = None,
        interaction_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.errors = errors or []
        self.interaction_id = interaction_id

    @classmethod
    def from_payload(
        cls,
        *,
        http_status: int,
        payload: Any,
        interaction_id: str | None,
    ) -> SissError:
        """Build the most specific subclass we can from the status + body."""
        errors = _parse_errors(payload)
        message = _format_message(http_status, errors)
        subclass = _subclass_for_status(http_status)
        return subclass(
            message,
            http_status=http_status,
            errors=errors,
            interaction_id=interaction_id,
        )


class SissAuthError(SissError):
    """HTTP 401 — invalid / expired OAuth token or missing APIM key."""


class SissScopeError(SissError):
    """HTTP 403 — token valid but caller lacks required scope."""


class SissValidationError(SissError):
    """HTTP 4xx other than 401/403/429 — malformed request or bad data."""


class SissRateLimitError(SissError):
    """HTTP 429 — rate limit exceeded. Inspect ``retry_after_seconds``."""

    retry_after_seconds: float | None

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        errors: list[SissErrorDetail] | None = None,
        interaction_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            http_status=http_status,
            errors=errors,
            interaction_id=interaction_id,
        )
        self.retry_after_seconds = retry_after_seconds


class SissServerError(SissError):
    """HTTP 5xx — upstream issue; usually worth a bounded retry."""


def _parse_errors(payload: Any) -> list[SissErrorDetail]:
    """Pull a ``ResponseErrorList`` out of a parsed JSON body.

    Tolerates missing fields and non-list ``errors`` values — SISS's
    error envelope is not perfectly stable across endpoints.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    out: list[SissErrorDetail] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        raw_meta = entry.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        out.append(
            SissErrorDetail(
                code=str(entry.get("code", "")),
                title=str(entry.get("title", "")),
                detail=str(entry.get("detail", "")),
                meta=meta,
            )
        )
    return out


def _format_message(http_status: int, errors: list[SissErrorDetail]) -> str:
    if errors:
        first = errors[0]
        tail = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        return f"SISS {http_status}: {first.code} {first.title} — {first.detail}{tail}"
    return f"SISS {http_status}: no error body"


def _subclass_for_status(http_status: int) -> type[SissError]:
    if http_status == 401:
        return SissAuthError
    if http_status == 403:
        return SissScopeError
    if http_status == 429:
        return SissRateLimitError
    if 400 <= http_status < 500:
        return SissValidationError
    if 500 <= http_status < 600:
        return SissServerError
    return SissError
