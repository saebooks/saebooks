"""Step-2 pre-accounting conversions — two-phase, fact-first with idempotency.

These tests exercise the service-layer contract added in step 2 (gitea #32):
each of the three conversion hand-offs creates the ENGINE FACT first (under an
idempotency key), then flips the MODULE-owned state second, so a crash between
the two phases is recoverable without double-creating the fact.

Per conversion we assert:
  * double-invocation with the SAME key → ONE fact + completed module state;
  * mid-crash recovery — phase 1 committed, phase 2 not, retry converges;
  * key mismatch (a genuinely different batch) → a SECOND fact, legitimately.

The mid-crash is simulated faithfully by monkeypatching the phase-2 internal to
raise AFTER phase 1 has committed the fact — i.e. running the two halves
separately — then re-running the full conversion as the retry.

Postgres-only: the idempotency machinery uses ``INSERT … ON CONFLICT …
RETURNING (xmax = 0)`` and ``idempotency_records`` carries a FORCE-RLS tenant
policy, neither of which the SQLite Cashbook backend implements.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine
from saebooks.models.purchase_order import PurchaseOrderStatus
from saebooks.models.time_entry import TimeEntry
from saebooks.models.user import User, UserRole
from saebooks.services import purchase_orders as po_svc
from saebooks.services import quotes as quotes_svc
from saebooks.services import time_entries as te_svc

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures / helpers
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
async def deps() -> dict[str, str]:
    """Active-company-scoped ids for all three conversions.

    Scopes to the company ``get_active_company_id`` falls back to (oldest by
    ``created_at``) so every create shares one company and the cross-company
    FK guards do not trip. Provisions a customer, a supplier, and a user.
    """
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                )
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert company is not None, "Seed company missing"

        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                    Account.company_id == company.id,
                ).limit(1)
            )
        ).scalars().first()
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.is_header.is_(False),
                    Account.company_id == company.id,
                ).limit(1)
            )
        ).scalars().first()

        customer = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.company_id == company.id,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()
        supplier = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.company_id == company.id,
                    Contact.contact_type == ContactType.SUPPLIER,
                ).limit(1)
            )
        ).scalars().first()
        user = (
            await session.execute(
                select(User).where(
                    User.tenant_id == DEFAULT_TENANT_ID,
                    User.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()

        made = False
        if customer is None:
            customer = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Step2 Customer",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(customer)
            made = True
        if supplier is None:
            supplier = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Step2 Supplier",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(supplier)
            made = True
        if user is None:
            user = User(
                tenant_id=DEFAULT_TENANT_ID,
                username="step2-user",
                role=UserRole.ADMIN.value,
            )
            session.add(user)
            made = True
        if made:
            await session.commit()
            await session.refresh(customer)
            await session.refresh(supplier)
            await session.refresh(user)

    assert income is not None, "Test DB has no INCOME account"
    assert expense is not None, "Test DB has no EXPENSE account"
    return {
        "company_id": str(company.id),
        "income_account_id": str(income.id),
        "expense_account_id": str(expense.id),
        "customer_id": str(customer.id),
        "supplier_id": str(supplier.id),
        "user_id": str(user.id),
    }


@asynccontextmanager
async def _tenant_session(deps: dict[str, str]):
    """AsyncSession with the tenant/company GUCs bound (mirrors get_session).

    Required so RLS-forced tables (quotes, invoices, bills, idempotency_records)
    resolve, and so services that self-commit re-bind the GUC on each new
    transaction via the module-level ``after_begin`` listener.
    """
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(DEFAULT_TENANT_ID)
        session.info["company_id"] = deps["company_id"]
        yield session


# ---- source-document builders (via the public HTTP API) -------------------


def _quote_payload(deps: dict[str, str]) -> dict:
    return {
        "customer_id": deps["customer_id"],
        "issue_date": "2026-05-01",
        "expiry_date": "2026-05-29",
        "notes": "Step2 quote",
        "lines": [
            {
                "description": "Design",
                "quantity": "10",
                "unit_price": "150.00",
                "account_id": deps["income_account_id"],
            },
        ],
    }


async def _make_accepted_quote(
    api_client: AsyncClient, deps: dict[str, str]
) -> tuple[str, int]:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(deps))
    assert r.status_code == 201, r.text
    quote_id = r.json()["id"]
    v = r.json()["version"]
    r = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    assert r.status_code == 200, r.text
    v = r.json()["version"]
    r = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    assert r.status_code == 200, r.text
    return quote_id, r.json()["version"]


def _po_payload(deps: dict[str, str]) -> dict:
    return {
        "contact_id": deps["supplier_id"],
        "issue_date": "2026-04-01",
        "expected_date": "2026-04-15",
        "notes": "Step2 PO",
        "lines": [
            {
                "description": "Steel plate",
                "account_id": deps["expense_account_id"],
                "quantity": "10",
                "unit_price": "50.00",
                "discount_pct": "0",
            },
            {
                "description": "Bolts",
                "account_id": deps["expense_account_id"],
                "quantity": "100",
                "unit_price": "1.50",
                "discount_pct": "0",
            },
        ],
    }


async def _make_open_po(
    api_client: AsyncClient, deps: dict[str, str]
) -> tuple[str, int, str]:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(deps))
    assert r.status_code == 201, r.text
    po_id = r.json()["id"]
    v = r.json()["version"]
    r = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send", headers={"If-Match": str(v)}
    )
    assert r.status_code == 200, r.text
    return po_id, r.json()["version"], r.json()["number"]


async def _bills_from_po(session, number: str) -> list[Bill]:
    return list(
        (
            await session.execute(
                select(Bill).where(
                    Bill.tenant_id == DEFAULT_TENANT_ID,
                    Bill.notes == f"From PO {number}",
                )
            )
        ).scalars().all()
    )


async def _make_billable_entries(
    deps: dict[str, str], *, count: int
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with _tenant_session(deps) as s:
        for i in range(count):
            e = await te_svc.create(
                s,
                company_id=uuid.UUID(deps["company_id"]),
                user_id=uuid.UUID(deps["user_id"]),
                work_date=date(2026, 5, 1 + i),
                hours=Decimal("2"),
                description=f"Work {i}",
                contact_id=uuid.UUID(deps["customer_id"]),
                billable=True,
                rate=Decimal("100"),
                tenant_id=DEFAULT_TENANT_ID,
            )
            ids.append(e.id)
        await s.commit()
    return ids


async def _idempotency_identity(deps: dict[str, str], key: str) -> dict | None:
    async with _tenant_session(deps) as s:
        row = (
            await s.execute(
                text(
                    "SELECT response_body FROM idempotency_records "
                    "WHERE idempotency_key = :k"
                ),
                {"k": key},
            )
        ).first()
    if row is None or not row[0]:
        return None
    return json.loads(row[0])


# ---------------------------------------------------------------------------
# quote → invoice
# ---------------------------------------------------------------------------


async def test_quote_convert_crash_recovery_single_invoice(
    api_client: AsyncClient, deps: dict[str, str], monkeypatch
) -> None:
    """Phase 1 commits the invoice; a crash before the quote flip is
    recovered by re-running — the SAME invoice is returned (idempotency
    replay) and the quote flip completes. Exactly one invoice fact exists."""
    quote_id, version = await _make_accepted_quote(api_client, deps)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated crash between fact and module-state commit")

    monkeypatch.setattr(quotes_svc, "_flip_quote_invoiced", _boom)
    async with _tenant_session(deps) as s:
        with pytest.raises(RuntimeError):
            await quotes_svc.convert_to_invoice(
                s,
                uuid.UUID(quote_id),
                actor="test",
                expected_version=version,
                tenant_id=DEFAULT_TENANT_ID,
            )
    monkeypatch.undo()

    # Fact committed; quote NOT flipped and its version NOT bumped.
    async with _tenant_session(deps) as s:
        invs = (
            await s.execute(
                select(Invoice).where(
                    Invoice.source_quote_id == uuid.UUID(quote_id)
                )
            )
        ).scalars().all()
        assert len(invs) == 1
        first_invoice_id = invs[0].id
        q = await quotes_svc._get_with_lines(s, uuid.UUID(quote_id))
        assert q is not None
        assert q.status == quotes_svc.QuoteStatus.ACCEPTED
        assert q.version == version

    # Retry the full conversion → replay same invoice + complete the flip.
    async with _tenant_session(deps) as s:
        q2, inv2 = await quotes_svc.convert_to_invoice(
            s,
            uuid.UUID(quote_id),
            actor="test",
            expected_version=version,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert inv2.id == first_invoice_id
        assert q2.status == quotes_svc.QuoteStatus.INVOICED
        assert q2.invoice_id == first_invoice_id

    # Still exactly one invoice fact.
    async with _tenant_session(deps) as s:
        invs = (
            await s.execute(
                select(Invoice).where(
                    Invoice.source_quote_id == uuid.UUID(quote_id)
                )
            )
        ).scalars().all()
        assert len(invs) == 1


async def test_quote_convert_double_full_invocation_one_fact(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """A full second convert of an already-INVOICED quote is rejected by the
    ACCEPTED-status guard (the existing validation message is preserved) and
    no second invoice is minted."""
    quote_id, version = await _make_accepted_quote(api_client, deps)
    async with _tenant_session(deps) as s:
        q, _inv = await quotes_svc.convert_to_invoice(
            s,
            uuid.UUID(quote_id),
            actor="test",
            expected_version=version,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert q.status == quotes_svc.QuoteStatus.INVOICED
        new_version = q.version

    async with _tenant_session(deps) as s:
        with pytest.raises(quotes_svc.QuoteError) as ei:
            await quotes_svc.convert_to_invoice(
                s,
                uuid.UUID(quote_id),
                actor="test",
                expected_version=new_version,
                tenant_id=DEFAULT_TENANT_ID,
            )
        assert "convert-to-invoice requires ACCEPTED" in str(ei.value)

    async with _tenant_session(deps) as s:
        invs = (
            await s.execute(
                select(Invoice).where(
                    Invoice.source_quote_id == uuid.UUID(quote_id)
                )
            )
        ).scalars().all()
        assert len(invs) == 1


async def test_quote_convert_key_is_per_quote(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """The idempotency key is the quote id: distinct quotes mint distinct
    invoices, and replaying phase 1 for one quote returns its own invoice."""
    q1, v1 = await _make_accepted_quote(api_client, deps)
    q2, v2 = await _make_accepted_quote(api_client, deps)
    async with _tenant_session(deps) as s:
        _q, inv1 = await quotes_svc.convert_to_invoice(
            s, uuid.UUID(q1), actor="t", expected_version=v1,
            tenant_id=DEFAULT_TENANT_ID,
        )
    async with _tenant_session(deps) as s:
        _q, inv2 = await quotes_svc.convert_to_invoice(
            s, uuid.UUID(q2), actor="t", expected_version=v2,
            tenant_id=DEFAULT_TENANT_ID,
        )
    assert inv1.id != inv2.id

    # Replaying phase 1 for q1 returns the SAME invoice, not a new one.
    async with _tenant_session(deps) as s:
        quote = await quotes_svc._get_with_lines(s, uuid.UUID(q1))
        assert quote is not None
        inv1b = await quotes_svc._ensure_invoice_fact(s, quote, "t")
        assert inv1b.id == inv1.id


# ---------------------------------------------------------------------------
# PO → bill
# ---------------------------------------------------------------------------


async def test_po_convert_crash_recovery_single_bill(
    api_client: AsyncClient, deps: dict[str, str], monkeypatch
) -> None:
    """Phase 1 commits the DRAFT bill; a crash before the received_qty advance
    is recovered by re-running — the SAME bill is returned and the advance
    completes. Exactly one bill exists for the receipt batch."""
    po_id, version, number = await _make_open_po(api_client, deps)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated crash after bill commit")

    monkeypatch.setattr(po_svc, "_apply_po_advance", _boom)
    async with _tenant_session(deps) as s:
        with pytest.raises(RuntimeError):
            await po_svc.convert_to_bill(
                s,
                uuid.UUID(po_id),
                actor="test",
                expected_version=version,
                tenant_id=DEFAULT_TENANT_ID,
            )
    monkeypatch.undo()

    # Bill committed; PO untouched (still OPEN, received 0, version unchanged).
    async with _tenant_session(deps) as s:
        bills = await _bills_from_po(s, number)
        assert len(bills) == 1
        bill_id = bills[0].id
        po = await po_svc._get_with_lines(s, uuid.UUID(po_id))
        assert po is not None
        assert po.status == PurchaseOrderStatus.OPEN
        assert all(ln.received_qty == Decimal("0") for ln in po.lines)
        assert po.version == version

    # Retry the full conversion → replay same bill + advance received_qty.
    async with _tenant_session(deps) as s:
        po2, bill2 = await po_svc.convert_to_bill(
            s,
            uuid.UUID(po_id),
            actor="test",
            expected_version=version,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert bill2.id == bill_id
        assert po2.status == PurchaseOrderStatus.RECEIVED

    async with _tenant_session(deps) as s:
        assert len(await _bills_from_po(s, number)) == 1


async def test_po_convert_second_batch_new_bill(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """A genuinely later receipt batch — even one that bills the identical
    quantity — has a different key (the received_qty snapshot advanced) and
    legitimately creates a SECOND bill."""
    po_id, version, number = await _make_open_po(api_client, deps)

    async with _tenant_session(deps) as s:
        po1, bill_a = await po_svc.convert_to_bill(
            s,
            uuid.UUID(po_id),
            actor="test",
            expected_version=version,
            tenant_id=DEFAULT_TENANT_ID,
            quantities={1: Decimal("3")},
        )
        assert po1.status == PurchaseOrderStatus.PARTIAL
        v2 = po1.version
        bill_a_id = bill_a.id

    async with _tenant_session(deps) as s:
        _po2, bill_b = await po_svc.convert_to_bill(
            s,
            uuid.UUID(po_id),
            actor="test",
            expected_version=v2,
            tenant_id=DEFAULT_TENANT_ID,
            quantities={1: Decimal("3")},  # identical qty, different batch
        )
        assert bill_b.id != bill_a_id

    async with _tenant_session(deps) as s:
        assert len(await _bills_from_po(s, number)) == 2


async def test_po_convert_full_then_full_rejected_one_bill(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """Once fully received (RECEIVED), a second convert is rejected by the
    status guard — no second bill."""
    po_id, version, number = await _make_open_po(api_client, deps)
    async with _tenant_session(deps) as s:
        po1, _bill = await po_svc.convert_to_bill(
            s,
            uuid.UUID(po_id),
            actor="test",
            expected_version=version,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert po1.status == PurchaseOrderStatus.RECEIVED
        v2 = po1.version

    async with _tenant_session(deps) as s:
        with pytest.raises(po_svc.PurchaseOrderError) as ei:
            await po_svc.convert_to_bill(
                s,
                uuid.UUID(po_id),
                actor="test",
                expected_version=v2,
                tenant_id=DEFAULT_TENANT_ID,
            )
        assert "convert requires OPEN or PARTIAL" in str(ei.value)

    async with _tenant_session(deps) as s:
        assert len(await _bills_from_po(s, number)) == 1


# ---------------------------------------------------------------------------
# time-entries → invoice line
# ---------------------------------------------------------------------------


async def test_time_convert_crash_recovery_single_line(
    deps: dict[str, str], monkeypatch
) -> None:
    """Phase 1 commits the invoice line; a crash before the entry back-ref is
    recovered by re-running — the SAME invoice line is returned and the
    back-ref completes. No second invoice / line is created."""
    entry_ids = await _make_billable_entries(deps, count=2)
    key, _body = te_svc._time_convert_key(entry_ids)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated crash after invoice-line commit")

    monkeypatch.setattr(te_svc, "_backref_time_entries", _boom)
    async with _tenant_session(deps) as s:
        with pytest.raises(RuntimeError):
            await te_svc.convert_to_invoice_line(
                s,
                company_id=uuid.UUID(deps["company_id"]),
                entry_ids=entry_ids,
                tenant_id=DEFAULT_TENANT_ID,
            )
    monkeypatch.undo()

    # Line fact committed (identity recorded); entries NOT back-reffed yet.
    identity = await _idempotency_identity(deps, key)
    assert identity is not None
    crash_invoice_id = uuid.UUID(identity["invoice_id"])
    crash_line_id = uuid.UUID(identity["invoice_line_id"])
    async with _tenant_session(deps) as s:
        entries = (
            await s.execute(select(TimeEntry).where(TimeEntry.id.in_(entry_ids)))
        ).scalars().all()
        assert all(e.invoice_line_id is None for e in entries)

    # Retry → replay same invoice + line, back-ref completes.
    async with _tenant_session(deps) as s:
        result = await te_svc.convert_to_invoice_line(
            s,
            company_id=uuid.UUID(deps["company_id"]),
            entry_ids=entry_ids,
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert result.invoice_id == crash_invoice_id
        assert result.invoice_line_id == crash_line_id

    async with _tenant_session(deps) as s:
        entries = (
            await s.execute(select(TimeEntry).where(TimeEntry.id.in_(entry_ids)))
        ).scalars().all()
        assert all(e.invoice_line_id == crash_line_id for e in entries)
        # The minted invoice carries exactly one line — no double-create.
        line_count = (
            await s.execute(
                select(InvoiceLine).where(
                    InvoiceLine.invoice_id == crash_invoice_id
                )
            )
        ).scalars().all()
        assert len(line_count) == 1


async def test_time_convert_double_full_invocation_one_line(
    deps: dict[str, str]
) -> None:
    """A full second convert of the same entries is rejected by the
    already-converted guard (message preserved) — one line only."""
    entry_ids = await _make_billable_entries(deps, count=1)
    async with _tenant_session(deps) as s:
        r1 = await te_svc.convert_to_invoice_line(
            s,
            company_id=uuid.UUID(deps["company_id"]),
            entry_ids=entry_ids,
            tenant_id=DEFAULT_TENANT_ID,
        )
    async with _tenant_session(deps) as s:
        with pytest.raises(te_svc.TimeEntryError) as ei:
            await te_svc.convert_to_invoice_line(
                s,
                company_id=uuid.UUID(deps["company_id"]),
                entry_ids=entry_ids,
                tenant_id=DEFAULT_TENANT_ID,
            )
        assert ei.value.code == "already_converted"
    # The one entry points at exactly the first line.
    async with _tenant_session(deps) as s:
        e = (
            await s.execute(select(TimeEntry).where(TimeEntry.id == entry_ids[0]))
        ).scalars().first()
        assert e is not None
        assert e.invoice_line_id == r1.invoice_line_id


async def test_time_convert_different_entries_new_line(
    deps: dict[str, str]
) -> None:
    """A different set of entries hashes to a different key → a new invoice
    line, legitimately."""
    ids1 = await _make_billable_entries(deps, count=1)
    ids2 = await _make_billable_entries(deps, count=1)
    async with _tenant_session(deps) as s:
        r1 = await te_svc.convert_to_invoice_line(
            s,
            company_id=uuid.UUID(deps["company_id"]),
            entry_ids=ids1,
            tenant_id=DEFAULT_TENANT_ID,
        )
    async with _tenant_session(deps) as s:
        r2 = await te_svc.convert_to_invoice_line(
            s,
            company_id=uuid.UUID(deps["company_id"]),
            entry_ids=ids2,
            tenant_id=DEFAULT_TENANT_ID,
        )
    assert r1.invoice_line_id != r2.invoice_line_id


async def test_time_convert_append_to_existing_draft(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """Existing-DRAFT case routes through the step-1 line-append service path
    and returns the created line without clobbering the first line."""
    r = await api_client.post(
        "/api/v1/invoices",
        json={
            "contact_id": deps["customer_id"],
            "issue_date": "2026-04-01",
            "due_date": "2026-05-01",
            "lines": [
                {
                    "description": "Base line",
                    "account_id": deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": "500.00",
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    invoice_id = r.json()["id"]

    entry_ids = await _make_billable_entries(deps, count=1)
    async with _tenant_session(deps) as s:
        result = await te_svc.convert_to_invoice_line(
            s,
            company_id=uuid.UUID(deps["company_id"]),
            entry_ids=entry_ids,
            invoice_id=uuid.UUID(invoice_id),
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert result.invoice_id == uuid.UUID(invoice_id)

    g = (await api_client.get(f"/api/v1/invoices/{invoice_id}")).json()
    assert len(g["lines"]) == 2
    async with _tenant_session(deps) as s:
        e = (
            await s.execute(select(TimeEntry).where(TimeEntry.id == entry_ids[0]))
        ).scalars().first()
        assert e is not None
        assert e.invoice_line_id == result.invoice_line_id
