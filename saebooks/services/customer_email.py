"""Customer-facing outbound email — thin facade over the comms module.

Presentation, transport and POLICY moved to the app comms module (#32)
--------------------------------------------------------------------
This module used to own the whole outbound-customer-email pipeline: the
two-key kill switch, the FROM allowlist, the draft-vs-send decision, the
Microsoft Graph Outlook-draft handoff, and the Resend network call. All of
that POLICY + TRANSPORT now lives in the app comms module (saebooks-web),
reached over HTTP — the same accountant/bookkeeper split as the #31 render
service. See ``saebooks.services.comms_client``.

What STAYS engine-side (audit facts)
------------------------------------
The engine is the system of record for *what was attempted*:

  * input-contract validation (``CustomerEmailError`` on a bad doc_type /
    empty recipient / empty subject / empty body) — unchanged, callers still
    catch it;
  * the per-tenant ``tenants.outbound_email_enabled`` DB flag is READ here (a
    fact) and passed to the module in ``meta`` — the module ANDs it with its
    own ``SAEBOOKS_EMAIL_SEND_ENABLED`` env key + FROM allowlist to make the
    decision. The engine does NOT decide;
  * the immutable audit row in ``email_send_log`` (attachment bytes + sha256 +
    the module's outcome) — still written here so history is answerable from
    the engine DB alone (see ``services.email_log`` / ``api.v1.email_log``);
  * a copy of the composed message as an ``.eml`` in
    ``settings.mail_outbox_dir`` — an engine-side audit artifact.

What MOVED to the module (policy + transport)
---------------------------------------------
  * the two-key kill switch env key ``SAEBOOKS_EMAIL_SEND_ENABLED`` and the
    draft-mode key ``SAEBOOKS_EMAIL_DRAFT_MODE``;
  * the per-tenant FROM allowlist;
  * the draft-vs-send decision, the Graph Outlook-draft creation, and the
    Resend send.

The facade NEVER re-implements the gate: it forwards the facts and maps the
module's ``{"outcome": sent|drafted|blocked, "provider_id", "detail"}`` back
onto the exact ``SendResult`` the callers (invoices / bills / quotes)
expected before, so their behaviour and the email_send_log rows are
identical. If the module is unreachable / times out / answers non-200 the
facade raises ``CommsServiceError`` (the pre-extraction code never raised on
transport failure — it returned mode='failed' — so this is a NEW failure
mode; the invoice/bill/quote endpoints catch it and return HTTP 502).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.services.comms_client import (
    CommsServiceError,
    encode_attachment,
    post_comms_send,
)

# Re-exported so callers can ``from saebooks.services.customer_email import
# CommsServiceError`` alongside CustomerEmailError (they catch both).
__all__ = [
    "CommsServiceError",
    "CustomerEmailAttachment",
    "CustomerEmailError",
    "SendResult",
    "send_customer_email",
]

logger = logging.getLogger(__name__)

# Valid document types — an input-contract check, kept engine-side so callers
# still get CustomerEmailError (HTTP 422) for a bad doc_type. The FROM
# allowlist is now the module's concern.
_VALID_DOC_TYPES = {"quote", "invoice", "bill", "credit_note", "remittance", "letterhead"}


@dataclass(frozen=True)
class CustomerEmailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/pdf"


@dataclass(frozen=True)
class SendResult:
    mode: str           # 'sent' | 'blocked' | 'failed' | 'queued' | 'drafted'
    log_id: uuid.UUID
    message_id: str | None = None
    outbox_path: str | None = None
    reason: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


class CustomerEmailError(RuntimeError):
    """Raised when the caller has given us something we can't even attempt."""


def _build_eml(
    *,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[CustomerEmailAttachment],
    message_id: str,
) -> bytes:
    """Construct a fully-formed RFC 5322 .eml for the audit outbox."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = f"<{message_id}@saebooks>"
    msg.set_content(body_text or _strip_html(body_html))
    msg.add_alternative(body_html, subtype="html")
    for att in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            att.content, maintype=maintype, subtype=subtype, filename=att.filename
        )
    return bytes(msg)


def _strip_html(html: str) -> str:
    """Crude HTML → text fallback; good enough for the alternative part."""
    import re
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _check_tenant_outbound_enabled(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Read the per-tenant DB flag (a FACT the module ANDs into its gate)."""
    from sqlalchemy import text
    result = await session.execute(
        text("SELECT outbound_email_enabled FROM tenants WHERE id = :tid"),
        {"tid": str(tenant_id)},
    )
    row = result.first()
    return bool(row and row[0])


async def _record_send_log(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    doc_type: str,
    doc_id: uuid.UUID,
    doc_version: int,
    sent_by_user_id: uuid.UUID | None,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachment_filenames: list[str],
    attachment_bytes: list[bytes],
    attachment_sha256: list[str],
    attachment_content_types: list[str],
    resend_message_id: str | None,
    resend_status: str,
    resend_error: str | None,
    kill_switch_reason: str | None,
) -> uuid.UUID:
    """Insert the audit row and return its id.

    Captures attachment bytes + sha256 + content_type as parallel arrays so
    "what exact PDF went out at this moment in time" is answerable from the
    audit row alone, with no external service dependency.
    """
    # Insert via the ORM (not raw SQL): the array columns bind Python lists,
    # which the raw text() path could not bind on SQLite ("type 'list' is not
    # supported") — the EmailSendLog model's ARRAY columns carry a
    # ``with_variant(JSON, "sqlite")`` so the list serialises as JSON on the
    # Community/SQLite backend and as a native array on Postgres. The tamper
    # triggers (migration 0125) block UPDATE/DELETE only, so INSERT is fine.
    from saebooks.models.email_send_log import EmailSendLog

    new_id = uuid.uuid4()
    session.add(
        EmailSendLog(
            id=new_id,
            tenant_id=tenant_id,
            doc_type=doc_type,
            doc_id=doc_id,
            doc_version=doc_version,
            sent_by_user_id=sent_by_user_id,
            from_addr=from_addr,
            to_addrs=to,
            cc_addrs=cc,
            bcc_addrs=bcc,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            attachment_filenames=attachment_filenames,
            attachment_bytes=attachment_bytes,
            attachment_sha256=attachment_sha256,
            attachment_content_types=attachment_content_types,
            resend_message_id=resend_message_id,
            resend_status=resend_status,
            resend_error=resend_error,
            kill_switch_reason=kill_switch_reason,
        )
    )
    await session.flush()
    return new_id


async def send_customer_email(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    doc_type: str,
    doc_id: uuid.UUID,
    doc_version: int,
    sent_by_user_id: uuid.UUID | None,
    from_addr: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[CustomerEmailAttachment] | None = None,
    sae_relay_entitled: bool = True,
) -> SendResult:
    """Send (or draft, or block) a customer-facing email via the comms module.

    Signature + return contract are unchanged from the pre-#32 engine
    implementation. The kill switch, draft mode and FROM allowlist now live in
    the module; this facade forwards the facts and records the outcome.

    ``sae_relay_entitled`` (Wave B / Richard's decision 7, CHARTER §12.1
    "SAE-hosted SMTP for invoice delivery" — Business+): the caller resolves
    this from the request's effective tier (``features.
    feature_enabled_for_request(FLAG_SMTP_RELAY, request)``) and passes it
    in. When ``False`` (below-Business tenant), this function does NOT call
    the comms module at all — it degrades to the exact same shape the
    module already uses for a disabled/absent relay (``mode="blocked"``,
    with a reason), rather than raising or 404ing the request. Defaults to
    ``True`` so callers that don't pass it (tests, any future non-tier-aware
    caller) keep today's behaviour; every real HTTP call site (invoices /
    bills / quotes ``/send-email``) passes it explicitly.

    Raises
    ------
    CustomerEmailError
        On a bad input (invalid doc_type / no recipient / empty subject or
        body) — same as before.
    CommsServiceError
        When the comms module is unreachable, times out, or answers non-200.
        (Legacy code returned mode='failed' on transport failure; the extract
        raises so the caller can surface a genuine 502.)
    """
    cc = cc or []
    bcc = bcc or []
    attachments = attachments or []

    if doc_type not in _VALID_DOC_TYPES:
        raise CustomerEmailError(f"invalid doc_type: {doc_type!r}")
    if not to:
        raise CustomerEmailError("at least one recipient is required")
    if not subject.strip():
        raise CustomerEmailError("subject required")
    if not body_html.strip():
        raise CustomerEmailError("body_html required")

    attachment_filenames = [att.filename for att in attachments]
    attachment_bytes_list = [att.content for att in attachments]
    attachment_sha256_list = [hashlib.sha256(att.content).hexdigest() for att in attachments]
    attachment_content_types = [att.content_type for att in attachments]
    message_id = uuid.uuid4().hex

    # Engine-side audit artifact: a copy of exactly what we handed the module.
    eml_bytes = _build_eml(
        from_addr=from_addr, to=to, cc=cc, bcc=bcc,
        subject=subject, body_html=body_html, body_text=body_text,
        attachments=attachments, message_id=message_id,
    )
    outbox_dir = Path(settings.mail_outbox_dir) / "customer_email"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    outbox_path = outbox_dir / f"{timestamp}__{message_id[:8]}.eml"
    outbox_path.write_bytes(eml_bytes)

    if not sae_relay_entitled:
        # Below-Business tenant: SAE-hosted relay is a Business+ feature
        # (CHARTER §12.1 "SAE-hosted SMTP for invoice delivery"). Degrade
        # exactly the way the module already degrades a disabled/absent
        # relay (outcome="blocked") instead of raising or 404ing — the
        # .eml audit copy above still exists so "what would have gone
        # out" is answerable, but we never phone the comms module for a
        # tenant whose licence doesn't include it.
        outcome, provider_id, detail = (
            "blocked",
            None,
            "SAE-hosted email delivery requires the Business tier or "
            "above. Download the document and send it from your own "
            "email client, or upgrade to Business.",
        )
    else:
        # Per-tenant outbound flag — a fact the module ANDs into its gate.
        tenant_outbound_enabled = await _check_tenant_outbound_enabled(session, tenant_id)

        # Hand the message + gating facts to the module. It owns the decision.
        payload = {
            "kind": "customer_doc",
            "to": to,
            "subject": subject,
            "body_html": body_html,
            "body_text": body_text,
            "attachments": [
                encode_attachment(att.filename, att.content, att.content_type)
                for att in attachments
            ],
            "meta": {
                "tenant_id": str(tenant_id),
                "doc_type": doc_type,
                "doc_id": str(doc_id),
                "doc_version": doc_version,
                "sent_by_user_id": str(sent_by_user_id) if sent_by_user_id else None,
                "from_addr": from_addr,
                "cc": cc,
                "bcc": bcc,
                "tenant_outbound_enabled": tenant_outbound_enabled,
            },
        }
        result = await post_comms_send(payload)  # raises CommsServiceError on transport failure

        outcome = result.outcome
        provider_id = result.provider_id
        detail = result.detail

    # Map the module's decision onto the legacy email_send_log columns.
    #   sent    -> status 'sent',    id in resend_message_id, no reason
    #   drafted -> status 'drafted', Graph draft id, reason in kill_switch_reason
    #   blocked -> status 'blocked', no id, reason in kill_switch_reason
    #   (any other, e.g. 'failed') -> status verbatim, detail in resend_error
    if outcome == "sent":
        resend_status, resend_error, kill_switch_reason = "sent", None, None
    elif outcome == "drafted":
        resend_status, resend_error, kill_switch_reason = "drafted", None, detail
    elif outcome == "blocked":
        resend_status, resend_error, kill_switch_reason = "blocked", None, detail
    else:
        resend_status, resend_error, kill_switch_reason = outcome, detail, detail
        logger.warning(
            "customer_email module returned non-standard outcome %r for doc %s/%s: %s",
            outcome, doc_type, doc_id, detail,
        )

    log_id = await _record_send_log(
        session,
        tenant_id=tenant_id, doc_type=doc_type, doc_id=doc_id, doc_version=doc_version,
        sent_by_user_id=sent_by_user_id,
        from_addr=from_addr, to=to, cc=cc, bcc=bcc,
        subject=subject, body_html=body_html, body_text=body_text,
        attachment_filenames=attachment_filenames,
        attachment_bytes=attachment_bytes_list,
        attachment_sha256=attachment_sha256_list,
        attachment_content_types=attachment_content_types,
        resend_message_id=provider_id,
        resend_status=resend_status,
        resend_error=resend_error,
        kill_switch_reason=kill_switch_reason,
    )

    return SendResult(
        mode=outcome,
        log_id=log_id,
        message_id=provider_id,
        outbox_path=str(outbox_path),
        reason=detail,
        errors=(detail,) if (outcome not in {"sent", "drafted", "blocked"} and detail) else (),
    )
