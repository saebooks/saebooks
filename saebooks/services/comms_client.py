"""Comms-module HTTP client — the engine side of the #32 extraction.

Email transport (SMTP/Resend), the customer-email kill switch + FROM
allowlist, the Outlook/Graph draft handoff, and the Jinja email templates
all live in the app "comms" module now (saebooks-web). The engine is the
*accountant*: it produces facts (recipients, subject, assembled HTML,
attachment bytes, the per-tenant outbound flag) and records audit rows. The
module is the *bookkeeper*: it owns policy (send/draft/block) and transport.

This module is the single thin HTTP client both email facades
(``services.customer_email`` and ``services.email``) route through. It does
NOT interpret outcomes — it just performs the POST and hands back the
module's decision, raising ``CommsServiceError`` on any transport-level
failure or non-200 response.

Contract
--------
``POST {settings.comms_service_url}/internal/comms/send``

Headers::

    Content-Type: application/json
    X-Comms-Token: {settings.comms_service_token}   (only when non-empty)

Request body (JSON)::

    {
      "kind": "customer_doc" | "magic_link" | "billing_receipt" | "raw",
      "to":         ["a@example.com", ...],          # always a list
      "subject":    "…",
      "body_html":  "<p>…</p>" | null,               # null for magic_link
      "body_text":  "…" | null,
      "attachments": [
        {"filename": "INV-1.pdf",
         "content_b64": "<base64>",
         "content_type": "application/pdf"}
      ],
      "meta": { … kind-specific, see the two facades … }
    }

Response body (JSON, HTTP 200)::

    {"outcome": "sent" | "drafted" | "blocked",
     "provider_id": "<id>" | null,
     "detail": "<human reason / error>" | null}

Any connection error, timeout, or non-200 status raises
``CommsServiceError``.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Transport can be a real SMTP handshake or a Resend/Graph round-trip inside
# the module, plus a Jinja render — allow a generous ceiling like the render
# service client does.
_COMMS_TIMEOUT_SECONDS = 60.0


class CommsServiceError(RuntimeError):
    """Raised when the comms module is unreachable or answers non-200.

    Distinct from a *policy* outcome (blocked) or an *input* error
    (CustomerEmailError / EmailError): this means the transport call to the
    module itself failed, so the caller could not obtain a decision at all.
    """


@dataclass(frozen=True)
class CommsResult:
    """The module's decision for one send attempt."""

    outcome: str            # "sent" | "drafted" | "blocked" (module may extend)
    provider_id: str | None  # Resend id / Graph draft id / None
    detail: str | None       # human-readable reason or error, if any


def encode_attachment(filename: str, content: bytes, content_type: str) -> dict[str, str]:
    """Serialise one attachment to the wire shape the module expects."""
    return {
        "filename": filename,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "content_type": content_type,
    }


async def post_comms_send(payload: dict) -> CommsResult:
    """POST a message to the comms module and return its decision.

    Raises
    ------
    CommsServiceError
        On connection failure, timeout, or any non-200 status.
    """
    from saebooks.config import settings

    base_url = settings.comms_service_url.rstrip("/")
    url = f"{base_url}/internal/comms/send"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = settings.comms_service_token
    if token:
        headers["X-Comms-Token"] = token

    try:
        async with httpx.AsyncClient(timeout=_COMMS_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise CommsServiceError(
            f"Timeout waiting for comms service at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        # httpx.ConnectError and every other transport-level failure.
        raise CommsServiceError(
            f"Cannot reach comms service at {url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise CommsServiceError(
            f"comms service {url} returned HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise CommsServiceError(
            f"comms service {url} returned non-JSON body: {resp.text[:200]}"
        ) from exc

    outcome = body.get("outcome")
    if not outcome:
        raise CommsServiceError(
            f"comms service {url} response missing 'outcome': {body!r}"
        )
    return CommsResult(
        outcome=str(outcome),
        provider_id=body.get("provider_id"),
        detail=body.get("detail"),
    )
