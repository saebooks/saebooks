"""Internal email facade — magic-link + generic transactional mail.

This is the engine side of the #32 comms extraction for NON-customer-facing
mail (magic links, signup verification / reset, subscription receipts, the
web invoice-email router). The SMTP transport, the outbox fallback, and the
Jinja email templates all moved to the app comms module (saebooks-web); this
module just assembles a request and POSTs it — see
``saebooks.services.comms_client``.

Two public send functions, both preserving the pre-extraction signatures so
their callers are untouched at the call site:

  * ``send_email(to, subject, template, context, …)`` — TEMPLATE mail. The
    template name + context travel in ``meta``; the module renders the Jinja
    template (which now lives module-side) and sends. Used by the magic-link
    service. POSTs ``kind="magic_link"``.

  * ``send_raw_email(to, subject, html, …)`` — pre-assembled HTML mail. Same
    signature as the old ``services.mailer.send_email``; used by the signup /
    billing / web-invoice call sites that already build their own HTML. POSTs
    ``kind="raw"`` (or ``kind="billing_receipt"`` when the caller asks).

The old ``EmailError`` / ``EmailAttachment`` / ``EmailResult`` symbols are
re-homed here (mailer.py is deleted). Transport failure raises ``EmailError``
— exactly what the old SMTP path raised — so no caller ``except`` clause
changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from saebooks.services.comms_client import (
    CommsServiceError,
    encode_attachment,
    post_comms_send,
)

if TYPE_CHECKING:
    from saebooks.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "EmailAttachment",
    "EmailError",
    "EmailResult",
    "send_email",
    "send_raw_email",
]


class EmailError(RuntimeError):
    """Raised when an email cannot be assembled or the module rejects it."""


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class EmailResult:
    """What callers get back. Callers currently ignore it, but the shape is
    preserved for compatibility. ``mode`` reflects the module's outcome
    mapped onto the legacy transport labels: sent→"smtp", blocked→"outbox",
    anything else passed through verbatim."""

    mode: str  # "smtp" | "outbox" | <module outcome>
    message_id: str
    outbox_path: str | None = None
    recipients: tuple[str, ...] = field(default_factory=tuple)


def _attachments_wire(attachments: list[EmailAttachment] | None) -> list[dict[str, str]]:
    return [
        encode_attachment(att.filename, att.content, att.mime_type)
        for att in attachments or []
    ]


async def _send_via_comms(
    payload: dict, recipients: list[str]
) -> EmailResult:
    """POST to the comms module and map its decision onto EmailResult.

    Raises ``EmailError`` on transport failure (mirrors the old SMTP path,
    which raised ``EmailError`` on delivery failure — callers already catch
    a broad Exception or let it 500).
    """
    try:
        result = await post_comms_send(payload)
    except CommsServiceError as exc:
        raise EmailError(f"comms service delivery failed: {exc}") from exc

    if result.outcome == "sent":
        mode = "smtp"
    elif result.outcome == "blocked":
        mode = "outbox"
    else:
        mode = result.outcome
    return EmailResult(
        mode=mode,
        message_id=result.provider_id or "",
        outbox_path=result.detail,
        recipients=tuple(recipients),
    )


async def send_email(
    to: str | list[str],
    subject: str,
    template: str,
    context: dict | None = None,
    *,
    text: str | None = None,
    attachments: list[EmailAttachment] | None = None,
    sender: str | None = None,
) -> EmailResult:
    """Send a TEMPLATE email via the comms module (``kind="magic_link"``).

    The module renders ``template`` (a Jinja template that now lives
    module-side) with ``context`` and sends. Signature is unchanged from the
    pre-#32 ``services.email.send_email``.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailError("No recipients supplied")

    payload = {
        "kind": "magic_link",
        "to": recipients,
        "subject": subject,
        "body_html": None,          # module renders the template
        "body_text": text,
        "attachments": _attachments_wire(attachments),
        "meta": {
            "template": template,
            "context": context or {},
            "sender": sender,
        },
    }
    return await _send_via_comms(payload, recipients)


async def send_raw_email(
    to: str | list[str],
    subject: str,
    html: str,
    *,
    text: str | None = None,
    attachments: list[EmailAttachment] | None = None,
    sender: str | None = None,
    settings: Settings | None = None,   # accepted for signature parity; unused
    kind: str = "raw",
) -> EmailResult:
    """Send PRE-ASSEMBLED HTML via the comms module.

    Same signature as the old ``services.mailer.send_email`` (plus an optional
    ``kind`` so the billing receipt path can label itself
    ``"billing_receipt"``). ``settings`` is accepted and ignored — the comms
    URL comes from the global settings inside the client.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailError("No recipients supplied")

    payload = {
        "kind": kind,
        "to": recipients,
        "subject": subject,
        "body_html": html,
        "body_text": text,
        "attachments": _attachments_wire(attachments),
        "meta": {"sender": sender},
    }
    return await _send_via_comms(payload, recipients)
