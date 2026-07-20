"""Cashbook -> full tier-flip integration test.

Scenario:
  1. Company starts in cashbook mode.
  2. Three invoices are issued (posted) -- assert no JE is created for any.
  3. One receipt is posted for Invoice 1 (full payment) -- assert combined
     Dr Bank / Cr Income JE is created (cashbook single-entry receipt).
  4. Company is flipped from cashbook -> full via upgrade_cashbook_to_full.
  5. Assert A/R JEs were backfilled for Invoice 2 and Invoice 3 (still open).
  6. Assert the global trial balance across all POSTED JEs for the company
     is debits == credits.
  7. Assert the backfill is idempotent (re-run returns count=0).

Invoice 1 is fully paid before the flip so it must NOT appear in the
backfill. Invoices 2 and 3 are unpaid (amount_paid == 0) so they must
be backfilled with Dr Receivables (1-1200) / Cr Income JEs.

Written at the service layer (no HTTP). All session work uses
AsyncSessionLocal to match the production session factory; no
transaction sharing across assertions.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.payment import PaymentDirection
from saebooks.services import invoices as inv_svc
from saebooks.services import payments as pay_svc
from saebooks.services.cashbook import upgrade_cashbook_to_full
from saebooks.services.edition import (
    backfill_invoice_journals,
    list_open_invoices_for_backfill,
)

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_company_cashbook() -> tuple[uuid.UUID, uuid.UUID]:
    """Flip the oldest seed company to cashbook mode.

    Also purges any pre-existing invoices/payments/JEs from prior tests in
    the same test-session so the document_counter starts at INV-000001 for
    this test (otherwise tier_flip collides on INV-000003+ when other
    invoice-touching tests have already advanced the counter / minted that
    number).

    Returns (tenant_id, company_id).
    """
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "No seed company found"

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()

        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.tax_registered = False
        await session.commit()

    # Clean slate for this company's invoice ledger so the auto-numbered
    # INVs minted by this test cannot collide with leftovers from a prior
    # test in the same session. Order matters: payments/allocations before
    # invoices/JE lines so FKs unwind cleanly.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM payment_allocations WHERE payment_id IN "
                "(SELECT id FROM payments WHERE company_id = :cid)"
            ).bindparams(cid=co.id)
        )
        await session.execute(
            text(
                "UPDATE payments SET journal_entry_id = NULL "
                "WHERE company_id = :cid"
            ).bindparams(cid=co.id)
        )
        await session.execute(
            text(
                "UPDATE invoices SET journal_entry_id = NULL "
                "WHERE company_id = :cid"
            ).bindparams(cid=co.id)
        )
        await session.execute(
            text("DELETE FROM payments WHERE company_id = :cid").bindparams(cid=co.id)
        )
        await session.execute(
            text("DELETE FROM invoice_lines WHERE invoice_id IN "
                 "(SELECT id FROM invoices WHERE company_id = :cid)"
            ).bindparams(cid=co.id)
        )
        await session.execute(
            text("DELETE FROM invoices WHERE company_id = :cid").bindparams(cid=co.id)
        )
        await session.execute(
            text("DELETE FROM journal_lines WHERE entry_id IN "
                 "(SELECT id FROM journal_entries WHERE company_id = :cid)"
            ).bindparams(cid=co.id)
        )
        await session.execute(
            text("DELETE FROM journal_entries WHERE company_id = :cid").bindparams(cid=co.id)
        )
        await session.execute(
            text(
                "UPDATE document_counters SET next_value = 1 "
                "WHERE company_id = :cid AND kind IN ('invoice', 'payment')"
            ).bindparams(cid=co.id)
        )
        await session.commit()
        return co.tenant_id, co.id


async def _restore_company_cashbook(company_id: uuid.UUID) -> None:
    """Restore company to cashbook mode after the test."""
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, company_id)
        assert co is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        await session.commit()


async def _get_income_account_id(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.account_type == AccountType.INCOME,
                    Account.archived_at.is_(None),
                )
                .order_by(Account.code)
                .limit(1)
            )
        ).scalar_one_or_none()
        assert acct is not None, "No INCOME account in seed CoA"
        return acct.id


async def _get_or_create_contact_id(
    company_id: uuid.UUID, tenant_id: uuid.UUID
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        ct = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if ct is not None:
            return ct.id
        ct = Contact(
            company_id=company_id,
            tenant_id=tenant_id,
            name="Tier-Flip Test Customer",
            contact_type=ContactType.CUSTOMER,
        )
        session.add(ct)
        await session.commit()
        return ct.id


async def _get_bank_account_id(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        return acct.id


async def _create_and_post_invoice(
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    income_account_id: uuid.UUID,
    amount: Decimal,
    issue_date: date,
) -> Invoice:
    async with AsyncSessionLocal() as session:
        draft = await inv_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            issue_date=issue_date,
            due_date=date(2026, 7, 31),
            lines=[
                {
                    "description": f"Consulting -- ${amount}",
                    "account_id": income_account_id,
                    "quantity": "1",
                    "unit_price": str(amount),
                    "discount_pct": "0",
                }
            ],
        )
        posted = await inv_svc.post_invoice(session, draft.id)
        return posted


async def _total_je_debits_credits(company_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """Sum debit/credit across all POSTED JE lines for the company."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(JournalLine.debit, JournalLine.credit)
                .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                .where(
                    JournalEntry.company_id == company_id,
                    JournalEntry.status == "POSTED",
                )
            )
        ).all()
        dr = sum((r.debit for r in rows), Decimal("0"))
        cr = sum((r.credit for r in rows), Decimal("0"))
        return dr, cr


# ---------------------------------------------------------------------------
# The integration test
# ---------------------------------------------------------------------------


async def test_cashbook_to_full_tier_flip_round_trip() -> None:
    """Cashbook invoices -> receipt -> mode flip -> A/R backfill -> trial balance."""

    tenant_id, company_id = await _seed_company_cashbook()
    income_account_id = await _get_income_account_id(company_id)
    contact_id = await _get_or_create_contact_id(company_id, tenant_id)
    bank_account_id = await _get_bank_account_id(company_id)

    # ------------------------------------------------------------------ #
    # Step 2: post 3 invoices in cashbook mode -- expect NO JE on any    #
    # ------------------------------------------------------------------ #
    inv1 = await _create_and_post_invoice(
        company_id, contact_id, income_account_id,
        Decimal("1000.00"), date(2026, 5, 1),
    )
    inv2 = await _create_and_post_invoice(
        company_id, contact_id, income_account_id,
        Decimal("2000.00"), date(2026, 5, 10),
    )
    inv3 = await _create_and_post_invoice(
        company_id, contact_id, income_account_id,
        Decimal("3000.00"), date(2026, 5, 15),
    )

    assert inv1.status == InvoiceStatus.POSTED
    assert inv2.status == InvoiceStatus.POSTED
    assert inv3.status == InvoiceStatus.POSTED

    assert inv1.journal_entry_id is None, (
        "Cashbook invoice must not create a JE on issue "
        f"(inv1.journal_entry_id={inv1.journal_entry_id!r})"
    )
    assert inv2.journal_entry_id is None, (
        "Cashbook invoice must not create a JE on issue "
        f"(inv2.journal_entry_id={inv2.journal_entry_id!r})"
    )
    assert inv3.journal_entry_id is None, (
        "Cashbook invoice must not create a JE on issue "
        f"(inv3.journal_entry_id={inv3.journal_entry_id!r})"
    )

    # ------------------------------------------------------------------ #
    # Step 3: post a receipt for Invoice 1 (fully pays it)              #
    # ------------------------------------------------------------------ #
    async with AsyncSessionLocal() as session:
        pay = await pay_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact_id,
            bank_account_id=bank_account_id,
            payment_date=date(2026, 5, 20),
            amount=Decimal("1000.00"),
            direction=PaymentDirection.INCOMING,
        )
        pay_id = pay.id

    async with AsyncSessionLocal() as session:
        await pay_svc.allocate(
            session,
            pay_id,
            invoice_allocations=[(inv1.id, Decimal("1000.00"))],
        )

    async with AsyncSessionLocal() as session:
        posted_pay = await pay_svc.post_payment(session, pay_id)

    assert posted_pay.journal_entry_id is not None, (
        "Cashbook receipt must post a combined Dr Bank / Cr Income JE"
    )

    # Verify receipt JE balances.
    async with AsyncSessionLocal() as session:
        lines = (
            await session.execute(
                select(JournalLine).where(
                    JournalLine.entry_id == posted_pay.journal_entry_id
                )
            )
        ).scalars().all()
        dr_receipt = sum((ln.debit for ln in lines), Decimal("0"))
        cr_receipt = sum((ln.credit for ln in lines), Decimal("0"))

    assert dr_receipt == cr_receipt, (
        f"Receipt JE not balanced: DR={dr_receipt} CR={cr_receipt}"
    )
    assert dr_receipt == Decimal("1000.00"), (
        f"Receipt JE DR total wrong: expected 1000.00, got {dr_receipt}"
    )

    # ------------------------------------------------------------------ #
    # Step 4: verify backfill candidates before the flip                 #
    # ------------------------------------------------------------------ #
    async with AsyncSessionLocal() as session:
        candidates = await list_open_invoices_for_backfill(session, company_id)
        candidate_ids = {c.id for c in candidates}

    assert inv1.id not in candidate_ids, (
        "Invoice 1 (fully paid) must not be a backfill candidate"
    )
    assert inv2.id in candidate_ids, "Invoice 2 (unpaid) must be a backfill candidate"
    assert inv3.id in candidate_ids, "Invoice 3 (unpaid) must be a backfill candidate"

    # ------------------------------------------------------------------ #
    # Step 5: flip to full -- must trigger backfill                      #
    # ------------------------------------------------------------------ #
    async with AsyncSessionLocal() as session:
        upgraded = await upgrade_cashbook_to_full(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            actor="pytest-tier-flip",
        )

    assert upgraded.bookkeeping_mode == "full", (
        f"Expected bookkeeping_mode='full', got {upgraded.bookkeeping_mode!r}"
    )

    # ------------------------------------------------------------------ #
    # Step 6: verify backfill results                                    #
    # ------------------------------------------------------------------ #
    async with AsyncSessionLocal() as session:
        r_inv1 = await session.get(Invoice, inv1.id)
        r_inv2 = await session.get(Invoice, inv2.id)
        r_inv3 = await session.get(Invoice, inv3.id)

    assert r_inv1.journal_entry_id is None, (
        "Invoice 1 (paid) must not have been backfilled with an A/R JE"
    )
    assert r_inv2.journal_entry_id is not None, (
        "Invoice 2 (open) must have been backfilled with a Dr A/R JE"
    )
    assert r_inv3.journal_entry_id is not None, (
        "Invoice 3 (open) must have been backfilled with a Dr A/R JE"
    )

    # Verify each backfill JE: balanced, correct total, has Dr A/R line.
    async with AsyncSessionLocal() as session:
        ar_acct_id = (
            await session.execute(
                select(Account.id).where(
                    Account.company_id == company_id,
                    Account.code == "1-1200",
                )
            )
        ).scalar_one()

    for label, inv_obj, expected_total in [
        ("Invoice 2", r_inv2, Decimal("2000.00")),
        ("Invoice 3", r_inv3, Decimal("3000.00")),
    ]:
        async with AsyncSessionLocal() as session:
            lines = (
                await session.execute(
                    select(JournalLine).where(
                        JournalLine.entry_id == inv_obj.journal_entry_id
                    )
                )
            ).scalars().all()

        dr = sum((ln.debit for ln in lines), Decimal("0"))
        cr = sum((ln.credit for ln in lines), Decimal("0"))
        assert dr == cr, f"{label} backfill JE not balanced: DR={dr} CR={cr}"
        assert dr == expected_total, (
            f"{label} backfill JE DR total wrong: expected {expected_total}, got {dr}"
        )
        ar_dr_lines = [
            ln for ln in lines
            if ln.account_id == ar_acct_id and ln.debit > Decimal("0")
        ]
        assert ar_dr_lines, (
            f"{label} backfill JE missing Dr Receivables (1-1200) line. "
            f"Lines: {[(str(ln.account_id), ln.debit, ln.credit) for ln in lines]}"
        )

    # ------------------------------------------------------------------ #
    # Step 7: trial balance across entire company must reconcile         #
    # ------------------------------------------------------------------ #
    total_dr, total_cr = await _total_je_debits_credits(company_id)
    assert total_dr == total_cr, (
        f"Trial balance does not reconcile after tier flip: "
        f"DR={total_dr} CR={total_cr} (diff={total_dr - total_cr})"
    )

    # ------------------------------------------------------------------ #
    # Step 8: backfill is idempotent                                     #
    # ------------------------------------------------------------------ #
    async with AsyncSessionLocal() as session:
        count = await backfill_invoice_journals(
            session, company_id, actor="pytest-idempotency"
        )
        await session.commit()
    assert count == 0, (
        f"Backfill is not idempotent: re-run backfilled {count} already-processed invoice(s)"
    )

    # ------------------------------------------------------------------ #
    # Restore company for subsequent test runs                           #
    # ------------------------------------------------------------------ #
    await _restore_company_cashbook(company_id)
