"""Tests for ``saebooks.services.mailer``.

Covers:

1. No SMTP host configured → .eml lands in the outbox dir, the file
   is a valid RFC 5322 message with html + text alternatives and an
   attached PDF.
2. SMTP host configured → ``aiosmtplib.send`` is invoked with the
   expected host/port/credentials and the recipient list.
3. Empty ``to`` raises EmailError.
4. Empty ``smtp_from`` raises EmailError.
5. Outbox write failure raises EmailError wrapping the OSError.
6. Custom ``sender`` overrides ``settings.smtp_from``.
7. ``text=`` kwarg overrides the crude HTML-stripped fallback.
"""
from __future__ import annotations

import email
import email.policy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from saebooks.config import Settings
from saebooks.services import mailer
from saebooks.services.mailer import EmailAttachment, EmailError, send_email


def _dev_settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings with SMTP empty so the outbox path is exercised by default."""
    kwargs: dict[str, object] = {
        "SMTP_HOST": "",
        "SMTP_FROM": "books@sauer.com.au",
        "SAEBOOKS_MAIL_OUTBOX_DIR": str(tmp_path),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


def _live_settings(**overrides: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        SMTP_HOST="mail.example.com",
        SMTP_PORT=587,
        SMTP_USER="user",
        SMTP_PASSWORD="pw",
        SMTP_FROM="books@sauer.com.au",
        SMTP_TLS=True,
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------- #
# Outbox mode (SMTP_HOST empty)                                          #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outbox_mode_writes_eml(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    result = await send_email(
        "acme@example.com",
        "Your invoice INV-000001",
        "<p>Hello <b>world</b></p>",
        settings=cfg,
    )
    assert result.mode == "outbox"
    assert result.outbox_path is not None
    assert result.recipients == ("acme@example.com",)

    eml_path = Path(result.outbox_path)
    assert eml_path.exists()
    assert eml_path.suffix == ".eml"

    msg = email.message_from_bytes(eml_path.read_bytes())
    assert msg["To"] == "acme@example.com"
    assert msg["From"] == "books@sauer.com.au"
    assert msg["Subject"] == "Your invoice INV-000001"
    # multipart/alternative — text + html
    assert msg.is_multipart()
    parts = {p.get_content_type() for p in msg.walk()}
    assert "text/html" in parts
    assert "text/plain" in parts


@pytest.mark.asyncio
async def test_outbox_attachment(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    pdf_bytes = b"%PDF-1.4 fake"
    result = await send_email(
        "acme@example.com",
        "Invoice",
        "<p>see attached</p>",
        attachments=[EmailAttachment("inv.pdf", pdf_bytes, "application/pdf")],
        settings=cfg,
    )
    assert result.outbox_path is not None
    msg = email.message_from_bytes(Path(result.outbox_path).read_bytes())
    payloads = [
        p.get_payload(decode=True)
        for p in msg.walk()
        if p.get_filename() == "inv.pdf"
    ]
    assert payloads == [pdf_bytes]


@pytest.mark.asyncio
async def test_outbox_list_of_recipients(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    result = await send_email(
        ["a@example.com", "b@example.com"],
        "Invoice",
        "<p>hi</p>",
        settings=cfg,
    )
    assert result.recipients == ("a@example.com", "b@example.com")
    msg = email.message_from_bytes(Path(result.outbox_path or "").read_bytes())
    assert msg["To"] == "a@example.com, b@example.com"


@pytest.mark.asyncio
async def test_outbox_write_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point outbox_dir at a path where mkdir will fail.
    bad_path = tmp_path / "file-not-dir"
    bad_path.write_text("I am a file")
    cfg = Settings(  # type: ignore[call-arg]
        SMTP_HOST="",
        SMTP_FROM="books@sauer.com.au",
        SAEBOOKS_MAIL_OUTBOX_DIR=str(bad_path),
    )
    with pytest.raises(EmailError, match="Cannot create mail outbox"):
        await send_email("a@example.com", "test", "<p>x</p>", settings=cfg)


# ---------------------------------------------------------------------- #
# SMTP mode                                                              #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_smtp_mode_calls_aiosmtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _live_settings()
    mock = AsyncMock()
    monkeypatch.setattr(mailer.aiosmtplib, "send", mock)

    result = await send_email(
        "acme@example.com",
        "Invoice",
        "<p>hi</p>",
        settings=cfg,
    )
    assert result.mode == "smtp"
    mock.assert_awaited_once()
    kwargs = mock.await_args.kwargs
    assert kwargs["hostname"] == "mail.example.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "user"
    assert kwargs["password"] == "pw"
    assert kwargs["start_tls"] is True


@pytest.mark.asyncio
async def test_smtp_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _live_settings()

    async def boom(*_args: object, **_kw: object) -> None:
        raise OSError("Connection refused")

    monkeypatch.setattr(mailer.aiosmtplib, "send", boom)
    with pytest.raises(EmailError, match="SMTP delivery failed"):
        await send_email("a@example.com", "test", "<p>x</p>", settings=cfg)


# ---------------------------------------------------------------------- #
# Guards                                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_recipient_raises(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    with pytest.raises(EmailError, match="No recipients"):
        await send_email([], "test", "<p>x</p>", settings=cfg)


@pytest.mark.asyncio
async def test_empty_sender_raises(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path, SMTP_FROM="")
    with pytest.raises(EmailError, match="No sender"):
        await send_email("a@example.com", "test", "<p>x</p>", settings=cfg)


@pytest.mark.asyncio
async def test_custom_sender_overrides_settings(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    result = await send_email(
        "a@example.com",
        "test",
        "<p>x</p>",
        sender="custom@sauer.com.au",
        settings=cfg,
    )
    msg = email.message_from_bytes(Path(result.outbox_path or "").read_bytes())
    assert msg["From"] == "custom@sauer.com.au"


@pytest.mark.asyncio
async def test_explicit_text_part_wins(tmp_path: Path) -> None:
    cfg = _dev_settings(tmp_path)
    result = await send_email(
        "a@example.com",
        "test",
        "<p>ignore me</p>",
        text="HELLO CUSTOM TEXT",
        settings=cfg,
    )
    msg = email.message_from_bytes(
        Path(result.outbox_path or "").read_bytes(), policy=email.policy.default
    )
    text_parts = [
        p.get_content()
        for p in msg.walk()
        if p.get_content_type() == "text/plain"
    ]
    assert any("HELLO CUSTOM TEXT" in t for t in text_parts)
