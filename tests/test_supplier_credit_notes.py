"""Supplier (purchase) credit note — service + posting + RLS + API.

Covers migration 0157 (``supplier_credit_notes`` + ``supplier_credit_note_lines``),
``services/supplier_credit_notes.py``, and ``/api/v1/supplier_credit_notes``.

The supplier credit note is the purchase-side mirror of the customer credit
note (money-in / negative-expense): it reverses a purchase —
Dr AP control (2-1200) / Cr expense / Cr GST Paid (2-1330, input credit
reversed).

Structural / RLS (Postgres only):
  * RLS ENABLE + FORCE + a ``tenant_isolation`` policy on supplier_credit_notes.
  * Cross-tenant probe via the NOBYPASSRLS saebooks_app role.

Service (superuser session, like tests/test_credit_notes.py): create/draft
totals, post -> reverse-sign JE (Dr AP / Cr expense / Cr GST Paid),
origin=SUPPLIER_CREDIT_NOTE + source linkage, void -> reversal, expense-only
account validation.

API: 401 without bearer; create/list/get/post/void.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.document_counter import DocumentCounter
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
)
from saebooks.models.supplier_credit_note import (
    SupplierCreditNote,
    SupplierCreditNoteStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.seed.load_au_coa import _load_accounts, ensure_tax_codes
from saebooks.services import bills as bill_svc
from saebooks.services import reports as report_svc
from saebooks.services import supplier_credit_notes as svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TABLE = "supplier_credit_notes"
_APP_ROLE_PASSWORD = "saebooks_app_test_pw"
_APP_ENGINE_URL_TEMPLATE = "postgresql+asyncpg://saebooks_app:{pw}@db:5432/{db}"


# --------------------------------------------------------------------------- #
# Structural RLS assertions (non-negotiable new-table checklist)
# --------------------------------------------------------------------------- #
async def test_scn_has_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :n"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, "supplier_credit_notes absent — migration 0157 missing"
    assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True), (
        "RLS not ENABLE+FORCE on supplier_credit_notes — 0157 incomplete"
    )


async def test_scn_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT qual FROM pg_policies "
                    "WHERE tablename = :n AND policyname = 'tenant_isolation'"
                ),
                {"n": _TABLE},
            )
        ).first()
    assert row is not None, "supplier_credit_notes missing tenant_isolation policy"
    assert "tenant_id" in row.qual and "current_setting" in row.qual


# --------------------------------------------------------------------------- #
# Cross-tenant probe (NOBYPASSRLS saebooks_app)
# --------------------------------------------------------------------------- #
def _resolve_app_url() -> str:
    raw = str(_owner_engine.url)
    db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0]
    return _APP_ENGINE_URL_TEMPLATE.format(pw=_APP_ROLE_PASSWORD, db=db_name)


async def _ensure_app_role_login() -> bool:
    async with _owner_engine.begin() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if exists is None:
            return False
        await conn.execute(
            text(f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'")
        )
    return True


@pytest_asyncio.fixture(scope="module")
async def app_engine() -> AsyncIterator[Any]:
    if not await _ensure_app_role_login():
        pytest.skip("saebooks_app role missing — migration 0056 not applied")
    eng = create_async_engine(_resolve_app_url(), poolclass=NullPool, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def seeded_two_tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each a company + a contact + one supplier_credit_note row."""
    Owner = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            contact_id = uuid.uuid4()
            scn_id = uuid.uuid4()
            session.add(Tenant(id=tid, name=f"SCN-{label}-{suffix}",
                               slug=f"scn-{label}-{suffix}"))
            await session.flush()
            session.add(Company(id=cid, tenant_id=tid,
                                name=f"SCN-{label}-{suffix}", base_currency="AUD"))
            await session.flush()
            session.add(Contact(id=contact_id, company_id=cid, tenant_id=tid,
                                name=f"Supplier {label}",
                                contact_type=ContactType.SUPPLIER))
            await session.flush()
            session.add(SupplierCreditNote(
                id=scn_id, tenant_id=tid, company_id=cid, contact_id=contact_id,
                issue_date=date(2026, 6, 6),
                status=SupplierCreditNoteStatus.DRAFT,
                subtotal=Decimal("0"), tax_total=Decimal("0"),
                total=Decimal("0")))
            await session.flush()
            out[label] = {"tenant_id": tid, "company_id": cid, "scn_id": scn_id}
        await session.commit()
    yield out
    async with Owner() as session:
        for label in ("tenant_a", "tenant_b"):
            row = out[label]
            await session.execute(text("DELETE FROM supplier_credit_notes WHERE id = :i"),
                                  {"i": row["scn_id"]})
            await session.execute(text("DELETE FROM contacts WHERE company_id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM companies WHERE id = :i"),
                                  {"i": row["company_id"]})
            await session.execute(text("DELETE FROM tenants WHERE id = :i"),
                                  {"i": row["tenant_id"]})
        await session.commit()


async def test_scn_visible_to_own_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a = seeded_two_tenants["tenant_a"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a["tenant_id"])},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM supplier_credit_notes WHERE id = :i"),
                {"i": a["scn_id"]},
            )
        ).all()
    assert len(visible) == 1, "tenant A cannot see its own SCN — RLS too tight"


async def test_scn_invisible_across_tenant(
    app_engine: Any, seeded_two_tenants: dict[str, Any]
) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    a_tenant = seeded_two_tenants["tenant_a"]["tenant_id"]
    b_scn = seeded_two_tenants["tenant_b"]["scn_id"]
    async with AppSession() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(a_tenant)},
        )
        visible = (
            await session.execute(
                text("SELECT id FROM supplier_credit_notes WHERE id = :i"),
                {"i": b_scn},
            )
        ).all()
    assert len(visible) == 0, "tenant A leaked tenant B's SCN — isolation broken"


async def test_scn_no_tenant_set_returns_zero(app_engine: Any) -> None:
    AppSession = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    async with AppSession() as session, session.begin():
        rows = (
            await session.execute(text("SELECT count(*) FROM supplier_credit_notes"))
        ).scalar_one()
    assert rows == 0, f"expected 0 SCNs with no tenant set, got {rows}"


# --------------------------------------------------------------------------- #
# Service-layer (superuser session against the seed company)
# --------------------------------------------------------------------------- #
async def _seed_ctx() -> dict[str, Any]:
    """Resolve the seed company's expense + AP + GST accounts + a supplier."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "6-1000"
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Test SCN Supplier",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=company.id, tenant_id=company.tenant_id,
                name="Test SCN Supplier", contact_type=ContactType.SUPPLIER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing
        return {
            "company_id": company.id,
            "tenant_id": company.tenant_id,
            "contact_id": contact.id,
            "expense_id": expense.id,
            "gst_id": gst.id,
        }


def _line(account_id: uuid.UUID, gst: uuid.UUID, amount: Decimal) -> dict[str, object]:
    return {
        "description": "Materials refund MS-020-28",
        "account_id": account_id,
        "tax_code_id": gst,
        "quantity": Decimal("1"),
        "unit_price": amount,
        "discount_pct": Decimal("0"),
    }


async def test_create_draft_computes_totals_with_gst() -> None:
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        scn = await svc.api_create(
            session,
            company_id=d["company_id"],
            tenant_id=d["tenant_id"],
            actor="test",
            contact_id=d["contact_id"],
            issue_date=date(2026, 6, 6),
            lines=[_line(d["expense_id"], d["gst_id"], Decimal("100.00"))],
            reason="Return of defective materials",
        )
    assert scn.subtotal == Decimal("100.00")
    assert scn.tax_total == Decimal("10.00")
    assert scn.total == Decimal("110.00")
    assert scn.status == SupplierCreditNoteStatus.DRAFT
    assert scn.number is not None
    assert scn.number.startswith("SCN-")


async def test_post_reverse_sign_journal_dr_ap_cr_expense_cr_gst_paid() -> None:
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        scn = await svc.api_create(
            session,
            company_id=d["company_id"],
            tenant_id=d["tenant_id"],
            actor="test",
            contact_id=d["contact_id"],
            issue_date=date(2026, 6, 6),
            lines=[_line(d["expense_id"], d["gst_id"], Decimal("200.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.api_post(
            session, scn.id, "test", scn.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
    assert posted.status == SupplierCreditNoteStatus.POSTED
    assert posted.journal_entry_id is not None

    async with AsyncSessionLocal() as session:
        entry = await session.get(JournalEntry, posted.journal_entry_id)
        assert entry is not None
        assert entry.status == EntryStatus.POSTED
        assert entry.origin == JournalOrigin.SUPPLIER_CREDIT_NOTE
        assert entry.source_type == "supplier_credit_note"
        assert entry.source_id == posted.id
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        ).scalars().all()
        debits = sum((ln.debit for ln in lines), Decimal("0"))
        credits = sum((ln.credit for ln in lines), Decimal("0"))
        assert debits == credits == Decimal("220.00")

        # Map account code -> (debit, credit).
        acct_ids = [ln.account_id for ln in lines]
        accts = {
            a.id: a.code
            for a in (
                await session.execute(
                    select(Account).where(Account.id.in_(acct_ids))
                )
            ).scalars().all()
        }
        by_code: dict[str, tuple[Decimal, Decimal]] = {}
        for ln in lines:
            code = accts[ln.account_id]
            d_c = by_code.get(code, (Decimal("0"), Decimal("0")))
            by_code[code] = (d_c[0] + ln.debit, d_c[1] + ln.credit)

        # Dr AP control 2-1200 = total; Cr expense = subtotal; Cr GST Paid = tax.
        assert by_code["2-1200"] == (Decimal("220.00"), Decimal("0"))
        assert by_code["6-1000"] == (Decimal("0"), Decimal("200.00"))
        assert by_code["2-1330"] == (Decimal("0"), Decimal("20.00"))


async def test_void_posts_reversal() -> None:
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        scn = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            contact_id=d["contact_id"], issue_date=date(2026, 6, 6),
            lines=[_line(d["expense_id"], d["gst_id"], Decimal("50.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted = await svc.api_post(
            session, scn.id, "test", scn.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
        orig_je = posted.journal_entry_id
    async with AsyncSessionLocal() as session:
        voided = await svc.api_void(
            session, posted.id, "test", posted.version,
            tenant_id=d["tenant_id"], company_id=d["company_id"],
        )
    assert voided.status == SupplierCreditNoteStatus.VOIDED
    assert voided.void_journal_entry_id is not None
    async with AsyncSessionLocal() as session:
        je = await session.get(JournalEntry, orig_je)
        assert je is not None and je.status == EntryStatus.REVERSED


async def test_income_account_rejected_on_post() -> None:
    """A supplier credit note line must be expense-type — income is rejected."""
    d = await _seed_ctx()
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == d["company_id"], Account.code == "4-6000"
                )
            )
        ).scalar_one()
        scn = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            contact_id=d["contact_id"], issue_date=date(2026, 6, 6),
            lines=[_line(income.id, d["gst_id"], Decimal("10.00"))],
        )
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.SupplierCreditNoteError, match="expense-type"):
            await svc.api_post(
                session, scn.id, "test", scn.version,
                tenant_id=d["tenant_id"], company_id=d["company_id"],
            )


# --------------------------------------------------------------------------- #
# Cross-company original_bill_id guard (gating finding 1)
# --------------------------------------------------------------------------- #
async def _other_company_posted_bill() -> dict[str, Any]:
    """Create a fully-independent company B with its own CoA + a POSTED bill.

    Returns company_id, contact_id, bill_id and the bill's total for company B.
    """
    async with AsyncSessionLocal() as session:
        company = Company(
            name=f"SCN-Other-Co-{uuid.uuid4().hex[:8]}",
            base_currency="AUD",
            tenant_id=_DEFAULT_TENANT,
        )
        session.add(company)
        await session.commit()
        await session.refresh(company)

        await ensure_tax_codes(session, company.id)
        await _load_accounts(session, company)
        await session.commit()

        session.add(
            DocumentCounter(
                company_id=company.id, kind="bill", prefix="OBILL-",
                pad_width=6, next_value=1,
            )
        )
        contact = Contact(
            company_id=company.id, tenant_id=company.tenant_id,
            name="Other Co Supplier", contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id, Account.code == "6-1000"
                )
            )
        ).scalar_one()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id, TaxCode.code == "GST"
                )
            )
        ).scalar_one()

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=company.id,
            contact_id=contact.id,
            issue_date=date(2026, 6, 6),
            due_date=date(2026, 7, 6),
            lines=[_line(expense.id, gst.id, Decimal("100.00"))],
        )
    async with AsyncSessionLocal() as session:
        posted_bill = await bill_svc.post_bill(session, bill.id, posted_by="test")

    return {
        "company_id": company.id,
        "contact_id": contact.id,
        "bill_id": posted_bill.id,
        "total": posted_bill.total,
    }


async def test_create_rejects_cross_company_original_bill_id() -> None:
    """A company-A SCN referencing company-B's bill is rejected — and B's
    bill is left completely untouched (the critic's live-demonstrated leak).
    """
    d = await _seed_ctx()
    other = await _other_company_posted_bill()

    async with AsyncSessionLocal() as session:
        with pytest.raises(
            svc.SupplierCreditNoteError, match="not found for this company"
        ):
            await svc.api_create(
                session,
                company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
                contact_id=d["contact_id"], issue_date=date(2026, 6, 6),
                lines=[_line(d["expense_id"], d["gst_id"], Decimal("50.00"))],
                original_bill_id=other["bill_id"],
            )

    # Probe: company B's bill is untouched -- still fully outstanding and
    # still visible in its own aged payables.
    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, other["bill_id"])
        assert bill.amount_paid == Decimal("0.00")
        assert bill.company_id == other["company_id"]

    async with AsyncSessionLocal() as session:
        report = await report_svc.aged_ap(
            session, other["company_id"], as_at=date(2026, 6, 6)
        )
    assert any(
        row.invoice_id == other["bill_id"]
        for group in report.groups
        for row in group.invoices
    ), "company B's bill dropped out of its own aged payables"


async def test_update_rejects_cross_company_original_bill_id() -> None:
    """The same guard applies on api_update (setting original_bill_id after
    the fact), not just api_create."""
    d = await _seed_ctx()
    other = await _other_company_posted_bill()

    async with AsyncSessionLocal() as session:
        scn = await svc.api_create(
            session,
            company_id=d["company_id"], tenant_id=d["tenant_id"], actor="test",
            contact_id=d["contact_id"], issue_date=date(2026, 6, 6),
            lines=[_line(d["expense_id"], d["gst_id"], Decimal("50.00"))],
        )
    assert scn.original_bill_id is None

    async with AsyncSessionLocal() as session:
        with pytest.raises(
            svc.SupplierCreditNoteError, match="not found for this company"
        ):
            await svc.api_update(
                session, scn.id, "test", scn.version,
                tenant_id=d["tenant_id"], company_id=d["company_id"],
                original_bill_id=other["bill_id"],
            )

    async with AsyncSessionLocal() as session:
        reloaded = await svc.api_get(
            session, scn.id, tenant_id=d["tenant_id"], company_id=d["company_id"]
        )
        assert reloaded is not None
        assert reloaded.original_bill_id is None

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.get(session, other["bill_id"])
        assert bill.amount_paid == Decimal("0.00")


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def unauth_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_api_unauth_rejected(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/supplier_credit_notes")
    assert r.status_code == 401


async def test_api_create_post_get_void(api_client: AsyncClient) -> None:
    d = await _seed_ctx()
    hdr = {"X-Company-Id": str(d["company_id"])}
    body = {
        "contact_id": str(d["contact_id"]),
        "issue_date": "2026-06-06",
        "supplier_reference": "MS-020-28",
        "lines": [
            {
                "description": "Materials refund",
                "account_id": str(d["expense_id"]),
                "tax_code_id": str(d["gst_id"]),
                "quantity": "1",
                "unit_price": "300.00",
            }
        ],
    }
    r = await api_client.post("/api/v1/supplier_credit_notes", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["total"] == "330.00"
    assert out["status"] == "DRAFT"
    scn_id = out["id"]
    version = out["version"]

    # Post (If-Match).
    r = await api_client.post(
        f"/api/v1/supplier_credit_notes/{scn_id}/post",
        headers={**hdr, "If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["status"] == "POSTED"
    assert posted["journal_entry_id"]

    # Get.
    r = await api_client.get(f"/api/v1/supplier_credit_notes/{scn_id}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["status"] == "POSTED"

    # Void (If-Match on the bumped version).
    r = await api_client.post(
        f"/api/v1/supplier_credit_notes/{scn_id}/void",
        headers={**hdr, "If-Match": str(posted["version"])},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "VOIDED"

    # List shows it.
    r = await api_client.get("/api/v1/supplier_credit_notes", headers=hdr)
    assert r.status_code == 200
    assert any(it["id"] == scn_id for it in r.json()["items"])
