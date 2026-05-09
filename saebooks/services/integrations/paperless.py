"""Paperless-ngx document-store integration.

Two responsibilities:

1. **Fetch** — query the local Paperless REST API (`/api/documents/{id}/`)
   for document metadata (title, tags, created, mime type). Used by the
   attachment picker to confirm a doc ID is valid before linking.
2. **Attach** — write a link into ``JournalEntry.attachments`` JSONB so
   users can jump from a journal entry to the underlying receipt/bill
   scan with one click.

The browser-facing URL is built from ``settings.paperless_url`` (the
URL the user sees in their browser, e.g. ``https://papers.sauer.com.au``)
— different from ``settings.paperless_api_url`` which may be an
internal hostname (``http://paperless:8000`` over the Docker network)
that only the server uses.

Authentication is Paperless's token auth: ``Authorization: Token <t>``.
Tokens are issued from the Paperless UI (Admin → API Tokens).

Design deliberately does NOT copy document bytes into saebooks — the
document stays in Paperless, we store only its ID + a cached title so
the UI can render a clickable chip without a round-trip. Deletion of
the Paperless doc doesn't break saebooks; the link just returns 404
when clicked.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.journal import JournalEntry

logger = logging.getLogger("saebooks.paperless")


class PaperlessError(RuntimeError):
    """Base class for Paperless-layer errors."""


class PaperlessNotConfiguredError(PaperlessError):
    """Raised when a Paperless call is attempted without API creds."""


class PaperlessDocumentNotFoundError(PaperlessError):
    """Raised when the requested document doesn't exist in Paperless."""


@dataclass(frozen=True)
class PaperlessAttachment:
    """Minimal metadata cached in JournalEntry.attachments for rendering."""

    document_id: int
    title: str
    browser_url: str
    mime_type: str | None = None


class PaperlessClient:
    """Thin async wrapper around the Paperless-ngx REST API.

    ``api_url`` is the server-facing URL (``http://paperless:8000`` on
    the Docker network, typically); ``browser_url`` is what the user
    will click through to in their browser.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.paperless_api_token:
            raise PaperlessNotConfiguredError(
                "PAPERLESS_API_TOKEN is not configured; cannot reach Paperless."
            )
        # api_url falls back to the browser url if the split wasn't set.
        api_base = (settings.paperless_api_url or settings.paperless_url).rstrip("/")
        if not api_base:
            raise PaperlessNotConfiguredError(
                "PAPERLESS_API_URL / PAPERLESS_URL not configured."
            )
        self._api_base = api_base
        self._browser_base = (
            settings.paperless_url or settings.paperless_api_url
        ).rstrip("/")
        self._token = settings.paperless_api_token
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owned = client is None

    async def __aenter__(self) -> PaperlessClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
        }

    async def get_document(self, document_id: int) -> dict[str, Any]:
        """Fetch raw Paperless document metadata."""
        url = f"{self._api_base}/api/documents/{document_id}/"
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code == 404:
            raise PaperlessDocumentNotFoundError(
                f"Paperless document {document_id} not found"
            )
        if resp.status_code != 200:
            raise PaperlessError(
                f"Paperless returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise PaperlessError(
                f"Paperless returned non-object: {type(data).__name__}"
            )
        return data

    async def fetch_attachment(self, document_id: int) -> PaperlessAttachment:
        """Fetch + normalise to :class:`PaperlessAttachment`."""
        data = await self.get_document(document_id)
        title = str(data.get("title") or f"Document {document_id}")
        mime = data.get("mime_type") or data.get("original_mime_type")
        return PaperlessAttachment(
            document_id=document_id,
            title=title,
            browser_url=self.browser_url(document_id),
            mime_type=str(mime) if mime else None,
        )

    def browser_url(self, document_id: int) -> str:
        """Build the user-facing detail URL for ``document_id``."""
        return build_browser_url(self._browser_base, document_id)


def build_browser_url(browser_base: str, document_id: int) -> str:
    """Pure: build the Paperless web-UI detail URL.

    Paperless-ngx uses ``/documents/<id>/details`` in its frontend
    router. Kept as a free function so tests can exercise it without
    constructing a real client + settings.
    """
    base = browser_base.rstrip("/")
    return f"{base}/documents/{document_id}/details"


async def attach_to_journal(
    session: AsyncSession,
    journal_id: uuid.UUID,
    attachment: PaperlessAttachment,
) -> JournalEntry:
    """Link ``attachment`` onto a journal entry's ``attachments`` JSONB.

    Stored as ``{"paperless": [{"id", "title", "url", "mime"}, ...]}``
    so the JSONB can also hold non-Paperless links later (email refs,
    local file uploads, etc.) without schema churn. Duplicate attaches
    are idempotent — same ``document_id`` won't be added twice.
    """
    entry = await session.get(JournalEntry, journal_id)
    if entry is None:
        raise PaperlessError(f"Journal entry {journal_id} not found")
    bag = dict(entry.attachments or {})
    items: list[dict[str, Any]] = list(bag.get("paperless", []))
    if any(int(item.get("id", -1)) == attachment.document_id for item in items):
        return entry
    items.append(
        {
            "id": attachment.document_id,
            "title": attachment.title,
            "url": attachment.browser_url,
            "mime": attachment.mime_type,
        }
    )
    bag["paperless"] = items
    entry.attachments = bag
    logger.info(
        "paperless: attached doc_id=%s to journal_id=%s",
        attachment.document_id,
        journal_id,
    )
    return entry


__all__ = [
    "PaperlessAttachment",
    "PaperlessClient",
    "PaperlessDocumentNotFoundError",
    "PaperlessError",
    "PaperlessNotConfiguredError",
    "attach_to_journal",
    "build_browser_url",
]
