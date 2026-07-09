"""FakeMailSource-driven tests for the email-in poller walk (spec §4).

Drives ``services/inbox_mail.poll_mailbox`` end-to-end against the real
(migrated) Postgres — routing via the live SECURITY DEFINER enumerator,
real ``inbox_documents`` / ``inbox_email_messages`` rows — with the
vault mocked at the module boundary (the same pattern as the other
inbox tests). No mail server, no HTTP.

Coverage: token routing, unknown-token quarantine (never a bounce),
attachments-first-ledger-last, source_ref replay-after-crash, byte
duplicates → DUPLICATE rows, body-only mail, oversize/wrong-type
skipped_count vs silent inline skip, per-tenant daily quota, ledger
replay (already-processed message re-files without re-ingesting), and
one poisoned message not stopping the walk.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from saebooks.config import settings as _settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.inbox_document import InboxDocument, InboxDocumentStatus
from saebooks.models.inbox_email import InboxEmailAddress, InboxEmailMessage
from saebooks.models.tenant import Tenant
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import inbox_mail
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only

_DOMAIN = "in.saebooks.test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_domain", _DOMAIN)
    monkeypatch.setattr(_settings, "inbox_email_daily_quota", 200)


@pytest.fixture(autouse=True)
def _vault_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, list[Any]] = {"upload": []}

    async def fake_upload(tenant_id, *, file, filename, content_type, actor=None):
        fid = uuid.uuid4()
        calls["upload"].append({"filename": filename, "size": len(file)})
        return {"id": str(fid)}

    monkeypatch.setattr(vault_client, "upload", fake_upload)
    return calls


@pytest.fixture
async def tenant() -> dict[str, Any]:
    """A fresh tenant with one active ingestion address — full isolation
    from other tests sharing the DB."""
    async with AsyncSessionLocal() as session:
        suffix = uuid.uuid4().hex[:8]
        t = Tenant(
            id=uuid.uuid4(),
            name=f"MAILWALK-{suffix}",
            slug=f"mailwalk-{suffix}",
        )
        session.add(t)
        await session.flush()
        addr = await inbox_svc.create_email_address(session, t.id)
        await session.commit()
        return {"tenant_id": t.id, "token": addr.token}


class FakeMailSource:
    mailbox = "catchall@in.saebooks.test"

    def __init__(self, messages: list[inbox_mail.MailMessage]) -> None:
        self._messages = {m.handle: m for m in messages}
        self.moves: list[tuple[str, str]] = []
        self.fetch_errors: set[str] = set()

    async def list_messages(self) -> list[str]:
        return list(self._messages)

    async def fetch(self, handle: str) -> inbox_mail.MailMessage:
        if handle in self.fetch_errors:
            raise RuntimeError("simulated fetch failure")
        return self._messages[handle]

    async def move(self, handle: str, folder: str) -> None:
        self.moves.append((handle, folder))
        self._messages.pop(handle, None)

    async def close(self) -> None:
        pass


def _jpeg(n: int = 64) -> bytes:
    return b"\xff\xd8JPEG" + os.urandom(n)


def _msg(
    token: str,
    *,
    handle: str | None = None,
    attachments: list[inbox_mail.MailAttachment] | None = None,
    message_id: str | None = None,
    to_domain: str = _DOMAIN,
) -> inbox_mail.MailMessage:
    return inbox_mail.MailMessage(
        handle=handle or uuid.uuid4().hex[:8],
        message_id=message_id or f"<{uuid.uuid4().hex}@sender.example>",
        from_addr="supplier@sender.example",
        subject="Tax invoice",
        recipients=[f"{token}@{to_domain}"],
        received_at=datetime.now(UTC),
        attachments=attachments or [],
    )


def _att(
    data: bytes,
    *,
    mime: str = "image/jpeg",
    filename: str = "invoice.jpg",
    inline: bool = False,
) -> inbox_mail.MailAttachment:
    return inbox_mail.MailAttachment(
        filename=filename, mime=mime, data=data, inline=inline
    )


async def _docs_for(tenant_id: uuid.UUID) -> list[InboxDocument]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(InboxDocument)
                .where(InboxDocument.tenant_id == tenant_id)
                .order_by(InboxDocument.created_at)
            )
        ).scalars().all()
        return list(rows)


async def _ledger_for(tenant_id: uuid.UUID) -> list[InboxEmailMessage]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(InboxEmailMessage).where(
                    InboxEmailMessage.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        return list(rows)


async def _poll(source: FakeMailSource) -> inbox_mail.PollOutcome:
    return await inbox_mail.poll_mailbox(
        source, AsyncSessionLocal, settings=_settings
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_routed_message_ingests_attachments_and_writes_ledger(
    tenant: dict[str, Any], _vault_stubs: dict
) -> None:
    msg = _msg(tenant["token"], attachments=[_att(_jpeg()), _att(_jpeg())])
    source = FakeMailSource([msg])

    outcome = await _poll(source)
    assert outcome.processed == 1
    assert outcome.documents_created == 2
    assert source.moves == [(msg.handle, "Processed")]

    docs = await _docs_for(tenant["tenant_id"])
    assert len(docs) == 2
    for index, doc in enumerate(docs):
        assert str(doc.source) == "EMAIL"
        assert str(doc.status) == "RECEIVED"  # extraction deferred to the sweep
        assert doc.source_ref == f"{msg.message_id}#{index}"
    ledger = await _ledger_for(tenant["tenant_id"])
    assert len(ledger) == 1
    assert ledger[0].document_count == 2
    assert ledger[0].skipped_count == 0
    assert ledger[0].mailbox == source.mailbox
    assert ledger[0].message_id == msg.message_id
    assert len(_vault_stubs["upload"]) == 2


async def test_unknown_token_quarantined_never_bounced(
    tenant: dict[str, Any],
) -> None:
    msg = _msg("nosuchtokenaaa2", attachments=[_att(_jpeg())])
    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.quarantined == 1
    assert outcome.processed == 0
    assert source.moves == [(msg.handle, "Quarantine")]
    assert await _docs_for(tenant["tenant_id"]) == []
    assert await _ledger_for(tenant["tenant_id"]) == []


async def test_wrong_domain_recipient_does_not_route(
    tenant: dict[str, Any],
) -> None:
    """A recipient with the right token at the WRONG domain must not
    route — the ingestion domain is part of the credential."""
    msg = _msg(tenant["token"], to_domain="evil.example")
    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.quarantined == 1
    assert await _ledger_for(tenant["tenant_id"]) == []


async def test_revoked_address_stops_routing(tenant: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as session:
        addr = (
            await session.execute(
                select(InboxEmailAddress).where(
                    InboxEmailAddress.tenant_id == tenant["tenant_id"]
                )
            )
        ).scalars().one()
        inbox_svc.revoke_email_address(addr)
        await session.commit()

    msg = _msg(tenant["token"], attachments=[_att(_jpeg())])
    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.quarantined == 1
    assert await _docs_for(tenant["tenant_id"]) == []


# ---------------------------------------------------------------------------
# Replay after crash + duplicates
# ---------------------------------------------------------------------------


async def test_replay_after_crash_skips_completed_attachment(
    tenant: dict[str, Any], _vault_stubs: dict
) -> None:
    """Crash simulation: attachment #0 was ingested but the run died
    before the ledger row / folder move. The replay must skip #0 via the
    source_ref unique, process #1, and record BOTH in the ledger."""
    first, second = _jpeg(), _jpeg()
    msg = _msg(tenant["token"], attachments=[_att(first), _att(second)])

    async with AsyncSessionLocal() as session:
        doc0, verdict = await inbox_svc.ingest_email_attachment(
            session,
            tenant["tenant_id"],
            data=first,
            filename="invoice.jpg",
            mime="image/jpeg",
            source_ref=f"{msg.message_id}#0",
        )
        assert verdict == "INGESTED"
        await session.commit()
        crashed_doc_id = doc0.id
    uploads_before = len(_vault_stubs["upload"])

    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.processed == 1
    assert outcome.replays == 1
    assert outcome.documents_created == 1  # only attachment #1

    docs = await _docs_for(tenant["tenant_id"])
    assert len(docs) == 2  # no third row — no silent loss, no double-store
    assert docs[0].id == crashed_doc_id
    assert len(_vault_stubs["upload"]) == uploads_before + 1
    ledger = await _ledger_for(tenant["tenant_id"])
    assert ledger[0].document_count == 2


async def test_byte_duplicate_lands_as_duplicate_row(
    tenant: dict[str, Any], _vault_stubs: dict
) -> None:
    """The same bytes emailed twice: the second attachment becomes a
    DUPLICATE row pointing at the original (duplicate_of_id + the
    original's blob — no second vault upload) so the inbox shows
    'already sent'."""
    data = _jpeg()
    msg1 = _msg(tenant["token"], attachments=[_att(data)])
    msg2 = _msg(tenant["token"], attachments=[_att(data)])

    source = FakeMailSource([msg1])
    await _poll(source)
    uploads_after_first = len(_vault_stubs["upload"])

    outcome = await _poll(FakeMailSource([msg2]))
    assert outcome.duplicates == 1
    assert outcome.documents_created == 0
    assert len(_vault_stubs["upload"]) == uploads_after_first  # no new blob

    docs = await _docs_for(tenant["tenant_id"])
    assert len(docs) == 2
    original, dup = docs
    assert str(dup.status) == InboxDocumentStatus.DUPLICATE.value
    assert dup.duplicate_of_id == original.id
    assert dup.vault_file_id == original.vault_file_id
    assert dup.source_ref == f"{msg2.message_id}#0"


async def test_already_processed_message_refiles_without_reingest(
    tenant: dict[str, Any],
) -> None:
    """Ledger row exists (only the folder move failed last run): the
    message re-files to Processed with zero new document rows."""
    msg = _msg(tenant["token"], attachments=[_att(_jpeg())])
    source = FakeMailSource([msg])
    await _poll(source)
    docs_before = len(await _docs_for(tenant["tenant_id"]))

    # Same message shows up again (move failed / duplicate delivery).
    replay = FakeMailSource(
        [
            _msg(
                tenant["token"],
                message_id=msg.message_id,
                attachments=[_att(_jpeg())],
            )
        ]
    )
    outcome = await _poll(replay)
    assert outcome.processed == 1
    assert outcome.documents_created == 0
    assert len(await _docs_for(tenant["tenant_id"])) == docs_before
    assert replay.moves[0][1] == "Processed"


# ---------------------------------------------------------------------------
# Attachment qualification
# ---------------------------------------------------------------------------


async def test_body_only_mail_recorded_with_zero_documents(
    tenant: dict[str, Any],
) -> None:
    msg = _msg(tenant["token"])  # no attachments
    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.processed == 1
    ledger = await _ledger_for(tenant["tenant_id"])
    assert len(ledger) == 1
    assert ledger[0].document_count == 0
    assert ledger[0].skipped_count == 0
    assert source.moves == [(msg.handle, "Processed")]
    assert await _docs_for(tenant["tenant_id"]) == []


async def test_oversize_and_wrong_type_counted_inline_logo_silent(
    tenant: dict[str, Any],
) -> None:
    attachments = [
        _att(_jpeg()),  # qualifies
        _att(b"x" * (inbox_mail.MAX_ATTACHMENT_BYTES + 1)),  # oversize → counted
        _att(b"plain text", mime="text/plain", filename="terms.txt"),  # counted
        _att(b"tiny-logo", inline=True, filename="logo.png", mime="image/png"),
    ]
    msg = _msg(tenant["token"], attachments=attachments)
    outcome = await _poll(FakeMailSource([msg]))
    assert outcome.documents_created == 1
    assert outcome.attachments_skipped == 2  # inline logo is SILENT

    docs = await _docs_for(tenant["tenant_id"])
    assert len(docs) == 1
    assert docs[0].source_ref == f"{msg.message_id}#0"  # index stable
    ledger = await _ledger_for(tenant["tenant_id"])
    assert ledger[0].document_count == 1
    assert ledger[0].skipped_count == 2


async def test_large_inline_image_is_ingested(tenant: dict[str, Any]) -> None:
    """Inline but ≥20 KiB — a real scanned document pasted into the
    body, not a signature logo. Must ingest."""
    big_inline = _att(
        b"\xff\xd8" + os.urandom(inbox_mail.INLINE_SKIP_BYTES + 10), inline=True
    )
    msg = _msg(tenant["token"], attachments=[big_inline])
    outcome = await _poll(FakeMailSource([msg]))
    assert outcome.documents_created == 1


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


async def test_daily_quota_quarantines_excess(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "inbox_email_daily_quota", 1)
    msg1 = _msg(tenant["token"], attachments=[_att(_jpeg())], handle="m1")
    msg2 = _msg(tenant["token"], attachments=[_att(_jpeg())], handle="m2")

    source = FakeMailSource([msg1])
    await _poll(source)
    assert source.moves == [("m1", "Processed")]

    source2 = FakeMailSource([msg2])
    outcome = await _poll(source2)
    assert outcome.quarantined == 1
    assert source2.moves == [("m2", "Quarantine")]
    # No ledger row and no documents for the quarantined message.
    docs = await _docs_for(tenant["tenant_id"])
    assert len(docs) == 1
    assert len(await _ledger_for(tenant["tenant_id"])) == 1


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


async def test_poisoned_message_does_not_stop_the_walk(
    tenant: dict[str, Any],
) -> None:
    bad = _msg(tenant["token"], handle="bad")
    good = _msg(tenant["token"], attachments=[_att(_jpeg())], handle="good")
    source = FakeMailSource([bad, good])
    source.fetch_errors.add("bad")

    outcome = await _poll(source)
    assert outcome.failed == 1
    assert outcome.processed == 1
    # The poisoned message was NOT moved — it stays for the next run.
    assert ("bad", "Processed") not in source.moves
    assert ("bad", "Quarantine") not in source.moves
    assert ("good", "Processed") in source.moves


async def test_vault_failure_leaves_message_for_next_run(
    tenant: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blob durability failed mid-message → the message is neither
    Processed nor Quarantined; the next run retries it."""

    async def boom(*a: Any, **kw: Any) -> dict:
        raise vault_client.VaultUnavailable("simulated outage")

    monkeypatch.setattr(vault_client, "upload", boom)
    msg = _msg(tenant["token"], attachments=[_att(_jpeg())])
    source = FakeMailSource([msg])
    outcome = await _poll(source)
    assert outcome.failed == 1
    assert source.moves == []
    assert await _ledger_for(tenant["tenant_id"]) == []


# ---------------------------------------------------------------------------
# Config-driven adapter selection (names only, no secrets)
# ---------------------------------------------------------------------------


async def test_mail_source_unconfigured_raises_with_var_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_provider", "")
    with pytest.raises(inbox_mail.MailNotConfiguredError, match="PROVIDER"):
        inbox_mail.mail_source_from_settings(_settings)

    monkeypatch.setattr(_settings, "inbox_mail_provider", "imap")
    with pytest.raises(
        inbox_mail.MailNotConfiguredError, match="SAEBOOKS_INBOX_IMAP_HOST"
    ):
        inbox_mail.mail_source_from_settings(_settings)

    monkeypatch.setattr(_settings, "inbox_mail_provider", "graph")
    with pytest.raises(
        inbox_mail.MailNotConfiguredError, match="SAEBOOKS_INBOX_GRAPH_TENANT_ID"
    ):
        inbox_mail.mail_source_from_settings(_settings)


async def test_mail_source_builds_configured_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_provider", "imap")
    monkeypatch.setattr(_settings, "inbox_imap_host", "mail.example")
    monkeypatch.setattr(_settings, "inbox_imap_username", "catchall@in.test")
    monkeypatch.setattr(_settings, "inbox_imap_password", "test-only")
    src = inbox_mail.mail_source_from_settings(_settings)
    assert isinstance(src, inbox_mail.ImapMailSource)
    assert src.mailbox == "catchall@in.test"

    monkeypatch.setattr(_settings, "inbox_mail_provider", "graph")
    monkeypatch.setattr(_settings, "inbox_graph_tenant_id", "t")
    monkeypatch.setattr(_settings, "inbox_graph_client_id", "c")
    monkeypatch.setattr(_settings, "inbox_graph_client_secret", "s")
    monkeypatch.setattr(_settings, "inbox_graph_mailbox", "in@corp.example")
    src2 = inbox_mail.mail_source_from_settings(_settings)
    assert isinstance(src2, inbox_mail.GraphMailSource)
    assert src2.mailbox == "in@corp.example"
    await src2.close()


# ---------------------------------------------------------------------------
# RFC 822 parsing
# ---------------------------------------------------------------------------


def test_parse_rfc822_extracts_recipients_and_attachment() -> None:
    raw = (
        b"From: Supplier <billing@sender.example>\r\n"
        b"To: abc123def456ok@" + _DOMAIN.encode() + b"\r\n"
        b"Cc: someone@else.example\r\n"
        b"Subject: Invoice 42\r\n"
        b"Message-ID: <42@sender.example>\r\n"
        b"Date: Fri, 03 Jul 2026 10:00:00 +1000\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/plain\r\n\r\nsee attached\r\n"
        b"--B\r\n"
        b"Content-Type: application/pdf; name=inv.pdf\r\n"
        b"Content-Disposition: attachment; filename=inv.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"%PDF-1.4 fake")
        + b"\r\n--B--\r\n"
    )
    parsed = inbox_mail.parse_rfc822(raw, "u1")
    assert parsed.message_id == "<42@sender.example>"
    assert parsed.from_addr == "billing@sender.example"
    assert f"abc123def456ok@{_DOMAIN}" in parsed.recipients
    assert "someone@else.example" in parsed.recipients
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename == "inv.pdf"
    assert att.mime == "application/pdf"
    assert att.data == b"%PDF-1.4 fake"
    assert att.inline is False

    tokens = inbox_mail.candidate_tokens(parsed.recipients, _DOMAIN)
    assert tokens == ["abc123def456ok"]


def test_parse_rfc822_missing_message_id_gets_stable_fallback() -> None:
    raw = b"From: a@b.c\r\nTo: t@" + _DOMAIN.encode() + b"\r\n\r\nbody"
    p1 = inbox_mail.parse_rfc822(raw, "u1")
    p2 = inbox_mail.parse_rfc822(raw, "u2")
    assert p1.message_id.startswith("<no-message-id-")
    assert p1.message_id == p2.message_id  # stable replay key
