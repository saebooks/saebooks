"""Tests for the GL-drill filters + contact enrichment on
``GET /api/v1/accounts/{id}/ledger`` (M3 gap: no contact_id/description/
source_type filters + contact_name on the account ledger).

Journal rows are constructed directly via the ORM — mirrors
``test_accounts_gl_movement.py``'s ``_entry`` helper (a balanced 2-line
entry per fixture row: the target account under test + a contra account) —
rather than going through the full invoice/bill posting services, so each
row's ``source_type``/``source_id``/``description`` can be pinned exactly.
``source_id`` is still a real ``Invoice.id`` (with a real ``contact_id``) so
the ``contact_id`` filter's per-source subquery join has something genuine
to resolve. Test-session transactions auto-declare ``app.db_rebuild=on``
(``tests/conftest.py``), which bypasses the ``trg_je_engine_guard`` raw-INSERT
provenance checks that would otherwise reject an origin/source_type built by
hand.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine

pytestmark = pytest.mark.postgres_only


@pytest.fixture
async def api_client(seeded_company) -> AsyncClient:
    company_id, _tenant_id, _accounts = seeded_company
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    ) as ac:
        yield ac


async def _journal_entry(
    session,
    *,
    company_id,
    tenant_id,
    target_id,
    contra_id,
    ref: str,
    description: str,
    entry_date: date,
    source_type: str,
    source_id: uuid.UUID,
    amount: str,
) -> uuid.UUID:
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=entry_date,
        status=EntryStatus.POSTED,
        source_type=source_type,
        source_id=source_id,
    )
    session.add(entry)
    await session.flush()
    session.add_all([
        JournalLine(
            entry_id=entry.id, company_id=company_id, line_no=1,
            account_id=target_id, description=description,
            debit=Decimal(amount), credit=Decimal("0"),
        ),
        JournalLine(
            entry_id=entry.id, company_id=company_id, line_no=2,
            account_id=contra_id,
            debit=Decimal("0"), credit=Decimal(amount),
        ),
    ])
    await session.flush()
    return entry.id


@pytest.fixture
async def ledger_fixture(seeded_company):
    company_id, tenant_id, accounts = seeded_company
    target_id, contra_id = accounts
    tag = uuid.uuid4().hex[:8]

    async with AsyncSessionLocal() as session:
        contact_x = Contact(
            tenant_id=tenant_id, company_id=company_id,
            name=f"LedgerX-{tag}", contact_type=ContactType.CUSTOMER,
        )
        contact_y = Contact(
            tenant_id=tenant_id, company_id=company_id,
            name=f"LedgerY-{tag}", contact_type=ContactType.CUSTOMER,
        )
        session.add_all([contact_x, contact_y])
        await session.flush()

        inv_x = Invoice(
            company_id=company_id, tenant_id=tenant_id, contact_id=contact_x.id,
            issue_date=date(2026, 1, 10), due_date=date(2026, 2, 9),
            status=InvoiceStatus.POSTED, total=Decimal("500.00"),
        )
        inv_y = Invoice(
            company_id=company_id, tenant_id=tenant_id, contact_id=contact_y.id,
            issue_date=date(2026, 1, 15), due_date=date(2026, 2, 14),
            status=InvoiceStatus.POSTED, total=Decimal("300.00"),
        )
        session.add_all([inv_x, inv_y])
        await session.flush()

        desc_x = f"ledger-test-{tag}-invoice-x"
        desc_y = f"ledger-test-{tag}-invoice-y"
        desc_transfer = f"ledger-test-{tag}-transfer"
        # No literal '%'/'_' in this description — used to prove the
        # description filter escapes ILIKE wildcards rather than passing
        # the search term straight through.
        desc_escape = f"escapeprobe{tag}abc"

        await _journal_entry(
            session, company_id=company_id, tenant_id=tenant_id,
            target_id=target_id, contra_id=contra_id,
            ref=f"LDG-{tag}-1", description=desc_x, entry_date=date(2026, 1, 10),
            source_type="invoice", source_id=inv_x.id, amount="500.00",
        )
        await _journal_entry(
            session, company_id=company_id, tenant_id=tenant_id,
            target_id=target_id, contra_id=contra_id,
            ref=f"LDG-{tag}-2", description=desc_y, entry_date=date(2026, 1, 15),
            source_type="invoice", source_id=inv_y.id, amount="300.00",
        )
        # No-contact-linkage source_type on the same account — narrows the
        # source_type filter tests and stays out of the contact_id matches.
        await _journal_entry(
            session, company_id=company_id, tenant_id=tenant_id,
            target_id=target_id, contra_id=contra_id,
            ref=f"LDG-{tag}-3", description=desc_transfer, entry_date=date(2026, 1, 20),
            source_type="transfer", source_id=uuid.uuid4(), amount="10.00",
        )
        await _journal_entry(
            session, company_id=company_id, tenant_id=tenant_id,
            target_id=target_id, contra_id=contra_id,
            ref=f"LDG-{tag}-4", description=desc_escape, entry_date=date(2026, 1, 21),
            source_type="transfer", source_id=uuid.uuid4(), amount="1.00",
        )
        await session.commit()

    yield {
        "account_id": str(target_id),
        "company_id": str(company_id),
        "contact_x_id": str(contact_x.id),
        "contact_x_name": contact_x.name,
        "contact_y_id": str(contact_y.id),
        "invoice_x_id": str(inv_x.id),
        "desc_x": desc_x,
        "desc_y": desc_y,
        "desc_transfer": desc_transfer,
        "desc_escape": desc_escape,
        "tag": tag,
    }

    async with AsyncSessionLocal() as session:
        await session.execute(delete(JournalEntry).where(JournalEntry.company_id == company_id))
        await session.execute(delete(Invoice).where(Invoice.company_id == company_id))
        await session.execute(delete(Contact).where(Contact.company_id == company_id))
        await session.commit()


def _ledger_url(fixture: dict) -> str:
    return f"/api/v1/accounts/{fixture['account_id']}/ledger"


# ---------------------------------------------------------------------------
# Individual filters narrow correctly
# ---------------------------------------------------------------------------


async def test_ledger_filter_by_contact_id(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture), params={"contact_id": ledger_fixture["contact_x_id"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [i["description"] for i in body["items"]] == [ledger_fixture["desc_x"]]


async def test_ledger_filter_contact_id_cross_company_isolation(
    api_client: AsyncClient, ledger_fixture: dict, seeded_company,
) -> None:
    """A real ``contact_id`` belonging to a DIFFERENT company must yield
    zero rows on this company's ledger, not this company's foreign rows
    and not an error.

    The ``contact_id`` filter's per-source subquery is scoped to the
    requesting ``company_id`` (``accounts.py``'s
    ``_contact_linked_source_models`` usage inside ``get_account_ledger``),
    so a genuinely-real contact from a second company should simply never
    match — this proves that scoping rather than merely proving "an
    unknown UUID returns nothing" (which any lookup would satisfy)."""
    _company_id, tenant_id, _accounts = seeded_company
    foreign_company_id = uuid.uuid4()

    async with AsyncSessionLocal() as session:
        session.add(Company(
            id=foreign_company_id, tenant_id=tenant_id,
            name=f"Foreign Co {foreign_company_id.hex[:8]}",
            base_currency="AUD", fin_year_start_month=7, audit_mode="immutable",
        ))
        await session.flush()
        foreign_contact = Contact(
            tenant_id=tenant_id, company_id=foreign_company_id,
            name="Foreign Contact", contact_type=ContactType.CUSTOMER,
        )
        session.add(foreign_contact)
        await session.commit()
        foreign_contact_id = foreign_contact.id

    try:
        r = await api_client.get(
            _ledger_url(ledger_fixture), params={"contact_id": str(foreign_contact_id)}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 0
        assert body["items"] == []
    finally:
        async with AsyncSessionLocal() as session:
            contact_row = await session.get(Contact, foreign_contact_id)
            if contact_row is not None:
                await session.delete(contact_row)
                await session.flush()
            co = await session.get(Company, foreign_company_id)
            if co is not None:
                await session.delete(co)
            await session.commit()


async def test_ledger_filter_by_description(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture), params={"description": ledger_fixture["desc_y"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["description"] == ledger_fixture["desc_y"]


async def test_ledger_filter_by_description_escapes_wildcards(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    """A '%' typed into the search term must be treated as a literal
    character, not passed through as an unescaped ILIKE wildcard.

    ``desc_escape`` ("escapeprobe<tag>abc") has "abc" immediately following
    the tag — nothing between them. Searching for "escapeprobe<tag>%abc"
    (a literal '%' typed by the caller, with nothing actually between tag
    and "abc" in the real row) must NOT match once the '%' is escaped to a
    literal character. If the endpoint instead passed the term straight to
    ILIKE, the unescaped '%' matches the empty gap and false-positives.
    """
    tag = ledger_fixture["tag"]
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={"description": f"escapeprobe{tag}%abc"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0

    # Sanity: the real (unescaped) substring still matches, proving the
    # zero result above is the escaping, not a broken filter.
    r2 = await api_client.get(
        _ledger_url(ledger_fixture), params={"description": ledger_fixture["desc_escape"]}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["total"] == 1


async def test_ledger_filter_by_description_escapes_underscore_wildcard(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    """A '_' typed into the search term must be treated as a literal
    character, not passed through as an unescaped ILIKE single-char
    wildcard. ``desc_escape`` has 'a' where this probe substitutes '_' —
    if '_' were unescaped it matches any single character (including
    that 'a') and false-positives.
    """
    tag = ledger_fixture["tag"]
    probe = f"escapeprobe{tag}_bc"  # '_' stands in for the real 'a'
    r = await api_client.get(_ledger_url(ledger_fixture), params={"description": probe})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0

    r2 = await api_client.get(
        _ledger_url(ledger_fixture), params={"description": ledger_fixture["desc_escape"]}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["total"] == 1


async def test_ledger_filter_by_description_escapes_trailing_backslash(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    """A lone trailing backslash in the search term must not break the
    ILIKE call (invalid trailing escape sequence) and must not match a
    description with no literal backslash — ``_escape_ilike`` doubles it
    to a literal ``\\`` character, which ``desc_escape`` does not contain,
    so the probe returns zero results (a 500 here would mean the escaping
    left a dangling escape character for postgres to choke on).
    """
    tag = ledger_fixture["tag"]
    probe = f"escapeprobe{tag}abc\\"  # trailing lone backslash
    r = await api_client.get(_ledger_url(ledger_fixture), params={"description": probe})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


async def test_ledger_filter_by_source_type(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(_ledger_url(ledger_fixture), params={"source_type": "invoice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert {i["description"] for i in body["items"]} == {
        ledger_fixture["desc_x"], ledger_fixture["desc_y"],
    }
    assert all(i["source_type"] == "invoice" for i in body["items"])


async def test_ledger_filter_by_source_type_case_insensitive(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    """``source_type`` is normalised with ``.lower()`` before the
    frozenset membership check (matching the sibling case-insensitive
    filter pattern elsewhere in the API), so 'Invoice'/'INVOICE' match the
    same rows as 'invoice'."""
    r = await api_client.get(_ledger_url(ledger_fixture), params={"source_type": "Invoice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert all(i["source_type"] == "invoice" for i in body["items"])


async def test_ledger_filter_unknown_source_type_400(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(_ledger_url(ledger_fixture), params={"source_type": "not_a_real_type"})
    assert r.status_code == 400
    assert "not_a_real_type" in r.text


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------


async def test_ledger_filters_combined(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={
            "source_type": "invoice",
            "contact_id": ledger_fixture["contact_x_id"],
            "description": ledger_fixture["desc_x"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["description"] == ledger_fixture["desc_x"]

    # Combining source_type=invoice with contact Y's description narrows to
    # zero — proves the filters AND together, not OR.
    r2 = await api_client.get(
        _ledger_url(ledger_fixture),
        params={
            "source_type": "invoice",
            "contact_id": ledger_fixture["contact_x_id"],
            "description": ledger_fixture["desc_y"],
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["total"] == 0


# ---------------------------------------------------------------------------
# balance / opening_balance null convention
# ---------------------------------------------------------------------------


async def test_ledger_balance_present_when_unfiltered(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={"date_from": "2026-01-01", "sort": "date", "direction": "asc"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opening_balance"] == "0"
    assert body["items"], "expected rows"
    assert all(i["balance"] is not None for i in body["items"])


async def test_ledger_balance_null_when_filtered(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={
            "date_from": "2026-01-01",
            "sort": "date",
            "direction": "asc",
            "contact_id": ledger_fixture["contact_x_id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opening_balance"] is None
    assert body["items"], "expected rows"
    assert all(i["balance"] is None for i in body["items"])


# ---------------------------------------------------------------------------
# contact_name enrichment
# ---------------------------------------------------------------------------


async def test_ledger_row_contact_name_enrichment(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={"description": ledger_fixture["desc_x"], "include_contact_name": "true"},
    )
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["source_type"] == "invoice"
    assert item["source_id"] == ledger_fixture["invoice_x_id"]
    assert item["contact_name"] == ledger_fixture["contact_x_name"]


async def test_ledger_row_contact_name_null_for_unlinked_source(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={"description": ledger_fixture["desc_transfer"], "include_contact_name": "true"},
    )
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["source_type"] == "transfer"
    assert item["contact_name"] is None


async def test_ledger_default_omits_contact_name(
    api_client: AsyncClient, ledger_fixture: dict
) -> None:
    r = await api_client.get(
        _ledger_url(ledger_fixture), params={"description": ledger_fixture["desc_x"]}
    )
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert "contact_name" not in item


# ---------------------------------------------------------------------------
# Pagination respects filters (count query)
# ---------------------------------------------------------------------------


async def test_ledger_pagination_total_respects_filters(
    api_client: AsyncClient, ledger_fixture: dict, seeded_company
) -> None:
    _company_id, tenant_id, accounts = seeded_company
    _target_id, contra_id = accounts
    tag = ledger_fixture["tag"]
    batch_token = f"ledger-batch-{tag}"

    async with AsyncSessionLocal() as session:
        for n in range(3):
            await _journal_entry(
                session, company_id=uuid.UUID(ledger_fixture["company_id"]),
                tenant_id=tenant_id,
                target_id=uuid.UUID(ledger_fixture["account_id"]), contra_id=contra_id,
                ref=f"LDG-{tag}-batch-{n}", description=f"{batch_token}-{n}",
                entry_date=date(2026, 1, 25),
                source_type="transfer", source_id=uuid.uuid4(), amount="5.00",
            )
        await session.commit()

    r = await api_client.get(
        _ledger_url(ledger_fixture),
        params={"description": batch_token, "limit": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
