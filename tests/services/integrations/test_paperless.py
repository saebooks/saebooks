"""Unit tests for saebooks.services.integrations.paperless.

respx-mocks the Paperless REST API. attach_to_journal is exercised
against the real DB to confirm the JSONB bag is written.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services.integrations.paperless import (
    PaperlessAttachment,
    PaperlessClient,
    PaperlessDocumentNotFoundError,
    PaperlessError,
    PaperlessNotConfiguredError,
    attach_to_journal,
    build_browser_url,
)

API_BASE = "http://paperless-internal:8000"
BROWSER_BASE = "https://papers.example.com"


def _settings(**overrides: object) -> Settings:
    base = {
        "PAPERLESS_API_URL": API_BASE,
        "PAPERLESS_URL": BROWSER_BASE,
        "PAPERLESS_API_TOKEN": "token-xyz",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_browser_url_appends_details_suffix() -> None:
    assert (
        build_browser_url("https://x.com", 42)
        == "https://x.com/documents/42/details"
    )
    # trailing slash tolerant
    assert (
        build_browser_url("https://x.com/", 42)
        == "https://x.com/documents/42/details"
    )


async def test_client_raises_when_token_missing() -> None:
    with pytest.raises(PaperlessNotConfiguredError, match="TOKEN"):
        PaperlessClient(settings=_settings(PAPERLESS_API_TOKEN=""))


async def test_client_raises_when_api_url_missing() -> None:
    with pytest.raises(PaperlessNotConfiguredError, match="PAPERLESS_API_URL"):
        PaperlessClient(
            settings=_settings(PAPERLESS_API_URL="", PAPERLESS_URL="")
        )


@respx.mock
async def test_fetch_attachment_returns_normalised_shape() -> None:
    respx.get(f"{API_BASE}/api/documents/42/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Receipt — Jaycar",
                "mime_type": "application/pdf",
            },
        )
    )
    async with PaperlessClient(settings=_settings()) as pc:
        att = await pc.fetch_attachment(42)
    assert isinstance(att, PaperlessAttachment)
    assert att.document_id == 42
    assert att.title == "Receipt — Jaycar"
    assert att.browser_url == f"{BROWSER_BASE}/documents/42/details"
    assert att.mime_type == "application/pdf"


@respx.mock
async def test_fetch_attachment_404_raises_not_found() -> None:
    respx.get(f"{API_BASE}/api/documents/999/").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    async with PaperlessClient(settings=_settings()) as pc:
        with pytest.raises(PaperlessDocumentNotFoundError, match="999"):
            await pc.fetch_attachment(999)


@respx.mock
async def test_fetch_attachment_500_raises_generic_error() -> None:
    respx.get(f"{API_BASE}/api/documents/1/").mock(
        return_value=httpx.Response(500, text="fail")
    )
    async with PaperlessClient(settings=_settings()) as pc:
        with pytest.raises(PaperlessError, match="HTTP 500"):
            await pc.fetch_attachment(1)


@respx.mock
async def test_fetch_attachment_falls_back_to_synth_title() -> None:
    respx.get(f"{API_BASE}/api/documents/7/").mock(
        return_value=httpx.Response(
            200, json={"id": 7}  # no title field
        )
    )
    async with PaperlessClient(settings=_settings()) as pc:
        att = await pc.fetch_attachment(7)
    assert att.title == "Document 7"


@respx.mock
async def test_client_sends_token_header() -> None:
    route = respx.get(f"{API_BASE}/api/documents/1/").mock(
        return_value=httpx.Response(200, json={"id": 1, "title": "x"})
    )
    async with PaperlessClient(settings=_settings()) as pc:
        await pc.fetch_attachment(1)
    sent = route.calls.last.request.headers["authorization"]
    assert sent == "Token token-xyz"


# ----- attach_to_journal DB integration ----- #


async def _build_sample_journal() -> uuid.UUID:
    """Create a throwaway POSTED journal entry + return its id."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        acc = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().first()
        assert acc is not None

        from datetime import date
        from decimal import Decimal

        entry = JournalEntry(
            company_id=company.id,
            ref=f"TEST-{uuid.uuid4().hex[:8]}",
            entry_date=date.today(),
            description="paperless test",
            status=EntryStatus.DRAFT,
        )
        entry.lines.append(
            JournalLine(
                line_no=1,
                account_id=acc.id,
                debit=Decimal("0"),
                credit=Decimal("0"),
            )
        )
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry.id


async def _delete_journal(jid: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, jid)
        if entry is not None:
            await session.delete(entry)
            await session.commit()


async def test_attach_to_journal_writes_bag_and_is_idempotent() -> None:
    jid = await _build_sample_journal()
    try:
        attachment = PaperlessAttachment(
            document_id=42,
            title="Receipt — Jaycar",
            browser_url="https://papers.example.com/documents/42/details",
            mime_type="application/pdf",
        )
        async with AsyncSessionLocal() as session:
            entry = await attach_to_journal(session, jid, attachment)
            await session.commit()
            assert entry.attachments is not None
            bag = entry.attachments["paperless"]
            assert len(bag) == 1
            assert bag[0]["id"] == 42
            assert bag[0]["title"] == "Receipt — Jaycar"

        # Second attach with same doc_id — should be a no-op
        async with AsyncSessionLocal() as session:
            entry = await attach_to_journal(session, jid, attachment)
            await session.commit()
            assert len(entry.attachments["paperless"]) == 1
    finally:
        await _delete_journal(jid)


async def test_attach_appends_second_doc() -> None:
    jid = await _build_sample_journal()
    try:
        a1 = PaperlessAttachment(
            document_id=1, title="A",
            browser_url="https://papers.example.com/documents/1/details",
        )
        a2 = PaperlessAttachment(
            document_id=2, title="B",
            browser_url="https://papers.example.com/documents/2/details",
        )
        async with AsyncSessionLocal() as session:
            await attach_to_journal(session, jid, a1)
            entry = await attach_to_journal(session, jid, a2)
            await session.commit()
            ids = sorted(item["id"] for item in entry.attachments["paperless"])
            assert ids == [1, 2]
    finally:
        await _delete_journal(jid)


async def test_attach_raises_when_journal_missing() -> None:
    async with AsyncSessionLocal() as session:
        with pytest.raises(PaperlessError, match="not found"):
            await attach_to_journal(
                session,
                uuid.uuid4(),
                PaperlessAttachment(
                    document_id=99,
                    title="nope",
                    browser_url="https://x/documents/99/details",
                ),
            )
