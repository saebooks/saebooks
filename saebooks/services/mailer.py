"""Outbound email — SMTP with a filesystem-outbox dev fallback.

Design:
    - Configured via ``settings.smtp_host`` etc. Empty ``smtp_host``
      flips the mailer into outbox mode — every call writes an RFC
      5322 .eml into ``settings.mail_outbox_dir`` instead of hitting
      the wire. That dir is typically a bind-mount in local dev so
      developers can open the files in a MUA.
    - Attachments are ``(filename, bytes, mime_type)`` tuples — the
      invoice PDF from ``services/pdf.py`` drops straight in.
    - Raises ``EmailError`` on SMTP failure; outbox mode only raises
      on filesystem errors.

Minimal on purpose — no HTML-alternative/plaintext branching, no
Markdown rendering, no templating. Render templates elsewhere and
pass a fully-formed HTML body in.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

import aiosmtplib

if TYPE_CHECKING:
    from saebooks.config import Settings

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """Raised when an email cannot be delivered or stored."""


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True)
class EmailResult:
    """What the caller gets back — lets tests assert outbox vs SMTP path."""

    mode: str  # "smtp" | "outbox"
    message_id: str
    outbox_path: str | None = None
    recipients: tuple[str, ...] = field(default_factory=tuple)


def _build_message(
    *,
    sender: str,
    to: list[str],
    subject: str,
    html: str,
    text: str | None,
    attachments: list[EmailAttachment] | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(text or _html_to_text(html))
    msg.add_alternative(html, subtype="html")
    for att in attachments or []:
        maintype, _, subtype = att.mime_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            att.content,
            maintype=maintype,
            subtype=subtype,
            filename=att.filename,
        )
    return msg


def _html_to_text(html: str) -> str:
    """Crude HTML → plain-text fallback for the text/plain alternative.

    Not marketing-quality — strips tags, collapses whitespace. If a
    caller cares about text-part rendering they pass ``text=`` explicitly.
    """
    no_tags = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", no_tags)).strip()


def _slug(value: str) -> str:
    """Filesystem-safe slug for outbox filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "msg"


async def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    *,
    text: str | None = None,
    attachments: list[EmailAttachment] | None = None,
    sender: str | None = None,
    settings: Settings | None = None,
) -> EmailResult:
    """Send ``html`` to ``to``.

    When ``settings.smtp_host`` is empty the message is written to
    ``settings.mail_outbox_dir`` as an .eml file. Otherwise an SMTP
    session is opened against ``settings.smtp_host:settings.smtp_port``
    with STARTTLS when ``settings.smtp_tls`` is true.
    """
    from saebooks.config import settings as default_settings

    cfg = settings or default_settings
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailError("No recipients supplied")

    sender_addr = sender or cfg.smtp_from
    if not sender_addr:
        raise EmailError("No sender configured (settings.smtp_from is empty)")

    msg = _build_message(
        sender=sender_addr,
        to=recipients,
        subject=subject,
        html=html,
        text=text,
        attachments=attachments,
    )
    # message-id header — EmailMessage won't set it for us.
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    msg_id = f"<{ts}.{_slug(subject)}@saebooks>"
    msg["Message-ID"] = msg_id

    if not cfg.smtp_host:
        outbox_path = _write_outbox(cfg.mail_outbox_dir, msg, subject)
        logger.info("Email written to outbox at %s (to=%s)", outbox_path, recipients)
        return EmailResult(
            mode="outbox",
            message_id=msg_id,
            outbox_path=outbox_path,
            recipients=tuple(recipients),
        )

    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg.smtp_host,
            port=cfg.smtp_port,
            username=cfg.smtp_user or None,
            password=cfg.smtp_password or None,
            start_tls=cfg.smtp_tls,
        )
    except Exception as exc:
        raise EmailError(f"SMTP delivery failed: {exc}") from exc

    logger.info("Email sent via SMTP host=%s to=%s", cfg.smtp_host, recipients)
    return EmailResult(mode="smtp", message_id=msg_id, recipients=tuple(recipients))


def _write_outbox(dir_path: str, msg: EmailMessage, subject: str) -> str:
    outbox = Path(dir_path)
    try:
        outbox.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EmailError(
            f"Cannot create mail outbox at {dir_path!r}: {exc}"
        ) from exc

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    path = outbox / f"{ts}-{_slug(subject)}.eml"
    try:
        path.write_bytes(bytes(msg))
    except OSError as exc:
        raise EmailError(f"Cannot write outbox file {path}: {exc}") from exc
    return str(path)
