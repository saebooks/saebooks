"""Gap 3 — "flag for review" on transactions / invoices / expenses (0157).

Covers the ``flagged_for_review`` + ``review_note`` columns (migration 0157),
``services/review_flags.py``, and the ``POST /{id}/review-flag`` endpoints +
``?flagged=`` list filter on /api/v1/{journal_entries,invoices,expenses}.

Flagging is metadata only: it does NOT bump the entity version and does NOT post
a JE. Clearing the flag clears the note.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.expense import Expense
from saebooks.models.invoice import Invoice
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.services import review_flags as svc

pytestmark = pytest.mark.postgres_only


async def _seed() -> dict[str, Any]:
    """Create one invoice, one expense, one JE on the seed company; return ids."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "1-1110"
                )
            )
        ).scalar_one()
        contact = (
            await session.execute(
                select(Contact).where(Contact.company_id == company.id)
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id, tenant_id=company.tenant_id,
                name="RF Contact", contact_type=ContactType.CUSTOMER,
            )
            session.add(contact)
            await session.flush()

        inv = Invoice(
            company_id=company.id, tenant_id=company.tenant_id,
            contact_id=contact.id,
            issue_date=date(2026, 6, 6), due_date=date(2026, 7, 6),
        )
        exp = Expense(
            company_id=company.id, tenant_id=company.tenant_id,
            payment_account_id=bank.id, expense_date=date(2026, 6, 6),
        )
        ref = f"JE-RF-{uuid.uuid4().hex[:8]}"
        je = JournalEntry(
            company_id=company.id, tenant_id=company.tenant_id,
            ref=ref, entry_date=date(2026, 6, 6), status=EntryStatus.DRAFT,
        )
        session.add_all([inv, exp, je])
        await session.commit()
        return {
            "company_id": company.id,
            "tenant_id": company.tenant_id,
            "invoice_id": inv.id,
            "expense_id": exp.id,
            "je_id": je.id,
        }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "entity,key",
    [("invoice", "invoice_id"), ("expense", "expense_id"), ("journal_entry", "je_id")],
)
async def test_set_and_clear_flag(entity: str, key: str) -> None:
    d = await _seed()
    async with AsyncSessionLocal() as session:
        row = await svc.set_review_flag(
            session, entity, d[key],
            tenant_id=d["tenant_id"], company_id=d["company_id"],
            actor="test", flagged=True, review_note="Check this one",
        )
    assert row.flagged_for_review is True
    assert row.review_note == "Check this one"

    async with AsyncSessionLocal() as session:
        row = await svc.set_review_flag(
            session, entity, d[key],
            tenant_id=d["tenant_id"], company_id=d["company_id"],
            actor="test", flagged=False,
        )
    assert row.flagged_for_review is False
    assert row.review_note is None  # clearing the flag clears the note


async def test_flag_does_not_bump_version() -> None:
    d = await _seed()
    async with AsyncSessionLocal() as session:
        inv = await session.get(Invoice, d["invoice_id"])
        before = inv.version
    async with AsyncSessionLocal() as session:
        await svc.set_review_flag(
            session, "invoice", d["invoice_id"],
            tenant_id=d["tenant_id"], company_id=d["company_id"],
            actor="test", flagged=True, review_note="x",
        )
    async with AsyncSessionLocal() as session:
        inv = await session.get(Invoice, d["invoice_id"])
        assert inv.version == before, "flagging must not bump version"


async def test_unknown_entity_raises() -> None:
    d = await _seed()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReviewFlagError, match="Unknown"):
            await svc.set_review_flag(
                session, "nope", d["invoice_id"],
                tenant_id=d["tenant_id"], company_id=d["company_id"],
                actor="test", flagged=True,
            )


async def test_missing_row_raises() -> None:
    d = await _seed()
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.ReviewFlagError, match="not found"):
            await svc.set_review_flag(
                session, "invoice", uuid.uuid4(),
                tenant_id=d["tenant_id"], company_id=d["company_id"],
                actor="test", flagged=True,
            )


# --------------------------------------------------------------------------- #
# API — endpoints + ?flagged= filter
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def test_api_invoice_flag_and_filter(api_client: AsyncClient) -> None:
    d = await _seed()
    hdr = {"X-Company-Id": str(d["company_id"])}
    inv_id = str(d["invoice_id"])

    r = await api_client.post(
        f"/api/v1/invoices/{inv_id}/review-flag",
        json={"flagged": True, "review_note": "needs review"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["flagged_for_review"] is True
    assert out["review_note"] == "needs review"

    # Filter flagged=true includes it.
    r = await api_client.get("/api/v1/invoices?flagged=true", headers=hdr)
    assert r.status_code == 200
    assert any(it["id"] == inv_id for it in r.json()["items"])

    # Filter flagged=false excludes it.
    r = await api_client.get("/api/v1/invoices?flagged=false", headers=hdr)
    assert all(it["id"] != inv_id for it in r.json()["items"])

    # Clear.
    r = await api_client.post(
        f"/api/v1/invoices/{inv_id}/review-flag",
        json={"flagged": False}, headers=hdr,
    )
    assert r.status_code == 200
    assert r.json()["flagged_for_review"] is False


async def test_api_expense_flag_and_filter(api_client: AsyncClient) -> None:
    d = await _seed()
    hdr = {"X-Company-Id": str(d["company_id"])}
    exp_id = str(d["expense_id"])
    r = await api_client.post(
        f"/api/v1/expenses/{exp_id}/review-flag",
        json={"flagged": True}, headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["flagged_for_review"] is True
    r = await api_client.get("/api/v1/expenses?flagged=true", headers=hdr)
    assert any(it["id"] == exp_id for it in r.json()["items"])


async def test_api_journal_entry_flag_and_filter(api_client: AsyncClient) -> None:
    d = await _seed()
    hdr = {"X-Company-Id": str(d["company_id"])}
    je_id = str(d["je_id"])
    r = await api_client.post(
        f"/api/v1/journal_entries/{je_id}/review-flag",
        json={"flagged": True, "review_note": "manual JE — verify"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["flagged_for_review"] is True
    r = await api_client.get("/api/v1/journal_entries?flagged=true", headers=hdr)
    assert any(it["id"] == je_id for it in r.json()["items"])


async def test_api_flag_unknown_id_404(api_client: AsyncClient) -> None:
    d = await _seed()
    hdr = {"X-Company-Id": str(d["company_id"])}
    r = await api_client.post(
        f"/api/v1/invoices/{uuid.uuid4()}/review-flag",
        json={"flagged": True}, headers=hdr,
    )
    assert r.status_code == 404, r.text
