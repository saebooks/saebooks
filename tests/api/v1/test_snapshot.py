"""Contract tests for ``GET /api/v1/snapshot``.

Covers:
* All 14 ``_entity`` markers are present in the response
* Cursor line is the last line
* Per-entity ``_count`` matches the actual row count for the active company
* ``X-Cursor-Next`` header matches the ``_cursor`` value in the body
* Empty-company path: all 14 markers with ``_count: 0`` + cursor (no entity rows)
* Auth gate: 401 without bearer token
"""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.bill import Bill
from saebooks.models.budget import Budget
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.invoice import Invoice
from saebooks.models.item import Item
from saebooks.models.journal import JournalEntry
from saebooks.models.payment import Payment
from saebooks.models.project import Project
from saebooks.models.tax_code import TaxCode

pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------------
# Expected entity order (must match snapshot.py dependency order)
# ---------------------------------------------------------------------------

_ENTITY_NAMES = [
    "companies",
    "tax_codes",
    "accounts",
    "contacts",
    "items",
    "projects",
    "invoices",
    "bills",
    "payments",
    "journal_entries",
    "bank_accounts",
    "bank_statement_lines",
    "fixed_assets",
    "budgets",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ndjson(text: str) -> list[dict]:
    """Return all non-empty NDJSON lines as dicts."""
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


async def _active_company_id() -> uuid.UUID | None:
    """Return the ID of the first active company, or None."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    return company.id if company else None


async def _entity_limit() -> int:
    """Return the effective SAEBOOKS_SNAPSHOT_ENTITY_LIMIT (default 10 000)."""
    import os
    raw = os.getenv("SAEBOOKS_SNAPSHOT_ENTITY_LIMIT", "")
    try:
        return int(raw) if raw.strip() else 10_000
    except ValueError:
        return 10_000


async def _expected_counts(company_id: uuid.UUID) -> dict[str, int]:
    """Query actual row counts that _generate() would return.

    Capped at the entity limit to match snapshot behaviour.
    """
    limit = await _entity_limit()

    async with AsyncSessionLocal() as session:

        def _count(model, *extra_where):
            return select(func.count()).select_from(model).where(*extra_where)

        companies = 1  # snapshot always emits exactly the one company row

        tax_codes = (
            await session.execute(
                _count(TaxCode, TaxCode.company_id == company_id)
            )
        ).scalar_one()

        accounts = (
            await session.execute(
                _count(Account, Account.company_id == company_id)
            )
        ).scalar_one()

        contacts = (
            await session.execute(
                _count(Contact, Contact.company_id == company_id)
            )
        ).scalar_one()

        items = (
            await session.execute(
                _count(Item, Item.company_id == company_id)
            )
        ).scalar_one()

        projects = (
            await session.execute(
                _count(Project, Project.company_id == company_id)
            )
        ).scalar_one()

        invoices = (
            await session.execute(
                _count(Invoice, Invoice.company_id == company_id)
            )
        ).scalar_one()

        bills = (
            await session.execute(
                _count(Bill, Bill.company_id == company_id)
            )
        ).scalar_one()

        payments = (
            await session.execute(
                _count(Payment, Payment.company_id == company_id)
            )
        ).scalar_one()

        journal_entries = (
            await session.execute(
                _count(JournalEntry, JournalEntry.company_id == company_id)
            )
        ).scalar_one()

        bank_accounts = (
            await session.execute(
                _count(
                    Account,
                    Account.company_id == company_id,
                    Account.bsb.isnot(None),
                )
            )
        ).scalar_one()

        bank_statement_lines = (
            await session.execute(
                _count(
                    BankStatementLine,
                    BankStatementLine.company_id == company_id,
                )
            )
        ).scalar_one()

        fixed_assets = (
            await session.execute(
                _count(FixedAsset, FixedAsset.company_id == company_id)
            )
        ).scalar_one()

        budgets = (
            await session.execute(
                _count(Budget, Budget.company_id == company_id)
            )
        ).scalar_one()

    def _cap(n: int) -> int:
        return min(n, limit)

    return {
        "companies": companies,  # always 1 — companies query is WHERE id=X
        "tax_codes": _cap(tax_codes),
        "accounts": _cap(accounts),
        "contacts": _cap(contacts),
        "items": _cap(items),
        "projects": _cap(projects),
        "invoices": _cap(invoices),
        "bills": _cap(bills),
        "payments": _cap(payments),
        "journal_entries": _cap(journal_entries),
        "bank_accounts": _cap(bank_accounts),
        "bank_statement_lines": _cap(bank_statement_lines),
        "fixed_assets": _cap(fixed_assets),
        "budgets": _cap(budgets),
    }


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_snapshot_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/snapshot")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Content type + basic shape
# ---------------------------------------------------------------------------


async def test_snapshot_content_type(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")


async def test_snapshot_cursor_header_present(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    assert "X-Cursor-Next" in r.headers
    assert int(r.headers["X-Cursor-Next"]) >= 0


# ---------------------------------------------------------------------------
# All 14 entity markers are present
# ---------------------------------------------------------------------------


async def test_snapshot_all_14_entity_markers(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    entity_markers = {ln["_entity"] for ln in lines if "_entity" in ln}
    for name in _ENTITY_NAMES:
        assert name in entity_markers, f"Missing _entity marker: {name!r}"


async def test_snapshot_entity_marker_order(api_client: AsyncClient) -> None:
    """Entity markers must appear in dependency order."""
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    markers = [ln["_entity"] for ln in lines if "_entity" in ln]
    assert markers == _ENTITY_NAMES


# ---------------------------------------------------------------------------
# Cursor is the last line
# ---------------------------------------------------------------------------


async def test_snapshot_cursor_is_last_line(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    last = lines[-1]
    assert "_cursor" in last, f"Last line is not a cursor: {last!r}"


async def test_snapshot_cursor_matches_header(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    cursor_value = lines[-1]["_cursor"]
    assert int(r.headers["X-Cursor-Next"]) == cursor_value


async def test_snapshot_no_entity_marker_after_cursor(api_client: AsyncClient) -> None:
    """No ``_entity`` or ``_count`` lines may appear after the cursor."""
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    cursor_idx = next(i for i, ln in enumerate(lines) if "_cursor" in ln)
    after = lines[cursor_idx + 1:]
    assert after == [], f"Lines after cursor: {after!r}"


# ---------------------------------------------------------------------------
# Per-entity counts match the live DB
# ---------------------------------------------------------------------------


async def test_snapshot_per_entity_counts_match_db(api_client: AsyncClient) -> None:
    """Each _count must equal the actual row count for the active company."""
    company_id = await _active_company_id()
    if company_id is None:
        pytest.skip("No active company in test DB — count-match test requires seeded data")

    expected = await _expected_counts(company_id)

    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)

    reported = {ln["_entity"]: ln["_count"] for ln in lines if "_entity" in ln}
    for name in _ENTITY_NAMES:
        assert name in reported, f"Missing marker for {name!r}"
        assert reported[name] == expected[name], (
            f"{name}: snapshot reported {reported[name]}, DB has {expected[name]}"
        )


async def test_snapshot_row_count_matches_entity_count(api_client: AsyncClient) -> None:
    """The number of data rows between two consecutive markers matches ``_count``."""
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)

    # Split lines into blocks: each block starts at a marker line.
    # Last line is the cursor — excluded from iteration.
    current_entity: str | None = None
    current_count: int = 0
    data_rows: int = 0

    for ln in lines:
        if "_cursor" in ln:
            # Flush the last entity block before finishing.
            if current_entity is not None:
                assert data_rows == current_count, (
                    f"{current_entity}: marker says {current_count}, "
                    f"found {data_rows} data rows"
                )
            break
        if "_entity" in ln:
            # Flush previous entity.
            if current_entity is not None:
                assert data_rows == current_count, (
                    f"{current_entity}: marker says {current_count}, "
                    f"found {data_rows} data rows"
                )
            current_entity = ln["_entity"]
            current_count = ln["_count"]
            data_rows = 0
        else:
            data_rows += 1


# ---------------------------------------------------------------------------
# Cursor is a valid change_log max id
# ---------------------------------------------------------------------------


async def test_snapshot_cursor_is_nonnegative_integer(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    cursor = lines[-1]["_cursor"]
    assert isinstance(cursor, int)
    assert cursor >= 0


async def test_snapshot_cursor_equals_change_log_max(api_client: AsyncClient) -> None:
    """Cursor must be the ``coalesce(max(id), 0)`` of change_log at read time."""
    async with AsyncSessionLocal() as session:
        max_id = (
            await session.execute(
                select(func.coalesce(func.max(ChangeLog.id), 0))
            )
        ).scalar_one()

    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)
    cursor = lines[-1]["_cursor"]
    # Cursor must be >= the max we read before the snapshot (writes may have
    # happened between our read and the snapshot's own read).
    assert cursor >= max_id


# ---------------------------------------------------------------------------
# Empty-company path
# ---------------------------------------------------------------------------


async def test_snapshot_empty_company_all_markers_count_zero(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no active company exists the snapshot emits all 14 markers with
    count 0 followed by a cursor — no entity data rows."""

    # Patch the snapshot route's Company query to return None (no company).
    import saebooks.api.v1.snapshot as _snap_mod


    async def _patched_snapshot() -> AsyncClient:
        # We monkeypatch by calling the real endpoint but mocking
        # the session's company lookup to return None.
        pass

    # Use monkeypatch to temporarily override the Company query result by
    # patching AsyncSessionLocal inside snapshot to return a session that
    # yields no company. The cleanest approach: patch sqlalchemy execute
    # on the session to return None for the company query.
    #
    # Instead, re-use the already-tested _empty() code path by patching
    # Company.archived_at.is_(None) filter to yield nothing — we do this
    # by overriding the `snapshot` handler to call _empty directly.

    # The actual empty-company async generator is tested by directly
    # invoking the endpoint with all companies archived. To avoid altering
    # the shared DB, we patch `AsyncSessionLocal` in the snapshot module so
    # the company query returns None.

    from unittest.mock import MagicMock


    class _MockSession:
        info: dict = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, stmt):
            # Return 0 for change_log max, None for company query.
            result = MagicMock()
            result.scalar_one.return_value = 0
            result.scalars.return_value.first.return_value = None
            return result

    monkeypatch.setattr(_snap_mod, "AsyncSessionLocal", lambda: _MockSession())

    r = await api_client.get("/api/v1/snapshot")
    assert r.status_code == 200
    lines = _parse_ndjson(r.text)

    # All 14 markers present with count 0.
    markers = {ln["_entity"]: ln["_count"] for ln in lines if "_entity" in ln}
    assert set(markers.keys()) == set(_ENTITY_NAMES)
    for name in _ENTITY_NAMES:
        assert markers[name] == 0, f"{name}: expected count 0, got {markers[name]}"

    # Cursor is the last line.
    assert "_cursor" in lines[-1]

    # No entity data rows — only 14 marker lines + 1 cursor line.
    assert len(lines) == len(_ENTITY_NAMES) + 1
