"""Customer-facing outbound email via Resend — with a two-key kill switch.

This module is DISTINCT from saebooks.services.email + saebooks.services.mailer,
which handle internal email (magic-link signups, verification). Those use SMTP
and can stay on outbox-only mode for development. This module handles
customer-facing outbound (quotes, invoices, bills, credit notes, remittances,
letterheads) — every send going to a real customer inbox is a real-money
event, so it sits behind a hard gate.

# Kill switch

A real network call to Resend ONLY happens when BOTH:

    settings.customer_email_send_enabled == True      (SAEBOOKS_EMAIL_SEND_ENABLED env)
    tenants.outbound_email_enabled == True            (per-tenant DB column)

Default for both is False. Flipping either one alone does nothing — they're
AND'd, not OR'd. This is deliberate: env-only gate would mean a misconfigured
container could leak; DB-only gate would mean a buggy migration could leak.
Two-key requires deliberate operator intent on both axes.

# Blocked-path behaviour

When the gate refuses a send, the call is NOT a no-op:

  1. The email + PDF attachment is written to settings.mail_outbox_dir
     as `<isoutc>__<msgid>.eml` so the operator can inspect what would have
     gone out
  2. A row is inserted into email_send_log with resend_status='blocked'
     and kill_switch_reason populated — full audit trail
  3. The caller gets a SendResult(mode='blocked', ...) — the UI can show a
     "Saved to outbox; ask Richard to flip the switch" banner

This is the same end-to-end pipeline that an unblocked send takes (minus
the Resend POST). It ensures the WHOLE pipeline gets exercised during the
build period — composer, validation, PDF render, attachment, audit log,
SMTP envelope construction — without anything leaving the box.

# Authorization checks (NOT yet enforced — placeholder for Phase 0c)

The `from_addr` SHOULD be checked against a per-tenant allowlist
(`tenant_email_addresses` table — not yet created). For Phase 0 we accept
admin@saee.com.au and accounts@saee.com.au for the Sauer Pty Ltd tenant by
hardcoded constant. Other tenants → blocked with kill_switch_reason="no
allowlisted FROM for tenant".
"""
from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings

logger = logging.getLogger(__name__)


# Per-tenant FROM allowlist — Phase 0 hardcode; later moves to
# tenant_email_addresses DB table. Anything not in here is BLOCKED.
_TENANT_FROM_ALLOWLIST: dict[str, set[str]] = {
    "f6c01a9d-0d41-426c-aa61-e9e60e8a7995": {  # Sauer Pty Ltd ATF Saueesti Trust
        "admin@saee.com.au",
        "accounts@saee.com.au",
    },
}

_VALID_DOC_TYPES = {"quote", "invoice", "bill", "credit_note", "remittance", "letterhead"}


@dataclass(frozen=True)
class CustomerEmailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/pdf"


@dataclass(frozen=True)
class SendResult:
    mode: str           # 'sent' | 'blocked' | 'failed' | 'queued'
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
    """Construct a fully-formed RFC 5322 .eml for outbox inspection."""
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
    """Read the per-tenant DB flag."""
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
    resend_message_id: str | None,
    resend_status: str,
    resend_error: str | None,
    kill_switch_reason: str | None,
) -> uuid.UUID:
    """Insert the audit row and return its id."""
    from sqlalchemy import text
    new_id = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO email_send_log (
                id, tenant_id, doc_type, doc_id, doc_version,
                sent_by_user_id, from_addr, to_addrs, cc_addrs, bcc_addrs,
                subject, body_html, body_text, attachment_filenames,
                resend_message_id, resend_status, resend_error, kill_switch_reason
            ) VALUES (
                :id, :tenant_id, :doc_type, :doc_id, :doc_version,
                :sent_by_user_id, :from_addr, :to_addrs, :cc_addrs, :bcc_addrs,
                :subject, :body_html, :body_text, :attachment_filenames,
                :resend_message_id, :resend_status, :resend_error, :kill_switch_reason
            )
        """),
        {
            "id": str(new_id),
            "tenant_id": str(tenant_id),
            "doc_type": doc_type,
            "doc_id": str(doc_id),
            "doc_version": doc_version,
            "sent_by_user_id": str(sent_by_user_id) if sent_by_user_id else None,
            "from_addr": from_addr,
            "to_addrs": to,
            "cc_addrs": cc,
            "bcc_addrs": bcc,
            "subject": subject,
            "body_html": body_html,
            "body_text": body_text,
            "attachment_filenames": attachment_filenames,
            "resend_message_id": resend_message_id,
            "resend_status": resend_status,
            "resend_error": resend_error,
            "kill_switch_reason": kill_switch_reason,
        },
    )
    return new_id


async def _post_to_resend(
    *,
    api_key: str,
    api_url: str,
    from_addr: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_html: str,
    body_text: str | None,
    attachments: list[CustomerEmailAttachment],
) -> tuple[str | None, str | None]:
    """Make the actual Resend network call. Returns (message_id, error)."""
    payload: dict[str, Any] = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": body_html,
    }
    if body_text:
        payload["text"] = body_text
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if attachments:
        payload["attachments"] = [
            {
                "filename": att.filename,
                "content": base64.b64encode(att.content).decode("ascii"),
            }
            for att in attachments
        ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{api_url}/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            return None, f"Resend network error: {exc!r}"

    if 200 <= resp.status_code < 300:
        return resp.json().get("id"), None
    return None, f"Resend {resp.status_code}: {resp.text[:500]}"


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
) -> SendResult:
    """Send a customer-facing email, or block it.

    The two-key kill switch is checked here BEFORE any network call. Every
    attempt (blocked or actual) is recorded in email_send_log.
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
    message_id = uuid.uuid4().hex
    eml_bytes = _build_eml(
        from_addr=from_addr, to=to, cc=cc, bcc=bcc,
        subject=subject, body_html=body_html, body_text=body_text,
        attachments=attachments, message_id=message_id,
    )

    # ── Gate 1: env-level flag ──
    env_enabled = settings.customer_email_send_enabled

    # ── Gate 2: per-tenant DB flag ──
    tenant_enabled = await _check_tenant_outbound_enabled(session, tenant_id)

    # ── Gate 3: per-tenant FROM allowlist ──
    allowlist = _TENANT_FROM_ALLOWLIST.get(str(tenant_id), set())
    from_in_allowlist = from_addr in allowlist

    # ── Block reasons ──
    block_reasons: list[str] = []
    if not env_enabled:
        block_reasons.append("env SAEBOOKS_EMAIL_SEND_ENABLED is not true")
    if not tenant_enabled:
        block_reasons.append(f"tenants.outbound_email_enabled is false for tenant {tenant_id}")
    if not from_in_allowlist:
        block_reasons.append(
            f"from_addr {from_addr!r} not in tenant allowlist {sorted(allowlist) or '[]'}"
        )

    # Outbox path is built either way — useful when blocked, and a fallback
    # log file when sent
    outbox_dir = Path(settings.mail_outbox_dir) / "customer_email"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    outbox_path = outbox_dir / f"{timestamp}__{message_id[:8]}.eml"
    outbox_path.write_bytes(eml_bytes)

    if block_reasons:
        reason = "; ".join(block_reasons)
        logger.warning(
            "customer_email BLOCKED for doc %s/%s tenant %s: %s",
            doc_type, doc_id, tenant_id, reason,
        )
        log_id = await _record_send_log(
            session,
            tenant_id=tenant_id, doc_type=doc_type, doc_id=doc_id, doc_version=doc_version,
            sent_by_user_id=sent_by_user_id,
            from_addr=from_addr, to=to, cc=cc, bcc=bcc,
            subject=subject, body_html=body_html, body_text=body_text,
            attachment_filenames=attachment_filenames,
            resend_message_id=None,
            resend_status="blocked",
            resend_error=None,
            kill_switch_reason=reason,
        )
        return SendResult(
            mode="blocked", log_id=log_id, outbox_path=str(outbox_path), reason=reason,
        )

    # ── Past all gates — make the actual Resend call ──
    if not settings.resend_api_key:
        log_id = await _record_send_log(
            session,
            tenant_id=tenant_id, doc_type=doc_type, doc_id=doc_id, doc_version=doc_version,
            sent_by_user_id=sent_by_user_id,
            from_addr=from_addr, to=to, cc=cc, bcc=bcc,
            subject=subject, body_html=body_html, body_text=body_text,
            attachment_filenames=attachment_filenames,
            resend_message_id=None,
            resend_status="blocked",
            resend_error=None,
            kill_switch_reason="RESEND_API_KEY is empty",
        )
        return SendResult(
            mode="blocked", log_id=log_id, outbox_path=str(outbox_path),
            reason="RESEND_API_KEY is empty",
        )

    resend_message_id, resend_error = await _post_to_resend(
        api_key=settings.resend_api_key,
        api_url=settings.resend_api_url,
        from_addr=from_addr, to=to, cc=cc, bcc=bcc,
        subject=subject, body_html=body_html, body_text=body_text,
        attachments=attachments,
    )

    status = "sent" if resend_message_id else "failed"
    log_id = await _record_send_log(
        session,
        tenant_id=tenant_id, doc_type=doc_type, doc_id=doc_id, doc_version=doc_version,
        sent_by_user_id=sent_by_user_id,
        from_addr=from_addr, to=to, cc=cc, bcc=bcc,
        subject=subject, body_html=body_html, body_text=body_text,
        attachment_filenames=attachment_filenames,
        resend_message_id=resend_message_id,
        resend_status=status,
        resend_error=resend_error,
        kill_switch_reason=None,
    )

    return SendResult(
        mode=status, log_id=log_id, message_id=resend_message_id,
        outbox_path=str(outbox_path), errors=(resend_error,) if resend_error else (),
    )
