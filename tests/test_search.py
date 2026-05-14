"""Smoke tests for global search + keyboard-shortcut help page.

Creates one scratch ``Contact`` per test and verifies the union query
returns it. Also verifies:

* ``/search`` returns the full HTML page by default.
* ``/search`` returns the HTMX fragment (no ``<header>``) when the
  ``HX-Request`` header is set — that's how the Cmd-K palette lives.
* ``/help/shortcuts`` renders.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
pytestmark = pytest.mark.postgres_only


async def _first_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def _make_contact(name: str, *, archived: bool = False) -> uuid.UUID:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        contact = Contact(
            company_id=company_id,
            name=name,
            contact_type=ContactType.CUSTOMER,
        )
        if archived:
            from datetime import UTC, datetime
            contact.archived_at = datetime.now(UTC)
        session.add(contact)
        await session.commit()
        return contact.id


async def _cleanup_contact(contact_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        c = await session.get(Contact, contact_id)
        if c is not None:
            await session.delete(c)
            await session.commit()


@pytest.mark.asyncio
async def test_search_empty_query_renders_page(client: AsyncClient) -> None:
    r = await client.get("/search")
    assert r.status_code == 200
    # Full page includes the nav header.
    assert "<h1>Search</h1>" in r.text


@pytest.mark.asyncio
async def test_search_finds_contact_by_name(client: AsyncClient) -> None:
    unique = f"Zzz-Palette-Hit-{uuid.uuid4().hex[:8]}"
    contact_id = await _make_contact(unique)
    try:
        r = await client.get(f"/search?q={unique}")
        assert r.status_code == 200
        assert unique in r.text
        assert "search-hit-contact" in r.text
    finally:
        await _cleanup_contact(contact_id)


@pytest.mark.asyncio
async def test_search_excludes_archived_contacts(client: AsyncClient) -> None:
    unique = f"Zzz-Archived-{uuid.uuid4().hex[:8]}"
    contact_id = await _make_contact(unique, archived=True)
    try:
        r = await client.get(
            f"/search?q={unique}", headers={"HX-Request": "true"}
        )
        assert r.status_code == 200
        # Fragment: if the archived contact leaked into results, it
        # would appear inside a ``search-hit-contact`` list item.
        assert "search-hit-contact" not in r.text
        assert "No matches" in r.text
    finally:
        await _cleanup_contact(contact_id)


@pytest.mark.asyncio
async def test_search_case_insensitive(client: AsyncClient) -> None:
    unique = f"Zzz-Case-{uuid.uuid4().hex[:8]}"
    contact_id = await _make_contact(unique)
    try:
        # Query with different case than stored value.
        r = await client.get(f"/search?q={unique.upper()}")
        assert r.status_code == 200
        assert unique in r.text
    finally:
        await _cleanup_contact(contact_id)


@pytest.mark.asyncio
async def test_search_htmx_returns_fragment(client: AsyncClient) -> None:
    """HTMX request returns only the results fragment (no nav header)."""
    unique = f"Zzz-Fragment-{uuid.uuid4().hex[:8]}"
    contact_id = await _make_contact(unique)
    try:
        r = await client.get(
            f"/search?q={unique}",
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert unique in r.text
        # Fragment: no outer <header class="top">, no <h1>Search</h1>.
        assert "<header" not in r.text
        assert "<h1>Search</h1>" not in r.text
    finally:
        await _cleanup_contact(contact_id)


@pytest.mark.asyncio
async def test_search_htmx_empty_no_match(client: AsyncClient) -> None:
    r = await client.get(
        "/search?q=definitely-no-such-thing-exists-12345",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "No matches" in r.text


@pytest.mark.asyncio
async def test_search_finds_account_by_code(client: AsyncClient) -> None:
    # AU seed always has 1-1110 as the primary bank / cash at bank
    # account. Safe to search for without creating one.
    r = await client.get("/search?q=1-1110")
    assert r.status_code == 200
    assert "search-hit-account" in r.text
    assert "1-1110" in r.text


@pytest.mark.asyncio
async def test_help_shortcuts_renders(client: AsyncClient) -> None:
    r = await client.get("/help/shortcuts")
    assert r.status_code == 200
    assert "Keyboard shortcuts" in r.text
    # A few marker keys the page documents.
    assert "Ctrl" in r.text
    assert "Dashboard" in r.text
