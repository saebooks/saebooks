"""Multi-tenant "critics" seed — 10 separate tenants, 20 users, realistic AU data.

This is a one-shot dev/staging seeder used to populate a clean books-dev
install with a believable population of independent tenants for design
review and exploratory testing.  It is intentionally idempotent so it can
be re-run as the data model evolves.

Usage (inside the saebooks2-api-1 container)::

    python -m saebooks.cli.seed_critics

The script connects via the project's ``AsyncSessionLocal`` — the
``saebooks`` DB role owns the tables, so RLS does not apply.  Each
tenant is wrapped in its own transaction; if one fails, the script logs
the error and continues with the next.

The script emits a human-readable summary to stderr as it runs and a
machine-readable JSON document to stdout at the end (the caller can
``> creds.json`` to capture it).

NOTE: this script is *additive*.  It must NOT touch the default tenant
(``00000000-0000-0000-0000-000000000001``) or any of its existing rows.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import secrets
import string
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.models.user import User, UserRole
from saebooks.seed.load_au_coa import (
    ODOO_TYPE_MAP,
    SEED_DIR,
    _SKIP_CODES,
    _SUB_HEADERS,
    _hyphenate_code,
    _parse_bool,
)
from saebooks.services.bills import create_draft as create_bill_draft
from saebooks.services.contacts import create as create_contact
from saebooks.services.invoices import create_draft as create_invoice_draft
from saebooks.services.jwt_tokens import hash_password
from saebooks.services.tax_codes import AU_SEED as AU_TAX_SEED

logger = logging.getLogger("saebooks.cli.seed_critics")

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
LOGIN_URL = "https://books-dev.sauer.com.au/login"
EMAIL_DOMAIN = "critics.sauer.com.au"


# ---------------------------------------------------------------------------
# Tenant + user spec.  Two shapes — TEAM (multi-user company) and SOLO
# (single-admin trader).  Each user tuple is (username, display_name, role).
# ---------------------------------------------------------------------------

TENANTS: list[dict[str, Any]] = [
    # TEAMS (4 tenants, 9 users) ------------------------------------------
    {
        "slug": "apex-consulting",
        "name": "Apex Consulting",
        "type": "team",
        "users": [
            ("sarah_apex", "Sarah Mitchell", "admin"),
            ("marcus_apex", "Marcus Holland", "accountant"),
            ("chen_apex", "Chen Wei", "bookkeeper"),
        ],
        "industry": "consulting",
    },
    {
        "slug": "riverside-retail",
        "name": "Riverside Retail",
        "type": "team",
        "users": [
            ("amanda_riverside", "Amanda Foster", "admin"),
            ("ben_riverside", "Ben Carter", "bookkeeper"),
        ],
        "industry": "retail",
    },
    {
        "slug": "summit-pro",
        "name": "Summit Pro Services",
        "type": "team",
        "users": [
            ("michael_summit", "Michael Tan", "admin"),
            ("helen_summit", "Helen Roberts", "accountant"),
        ],
        "industry": "services",
    },
    {
        "slug": "enterprise-holdings",
        "name": "Enterprise Group Holdings",
        "type": "team",
        "users": [
            ("karen_enterprise", "Karen Walsh", "admin"),
            ("tom_enterprise", "Tom Bradshaw", "accountant"),
        ],
        "industry": "holdings",
    },
    # SOLO (11 tenants, 11 users) -----------------------------------------
    {
        "slug": "alex-kim",
        "name": "Alex Kim Trading",
        "type": "solo",
        "users": [("alex_kim", "Alex Kim", "admin")],
        "industry": "trading",
    },
    {
        "slug": "walsh-co",
        "name": "Walsh & Co",
        "type": "solo",
        "users": [("jamie_walsh", "Jamie Walsh", "admin")],
        "industry": "consulting",
    },
    {
        "slug": "riley-designs",
        "name": "Riley Designs",
        "type": "solo",
        "users": [("riley_chen", "Riley Chen", "admin")],
        "industry": "design",
    },
    {
        "slug": "morgan-property",
        "name": "Morgan Property",
        "type": "solo",
        "users": [("morgan_davies", "Morgan Davies", "admin")],
        "industry": "property",
    },
    {
        "slug": "brooks-auto",
        "name": "Brooks Automotive",
        "type": "solo",
        "users": [("taylor_brooks", "Taylor Brooks", "admin")],
        "industry": "automotive",
    },
    {
        "slug": "lee-imports",
        "name": "Lee Imports",
        "type": "solo",
        "users": [("jordan_lee", "Jordan Lee", "admin")],
        "industry": "imports",
    },
    {
        "slug": "nguyen-tradies",
        "name": "Nguyen Tradies",
        "type": "solo",
        "users": [("kim_nguyen", "Kim Nguyen", "admin")],
        "industry": "services",
    },
    {
        "slug": "patel-pharmacy",
        "name": "Patel Pharmacy",
        "type": "solo",
        "users": [("priya_patel", "Priya Patel", "admin")],
        "industry": "retail",
    },
    {
        "slug": "costa-freelance",
        "name": "Costa Freelance IT",
        "type": "solo",
        "users": [("dani_costa", "Dani Costa", "admin")],
        "industry": "consulting",
    },
    {
        "slug": "wilson-ecom",
        "name": "Wilson eCommerce",
        "type": "solo",
        "users": [("sam_wilson", "Sam Wilson", "admin")],
        "industry": "trading",
    },
    {
        "slug": "murray-photo",
        "name": "Murray Photography",
        "type": "solo",
        "users": [("casey_murray", "Casey Murray", "admin")],
        "industry": "design",
    },
]


# ---------------------------------------------------------------------------
# Industry-keyed contact templates — name + type + AU state.  Five to eight
# rows per template; the seeder picks the whole list per tenant.
# ---------------------------------------------------------------------------

CONTACT_TEMPLATES: dict[str, list[tuple[str, ContactType, str]]] = {
    "consulting": [
        ("Holcim Australia Pty Ltd", ContactType.CUSTOMER, "NSW"),
        ("Westfield Group", ContactType.CUSTOMER, "NSW"),
        ("Macquarie Bank", ContactType.CUSTOMER, "NSW"),
        ("Origin Energy Retail", ContactType.CUSTOMER, "VIC"),
        ("Officeworks", ContactType.SUPPLIER, "VIC"),
        ("Telstra Corporation", ContactType.SUPPLIER, "VIC"),
        ("Australia Post", ContactType.SUPPLIER, "VIC"),
    ],
    "retail": [
        ("Sunshine Coast Markets", ContactType.CUSTOMER, "QLD"),
        ("Beachside Cafe", ContactType.CUSTOMER, "QLD"),
        ("Local Walk-ins", ContactType.CUSTOMER, "QLD"),
        ("Coca-Cola Amatil", ContactType.SUPPLIER, "NSW"),
        ("Metcash Trading", ContactType.SUPPLIER, "NSW"),
        ("Visy Industries", ContactType.SUPPLIER, "VIC"),
        ("Origin Energy", ContactType.SUPPLIER, "VIC"),
        ("Property Trust QLD", ContactType.SUPPLIER, "QLD"),
    ],
    "services": [
        ("BHP Group Operations", ContactType.CUSTOMER, "WA"),
        ("Rio Tinto Iron Ore", ContactType.CUSTOMER, "WA"),
        ("Woodside Energy", ContactType.CUSTOMER, "WA"),
        ("Bunnings Trade", ContactType.SUPPLIER, "VIC"),
        ("Kennards Hire", ContactType.SUPPLIER, "NSW"),
        ("Caltex Australia", ContactType.SUPPLIER, "NSW"),
        ("Workforce Australia", ContactType.SUPPLIER, "VIC"),
    ],
    "holdings": [
        ("Subsidiary Alpha Pty Ltd", ContactType.CUSTOMER, "NSW"),
        ("Subsidiary Beta Pty Ltd", ContactType.CUSTOMER, "VIC"),
        ("Property Investments AU", ContactType.CUSTOMER, "QLD"),
        ("CBA Business Bank", ContactType.SUPPLIER, "NSW"),
        ("Allens Linklaters", ContactType.SUPPLIER, "NSW"),
        ("PwC Australia", ContactType.SUPPLIER, "NSW"),
        ("ASIC", ContactType.SUPPLIER, "ACT"),
    ],
    "trading": [
        ("Coles Group", ContactType.CUSTOMER, "VIC"),
        ("Woolworths Group", ContactType.CUSTOMER, "NSW"),
        ("Aldi Stores", ContactType.CUSTOMER, "NSW"),
        ("Asia Pacific Imports", ContactType.SUPPLIER, "NSW"),
        ("Sydney Freight Forwarders", ContactType.SUPPLIER, "NSW"),
        ("DHL Express", ContactType.SUPPLIER, "NSW"),
    ],
    "design": [
        ("Boutique Hotel Group", ContactType.CUSTOMER, "VIC"),
        ("Restaurant Group VIC", ContactType.CUSTOMER, "VIC"),
        ("Local Architects Pty Ltd", ContactType.CUSTOMER, "VIC"),
        ("Inner West Cafe", ContactType.CUSTOMER, "NSW"),
        ("Adobe Systems", ContactType.SUPPLIER, "NSW"),
        ("Officeworks", ContactType.SUPPLIER, "VIC"),
        ("Print Studio Melbourne", ContactType.SUPPLIER, "VIC"),
    ],
    "property": [
        ("Tenant — 12 Smith St Apt 1", ContactType.CUSTOMER, "QLD"),
        ("Tenant — 12 Smith St Apt 2", ContactType.CUSTOMER, "QLD"),
        ("Tenant — 45 Beach Rd", ContactType.CUSTOMER, "QLD"),
        ("Tenant — 88 Park Ave", ContactType.CUSTOMER, "QLD"),
        ("Local Plumbing Services", ContactType.SUPPLIER, "QLD"),
        ("Brisbane City Council", ContactType.SUPPLIER, "QLD"),
        ("Energex", ContactType.SUPPLIER, "QLD"),
        ("Strata Management Co", ContactType.SUPPLIER, "QLD"),
    ],
    "automotive": [
        ("Walk-in Customer", ContactType.CUSTOMER, "NSW"),
        ("Fleet Services NSW", ContactType.CUSTOMER, "NSW"),
        ("Local Taxi Co-op", ContactType.CUSTOMER, "NSW"),
        ("Repco Auto Parts", ContactType.SUPPLIER, "VIC"),
        ("Burson Automotive", ContactType.SUPPLIER, "VIC"),
        ("Castrol Australia", ContactType.SUPPLIER, "NSW"),
        ("Sydney Tyres Direct", ContactType.SUPPLIER, "NSW"),
    ],
    "imports": [
        ("Distribution Co AU", ContactType.CUSTOMER, "NSW"),
        ("Wholesale Partners VIC", ContactType.CUSTOMER, "VIC"),
        ("Online Reseller Group", ContactType.CUSTOMER, "QLD"),
        ("Shenzhen Manufacturing", ContactType.SUPPLIER, "NSW"),
        ("Singapore Trade Hub", ContactType.SUPPLIER, "NSW"),
        ("Customs Broker Sydney", ContactType.SUPPLIER, "NSW"),
        ("Maersk Line", ContactType.SUPPLIER, "VIC"),
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen_password(n: int = 12) -> str:
    """Random alphanumeric password — no ambiguous chars."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _gen_abn() -> str:
    """Generate an 11-digit string in the ABN format the validator accepts.

    The validator only checks length+digits, not the modulus-89 checksum,
    so any 11-digit string is fine for fixture data.
    """
    return "".join(secrets.choice(string.digits) for _ in range(11))


# ---------------------------------------------------------------------------
# Per-tenant subroutines — each takes an open AsyncSession that the caller
# commits/rolls-back as a unit.
# ---------------------------------------------------------------------------


async def ensure_tenant(session: AsyncSession, slug: str, name: str) -> tuple[Tenant, bool]:
    existing = await session.execute(select(Tenant).where(Tenant.slug == slug))
    row = existing.scalars().first()
    if row is not None:
        return row, False
    tenant = Tenant(id=uuid.uuid4(), slug=slug, name=name)
    session.add(tenant)
    await session.flush()
    return tenant, True


async def ensure_company(
    session: AsyncSession, *, tenant_id: uuid.UUID, name: str, abn: str
) -> tuple[Company, bool]:
    existing = await session.execute(
        select(Company).where(
            Company.tenant_id == tenant_id,
            Company.name == name,
            Company.archived_at.is_(None),
        )
    )
    row = existing.scalars().first()
    if row is not None:
        return row, False
    company = Company(
        tenant_id=tenant_id,
        name=name,
        legal_name=name,
        abn=abn,
        base_currency="AUD",
        fin_year_start_month=7,
        version=1,
    )
    session.add(company)
    await session.flush()
    return company, True


async def load_accounts_for_company(
    session: AsyncSession, *, tenant_id: uuid.UUID, company: Company
) -> tuple[int, int]:
    """Mirror of seed.load_au_coa._load_accounts but with explicit tenant_id.

    The default Account.tenant_id resolves to the magic default tenant, so
    we MUST set tenant_id on every Account row we create here, otherwise
    the company would belong to one tenant and its accounts to another.
    """
    csv_path = SEED_DIR / "account.account-au.csv"
    inserted = skipped = 0

    existing = await session.execute(
        select(Account.code).where(Account.company_id == company.id)
    )
    existing_codes = {code for (code,) in existing.all()}

    # Sub-headers
    for code, name, acct_type in _SUB_HEADERS:
        if code in existing_codes:
            continue
        session.add(
            Account(
                tenant_id=tenant_id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=acct_type,
                is_header=True,
            )
        )
        existing_codes.add(code)
        inserted += 1

    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            raw_code = row["code"].strip()
            if raw_code in _SKIP_CODES:
                skipped += 1
                continue
            code = _hyphenate_code(raw_code)
            if code in existing_codes:
                skipped += 1
                continue
            odoo_type = row["account_type"].strip()
            mapped = ODOO_TYPE_MAP.get(odoo_type)
            if mapped is None:
                raise ValueError(f"Unmapped Odoo account_type {odoo_type!r} on code {code}")
            session.add(
                Account(
                    tenant_id=tenant_id,
                    company_id=company.id,
                    code=code,
                    name=row["name"].strip(),
                    account_type=mapped,
                    reconcile=_parse_bool(row.get("reconcile", "")),
                    extra={
                        "odoo_id": row.get("id"),
                        "odoo_account_type": odoo_type,
                        "tag_ids": row.get("tag_ids", ""),
                        "tax_ids": row.get("tax_ids", ""),
                        "non_trade": row.get("non_trade", ""),
                        "depreciation_model_id": row.get("depreciation_model_id", ""),
                        "description": row.get("description", ""),
                    },
                )
            )
            inserted += 1

    await session.flush()
    return inserted, skipped


async def load_au_tax_codes_for_company(
    session: AsyncSession, *, tenant_id: uuid.UUID, company_id: uuid.UUID
) -> int:
    """Tenant-aware variant of services.tax_codes.ensure_au_seed.

    Same canonical AU GST starter set, but with tenant_id explicitly set
    so the rows belong to the new tenant, not the default.
    """
    existing = await session.execute(
        select(TaxCode.code).where(
            TaxCode.company_id == company_id, TaxCode.archived_at.is_(None)
        )
    )
    have = {code for (code,) in existing.all()}
    inserted = 0
    for row in AU_TAX_SEED:
        if row["code"] in have:
            continue
        session.add(
            TaxCode(
                tenant_id=tenant_id,
                company_id=company_id,
                tax_system="GST",
                **row,
            )
        )
        inserted += 1
    await session.flush()
    return inserted


async def ensure_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    username: str,
    display_name: str,
    role: str,
    password: str,
) -> tuple[User, bool, str | None]:
    """Create the user if missing.  Returns (user, created, password-or-None).

    ``password`` is only echoed back when the row was newly created — for
    pre-existing rows we don't know the original plaintext, so the caller
    must mark the entry as "(existing)" in the credentials JSON.
    """
    email = f"{username}@{EMAIL_DOMAIN}"
    existing = await session.execute(select(User).where(User.username == username))
    row = existing.scalars().first()
    if row is not None:
        return row, False, None

    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        username=username,
        display_name=display_name,
        email=email,
        role=role,
        password_hash=hash_password(password),
        version=1,
    )
    session.add(user)
    await session.flush()
    return user, True, password


async def add_contacts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    industry: str,
) -> list[Contact]:
    """Create the per-industry contact roster for this tenant.

    Skips by (company_id, name).  Returns the full list (created + existing).
    """
    template = CONTACT_TEMPLATES.get(industry, CONTACT_TEMPLATES["consulting"])
    out: list[Contact] = []
    for name, ctype, state in template:
        existing = await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.name == name,
                Contact.archived_at.is_(None),
            )
        )
        row = existing.scalars().first()
        if row is not None:
            out.append(row)
            continue
        contact = await create_contact(
            session,
            company_id,
            tenant_id=tenant_id,
            actor="seed_critics",
            name=name,
            contact_type=ctype,
            email=None,
            phone=None,
            abn=_gen_abn(),
            state=state,
            country="Australia",
        )
        out.append(contact)
    return out


async def _pick_revenue_account(
    session: AsyncSession, *, company_id: uuid.UUID
) -> Account:
    """First non-header INCOME account for the company."""
    result = await session.execute(
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.account_type == AccountType.INCOME,
            Account.is_header.is_(False),
            Account.archived_at.is_(None),
        )
        .order_by(Account.code)
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        raise RuntimeError(f"No INCOME account found for company {company_id}")
    return row


async def _pick_expense_account(
    session: AsyncSession, *, company_id: uuid.UUID
) -> Account:
    """First non-header EXPENSE account."""
    result = await session.execute(
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.account_type == AccountType.EXPENSE,
            Account.is_header.is_(False),
            Account.archived_at.is_(None),
        )
        .order_by(Account.code)
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        raise RuntimeError(f"No EXPENSE account found for company {company_id}")
    return row


async def _pick_tax_code(
    session: AsyncSession, *, company_id: uuid.UUID, code: str = "GST"
) -> TaxCode | None:
    result = await session.execute(
        select(TaxCode).where(
            TaxCode.company_id == company_id,
            TaxCode.code == code,
            TaxCode.archived_at.is_(None),
        )
    )
    return result.scalars().first()


async def add_invoices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    contacts: list[Contact],
) -> int:
    """Create 3-5 draft invoices.  Skips if any draft already exists."""
    existing = await session.execute(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.DRAFT,
        )
    )
    if existing.scalars().first() is not None:
        return 0

    customers = [c for c in contacts if c.contact_type in (ContactType.CUSTOMER, ContactType.BOTH)]
    if not customers:
        return 0

    revenue_acct = await _pick_revenue_account(session, company_id=company_id)
    gst_code = await _pick_tax_code(session, company_id=company_id, code="GST")

    today = date.today()
    samples = [
        ("Professional services — March", Decimal("3"), Decimal("180.00")),
        ("Project delivery milestone", Decimal("1"), Decimal("4500.00")),
        ("Retainer — Q1 advisory", Decimal("1"), Decimal("2200.00")),
        ("Goods supplied (PO #1042)", Decimal("12"), Decimal("125.00")),
        ("Consultation hours", Decimal("8"), Decimal("220.00")),
    ]
    created = 0
    for i, (desc, qty, price) in enumerate(samples):
        contact = customers[i % len(customers)]
        issue = today - timedelta(days=15 - i * 3)
        due = issue + timedelta(days=30)
        invoice = await create_invoice_draft(
            session,
            company_id=company_id,
            contact_id=contact.id,
            issue_date=issue,
            due_date=due,
            notes=None,
            payment_terms="Net 30",
            currency="AUD",
            lines=[
                {
                    "description": desc,
                    "account_id": str(revenue_acct.id),
                    "tax_code_id": str(gst_code.id) if gst_code else None,
                    "quantity": str(qty),
                    "unit_price": str(price),
                    "discount_pct": "0",
                }
            ],
        )
        # The service uses default tenant_id; rewrite to our tenant.
        if invoice.tenant_id != tenant_id:
            invoice.tenant_id = tenant_id
            await session.flush()
        created += 1
    return created


async def add_bills(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    contacts: list[Contact],
) -> int:
    """Create 2-3 draft bills.  Skips if any draft already exists."""
    existing = await session.execute(
        select(Bill).where(
            Bill.company_id == company_id,
            Bill.status == BillStatus.DRAFT,
        )
    )
    if existing.scalars().first() is not None:
        return 0

    suppliers = [c for c in contacts if c.contact_type in (ContactType.SUPPLIER, ContactType.BOTH)]
    if not suppliers:
        return 0

    expense_acct = await _pick_expense_account(session, company_id=company_id)
    gst_code = await _pick_tax_code(session, company_id=company_id, code="GST")

    today = date.today()
    samples = [
        ("Office supplies — March", Decimal("1"), Decimal("420.55")),
        ("Internet & phone — March", Decimal("1"), Decimal("189.00")),
        ("Subscription software (annual)", Decimal("1"), Decimal("1100.00")),
    ]
    created = 0
    for i, (desc, qty, price) in enumerate(samples):
        supplier = suppliers[i % len(suppliers)]
        issue = today - timedelta(days=10 - i * 2)
        due = issue + timedelta(days=14)
        bill = await create_bill_draft(
            session,
            company_id=company_id,
            contact_id=supplier.id,
            issue_date=issue,
            due_date=due,
            supplier_reference=f"SUP-{1000 + i}",
            notes=None,
            currency="AUD",
            lines=[
                {
                    "description": desc,
                    "account_id": str(expense_acct.id),
                    "tax_code_id": str(gst_code.id) if gst_code else None,
                    "quantity": str(qty),
                    "unit_price": str(price),
                    "discount_pct": "0",
                }
            ],
        )
        if bill.tenant_id != tenant_id:
            bill.tenant_id = tenant_id
            await session.flush()
        created += 1
    return created


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def seed_one_tenant(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Seed a single tenant in its own session/transaction.

    Returns the list of credential rows for the tenant's users.  Re-raises
    any exception so the outer driver can catch + log + continue.
    """
    slug = spec["slug"]
    name = spec["name"]
    industry = spec.get("industry", "consulting")
    creds: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as session:
        try:
            # Tenant
            tenant, t_created = await ensure_tenant(session, slug, name)
            if t_created:
                logger.info("[%s] tenant created: %s", slug, tenant.id)
            else:
                logger.info("[%s] tenant exists: %s", slug, tenant.id)
                if tenant.id == DEFAULT_TENANT_ID:
                    raise RuntimeError(
                        f"Refusing to seed into the default tenant via slug '{slug}'"
                    )

            # Company
            company, c_created = await ensure_company(
                session,
                tenant_id=tenant.id,
                name=name,
                abn=_gen_abn(),
            )
            logger.info(
                "[%s] company %s: %s",
                slug,
                "created" if c_created else "exists",
                company.id,
            )

            # CoA
            ins, skip = await load_accounts_for_company(
                session, tenant_id=tenant.id, company=company
            )
            logger.info("[%s] accounts: %d new, %d existing", slug, ins, skip)

            # Tax codes
            tx_ins = await load_au_tax_codes_for_company(
                session, tenant_id=tenant.id, company_id=company.id
            )
            logger.info("[%s] tax codes: %d new", slug, tx_ins)

            # Users
            for username, display_name, role in spec["users"]:
                password = _gen_password()
                user, u_created, returned_pw = await ensure_user(
                    session,
                    tenant_id=tenant.id,
                    username=username,
                    display_name=display_name,
                    role=role,
                    password=password,
                )
                logger.info(
                    "[%s] user %s: %s (role=%s)",
                    slug,
                    "created" if u_created else "exists",
                    username,
                    role,
                )
                creds.append(
                    {
                        "tenant_slug": slug,
                        "tenant_name": name,
                        "username": username,
                        "email": user.email or f"{username}@{EMAIL_DOMAIN}",
                        "display_name": display_name,
                        "role": role,
                        "password": returned_pw if returned_pw is not None else "(existing — unknown)",
                        "login_url": LOGIN_URL,
                    }
                )

            # Contacts
            contacts = await add_contacts(
                session,
                tenant_id=tenant.id,
                company_id=company.id,
                industry=industry,
            )
            logger.info("[%s] contacts: %d total", slug, len(contacts))

            # Invoices
            inv_n = await add_invoices(
                session,
                tenant_id=tenant.id,
                company_id=company.id,
                contacts=contacts,
            )
            logger.info("[%s] draft invoices: %d new", slug, inv_n)

            # Bills
            bill_n = await add_bills(
                session,
                tenant_id=tenant.id,
                company_id=company.id,
                contacts=contacts,
            )
            logger.info("[%s] draft bills: %d new", slug, bill_n)

            await session.commit()
            return creds
        except Exception:
            await session.rollback()
            raise


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    started = datetime.now(UTC)
    print(f"[seed_critics] starting at {started.isoformat()}", file=sys.stderr)
    all_creds: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []

    for spec in TENANTS:
        slug = spec["slug"]
        try:
            creds = await seed_one_tenant(spec)
            all_creds.extend(creds)
        except Exception as exc:
            logger.exception("[%s] FAILED: %s", slug, exc)
            failures.append((slug, str(exc)))

    # Summary table to stderr -------------------------------------------------
    print("\n=== Seed summary ===", file=sys.stderr)
    header = f"{'tenant':24} {'username':22} {'role':12} {'password':14} {'login URL'}"
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)
    for row in all_creds:
        print(
            f"{row['tenant_slug']:24} {row['username']:22} {row['role']:12} "
            f"{row['password']:14} {row['login_url']}",
            file=sys.stderr,
        )
    if failures:
        print("\nFailures:", file=sys.stderr)
        for slug, err in failures:
            print(f"  {slug}: {err}", file=sys.stderr)
    print(
        f"\n[seed_critics] done — {len(all_creds)} users across "
        f"{len(TENANTS) - len(failures)}/{len(TENANTS)} tenants",
        file=sys.stderr,
    )

    # Machine-readable JSON to stdout ----------------------------------------
    json.dump({"critics": all_creds}, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
