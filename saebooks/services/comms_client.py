"""Comms-module client — PUBLIC SHIM (SAE-hosted transport off; BYO to enable).

The private build POSTs assembled messages to a comms module that performs the
actual transport (SMTP / Resend / Graph draft) — SAE runs a managed comms
service for hosted customers. The open engine ships the facade + symbols but no
managed transport: ``post_comms_send`` is an in-process no-op that returns a
``drafted`` outcome (the message is assembled and audited but not transmitted).
A self-hoster enables real delivery by wiring their own comms module / SMTP —
see docs/operations. Email-dependent flows (magic-link, receipts) degrade to
"drafted" rather than failing.

Public symbols preserved exactly (``services.email`` + ``services.customer_email``
import them): ``CommsServiceError``, ``CommsResult``, ``encode_attachment``,
``post_comms_send``.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class CommsServiceError(RuntimeError):
    """Preserved for callers that catch it; not raised by the no-op transport."""


@dataclass(frozen=True)
class CommsResult:
    """The transport decision for one send attempt."""

    outcome: str            # "sent" | "drafted" | "blocked"
    provider_id: str | None
    detail: str | None


def encode_attachment(filename: str, content: bytes, content_type: str) -> dict[str, str]:
    """Serialise one attachment to the wire shape (unchanged)."""
    return {
        "filename": filename,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "content_type": content_type,
    }


async def post_comms_send(payload: dict) -> CommsResult:
    """In-process no-op transport for the open engine.

    Returns a ``drafted`` outcome without transmitting. Configure a comms module
    / SMTP transport to enable real delivery on a self-hosted install.
    """
    logger.info(
        "comms transport not configured (open engine) — message drafted, "
        "not sent (kind=%s)",
        payload.get("kind"),
    )
    return CommsResult(
        outcome="drafted",
        provider_id=None,
        detail="comms transport not configured in the open engine",
    )
