"""Pay-run orchestration — select unpaid bills, build ABA file.

A *pay run* is the action of picking a set of POSTED, outstanding
bills for a single remitter bank account, choosing a processing
date, and emitting an APCA/ABA direct-entry file that the user
uploads to their internet banking portal. This module is the thin
orchestration layer on top of:

* ``services.bills`` (list POSTED bills with balance_due > 0)
* ``services.aba`` (pure CEMTEX builder)

Posting the resulting Payment + allocation is a separate explicit
step — we don't mint a Payment row on ABA export because the user
hasn't actually paid yet; they need to upload the file first. Once
the bank confirms settlement they come back and post the payments.
That matches QBO/Xero's "mark as paid after upload" flow.

A ``PayRunCandidate`` is a ``(Bill, balance_due, payee_contact)``
triple used by the UI to render the pick-list. A ``PayRunSelection``
is a ``(bill_id, amount)`` the user has ticked.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact
from saebooks.services import aba


class PayRunError(ValueError):
    """Raised on pay-run validation failure."""


@dataclass(frozen=True)
class PayRunCandidate:
    bill: Bill
    contact: Contact
    balance_due: Decimal


@dataclass(frozen=True)
class PayRunSelection:
    bill_id: uuid.UUID
    amount: Decimal


async def candidates_for_payrun(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> list[PayRunCandidate]:
    """Return every POSTED, non-archived bill with balance > 0.

    Ordered by due-date ascending so the earliest-due bills are at
    the top of the pick-list. Each row includes the payee Contact so
    the UI can flag suppliers missing bank details (BSB/account/title).
    """
    stmt = (
        select(Bill)
        .options(selectinload(Bill.lines))
        .where(
            Bill.company_id == company_id,
            Bill.status == BillStatus.POSTED,
            Bill.archived_at.is_(None),
            Bill.total > Bill.amount_paid,
        )
        .order_by(Bill.due_date.asc(), Bill.created_at.asc())
    )
    result = await session.execute(stmt)
    bills = list(result.scalars().all())

    # Collect contacts in one round-trip rather than lazy-loading per
    # bill (breaks async).
    contact_ids = {b.contact_id for b in bills}
    if not contact_ids:
        return []
    contacts_result = await session.execute(
        select(Contact).where(Contact.id.in_(contact_ids))
    )
    contacts_by_id = {c.id: c for c in contacts_result.scalars().all()}

    return [
        PayRunCandidate(
            bill=b,
            contact=contacts_by_id[b.contact_id],
            balance_due=(b.total - b.amount_paid),
        )
        for b in bills
    ]


async def build_aba_from_selection(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    bank_account_id: uuid.UUID,
    selections: list[PayRunSelection],
    process_date: date,
    description: str = "CREDITORS",
) -> str:
    """Render an ABA file from a list of selections.

    Validates remitter + every payee has the bank-detail fields set.
    Pulls ``supplier_reference`` (or ``BILL-<number>`` fallback) for
    each payee's lodgement reference. Returns the CRLF-joined file
    text — caller writes it to disk or streams as a response.
    """
    if not selections:
        raise PayRunError("Pay run requires at least one bill selection")

    # --- remitter --------------------------------------------------------
    bank = await session.get(Account, bank_account_id)
    if bank is None or bank.company_id != company_id:
        raise PayRunError(f"Bank account {bank_account_id} not found for company")
    _assert_remitter_fields(bank)

    # --- bills + payees ------------------------------------------------
    bill_ids = [s.bill_id for s in selections]
    bills_result = await session.execute(
        select(Bill).where(
            Bill.company_id == company_id,
            Bill.id.in_(bill_ids),
        )
    )
    bills_by_id: dict[uuid.UUID, Bill] = {
        b.id: b for b in bills_result.scalars().all()
    }

    missing = [bid for bid in bill_ids if bid not in bills_by_id]
    if missing:
        raise PayRunError(f"Bills not found: {missing}")

    contact_ids = {b.contact_id for b in bills_by_id.values()}
    contacts_result = await session.execute(
        select(Contact).where(Contact.id.in_(contact_ids))
    )
    contacts_by_id: dict[uuid.UUID, Contact] = {
        c.id: c for c in contacts_result.scalars().all()
    }

    # --- build detail lines ---------------------------------------------
    details: list[aba.AbaDetail] = []
    for sel in selections:
        bill = bills_by_id[sel.bill_id]
        contact = contacts_by_id[bill.contact_id]
        _assert_payee_fields(contact)

        balance_due = bill.total - bill.amount_paid
        if sel.amount <= 0:
            raise PayRunError(
                f"Bill {bill.id}: amount {sel.amount} must be positive"
            )
        if sel.amount > balance_due:
            raise PayRunError(
                f"Bill {bill.id}: selected {sel.amount} > balance due "
                f"{balance_due}"
            )

        lodgement = (
            (bill.supplier_reference or bill.number or "")[:18]
        ).strip()

        # Bank details on Account/Contact are `str | None`; the
        # _assert_* helpers above reject None, but mypy still sees the
        # narrower type, so cast via `or ""` for the remaining assignments.
        details.append(
            aba.AbaDetail(
                payee_bsb=contact.bank_bsb or "",
                payee_account_number=contact.bank_account_number or "",
                payee_account_title=(contact.bank_account_title or contact.name)[:32],
                amount_cents=aba.dollars_to_cents(sel.amount),
                lodgement_reference=lodgement,
                remitter_bsb=bank.bsb or "",
                remitter_account_number=bank.bank_account_number or "",
                remitter_name=(bank.bank_account_title or "")[:16],
                txn_code=aba.TXN_CREDIT_GENERAL,
            )
        )

    header = aba.AbaHeader(
        bank_abbreviation=bank.bank_abbreviation or "",
        user_name=(bank.bank_account_title or "")[:26],
        apca_user_id=bank.apca_user_id or "",
        description=description[:12],
        process_date_ddmmyy=process_date.strftime("%d%m%y"),
    )

    return aba.build_aba(header, details)


def _assert_remitter_fields(bank: Account) -> None:
    missing: list[str] = []
    for field in (
        "bsb",
        "bank_account_number",
        "bank_account_title",
        "apca_user_id",
        "bank_abbreviation",
    ):
        if not getattr(bank, field):
            missing.append(field)
    if missing:
        raise PayRunError(
            f"Bank account {bank.id} is missing ABA fields: {', '.join(missing)}. "
            "Edit the account and fill in its BSB, account number, account "
            "title, APCA User ID and 3-letter bank abbreviation."
        )


def _assert_payee_fields(contact: Contact) -> None:
    missing: list[str] = []
    for field in ("bank_bsb", "bank_account_number", "bank_account_title"):
        if not getattr(contact, field):
            missing.append(field)
    if missing:
        raise PayRunError(
            f"Supplier {contact.name} is missing ABA fields: "
            f"{', '.join(missing)}. Edit the contact and fill in its BSB, "
            "account number and account title."
        )
