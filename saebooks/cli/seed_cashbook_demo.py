"""Cashbook demo seed — sole-trader public demo dataset.

Idempotent. Designed to run on every container start when
``SAEBOOKS_RUN_CASHBOOK_DEMO_SEED=true`` is set (cashbook-demo compose
service env). Produces:

* Tenant ``00000000-0000-0000-0000-000000000001``
* Demo company "Sam Sole Trader" in ``bookkeeping_mode=\cashbook\``
  with AU chart of accounts pre-loaded
* GST-registered, FY starts 1 July
* AU GST tax codes seeded
* Demo user ``demo@cashbook.example`` (login by web auto-login middleware)
* ~30 cashbook entries spanning the last 90 days — a realistic mix of
  income (consulting, products) and expenses (fuel, software, materials,
  office supplies, phone), totalling order-of-magnitude ~$8k income vs
  ~$3k expenses

Re-running the seed is safe: company / user / tax codes / CoA all use
their existing idempotent seeders; cashbook entries skip via a marker
description prefix (``[demo]``) so we don\t double-up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import JournalEntry
from saebooks.models.user import User, UserRole
from saebooks.seed.load_au_coa import main as load_au_coa
from saebooks.services.cashbook import (
    CashbookError,
    record_cashbook_entry,
    setup_cashbook_mode,
)
from saebooks.services.companies import ensure_seed_company
from saebooks.services.jwt_tokens import hash_password
from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.quote import Quote, QuoteStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import contacts as contacts_svc
from saebooks.services import invoices as invoices_svc
from saebooks.services import quotes as quotes_svc

logger = logging.getLogger("saebooks.cli.seed_cashbook_demo")

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_DEMO_MARKER = "[demo]"


def _demo_email() -> str:
    return os.environ.get("SAEBOOKS_DEMO_EMAIL", "demo@cashbook.example")


def _demo_password() -> str:
    return os.environ.get("SAEBOOKS_DEMO_PASSWORD", "cashbook-demo")


def _company_name() -> str:
    return os.environ.get("SAEBOOKS_DEMO_COMPANY_NAME", "Sam Sole Trader")


async def _seed_company(session: AsyncSession) -> Company:
    """Ensure the demo company exists, return it."""
    existing = await session.execute(
        select(Company).where(Company.id == _TENANT_ID)
    )
    row = existing.scalars().first()
    if row is not None:
        return row

    os.environ["SEED_COMPANY_NAME"] = _company_name()
    os.environ["SEED_COMPANY_BASE_CURRENCY"] = "AUD"
    os.environ["SEED_COMPANY_FIN_YEAR_START_MONTH"] = "7"
    return await ensure_seed_company(session)


async def _seed_user(session: AsyncSession) -> User:
    email = _demo_email()
    existing = await session.execute(select(User).where(User.email == email))
    row = existing.scalars().first()
    if row is not None:
        return row
    user = User(
        tenant_id=_TENANT_ID,
        username=email.split("@")[0],
        display_name="Demo (Sam)",
        email=email,
        role=UserRole.ADMIN.value,
        password_hash=hash_password(_demo_password()),
        version=1,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _find_default_bank_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    """Pick a sensible bank account for cashbook setup.

    Strategy: prefer the lowest-numbered ASSET account whose code starts
    with the bank range used by the AU CoA seed (Odoo-format starts at
    ``11110``). Fall back to the lowest ASSET account if none match.
    """
    stmt = (
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.account_type == AccountType.ASSET,
        )
        .order_by(Account.code)
    )
    rows = (await session.execute(stmt)).scalars().all()
    for acc in rows:
        # Account code is hyphenated (e.g. "1-1110") at runtime.
        flat = acc.code.replace("-", "")
        if flat.startswith("1110") or flat.startswith("1111"):
            return acc
    return rows[0] if rows else None


async def _existing_demo_entry_count(
    session: AsyncSession, company_id: uuid.UUID
) -> int:
    stmt = select(JournalEntry).where(
        JournalEntry.company_id == company_id,
        JournalEntry.description.like(f"{_DEMO_MARKER}%"),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return len(rows)


# Demo dataset — sole-trader, mixed income + expenses, last 90 days.
# (days_ago, direction, category_code, amount, description)
_DEMO_ENTRIES: list[tuple[int, str, str, str, str]] = [
    # Income — consulting + product sales
    (88, "income", "INC_SALES", "1650.00", "Consulting — Acme Pty Ltd"),
    (84, "income", "INC_SALES", "275.00", "Tools resale"),
    (80, "expense", "EXP_VEHICLE", "98.40", "Fuel — Shell"),
    (78, "expense", "EXP_TELCO", "59.00", "Mobile — Telstra"),
    (74, "income", "INC_SALES", "880.00", "Consulting — Bayside Joinery"),
    (71, "expense", "EXP_SOFTWARE", "29.00", "Subscription — Notion"),
    (68, "expense", "EXP_OTHER", "44.50", "Officeworks — printer ink"),
    (65, "income", "INC_SALES", "330.00", "Saturday market sales"),
    (62, "expense", "EXP_VEHICLE", "112.30", "Fuel — BP"),
    (60, "expense", "EXP_MATERIALS", "486.20", "Bunnings — fasteners"),
    (57, "income", "INC_SALES", "2200.00", "Workshop — corporate"),
    (54, "expense", "EXP_SOFTWARE", "29.00", "Subscription — Notion"),
    (52, "expense", "EXP_TELCO", "59.00", "Mobile — Telstra"),
    (49, "income", "INC_SALES", "440.00", "Consulting — Petra T"),
    (46, "expense", "EXP_OTHER", "22.95", "Coffee + lunch — client meeting"),
    (43, "expense", "EXP_VEHICLE", "104.10", "Fuel — Shell"),
    (40, "income", "INC_SALES", "1100.00", "Consulting — Acme Pty Ltd"),
    (37, "expense", "EXP_MATERIALS", "215.80", "Reece — fittings"),
    (34, "income", "INC_SALES", "385.00", "Saturday market sales"),
    (31, "expense", "EXP_SOFTWARE", "29.00", "Subscription — Notion"),
    (29, "expense", "EXP_OTHER", "39.95", "Officeworks — folders"),
    (26, "income", "INC_SALES", "1320.00", "Consulting — Bayside Joinery"),
    (23, "expense", "EXP_TELCO", "59.00", "Mobile — Telstra"),
    (20, "expense", "EXP_VEHICLE", "96.80", "Fuel — Shell"),
    (17, "income", "INC_SALES", "660.00", "Saturday market sales"),
    (14, "expense", "EXP_MATERIALS", "354.40", "Bunnings — timber"),
    (11, "income", "INC_SALES", "990.00", "Consulting — Acme Pty Ltd"),
    (8, "expense", "EXP_SOFTWARE", "29.00", "Subscription — Notion"),
    (5, "income", "INC_SALES", "275.00", "Tools resale"),
    (2, "expense", "EXP_OTHER", "18.50", "Stationery"),
]


async def _seed_entries(
    session: AsyncSession, *, tenant_id: uuid.UUID, company_id: uuid.UUID
) -> int:
    today = date.today()
    inserted = 0
    for idx, (days_ago, direction, category, amount_s, desc) in enumerate(_DEMO_ENTRIES):
        entry_date = today - timedelta(days=days_ago)
        # Stable idempotency key per (slot index, date) so reseeding picks
        # up where we left off and never duplicates.
        idempotency_key = f"demo-seed-v1-{idx:03d}"
        try:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=entry_date,
                description=f"{_DEMO_MARKER} {desc}",
                amount=Decimal(amount_s),
                direction=direction,  # type: ignore[arg-type]
                category_code=category,
                idempotency_key=idempotency_key,
                actor="cashbook-demo-seed",
            )
            inserted += 1
        except CashbookError as exc:
            logger.warning(
                "skip entry %d (%s %s): %s", idx, direction, amount_s, exc
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "skip entry %d (%s %s): unexpected %r", idx, direction, amount_s, exc
            )
    return inserted




# ---------------------------------------------------------------------------
# Sample customers + invoices + quotes (idempotent via [demo] marker in notes)
# ---------------------------------------------------------------------------


async def _ensure_demo_contact(
    session: AsyncSession, *, company_id: uuid.UUID, name: str, email: str | None
) -> Contact:
    existing = (
        await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.name == name,
                Contact.archived_at.is_(None),
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing
    return await contacts_svc.create(
        session,
        company_id,
        actor="cashbook-demo-seed",
        tenant_id=_TENANT_ID,
        name=name,
        contact_type=ContactType.CUSTOMER,
        email=email,
    )


async def _first_income_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    rows = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.account_type == AccountType.INCOME,
            )
            .order_by(Account.code)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _tax_code_by_label(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> TaxCode | None:
    return (
        await session.execute(
            select(TaxCode).where(
                TaxCode.company_id == company_id,
                TaxCode.code == code,
            )
        )
    ).scalars().first()


async def _seed_invoices(
    session: AsyncSession, company_id: uuid.UUID
) -> int:
    """Insert ~3 demo invoices with [demo] marker in notes. Idempotent."""
    existing = (
        await session.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.notes.like("[demo]%"),
            )
        )
    ).scalars().all()
    if existing:
        return len(existing)

    income = await _first_income_account(session, company_id)
    gst = await _tax_code_by_label(session, company_id, "GST")
    fre = await _tax_code_by_label(session, company_id, "FRE")
    if not (income and gst and fre):
        logger.warning(
            "demo invoices: missing income (%s) gst (%s) fre (%s)", income, gst, fre
        )
        return 0

    acme = await _ensure_demo_contact(
        session, company_id=company_id, name="Acme Pty Ltd", email="finance@acme.com.au"
    )
    bobs = await _ensure_demo_contact(
        session, company_id=company_id, name="Bob's Bakery", email=None
    )
    await session.flush()

    today = date.today()
    fixtures = [
        # (contact, days_ago, due_days, description, gross_amount, post)
        (acme, 14, 14, "Consulting — May", Decimal("1100.00"), True),
        (bobs, 7,  21, "Website refresh",   Decimal("880.00"),  True),
        (acme, 1,  14, "Hosting + maintenance", Decimal("330.00"), False),
    ]
    inserted = 0
    for contact, ago, due_days, desc, gross, do_post in fixtures:
        issue = today - timedelta(days=ago)
        due = issue + timedelta(days=due_days)
        unit_price = (gross / Decimal("1.10")).quantize(Decimal("0.01"))
        try:
            inv = await invoices_svc.api_create(
                session,
                company_id,
                _TENANT_ID,
                "cashbook-demo-seed",
                contact_id=contact.id,
                issue_date=issue,
                due_date=due,
                lines=[
                    {
                        "description": desc,
                        "account_id": income.id,
                        "tax_code_id": gst.id,
                        "quantity": Decimal("1"),
                        "unit_price": unit_price,
                    }
                ],
                notes=f"[demo] {desc}",
            )
            await session.commit()
            if do_post:
                try:
                    await invoices_svc.api_post_invoice(
                        session,
                        inv.id,
                        actor="cashbook-demo-seed",
                        expected_version=inv.version,
                        tenant_id=_TENANT_ID,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "demo invoice %s: could not post (%r); leaving as draft",
                        inv.id,
                        exc,
                    )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo invoice %s skip: %r", desc, exc)
    return inserted


async def _seed_quotes(
    session: AsyncSession, company_id: uuid.UUID
) -> int:
    """Insert ~2 demo quotes with [demo] marker in notes. Idempotent."""
    existing = (
        await session.execute(
            select(Quote).where(
                Quote.company_id == company_id,
                Quote.notes.like("[demo]%"),
            )
        )
    ).scalars().all()
    if existing:
        return len(existing)

    income = await _first_income_account(session, company_id)
    gst = await _tax_code_by_label(session, company_id, "GST")
    if not (income and gst):
        return 0

    acme = await _ensure_demo_contact(
        session, company_id=company_id, name="Acme Pty Ltd", email="finance@acme.com.au"
    )
    bobs = await _ensure_demo_contact(
        session, company_id=company_id, name="Bob's Bakery", email=None
    )
    await session.flush()

    today = date.today()
    fixtures = [
        # (customer, days_ago, description, gross_amount, send)
        (acme, 3, "Site rebuild Q3", Decimal("4400.00"), True),
        (bobs, 1, "POS install", Decimal("1650.00"), False),
    ]
    inserted = 0
    for customer, ago, desc, gross, do_send in fixtures:
        issue = today - timedelta(days=ago)
        unit_price = (gross / Decimal("1.10")).quantize(Decimal("0.01"))
        try:
            q = await quotes_svc.api_create(
                session,
                company_id,
                _TENANT_ID,
                "cashbook-demo-seed",
                customer_id=customer.id,
                issue_date=issue,
                expiry_date=issue + timedelta(days=28),
                lines=[
                    {
                        "description": desc,
                        "account_id": income.id,
                        "tax_code_id": gst.id,
                        "quantity": Decimal("1"),
                        "unit_price": unit_price,
                    }
                ],
                notes=f"[demo] {desc}",
            )
            await session.commit()
            if do_send:
                try:
                    await quotes_svc.api_send(
                        session,
                        q.id,
                        actor="cashbook-demo-seed",
                        expected_version=q.version,
                        tenant_id=_TENANT_ID,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "demo quote %s: could not send (%r); leaving as draft", q.id, exc
                    )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo quote %s skip: %r", desc, exc)
    return inserted


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # 1. Company (with default tenant_id)
    async with AsyncSessionLocal() as session:
        company = await _seed_company(session)
        logger.info("Company: %s (%s)", company.name, company.id)

    # 2. AU chart of accounts
    await load_au_coa()

    company_id = company.id

    # 3. Tax codes
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(select(Company).where(Company.id == company_id))
        ).scalars().one()
        n_tc = await ensure_tax_codes(session, company.id)
        logger.info("Tax codes seeded: %d new", n_tc)

    # 4. Demo user
    async with AsyncSessionLocal() as session:
        user = await _seed_user(session)
        logger.info("User: %s (%s)", user.email, user.id)

    # 5. Pick bank account + flip company to cashbook mode
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(select(Company).where(Company.id == company_id))
        ).scalars().one()
        bank = await _find_default_bank_account(session, company.id)
        if bank is None:
            raise SystemExit(
                "Cashbook demo seed: no ASSET account found — CoA load failed?"
            )
        logger.info("Bank account: %s (%s)", bank.code, bank.name)

        if company.bookkeeping_mode != "cashbook":
            await setup_cashbook_mode(
                db=session,
                tenant_id=_TENANT_ID,
                company_id=company.id,
                bank_account_id=bank.id,
                actor="cashbook-demo-seed",
            )
            logger.info("Company flipped to cashbook mode.")
        else:
            logger.info("Company already in cashbook mode.")

    # 6. Cashbook entries
    async with AsyncSessionLocal() as session:
        existing = await _existing_demo_entry_count(
            session, company_id=company.id
        )
        logger.info("Existing demo entries: %d", existing)
        n = await _seed_entries(
            session, tenant_id=_TENANT_ID, company_id=company.id
        )
        logger.info("Cashbook entries inserted/preserved this run: %d", n)


    # 7. Sample invoices + quotes
    async with AsyncSessionLocal() as session:
        n_inv = await _seed_invoices(session, company_id)
        logger.info("Demo invoices inserted/preserved: %d", n_inv)
    async with AsyncSessionLocal() as session:
        n_q = await _seed_quotes(session, company_id)
        logger.info("Demo quotes inserted/preserved: %d", n_q)


if __name__ == "__main__":
    asyncio.run(main())
