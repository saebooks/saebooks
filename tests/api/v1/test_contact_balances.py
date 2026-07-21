"""Tests for the ``include_balances`` opt-in enrichment on
``GET /api/v1/contacts`` and ``GET /api/v1/contacts/{id}`` (M3 gap: no
unpaid/overdue/total/last_txn fields on the contact object).

Fixture data is posted through the real invoice/bill services (POST
/invoices + /invoices/{id}/post, same for bills) against a throwaway
``seeded_company`` (``tests/conftest.py``) — a brand-new company per test
with no ``PeriodLock`` rows, so genuinely-backdated (overdue) invoices/bills
post without hitting the period-lock guard that a shared, long-lived seed
company might have accumulated.

Covers:
* include_balances=false (default) — 'balances' key absent
* include_balances=true — correct unpaid/overdue for a posted overdue
  invoice + posted bill on the same contact; last_transaction_date
* list route — balances correct for 2+ contacts sharing a page
* tenant isolation — a foreign tenant cannot pull balances via X-Company-Id
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

# ``seeded_company`` (tests/conftest.py) only seeds one ASSET + one EXPENSE
# account — no AR/AP control accounts. Invoice/bill posting resolves the
# control account via companies.ar_control_account_code /
# ap_control_account_code, falling back to the AU convention codes (see
# services/control_accounts.py) — seed those two here so POST .../post
# doesn't 422 with "AR control account is missing".
_AR_CONTROL_CODE = "1-1200"
_AP_CONTROL_CODE = "2-1200"


async def _ensure_control_accounts(company_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        for code, name, account_type in (
            (_AR_CONTROL_CODE, "Trade Debtors", AccountType.ASSET),
            (_AP_CONTROL_CODE, "Trade Creditors", AccountType.LIABILITY),
        ):
            existing = (
                await session.execute(
                    select(Account).where(Account.company_id == company_id, Account.code == code)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(Account(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=code,
                    name=name,
                    account_type=account_type,
                    reconcile=True,
                ))
        await session.commit()


@pytest.fixture
async def api_client(seeded_company) -> AsyncClient:
    """Bearer client pinned to the throwaway seeded company via X-Company-Id.

    Teardown explicitly deletes the Invoice/Bill rows this test posted
    (cascades to their lines) *before* ``seeded_company``'s own finalizer
    runs (fixture teardown is LIFO, so this always runs first): posted
    invoices/bills reference the seeded accounts via ``invoice_lines``/
    ``bill_lines.account_id`` (``ON DELETE RESTRICT``), and
    ``seeded_company``'s teardown only ever anticipated bare JournalEntry
    fixture rows, not real posted records — without this, the company's
    cascade delete trips the RESTRICT constraint.
    """
    from sqlalchemy import delete as _delete

    from saebooks.models.bill import Bill
    from saebooks.models.invoice import Invoice

    company_id, tenant_id, _accounts = seeded_company
    await _ensure_control_accounts(company_id, tenant_id)
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

    async with AsyncSessionLocal() as session:
        await session.execute(_delete(Invoice).where(Invoice.company_id == company_id))
        await session.execute(_delete(Bill).where(Bill.company_id == company_id))
        await session.commit()


async def _make_contact(company_id: uuid.UUID, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        contact = Contact(
            tenant_id=tenant_id,
            company_id=company_id,
            name=name,
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        return contact.id


async def _post_invoice(
    client: AsyncClient,
    *,
    contact_id: uuid.UUID,
    account_id: uuid.UUID,
    issue_date: date,
    due_date: date,
    amount: str,
) -> dict:
    r = await client.post(
        "/api/v1/invoices",
        json={
            "contact_id": str(contact_id),
            "issue_date": issue_date.isoformat(),
            "due_date": due_date.isoformat(),
            "lines": [
                {
                    "description": "Balances test line",
                    "account_id": str(account_id),
                    "quantity": "1",
                    "unit_price": amount,
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/invoices/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _post_bill(
    client: AsyncClient,
    *,
    contact_id: uuid.UUID,
    account_id: uuid.UUID,
    issue_date: date,
    due_date: date,
    amount: str,
) -> dict:
    r = await client.post(
        "/api/v1/bills",
        json={
            "contact_id": str(contact_id),
            "issue_date": issue_date.isoformat(),
            "due_date": due_date.isoformat(),
            "lines": [
                {
                    "description": "Balances test line",
                    "account_id": str(account_id),
                    "quantity": "1",
                    "unit_price": amount,
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/bills/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Default (include_balances=false) omits the key
# ---------------------------------------------------------------------------


async def test_get_contact_default_omits_balances(
    api_client: AsyncClient, seeded_company
) -> None:
    company_id, tenant_id, _accounts = seeded_company
    cid = await _make_contact(company_id, tenant_id, f"NoBalances-{uuid.uuid4().hex[:8]}")

    r = await api_client.get(f"/api/v1/contacts/{cid}")
    assert r.status_code == 200, r.text
    assert "balances" not in r.json()


async def test_list_contacts_default_omits_balances(
    api_client: AsyncClient, seeded_company
) -> None:
    company_id, tenant_id, _accounts = seeded_company
    await _make_contact(company_id, tenant_id, f"NoBalancesList-{uuid.uuid4().hex[:8]}")

    r = await api_client.get("/api/v1/contacts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"], "expected at least the freshly-created contact"
    for item in body["items"]:
        assert "balances" not in item


# ---------------------------------------------------------------------------
# include_balances=true — unpaid / overdue / last_transaction_date
# ---------------------------------------------------------------------------


async def test_get_contact_balances_true_computes_unpaid_and_overdue(
    api_client: AsyncClient, seeded_company
) -> None:
    company_id, tenant_id, accounts = seeded_company
    asset_id, expense_id = accounts
    cid = await _make_contact(company_id, tenant_id, f"BalOne-{uuid.uuid4().hex[:8]}")

    today = date.today()
    inv_issue = today - timedelta(days=40)
    inv_due = today - timedelta(days=10)  # overdue
    bill_issue = today - timedelta(days=20)  # later than the invoice
    bill_due = today - timedelta(days=5)  # overdue

    await _post_invoice(
        api_client, contact_id=cid, account_id=asset_id,
        issue_date=inv_issue, due_date=inv_due, amount="1000.00",
    )
    await _post_bill(
        api_client, contact_id=cid, account_id=expense_id,
        issue_date=bill_issue, due_date=bill_due, amount="800.00",
    )

    r = await api_client.get(f"/api/v1/contacts/{cid}", params={"include_balances": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    balances = body["balances"]
    assert balances is not None

    assert Decimal(balances["receivable_unpaid"]) == Decimal("1000.00")
    assert Decimal(balances["receivable_overdue"]) == Decimal("1000.00")
    assert Decimal(balances["payable_unpaid"]) == Decimal("800.00")
    assert Decimal(balances["payable_overdue"]) == Decimal("800.00")
    # Bill issue_date (20 days ago) is later than invoice issue_date (40
    # days ago), so it wins the last_transaction_date max().
    assert balances["last_transaction_date"] == bill_issue.isoformat()


async def test_get_contact_balances_true_not_yet_overdue_excluded(
    api_client: AsyncClient, seeded_company
) -> None:
    """An invoice due in the future counts toward unpaid but not overdue."""
    company_id, tenant_id, accounts = seeded_company
    asset_id, _expense_id = accounts
    cid = await _make_contact(company_id, tenant_id, f"BalCurrent-{uuid.uuid4().hex[:8]}")

    today = date.today()
    await _post_invoice(
        api_client, contact_id=cid, account_id=asset_id,
        issue_date=today, due_date=today + timedelta(days=30), amount="500.00",
    )

    r = await api_client.get(f"/api/v1/contacts/{cid}", params={"include_balances": "true"})
    assert r.status_code == 200, r.text
    balances = r.json()["balances"]
    assert Decimal(balances["receivable_unpaid"]) == Decimal("500.00")
    assert Decimal(balances["receivable_overdue"]) == Decimal("0")


# ---------------------------------------------------------------------------
# List route — grouped-query balances for 2+ contacts on one page
# ---------------------------------------------------------------------------


async def test_list_contacts_balances_for_multiple_contacts(
    api_client: AsyncClient, seeded_company
) -> None:
    company_id, tenant_id, accounts = seeded_company
    asset_id, expense_id = accounts
    today = date.today()

    cid_a = await _make_contact(company_id, tenant_id, f"BalListA-{uuid.uuid4().hex[:8]}")
    cid_b = await _make_contact(company_id, tenant_id, f"BalListB-{uuid.uuid4().hex[:8]}")

    await _post_invoice(
        api_client, contact_id=cid_a, account_id=asset_id,
        issue_date=today - timedelta(days=10), due_date=today - timedelta(days=1),
        amount="250.00",
    )
    await _post_bill(
        api_client, contact_id=cid_b, account_id=expense_id,
        issue_date=today - timedelta(days=8), due_date=today + timedelta(days=20),
        amount="150.00",
    )

    r = await api_client.get("/api/v1/contacts", params={"include_balances": "true", "limit": 500})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {item["id"]: item for item in body["items"]}

    assert str(cid_a) in by_id, "contact A missing from list response"
    assert str(cid_b) in by_id, "contact B missing from list response"
    row_a = by_id[str(cid_a)]
    row_b = by_id[str(cid_b)]

    assert Decimal(row_a["balances"]["receivable_unpaid"]) == Decimal("250.00")
    assert Decimal(row_a["balances"]["receivable_overdue"]) == Decimal("250.00")
    assert Decimal(row_a["balances"]["payable_unpaid"]) == Decimal("0")

    assert Decimal(row_b["balances"]["payable_unpaid"]) == Decimal("150.00")
    # Bill due in 20 days — not overdue yet.
    assert Decimal(row_b["balances"]["payable_overdue"]) == Decimal("0")
    assert Decimal(row_b["balances"]["receivable_unpaid"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_contact_balances_tenant_isolation(
    api_client: AsyncClient, seeded_company
) -> None:
    """A foreign tenant cannot reach this company's contact balances at all
    — X-Company-Id is validated against the resolved tenant before the
    handler (and its balances computation) ever runs."""
    company_id, tenant_id, accounts = seeded_company
    asset_id, _expense_id = accounts
    cid = await _make_contact(company_id, tenant_id, f"BalIso-{uuid.uuid4().hex[:8]}")

    today = date.today()
    await _post_invoice(
        api_client, contact_id=cid, account_id=asset_id,
        issue_date=today - timedelta(days=5), due_date=today - timedelta(days=1),
        amount="999.00",
    )

    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            f"/api/v1/contacts/{cid}", params={"include_balances": "true"}
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # X-Company-Id belongs to tenant A, not tenant B → 404 before the
    # balances computation runs at all.
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Correctness: credit note netting + archived-invoice exclusion
# ---------------------------------------------------------------------------


async def test_get_contact_balances_credit_note_netted_and_archived_excluded(
    api_client: AsyncClient, seeded_company
) -> None:
    """Hand-computed balance: a POSTED credit note against the invoice nets
    its outstanding balance down (``_refresh_invoice_amount_paid`` sums
    ``CreditNote.total - CreditNote.amount_allocated`` for POSTED notes
    against ``original_invoice_id`` and folds it into ``amount_paid`` — see
    services/payments.py), and a second invoice with ``archived_at`` forced
    set is excluded from the aggregate entirely despite being POSTED (the
    ``Invoice.archived_at.is_(None)`` filter in ``_compute_contact_balances``).

    Invoice: $1000, overdue. Credit note: $300 against it, unallocated ->
    amount_paid becomes min(1000, 0 + 300) = 300, so outstanding = 700.
    Second invoice: $250, would otherwise be overdue/unpaid, but archived ->
    must contribute $0.
    """
    company_id, tenant_id, accounts = seeded_company
    asset_id, _expense_id = accounts
    cid = await _make_contact(company_id, tenant_id, f"BalNet-{uuid.uuid4().hex[:8]}")

    today = date.today()
    issue = today - timedelta(days=30)
    due = today - timedelta(days=5)  # overdue

    inv = await _post_invoice(
        api_client, contact_id=cid, account_id=asset_id,
        issue_date=issue, due_date=due, amount="1000.00",
    )
    inv_id = inv["id"]

    r = await api_client.post(
        "/api/v1/credit_notes",
        json={
            "contact_id": str(cid),
            "issue_date": today.isoformat(),
            "original_invoice_id": inv_id,
            "lines": [
                {
                    "description": "Partial credit against overdue invoice",
                    "account_id": str(asset_id),
                    "quantity": "1",
                    "unit_price": "300.00",
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    cn_body = r.json()
    cn_id = uuid.UUID(cn_body["id"])
    r2 = await api_client.post(
        f"/api/v1/credit_notes/{cn_id}/post",
        headers={"If-Match": str(cn_body["version"])},
    )
    assert r2.status_code == 200, r2.text

    try:
        # Second invoice, would be overdue/unpaid if it counted at all.
        inv2 = await _post_invoice(
            api_client, contact_id=cid, account_id=asset_id,
            issue_date=issue, due_date=due, amount="250.00",
        )
        async with AsyncSessionLocal() as session:
            from saebooks.models.invoice import Invoice as InvoiceModel
            row = await session.get(InvoiceModel, uuid.UUID(inv2["id"]))
            row.archived_at = datetime.now(UTC)
            await session.commit()

        r3 = await api_client.get(f"/api/v1/contacts/{cid}", params={"include_balances": "true"})
        assert r3.status_code == 200, r3.text
        balances = r3.json()["balances"]
        assert Decimal(balances["receivable_unpaid"]) == Decimal("700.00")
        assert Decimal(balances["receivable_overdue"]) == Decimal("700.00")
    finally:
        # api_client's own teardown only anticipates Invoice/Bill rows (see
        # its docstring) — the credit note (and its lines, ON DELETE
        # CASCADE) must go first or the company/account cascade teardown
        # trips credit_note_lines.account_id's RESTRICT FK.
        from sqlalchemy import delete as _delete

        from saebooks.models.credit_note import CreditNote
        async with AsyncSessionLocal() as session:
            await session.execute(_delete(CreditNote).where(CreditNote.id == cn_id))
            await session.commit()


# ---------------------------------------------------------------------------
# Multi-currency: aggregation sums base-currency, not document-currency,
# columns (fix for the currency-unaware aggregation bug)
# ---------------------------------------------------------------------------


async def test_get_contact_balances_uses_base_currency_not_document_total(
    api_client: AsyncClient, seeded_company
) -> None:
    """A foreign-currency invoice must contribute its base-currency amount
    (``base_total - base_amount_paid``) to the balance, not its raw
    document-currency ``total - amount_paid`` — summing document-currency
    amounts across differing currencies for one contact would silently mix
    unlike units into a single number.

    The public API always creates invoices at ``fx_rate=1`` (no ``fx_rate``
    field on ``InvoiceCreate``/``InvoiceUpdate`` — see
    ``services/invoices.py:api_create``/``update_draft``), so a real
    ``fx_rate != 1`` row is applied directly at the DB layer here, using
    exactly the arithmetic ``services.invoices._recalc`` uses when it
    maintains the ``base_*`` shadow columns (``base_subtotal = subtotal *
    fx_rate``, etc.) — i.e. this reproduces what a genuinely foreign-currency
    posting looks like on disk, it doesn't fabricate an unreachable state.
    """
    company_id, tenant_id, accounts = seeded_company
    asset_id, _expense_id = accounts
    cid = await _make_contact(company_id, tenant_id, f"BalFx-{uuid.uuid4().hex[:8]}")

    today = date.today()
    inv = await _post_invoice(
        api_client, contact_id=cid, account_id=asset_id,
        issue_date=today - timedelta(days=10), due_date=today - timedelta(days=1),
        amount="1000.00",
    )
    inv_id = uuid.UUID(inv["id"])

    rate = Decimal("0.60")
    async with AsyncSessionLocal() as session:
        from saebooks.models.invoice import Invoice as InvoiceModel
        row = await session.get(InvoiceModel, inv_id)
        row.currency = "USD"
        row.fx_rate = rate
        row.base_subtotal = row.subtotal * rate
        row.base_tax_total = row.tax_total * rate
        row.base_total = row.base_subtotal + row.base_tax_total
        row.base_amount_paid = row.amount_paid * rate
        await session.commit()

    r = await api_client.get(f"/api/v1/contacts/{cid}", params={"include_balances": "true"})
    assert r.status_code == 200, r.text
    balances = r.json()["balances"]
    # Base-currency amount (1000.00 USD * 0.60 = 600.00 AUD) — NOT the raw
    # document total of 1000.00 that a document-currency sum would produce.
    assert Decimal(balances["receivable_unpaid"]) == Decimal("600.00")
    assert Decimal(balances["receivable_overdue"]) == Decimal("600.00")
