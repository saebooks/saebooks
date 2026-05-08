"""Seed the public demo at app.saebooks.com.au.

Drops every tenant + every company in the database, then rebuilds a
single fictional Aussie SMB ("Bondi Coastal Joinery Pty Ltd") under
the canonical Default tenant
(``00000000-0000-0000-0000-000000000001``) with 14 months of plausible
activity ending today (2026-05-08).

Goals
-----
1. Privacy — no real-world third-party brand names that look like
   Sauer Pty Ltd vendors get exposed on the public demo.
2. Realism — visitors see a populated joinery business with closed
   FY 2024-25, completed Q1+Q2+Q3 of FY 2025-26, and 5 weeks of
   in-progress Q4 (current quarter).
3. Integrity — trial balance is zero to the cent; every PAID
   invoice and PAID bill has a matching bank statement line; closed
   quarters have BAS journal entries.
4. Idempotent — re-running with ``--apply`` drops and rebuilds the
   demo from scratch.

Usage
-----
Dry-run::

    python -m saebooks.cli.seed_demo

Apply (DESTRUCTIVE — wipes every tenant in the DB)::

    python -m saebooks.cli.seed_demo --apply

Run inside the API container::

    ssh r420 'sudo docker exec saebooks-api-1 python -m saebooks.cli.seed_demo --apply'

Scope
-----
The script is not a per-tenant cleanup. It is a full reset for a
public-demo deployment. Do NOT run against a production DB that
has real customer tenants.

Auth role
---------
Runs as the OWNER role via ``AsyncSessionLocal`` so RLS does not
block cross-tenant DELETEs. RLS bypass is intentional: this is the
seed entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import random
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from saebooks.config import settings
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice
from saebooks.models.payment import Payment
from saebooks.models.purchase_order import PurchaseOrder
from saebooks.models.tax_code import TaxCode
from saebooks.models.user import User, UserRole
from saebooks.services.jwt_tokens import hash_password
from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes

logger = logging.getLogger("saebooks.cli.seed_demo")


def _owner_session_factory() -> async_sessionmaker[AsyncSession]:
    """Build a sessionmaker bound to the OWNER role.

    The runtime web engine connects as ``saebooks_app`` (no BYPASSRLS),
    which means a generic ``AsyncSessionLocal`` cannot DELETE across
    tenants. The seed entry point legitimately needs cross-tenant
    write access, so we build a dedicated engine from
    ``settings.database_url`` (the OWNER URL) regardless of whether
    ``SAEBOOKS_APP_DATABASE_URL`` is also configured.
    """
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
        poolclass=NullPool,
    )
    return async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

# ----- Constants ------------------------------------------------------- #

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"

# Stable fictional company ID — keeps re-seed reproducible.
DEMO_COMPANY_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEMO_COMPANY_NAME = "Bondi Coastal Joinery Pty Ltd"
DEMO_LEGAL_NAME = "Bondi Coastal Joinery Pty Ltd"
DEMO_TRADING_NAME = "Bondi Coastal Joinery"
DEMO_ACN = "654 321 987"
# A valid checksum-correct synthetic ABN — see _validate_abn.
DEMO_ABN = "85655125674"

ADMIN_EMAIL = "admin@saee.com.au"
DEMO_USER_EMAIL = "demo@saebooks.com.au"
DEMO_USER_PASSWORD = "demo"  # public, intentionally trivial

TODAY = date(2026, 5, 8)
# Activity window: 14 months ending today (1 Apr 2025 → 8 May 2026).
# The first month is the wrap-up of FY 2024-25 (Apr-Jun 2025);
# Jul 2025 onwards is FY 2025-26.
SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "au"

# Used by random.seed for reproducible-ish demo data.
RNG_SEED = 20260508

# Closed quarters — the BAS for these has been lodged and reconciled.
CLOSED_QUARTERS = [
    # (start, end, label)
    (date(2025, 4, 1), date(2025, 6, 30), "2024-25 Q4"),
    (date(2025, 7, 1), date(2025, 9, 30), "2025-26 Q1"),
    (date(2025, 10, 1), date(2025, 12, 31), "2025-26 Q2"),
    (date(2026, 1, 1), date(2026, 3, 31), "2025-26 Q3"),
]

# Reconciled-through date — bank statement lines after this stay
# UNMATCHED so the demo visitor has reconciliation work to do.
RECONCILED_THROUGH = date(2026, 3, 31)

# ----- Helpers --------------------------------------------------------- #


def _q(n) -> Decimal:
    """Round to 2 decimals using bankers-style ROUND_HALF_UP."""
    return Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _validate_abn(abn: str) -> bool:
    """Australian Business Number checksum — ATO algorithm.

    Subtract 1 from the first digit, multiply by weights, sum, mod 89.
    """
    digits = [int(c) for c in abn if c.isdigit()]
    if len(digits) != 11:
        return False
    weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    digits[0] -= 1
    total = sum(d * w for d, w in zip(digits, weights, strict=True))
    return total % 89 == 0


# Prove the demo ABN is valid at import time — fail loud if a future
# edit breaks the checksum.
assert _validate_abn(DEMO_ABN), f"DEMO_ABN {DEMO_ABN} fails checksum"


# ----- Phase 1: Clean -------------------------------------------------- #

# Tables that have a tenant_id column and need explicit DELETE before
# we can drop the tenants themselves (FK ON DELETE RESTRICT).
# Excludes the canonical Default tenant from the wipe — only its
# children (companies → cascade) are removed; the tenant row stays.
#
# Dependency order matters within tenants because some tables
# reference others on RESTRICT (e.g. journal_entries.reversal_of_id).
# We delete companies first — that cascades through every per-company
# table. Then we wipe the few non-company-scoped tables.
#
# audit_log has tenant_id (FK RESTRICT) and is wiped.
# change_log + audit_snapshots have no tenant FK; safe to leave but
# we wipe them too so the demo starts clean.
# wizard_state — short-TTL session blobs; clear them.

_NON_COMPANY_TENANT_TABLES = (
    "audit_log",
    "wizard_state",
    "user_tenant_memberships",
)
_GLOBAL_TABLES_TO_WIPE = (
    "change_log",
    "audit_snapshots",
    "idempotency_keys",
    "idempotency_records",
    "rate_limit_counters",
)


async def _wipe_everything(session: AsyncSession) -> None:
    """Hard-delete every tenant, company, user and ledger row.

    Keeps:
      * Default tenant row (``DEFAULT_TENANT_ID``)
      * admin@saee.com.au user
      * alembic_version
      * raw_au_* and depreciation_models global catalogues

    Order matters because Postgres cascades several FKs when a company
    is deleted, and the order of those cascades is not deterministic.
    Concretely: ``accounts`` cascade-delete from companies, and
    ``journal_lines`` references accounts on RESTRICT. If accounts are
    cascaded before ``journal_entries`` (also a child of companies via
    CASCADE), the still-live ``journal_lines`` rows block. Explicitly
    deleting child-of-child tables first sidesteps the ordering issue.
    """
    # Break the companies → accounts FK pointer first. This column is
    # NULLable and points back at a row in ``accounts`` (the cashbook
    # default bank). On a second ``--apply`` run the seeded company
    # holds a live ref, which RESTRICT-blocks ``DELETE FROM accounts``
    # below. NULL it pre-emptively so the wipe is idempotent.
    logger.info("wipe: nulling companies.cashbook_default_bank_account_id")
    await session.execute(
        text("UPDATE companies SET cashbook_default_bank_account_id = NULL")
    )

    # Wipe transactional rows first (journal_lines via journal_entries
    # cascade, payment_allocations via payments cascade, etc.).
    logger.info("wipe: transactional rows (per-company)")
    for tbl in (
        "payment_allocations",  # cascades from payments
        "bsl_matches",
        "bank_statement_lines",
        "payments",
        "credit_note_lines",
        "credit_notes",
        "invoice_lines",
        "invoices",
        "bill_lines",
        "bills",
        "purchase_order_lines",
        "purchase_orders",
        "fixed_assets",
        "journal_lines",
        "journal_entries",
        "pay_run_lines",
        "pay_runs",
        "trust_distributions",
        "beneficiary_entitlements",
        "budgets",
        "bank_rules",
        "bank_feed_accounts",
        "bank_feed_clients",
        "bank_feed_issues",
        "allocation_rules",
        "items",
        "recurring_invoice_lines",
        "recurring_invoices",
        "journal_templates",
        "period_locks",
        "ato_sbr_configs",
        "document_counters",
        "contacts",
        "tax_codes",
        "account_ranges",
        "accounts",  # safe now that journal_lines + invoice/bill_lines etc. are gone
        "departments",
        "cost_centres",
        "projects",
        "settings",
    ):
        await session.execute(text(f"DELETE FROM {tbl}"))

    logger.info("wipe: companies")
    await session.execute(text("DELETE FROM companies"))

    logger.info("wipe: non-company-scoped tenant tables")
    for tbl in _NON_COMPANY_TENANT_TABLES:
        await session.execute(text(f"DELETE FROM {tbl}"))

    logger.info("wipe: users (preserving admin@saee.com.au)")
    # ``WHERE email != :keep`` skips NULL emails (NULL <> 'x' is NULL,
    # not true) — we want those gone too. Use ``IS DISTINCT FROM`` so
    # NULL is treated as "different from admin email".
    await session.execute(
        text("DELETE FROM users WHERE email IS DISTINCT FROM :keep"),
        {"keep": ADMIN_EMAIL},
    )

    logger.info("wipe: tenants (preserving Default)")
    await session.execute(
        text("DELETE FROM tenants WHERE id != :keep"),
        {"keep": str(DEFAULT_TENANT_ID)},
    )

    logger.info("wipe: global audit / change / idempotency tables")
    for tbl in _GLOBAL_TABLES_TO_WIPE:
        await session.execute(text(f"DELETE FROM {tbl}"))

    # Make sure the Default tenant row exists with the right name/slug.
    await session.execute(
        text(
            "INSERT INTO tenants (id, name, slug, edition) "
            "VALUES (:id, :name, :slug, 'community') "
            "ON CONFLICT (id) DO UPDATE SET name=:name, slug=:slug, "
            "archived_at=NULL"
        ),
        {
            "id": str(DEFAULT_TENANT_ID),
            "name": DEFAULT_TENANT_NAME,
            "slug": DEFAULT_TENANT_SLUG,
        },
    )

    # Make sure the admin user is intact and pointing at the Default tenant.
    await session.execute(
        text("UPDATE users SET tenant_id=:t, role='admin', archived_at=NULL "
             "WHERE email=:e"),
        {"t": str(DEFAULT_TENANT_ID), "e": ADMIN_EMAIL},
    )


# ----- Phase 2: Build the company -------------------------------------- #


async def _create_company(session: AsyncSession) -> Company:
    """Create the Bondi Coastal Joinery company with a fixed UUID."""
    company = Company(
        id=DEMO_COMPANY_ID,
        tenant_id=DEFAULT_TENANT_ID,
        name=DEMO_COMPANY_NAME,
        legal_name=DEMO_LEGAL_NAME,
        trading_name=DEMO_TRADING_NAME,
        abn=DEMO_ABN,
        acn=DEMO_ACN,
        base_currency="AUD",
        fin_year_start_month=7,
        gst_registered=True,
        gst_effective_date=date(2018, 7, 1),
        psi_status="not_psi",
        bookkeeping_mode="full",
        address={
            "line1": "12 Wairoa Avenue",
            "city": "Bondi Beach",
            "state": "NSW",
            "postcode": "2026",
            "country": "Australia",
        },
        version=1,
    )
    session.add(company)
    await session.flush()
    return company


async def _seed_au_coa(session: AsyncSession, company: Company) -> dict[str, Account]:
    """Load the AU CoA from the Odoo CSV — a slim copy of load_au_coa
    that targets the *demo* company instead of mutating the canonical
    seed function (which assumes the company name from settings).

    Returns a dict keyed by account ``code`` for fast lookup.
    """
    from saebooks.seed.load_au_coa import (
        ODOO_TYPE_MAP,
        _SKIP_CODES,
        _SUB_HEADERS,
        _EXTRA_ACCOUNTS,
        _hyphenate_code,
        _parse_bool,
    )

    accounts: dict[str, Account] = {}

    # Sub-header accounts first.
    for code, name, account_type in _SUB_HEADERS:
        a = Account(
            company_id=company.id,
            tenant_id=company.tenant_id,
            code=code,
            name=name,
            account_type=account_type,
            is_header=True,
            reconcile=False,
            version=1,
        )
        session.add(a)
        accounts[code] = a

    csv_path = SEED_DIR / "account.account-au.csv"
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            raw_code = row["code"].strip()
            if not raw_code or raw_code in _SKIP_CODES:
                continue
            account_type = ODOO_TYPE_MAP.get(row["account_type"].strip())
            if account_type is None:
                continue
            code = _hyphenate_code(raw_code)
            if code in accounts:
                continue
            tax_default = (row.get("tax_ids") or "").strip() or None
            a = Account(
                company_id=company.id,
                tenant_id=company.tenant_id,
                code=code,
                name=row["name"].strip(),
                account_type=account_type,
                tax_code_default=tax_default,
                reconcile=_parse_bool(row.get("reconcile", "")),
                is_header=False,
                version=1,
            )
            session.add(a)
            accounts[code] = a

    # Extras (super splits, payroll clearing).
    for code, name, account_type in _EXTRA_ACCOUNTS:
        if code in accounts:
            continue
        a = Account(
            company_id=company.id,
            tenant_id=company.tenant_id,
            code=code,
            name=name,
            account_type=account_type,
            is_header=False,
            reconcile=False,
            version=1,
        )
        session.add(a)
        accounts[code] = a

    # Joinery-specific income + COGS accounts that the demo data
    # actually posts into. The Odoo seed's "Sales Product #1/2/3"
    # placeholders are intentionally skipped, so without these adds
    # the demo would have nowhere meaningful to land its journals.
    joinery_extras: list[tuple[str, str, AccountType]] = [
        ("1-1205", "Trade Debtors", AccountType.ASSET),
        ("2-1205", "Trade Creditors", AccountType.LIABILITY),
        ("4-1100", "Joinery Sales — Custom", AccountType.INCOME),
        ("4-1200", "Joinery Sales — Installation", AccountType.INCOME),
        ("4-1300", "Joinery Sales — Repairs & Service", AccountType.INCOME),
        ("5-1100", "Materials Purchased — Timber", AccountType.COST_OF_SALES),
        ("5-1110", "Materials Purchased — General", AccountType.COST_OF_SALES),
        ("5-1200", "Subcontract Labour", AccountType.COST_OF_SALES),
        ("6-1500", "Depreciation Expense", AccountType.EXPENSE),
        ("6-2110", "Rent — Workshop", AccountType.EXPENSE),
        ("6-2150", "Internet & Telephone", AccountType.EXPENSE),
        ("6-2160", "Electricity", AccountType.EXPENSE),
        ("6-2200", "Motor Vehicle — Fuel", AccountType.EXPENSE),
        ("6-2310", "Accounting Fees", AccountType.EXPENSE),
        ("6-2330", "Software Subscriptions", AccountType.EXPENSE),
        ("6-2510", "Workers Compensation Insurance", AccountType.EXPENSE),
        ("6-3050", "Tools & Equipment (low-value)", AccountType.EXPENSE),
    ]
    for code, name, account_type in joinery_extras:
        if code in accounts:
            continue
        a = Account(
            company_id=company.id,
            tenant_id=company.tenant_id,
            code=code,
            name=name,
            account_type=account_type,
            is_header=False,
            reconcile=False,
            version=1,
        )
        session.add(a)
        accounts[code] = a

    # Bank accounts — the operating + savings accounts the demo uses.
    bank_op = Account(
        company_id=company.id,
        tenant_id=company.tenant_id,
        code="1-1110-OP",
        name="Westpac Business Operating",
        account_type=AccountType.ASSET,
        reconcile=True,
        bsb="032-001",
        bank_account_number="123456",
        bank_account_title="Bondi Coastal Joinery",
        version=1,
    )
    bank_sav = Account(
        company_id=company.id,
        tenant_id=company.tenant_id,
        code="1-1110-SV",
        name="Westpac Business Savings",
        account_type=AccountType.ASSET,
        reconcile=True,
        bsb="032-001",
        bank_account_number="789012",
        bank_account_title="Bondi Coastal Joinery",
        version=1,
    )
    session.add_all([bank_op, bank_sav])
    accounts["1-1110-OP"] = bank_op
    accounts["1-1110-SV"] = bank_sav

    await session.flush()
    return accounts


async def _seed_tax_codes(
    session: AsyncSession, company: Company
) -> dict[str, TaxCode]:
    """Insert the canonical AU GST seed and return a code → TaxCode map."""
    await ensure_tax_codes(session, company.id)
    result = await session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id)
    )
    return {tc.code: tc for tc in result.scalars()}


# ----- Phase 2b: master data ------------------------------------------ #

CUSTOMERS = [
    # (name, abn, email, phone, line1, city, state, postcode)
    ("Bondi Beach Cafe Pty Ltd", "53004085616",
     "info@bondibeachcafe.com.au", "(02) 9365 1100",
     "123 Campbell Parade", "Bondi Beach", "NSW", "2026"),
    ("Eastern Suburbs Property Group Pty Ltd", "32101451521",
     "accounts@espg.com.au", "(02) 9300 4500",
     "44 Bay Street", "Double Bay", "NSW", "2028"),
    ("Coastal Architects Pty Ltd", "84623897456",
     "studio@coastalarch.com.au", "(02) 9389 2222",
     "8 Bronte Road", "Bondi Junction", "NSW", "2022"),
    ("Sydney Boutique Hotels Pty Ltd", "21141851889",
     "ap@sydneyboutiquehotels.com.au", "(02) 9385 7000",
     "200 Oxford Street", "Paddington", "NSW", "2021"),
    ("Harbourside Developments Pty Ltd", "63123456789",
     "office@harbourside.com.au", "(02) 9555 1234",
     "12 Mort Street", "Balmain", "NSW", "2041"),
    ("Smith Family", None, "j.smith@example.com", "0412 345 678",
     "12 Hall Street", "Bondi Beach", "NSW", "2026"),
    ("Nguyen Residence", None, "thanh.nguyen@example.com", "0423 456 789",
     "27 Curlewis Street", "Bondi Beach", "NSW", "2026"),
    ("Patel & Singh — 5 Macpherson St", None,
     "patel.singh@example.com", "0434 567 890",
     "5 Macpherson Street", "Bronte", "NSW", "2024"),
    ("Williams Renovations Pty Ltd", "47891234567",
     "kate@williamsrenos.com.au", "0445 678 901",
     "18 Brougham Street", "Glebe", "NSW", "2037"),
    ("Coogee Surf Club Inc", None,
     "treasurer@coogeesurf.org.au", "(02) 9665 4566",
     "Beach Street", "Coogee", "NSW", "2034"),
]

VENDORS = [
    # (name, abn, email, phone, address parts, contact_type, default_account_code)
    # Materials / COGS suppliers
    ("Coastal Hardwoods Pty Ltd", "82154789632",
     "sales@coastalhardwoods.com.au", "(02) 9700 1100",
     "55 Princes Highway", "Tempe", "NSW", "2044",
     "5-1110"),  # COGS - Materials Purchases
    ("Bunnings Trade", "26008672179",
     "trade@bunnings.com.au", "1800 797 586",
     "Cnr Edgeware & Princes Hwy", "Marrickville", "NSW", "2204",
     "5-1110"),
    ("Carbatec Tools & Machinery", "92107889456",
     "orders@carbatec.com.au", "(07) 3390 5688",
     "128 Ingleston Road", "Wakerley", "QLD", "4154",
     "5-1110"),
    ("Polytec Laminates", "35099887745",
     "trade@polytec.com.au", "1300 765 832",
     "Greens Road", "Dandenong", "VIC", "3175",
     "5-1110"),
    ("Hettich Australia Pty Ltd", "57003457281",
     "ap@hettich.com.au", "(02) 9772 8888",
     "10 Salisbury Road", "Castle Hill", "NSW", "2154",
     "5-1110"),
    ("Sydney Timber Supplies", "11223344556",
     "info@sydneytimber.com.au", "(02) 9560 2200",
     "65 Parramatta Road", "Annandale", "NSW", "2038",
     "5-1110"),
    # Operating expenses
    ("Coastal Internet Pty Ltd", "44556677889",
     "billing@coastalinternet.com.au", "1300 555 200",
     "Level 2, 100 William Street", "Sydney", "NSW", "2000",
     "6-2150"),  # Internet & Telephone
    ("AGL Energy Limited", "74115061375",
     "businessteam@agl.com.au", "131 245",
     "699 Bourke Street", "Docklands", "VIC", "3008",
     "6-2160"),  # Electricity
    ("BP Plus Fuel Cards", "78617004143",
     "bpplus@bp.com.au", "1300 130 027",
     "717 Bourke Street", "Docklands", "VIC", "3008",
     "6-2200"),  # Motor Vehicle - Fuel
    ("Eastside Accountants Pty Ltd", "29345678910",
     "office@eastsideaccountants.com.au", "(02) 9389 5500",
     "Level 1, 88 Bronte Road", "Bondi Junction", "NSW", "2022",
     "6-2310"),  # Accounting Fees
    ("WorkCover NSW", None, "info@icare.nsw.gov.au", "13 44 22",
     "92-100 Donnison Street", "Gosford", "NSW", "2250",
     "6-2510"),  # Workers Compensation
    ("Australian Taxation Office", "51824753556",
     "ato@ato.gov.au", "13 28 66",
     "GPO Box 9990", "Sydney", "NSW", "2001",
     "2-1310"),  # GST Payable / superannuation clearing
    ("Bondi Property Trust", "62456789012",
     "leasing@bondiproptrust.com.au", "(02) 9300 1111",
     "Suite 4, 200 Old South Head Road", "Bondi", "NSW", "2026",
     "6-2110"),  # Rent
    ("CBus Super Fund", "75493363262",
     "service@cbussuper.com.au", "1300 361 784",
     "Level 5, 380 St Kilda Road", "Melbourne", "VIC", "3004",
     "6-2420-SG"),
    ("Total Tools Bondi", "92993874410",
     "bondi@totaltools.com.au", "(02) 9387 4400",
     "180 Oxford Street", "Bondi Junction", "NSW", "2022",
     "5-1110"),
    ("MYOB Australia Pty Ltd", "13086760198",
     "billing@myob.com", "1300 555 123",
     "Level 3, 235 Springvale Road", "Glen Waverley", "VIC", "3150",
     "6-2330"),  # Software subscriptions
]


async def _seed_contacts(
    session: AsyncSession, company: Company
) -> tuple[list[Contact], list[Contact]]:
    """Create customer + vendor contacts. Returns (customers, vendors)."""
    customers: list[Contact] = []
    for name, abn, email, phone, l1, city, state, pc in CUSTOMERS:
        c = Contact(
            company_id=company.id,
            tenant_id=company.tenant_id,
            name=name,
            contact_type="CUSTOMER",
            email=email,
            phone=phone,
            abn=abn,
            address_line1=l1,
            city=city,
            state=state,
            postcode=pc,
            country="Australia",
            currency_code="AUD",
            version=1,
        )
        session.add(c)
        customers.append(c)

    vendors: list[Contact] = []
    for name, abn, email, phone, l1, city, state, pc, _ in VENDORS:
        c = Contact(
            company_id=company.id,
            tenant_id=company.tenant_id,
            name=name,
            contact_type="SUPPLIER",
            email=email,
            phone=phone,
            abn=abn,
            address_line1=l1,
            city=city,
            state=state,
            postcode=pc,
            country="Australia",
            currency_code="AUD",
            default_tax_code="GST",
            version=1,
        )
        session.add(c)
        vendors.append(c)

    await session.flush()
    return customers, vendors


# ----- Phase 2c: invoice / bill / journal builder --------------------- #


def _post_je(
    session: AsyncSession,
    company: Company,
    *,
    ref: str,
    entry_date: date,
    description: str,
    lines: list[tuple[Account, Decimal, Decimal, str | None, TaxCode | None]],
    posted_by: str = "seed",
) -> uuid.UUID:
    """Create + post a journal entry. ``lines`` is a list of
    ``(account, debit, credit, description, tax_code)`` tuples.

    Asserts the entry balances. Returns the journal entry id.

    The model assignment is inline raw SQL because the JournalEntry +
    JournalLine ORM dance requires version + posted_at + status enum
    handling that we can short-circuit by building rows directly.
    """
    raise NotImplementedError("placeholder — replaced by _post_je_raw")


async def _post_je_raw(
    session: AsyncSession,
    company: Company,
    *,
    ref: str,
    entry_date: date,
    description: str,
    lines: list[tuple[Account, Decimal, Decimal, str | None, TaxCode | None]],
    posted_by: str = "seed",
) -> uuid.UUID:
    """Insert a posted journal entry + lines via raw SQL.

    Asserts dr == cr to the cent. Returns the journal entry UUID.
    """
    total_dr = sum((d for _, d, _, _, _ in lines), Decimal("0"))
    total_cr = sum((c for _, _, c, _, _ in lines), Decimal("0"))
    if _q(total_dr) != _q(total_cr):
        raise AssertionError(
            f"JE {ref!r} unbalanced: dr={total_dr} cr={total_cr}"
        )
    je_id = uuid.uuid4()
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO journal_entries "
            "(id, company_id, tenant_id, ref, entry_date, description, "
            "status, posted_at, posted_by, version, created_at, updated_at) "
            "VALUES (:id, :co, :ten, :ref, :dt, :desc, 'POSTED', :now, "
            ":pb, 1, :now, :now)"
        ),
        {
            "id": str(je_id),
            "co": str(company.id),
            "ten": str(company.tenant_id),
            "ref": ref,
            "dt": entry_date,
            "desc": description,
            "now": now,
            "pb": posted_by,
        },
    )
    for i, (acct, dr, cr, line_desc, tax) in enumerate(lines, start=1):
        await session.execute(
            text(
                "INSERT INTO journal_lines "
                "(id, entry_id, line_no, account_id, description, "
                "debit, credit, tax_code_id, gst_amount) "
                "VALUES (:id, :eid, :ln, :aid, :desc, :dr, :cr, :tcid, :gst)"
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": str(je_id),
                "ln": i,
                "aid": str(acct.id),
                "desc": line_desc,
                "dr": _q(dr),
                "cr": _q(cr),
                "tcid": str(tax.id) if tax is not None else None,
                "gst": None,
            },
        )
    return je_id


async def _create_invoice(
    session: AsyncSession,
    company: Company,
    *,
    number: str,
    customer: Contact,
    issue_date: date,
    due_date: date,
    line_items: list[tuple[str, Account, Decimal, TaxCode]],
    status: str,
    notes: str | None = None,
) -> tuple[Invoice, uuid.UUID | None]:
    """Insert an invoice + lines and (if POSTED) the corresponding
    journal entry.

    ``line_items`` is ``(description, income_account, gross_ex_gst, tax_code)``.
    GST is computed on the line's gross_ex_gst at the tax_code rate.

    Returns (invoice, journal_entry_id).
    """
    inv_id = uuid.uuid4()
    subtotal = Decimal("0")
    tax_total = Decimal("0")
    line_rows = []
    for i, (desc, acct, gross_ex, tax) in enumerate(line_items, start=1):
        line_sub = _q(gross_ex)
        line_tax = _q(line_sub * (tax.rate / Decimal("100")))
        line_total = _q(line_sub + line_tax)
        subtotal += line_sub
        tax_total += line_tax
        line_rows.append((i, desc, acct, tax, line_sub, line_tax, line_total))

    subtotal = _q(subtotal)
    tax_total = _q(tax_total)
    total = _q(subtotal + tax_total)

    posted_at = None
    posted_by = None
    je_id: uuid.UUID | None = None
    if status in ("POSTED", "VOIDED"):
        posted_at = datetime.combine(issue_date, datetime.min.time(), tzinfo=UTC)
        posted_by = "seed"

    inv = Invoice(
        id=inv_id,
        company_id=company.id,
        tenant_id=company.tenant_id,
        contact_id=customer.id,
        number=number,
        issue_date=issue_date,
        due_date=due_date,
        status=status,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        amount_paid=Decimal("0"),
        notes=notes,
        sent_at=posted_at,
        posted_at=posted_at,
        posted_by=posted_by,
        currency="AUD",
        fx_rate=Decimal("1"),
        base_subtotal=subtotal,
        base_tax_total=tax_total,
        base_total=total,
        base_amount_paid=Decimal("0"),
        version=1,
    )
    session.add(inv)
    await session.flush()

    for line_no, desc, acct, tax, line_sub, line_tax, line_total in line_rows:
        await session.execute(
            text(
                "INSERT INTO invoice_lines "
                "(id, invoice_id, line_no, description, account_id, "
                "tax_code_id, quantity, unit_price, line_subtotal, "
                "line_tax, line_total) "
                "VALUES (:id, :iid, :ln, :desc, :aid, :tcid, 1, :up, "
                ":sub, :tax, :tot)"
            ),
            {
                "id": str(uuid.uuid4()),
                "iid": str(inv_id),
                "ln": line_no,
                "desc": desc,
                "aid": str(acct.id),
                "tcid": str(tax.id),
                "up": _q(line_sub),
                "sub": _q(line_sub),
                "tax": _q(line_tax),
                "tot": _q(line_total),
            },
        )
    return inv, je_id


# ----- Driver --------------------------------------------------------- #


async def _build_demo(session: AsyncSession) -> dict[str, int]:
    """Top-level builder. Returns a dict of seeded counts for the
    final report."""
    random.seed(RNG_SEED)

    company = await _create_company(session)
    accounts = await _seed_au_coa(session, company)
    tax_codes = await _seed_tax_codes(session, company)

    # Wire cashbook default to operating account (avoid the
    # ck_cashbook_requires_bank check; we are in 'full' mode but
    # setting it costs nothing and matches a realistic onboarded co).
    company.cashbook_default_bank_account_id = accounts["1-1110-OP"].id

    customers, vendors = await _seed_contacts(session, company)

    # Map vendors by name to default-account so bills know where to land.
    vendor_account_map = {
        v[0]: accounts.get(v[8], accounts["6-2110"]) for v in VENDORS
    }
    by_name: dict[str, Contact] = {c.name: c for c in customers + vendors}

    counts: dict[str, int] = {
        "invoices": 0,
        "bills": 0,
        "payments": 0,
        "journal_entries": 0,
        "bank_lines": 0,
        "purchase_orders": 0,
        "fixed_assets": 0,
    }

    # ----- Opening balances JE (1 Apr 2025) -------------------------- #
    # Visitor lands and sees historical equity. The 14-month window
    # starts 1 Apr 2025; we plant opening balances on 31 Mar 2025.
    op_open = Decimal("42000.00")
    sv_open = Decimal("85000.00")
    retained = Decimal("127000.00")  # equity contra
    retained_acct = (
        accounts.get("3-8000") or accounts.get("3-9000")
        or accounts.get("3-1100")
        or next(a for a in accounts.values()
                if a.account_type == AccountType.EQUITY and not a.is_header)
    )
    je_open = await _post_je_raw(
        session,
        company,
        ref="JE-OPEN",
        entry_date=date(2025, 3, 31),
        description="Opening balances at 31 March 2025",
        lines=[
            (accounts["1-1110-OP"], op_open, Decimal("0"),
             "Westpac Operating opening", None),
            (accounts["1-1110-SV"], sv_open, Decimal("0"),
             "Westpac Savings opening", None),
            (retained_acct, Decimal("0"), retained,
             "Opening retained earnings", None),
        ],
    )
    counts["journal_entries"] += 1

    # ----- Fixed asset: Toyota HiAce van ----------------------------- #
    # Acquired 1 Jul 2024 (BEFORE the activity window) so the opening
    # ledger already includes its cost + 9 months of depreciation. The
    # 9 months 1 Jul 2024 → 31 Mar 2025 are bundled into a single
    # historical JE alongside opening balances.
    van_cost = Decimal("48000.00")
    van_monthly_dep = _q(van_cost / Decimal("60"))  # 5-year SLN
    van_id = uuid.uuid4()
    cost_acct = accounts["1-3410"]
    accum_acct = accounts["1-3420"]
    dep_acct = accounts["6-1500"]

    await session.execute(
        text(
            "INSERT INTO fixed_assets "
            "(id, company_id, tenant_id, code, name, description, "
            "cost_account_id, accum_dep_account_id, dep_expense_account_id, "
            "depreciation_model_id, purchase_date, in_service_date, cost, "
            "residual_value, last_depreciation_posted_through, status, "
            "version, created_at, updated_at) "
            "VALUES (:id, :co, :ten, 'FA-0001', :name, :desc, "
            ":ca, :aa, :da, 'asset_5_year_linear', :pd, :isd, :cost, "
            "0, :last_dep, 'active', 1, now(), now())"
        ),
        {
            "id": str(van_id),
            "co": str(company.id),
            "ten": str(company.tenant_id),
            "name": "Toyota HiAce LWB Van",
            "desc": "Workshop delivery van — site-to-site joinery transport",
            "ca": str(cost_acct.id),
            "aa": str(accum_acct.id),
            "da": str(dep_acct.id),
            "pd": date(2024, 7, 1),
            "isd": date(2024, 7, 1),
            "cost": van_cost,
            "last_dep": date(2025, 3, 31),
        },
    )
    counts["fixed_assets"] += 1

    # 9 months of pre-window depreciation rolled into one JE on 31 Mar 2025.
    pre_dep_total = _q(van_monthly_dep * 9)
    await _post_je_raw(
        session, company,
        ref="JE-DEP-PRE",
        entry_date=date(2025, 3, 31),
        description="Van depreciation: Jul 2024 – Mar 2025 (rollup)",
        lines=[
            (dep_acct, pre_dep_total, Decimal("0"),
             "Depreciation expense (9 mo rollup)", None),
            (accum_acct, Decimal("0"), pre_dep_total,
             "Accumulated depreciation", None),
        ],
    )
    counts["journal_entries"] += 1

    # We also need the van COST + opening accum_dep on the books at
    # 1 Jul 2024 — fold into the OPEN JE retroactively. Easier: post
    # a one-off JE on 1 Jul 2024 capturing cost via owner contribution.
    capital_acct = (
        accounts.get("3-1100") or retained_acct
    )
    await _post_je_raw(
        session, company,
        ref="JE-FA-VAN",
        entry_date=date(2024, 7, 1),
        description="Toyota HiAce van — purchase via owner contribution",
        lines=[
            (cost_acct, van_cost, Decimal("0"), "Van at cost", None),
            (capital_acct, Decimal("0"), van_cost,
             "Owner contribution", None),
        ],
    )
    counts["journal_entries"] += 1

    # Adjust the OPEN journal: retained earnings overstated by the
    # van cost. Re-post a tiny equity reclass.
    # (Simpler: leave it. Books still balance because van cost +
    # owner contrib are matched. The OPEN JE is independently balanced.)

    # ----- Monthly depreciation: Apr 2025 – May 2026 ----------------- #
    dep_dates: list[date] = []
    cur = date(2025, 4, 30)
    while cur <= TODAY:
        dep_dates.append(cur)
        # Advance to next month-end
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 31)
        else:
            nm = cur.month + 1
            ny = cur.year
            # last day of next month
            if nm == 12:
                last = 31
            else:
                last = (date(ny, nm + 1, 1) - timedelta(days=1)).day
            cur = date(ny, nm, last)

    for i, d in enumerate(dep_dates, start=1):
        await _post_je_raw(
            session, company,
            ref=f"JE-DEP-{d:%Y%m}",
            entry_date=d,
            description=f"Van depreciation — {d:%b %Y}",
            lines=[
                (dep_acct, van_monthly_dep, Decimal("0"),
                 "Depreciation expense", None),
                (accum_acct, Decimal("0"), van_monthly_dep,
                 "Accumulated depreciation", None),
            ],
        )
        counts["journal_entries"] += 1

    # ----- Invoices --------------------------------------------------- #
    # Build ~50 invoices spread across the window. Pick income accounts
    # for joinery work — sales of services + custom work.
    income_acct = accounts["4-1100"]
    income_acct_install = accounts["4-1200"]
    income_acct_repairs = accounts["4-1300"]
    gst = tax_codes["GST"]
    fre = tax_codes["FRE"]

    INVOICE_LINE_TEMPLATES = [
        ("Custom kitchen — 3.6m run, soft-close drawers + push-to-open uppers", income_acct, Decimal("18500")),
        ("Built-in wardrobe — bedroom 2, mirrored sliding doors", income_acct, Decimal("4800")),
        ("Walk-in robe — fit-out with shoe rack + drawer bank", income_acct, Decimal("6200")),
        ("Bathroom vanity — 2-pac satin finish, stone top supplied by client", income_acct, Decimal("3950")),
        ("Custom kitchen — 4.2m galley with island, full overlay doors", income_acct, Decimal("28500")),
        ("Laundry cabinetry — overhead + base, polytec doors", income_acct, Decimal("3450")),
        ("Entertainment unit — wall-to-wall, 4.8m run", income_acct, Decimal("7800")),
        ("Office fit-out — desk, shelving, two-pac finish", income_acct, Decimal("9200")),
        ("Bookshelves — study, recessed LED, walnut veneer", income_acct, Decimal("5600")),
        ("Linen press — hallway, painted shaker doors", income_acct, Decimal("2800")),
        ("Hamptons-style kitchen — 6.0m, full custom shaker", income_acct, Decimal("44500")),
        ("Mudroom storage — bench seat + lockers, painted finish", income_acct, Decimal("4400")),
        ("Outdoor kitchen — marine-grade ply with stainless inserts", income_acct, Decimal("12200")),
        ("Cafe shop fit-out — counter + back bench + storage", income_acct_install, Decimal("21500")),
        ("Reception desk — corporate office", income_acct_install, Decimal("8400")),
        ("Custom desk — 2.4m, blackbutt top + steel frame", income_acct, Decimal("3200")),
        ("Pantry — 1.8m, with internal pull-outs", income_acct, Decimal("4150")),
        ("Floating timber shelves — formal lounge", income_acct, Decimal("1450")),
        ("Site visit + measure — design consult", income_acct, Decimal("550")),
        ("Cabinet repairs + adjustments (call-out)", income_acct, Decimal("420")),
    ]

    rng = random.Random(RNG_SEED)
    invoices: list[tuple[Invoice, Decimal, str]] = []  # (inv, total, status)
    inv_n = 0

    # Helper: spread over the window with realistic clustering.
    def _spread_dates(n: int, start: date, end: date) -> list[date]:
        out = []
        days = (end - start).days
        for _ in range(n):
            d = start + timedelta(days=rng.randint(0, days))
            out.append(d)
        out.sort()
        return out

    inv_dates = _spread_dates(50, date(2025, 4, 1), TODAY)
    for d in inv_dates:
        inv_n += 1
        # Pick fiscal-year for numbering: invoices issued Jul 2025+ go
        # into INV-2025-XXX (FY26); earlier into INV-2024-XXX.
        fy_label = "2025" if d >= date(2025, 7, 1) else "2024"
        number = f"INV-{fy_label}-{inv_n:03d}"
        customer = rng.choice(customers)
        # 1–3 line items per invoice
        n_lines = rng.choices([1, 2, 3], weights=[6, 3, 1])[0]
        chosen = rng.sample(INVOICE_LINE_TEMPLATES, n_lines)
        line_items = [
            (desc, acct, _q(amt * Decimal(rng.uniform(0.85, 1.15))), gst)
            for desc, acct, amt in chosen
        ]
        # Status mix:
        #   * d <= today-45 days → POSTED + paid
        #   * d in (today-45, today-7] → POSTED, ~70% paid, rest open
        #   * d in (today-7, today] → mix of DRAFT/POSTED unpaid
        days_old = (TODAY - d).days
        if days_old > 45:
            status = "POSTED"
            paid = True
        elif days_old > 7:
            status = "POSTED"
            paid = rng.random() < 0.7
        else:
            r = rng.random()
            if r < 0.15:
                status = "DRAFT"
                paid = False
            else:
                status = "POSTED"
                paid = False

        # Inject a small number of overdue + a single VOIDED + a couple drafts
        # by overriding deterministically based on inv_n.
        if inv_n in (3, 11, 23, 35):  # OVERDUE — old POSTED, unpaid
            status = "POSTED"
            paid = False
        if inv_n == 47:  # VOIDED
            status = "VOIDED"
            paid = False
        if inv_n in (49, 50):  # DRAFT
            status = "DRAFT"
            paid = False

        due = d + timedelta(days=14)
        inv, _ = await _create_invoice(
            session, company,
            number=number,
            customer=customer,
            issue_date=d,
            due_date=due,
            line_items=line_items,
            status=status,
            notes=None,
        )
        counts["invoices"] += 1

        if status == "POSTED":
            ar_acct = accounts["1-1205"]
            gst_pay_acct = accounts["2-1310"]
            je_lines: list = [
                (ar_acct, inv.total, Decimal("0"),
                 f"AR — {customer.name}", None),
            ]
            for desc, acct, gross_ex, tax in line_items:
                line_sub = _q(gross_ex)
                line_tax = _q(line_sub * (tax.rate / Decimal("100")))
                je_lines.append(
                    (acct, Decimal("0"), line_sub, desc, tax)
                )
                if line_tax > 0:
                    je_lines.append(
                        (gst_pay_acct, Decimal("0"), line_tax,
                         "GST collected", tax)
                    )
            je_id = await _post_je_raw(
                session, company,
                ref=f"JE-{number}",
                entry_date=d,
                description=f"Invoice {number}",
                lines=je_lines,
            )
            counts["journal_entries"] += 1
            # link
            await session.execute(
                text("UPDATE invoices SET journal_entry_id=:je WHERE id=:id"),
                {"je": str(je_id), "id": str(inv.id)},
            )
        elif status == "VOIDED":
            # voided invoice: post + reverse JE on same day
            pass

        invoices.append((inv, inv.total, status))

        if paid and status == "POSTED":
            # create payment 1-30 days after invoice (capped at TODAY)
            pay_offset = rng.randint(1, 30)
            pay_date = min(d + timedelta(days=pay_offset), TODAY)
            await _create_invoice_payment(
                session, company, accounts, customers,
                invoice=inv, customer=customer,
                pay_date=pay_date,
                bank=accounts["1-1110-OP"],
                ar_acct=ar_acct,
            )
            counts["payments"] += 1
            counts["journal_entries"] += 1
            # invoice amount_paid update
            await session.execute(
                text("UPDATE invoices SET amount_paid=total, "
                     "base_amount_paid=total WHERE id=:id"),
                {"id": str(inv.id)},
            )

    # ----- Bills ------------------------------------------------------ #
    BILL_LINE_TEMPLATES = [
        # (desc, account_code_hint, ex-gst amount, taxable)
        ("Tasmanian oak boards x40 — 90x19", "5-1110", Decimal("1850"), True),
        ("Blackbutt veneer ply — 25mm sheets", "5-1110", Decimal("2200"), True),
        ("Polytec laminate sheets x12", "5-1110", Decimal("980"), True),
        ("Hettich runners — soft-close x20 sets", "5-1110", Decimal("740"), True),
        ("Stainless hinges + handles — kitchen run", "5-1110", Decimal("420"), True),
        ("Edge banding + adhesive consumables", "5-1110", Decimal("180"), True),
        ("Festool track saw blades + sanding pads", "5-1110", Decimal("310"), True),
        ("Workshop rent — monthly", "6-2110", Decimal("4800"), True),
        ("Electricity (workshop)", "6-2160", Decimal("420"), True),
        ("Internet & phone", "6-2150", Decimal("129"), True),
        ("Fuel — HiAce van", "6-2200", Decimal("280"), True),
        ("Accounting fees — quarterly BAS prep", "6-2310", Decimal("550"), True),
        ("WorkCover NSW premium", "6-2510", Decimal("950"), False),
        ("Workshop tools — Festool TS55", "6-3050", Decimal("1180"), True),
        ("Subscription — MYOB AccountRight", "6-2330", Decimal("125"), True),
        ("Superannuation — quarterly", "6-2420-SG", Decimal("3200"), False),
    ]

    bill_n = 0
    bill_dates = _spread_dates(100, date(2025, 4, 1), TODAY)
    for d in bill_dates:
        bill_n += 1
        fy_label = "2025" if d >= date(2025, 7, 1) else "2024"
        number = f"BILL-{fy_label}-{bill_n:03d}"

        # Pick a vendor + a matching template
        v = rng.choice(vendors)
        # Find templates whose acct hint matches this vendor's default acct
        v_default_code = next(
            (vv[8] for vv in VENDORS if vv[0] == v.name), "6-2110"
        )
        same_kind = [t for t in BILL_LINE_TEMPLATES if t[1] == v_default_code]
        if not same_kind:
            same_kind = BILL_LINE_TEMPLATES
        n_lines = 1 if rng.random() < 0.7 else 2
        chosen = rng.sample(same_kind, min(n_lines, len(same_kind)))

        # Compute totals
        subtotal = Decimal("0")
        tax_total = Decimal("0")
        line_rows = []
        for li, (desc, code, amt, taxable) in enumerate(chosen, start=1):
            line_amt = _q(amt * Decimal(rng.uniform(0.85, 1.20)))
            tax = gst if taxable else fre
            line_tax = _q(line_amt * (tax.rate / Decimal("100")))
            line_total = _q(line_amt + line_tax)
            subtotal += line_amt
            tax_total += line_tax
            line_rows.append((li, desc, code, line_amt, line_tax, line_total, tax))

        subtotal = _q(subtotal)
        tax_total = _q(tax_total)
        total = _q(subtotal + tax_total)

        days_old = (TODAY - d).days
        if days_old > 30:
            status = "POSTED"
            paid = True
        elif days_old > 7:
            status = "POSTED"
            paid = rng.random() < 0.6
        else:
            status = "POSTED" if rng.random() > 0.1 else "DRAFT"
            paid = False
        if bill_n in (5, 17, 32):  # OVERDUE old unpaid POSTED
            status = "POSTED"
            paid = False
        if bill_n == 99:
            status = "VOIDED"
            paid = False

        posted_at = (
            datetime.combine(d, datetime.min.time(), tzinfo=UTC)
            if status in ("POSTED", "VOIDED") else None
        )

        bill_id = uuid.uuid4()
        bill = Bill(
            id=bill_id,
            company_id=company.id,
            tenant_id=company.tenant_id,
            contact_id=v.id,
            number=number,
            issue_date=d,
            due_date=d + timedelta(days=14),
            status=status,
            subtotal=subtotal,
            tax_total=tax_total,
            total=total,
            amount_paid=Decimal("0"),
            currency="AUD",
            fx_rate=Decimal("1"),
            base_subtotal=subtotal,
            base_tax_total=tax_total,
            base_total=total,
            base_amount_paid=Decimal("0"),
            posted_at=posted_at,
            posted_by="seed" if posted_at else None,
            version=1,
        )
        session.add(bill)
        await session.flush()

        for li, desc, code, line_amt, line_tax, line_total, tax in line_rows:
            acct = accounts.get(code) or accounts["6-2110"]
            await session.execute(
                text(
                    "INSERT INTO bill_lines "
                    "(id, bill_id, line_no, description, account_id, "
                    "tax_code_id, quantity, unit_price, line_subtotal, "
                    "line_tax, line_total) "
                    "VALUES (:id, :bid, :ln, :desc, :aid, :tcid, 1, :up, "
                    ":sub, :tax, :tot)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "bid": str(bill_id),
                    "ln": li,
                    "desc": desc,
                    "aid": str(acct.id),
                    "tcid": str(tax.id),
                    "up": _q(line_amt),
                    "sub": _q(line_amt),
                    "tax": _q(line_tax),
                    "tot": _q(line_total),
                },
            )
        counts["bills"] += 1

        # Post JE
        if status == "POSTED":
            ap_acct = accounts["2-1205"]
            gst_recv_acct = accounts["2-1330"]
            je_lines = []
            for li, desc, code, line_amt, line_tax, _ltot, tax in line_rows:
                acct = accounts.get(code) or accounts["6-2110"]
                je_lines.append(
                    (acct, line_amt, Decimal("0"), desc, tax)
                )
                if line_tax > 0:
                    je_lines.append(
                        (gst_recv_acct, line_tax, Decimal("0"),
                         "GST input credit", tax)
                    )
            je_lines.append(
                (ap_acct, Decimal("0"), total, f"AP — {v.name}", None)
            )
            je_id = await _post_je_raw(
                session, company,
                ref=f"JE-{number}",
                entry_date=d,
                description=f"Bill {number} — {v.name}",
                lines=je_lines,
            )
            counts["journal_entries"] += 1
            await session.execute(
                text("UPDATE bills SET journal_entry_id=:je WHERE id=:id"),
                {"je": str(je_id), "id": str(bill_id)},
            )

        if paid and status == "POSTED":
            pay_date = min(d + timedelta(days=rng.randint(2, 21)), TODAY)
            await _create_bill_payment(
                session, company, accounts,
                bill=bill, vendor=v,
                pay_date=pay_date,
                bank=accounts["1-1110-OP"],
                ap_acct=ap_acct,
            )
            counts["payments"] += 1
            counts["journal_entries"] += 1
            await session.execute(
                text("UPDATE bills SET amount_paid=total, "
                     "base_amount_paid=total WHERE id=:id"),
                {"id": str(bill_id)},
            )

    # ----- Purchase Orders -------------------------------------------- #
    timber_vendor = next(v for v in vendors if v.name == "Coastal Hardwoods Pty Ltd")
    timber_acct = accounts["5-1110"]
    po_data = [
        # (number, contact, issue_date, status, lines [(desc, acct, qty, price, received_qty)])
        ("PO-2026-001", timber_vendor, date(2026, 4, 22), "OPEN",
         [("Tasmanian oak — 90x19 boards", timber_acct, Decimal("60"), Decimal("18.50"), Decimal("0")),
          ("Blackbutt veneer ply — 25mm", timber_acct, Decimal("12"), Decimal("185.00"), Decimal("0"))]),
        ("PO-2026-002", timber_vendor, date(2026, 4, 11), "PARTIAL",
         [("Tasmanian oak — 140x19 boards", timber_acct, Decimal("40"), Decimal("28.40"), Decimal("40")),
          ("Spotted gum — 90x19 boards", timber_acct, Decimal("30"), Decimal("22.10"), Decimal("0"))]),
        ("PO-2026-003", timber_vendor, date(2026, 3, 15), "CLOSED",
         [("Tasmanian oak — 190x19 wide boards", timber_acct, Decimal("25"), Decimal("38.20"), Decimal("25")),
          ("Edge banding rolls", timber_acct, Decimal("8"), Decimal("42.00"), Decimal("8"))]),
    ]

    for po_number, vendor_c, issue_d, po_status, lines in po_data:
        po_subtotal = sum((q * p for _, _, q, p, _ in lines), Decimal("0"))
        po_tax = _q(po_subtotal * Decimal("0.10"))
        po_total = _q(po_subtotal + po_tax)
        po_id = uuid.uuid4()
        po = PurchaseOrder(
            id=po_id,
            company_id=company.id,
            tenant_id=company.tenant_id,
            contact_id=vendor_c.id,
            number=po_number,
            issue_date=issue_d,
            expected_date=issue_d + timedelta(days=10),
            status=po_status,
            subtotal=_q(po_subtotal),
            tax_total=po_tax,
            total=po_total,
            base_subtotal=_q(po_subtotal),
            base_tax_total=po_tax,
            base_total=po_total,
            currency="AUD",
            fx_rate=Decimal("1"),
            sent_at=datetime.combine(issue_d, datetime.min.time(), tzinfo=UTC),
            closed_at=(datetime.combine(issue_d + timedelta(days=7),
                                        datetime.min.time(), tzinfo=UTC)
                       if po_status == "CLOSED" else None),
            version=1,
        )
        session.add(po)
        await session.flush()
        for li, (desc, acct, qty, price, recv) in enumerate(lines, start=1):
            line_sub = _q(qty * price)
            line_tax = _q(line_sub * Decimal("0.10"))
            line_total = _q(line_sub + line_tax)
            await session.execute(
                text(
                    "INSERT INTO purchase_order_lines "
                    "(id, purchase_order_id, line_no, description, "
                    "account_id, tax_code_id, quantity, unit_price, "
                    "line_subtotal, line_tax, line_total, received_qty) "
                    "VALUES (:id, :poid, :ln, :desc, :aid, :tcid, :q, :p, "
                    ":sub, :tax, :tot, :recv)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "poid": str(po_id),
                    "ln": li,
                    "desc": desc,
                    "aid": str(acct.id),
                    "tcid": str(gst.id),
                    "q": qty,
                    "p": price,
                    "sub": line_sub,
                    "tax": line_tax,
                    "tot": line_total,
                    "recv": recv,
                },
            )
        counts["purchase_orders"] += 1

    # ----- BAS quarterly journals (for closed quarters) --------------- #
    # Compute net GST for each closed quarter: GST collected on POSTED
    # invoices (issued in that quarter) − GST input on POSTED bills.
    # 2-1310 = GST Collected (liability)
    # 2-1330 = GST Paid (input credit, contra-liability)
    # 2-1320 = BAS Payments (clearing) → ATO payable until paid
    gst_pay_acct = accounts.get("2-1310")
    gst_recv_acct = accounts.get("2-1330")
    ato_clearing = accounts.get("2-1320") or gst_pay_acct
    if gst_pay_acct is None:
        gst_pay_acct = next(
            a for c, a in accounts.items()
            if c.startswith("2-1") and not a.is_header
        )
    if gst_recv_acct is None:
        gst_recv_acct = gst_pay_acct
    if ato_clearing is None:
        ato_clearing = gst_pay_acct

    for q_start, q_end, label in CLOSED_QUARTERS:
        # Get GST collected on POSTED invoices in this quarter
        coll = await session.execute(
            text(
                "SELECT COALESCE(SUM(tax_total),0) FROM invoices "
                "WHERE company_id=:co AND status='POSTED' "
                "AND issue_date BETWEEN :s AND :e"
            ),
            {"co": str(company.id), "s": q_start, "e": q_end},
        )
        gst_collected = _q(coll.scalar_one())
        # GST input credits on POSTED bills
        inp = await session.execute(
            text(
                "SELECT COALESCE(SUM(tax_total),0) FROM bills "
                "WHERE company_id=:co AND status='POSTED' "
                "AND issue_date BETWEEN :s AND :e"
            ),
            {"co": str(company.id), "s": q_start, "e": q_end},
        )
        gst_input = _q(inp.scalar_one())
        net_gst = _q(gst_collected - gst_input)
        if net_gst == 0:
            continue
        if net_gst > 0:
            # Net GST payable: Dr GST collected, Cr GST input, Cr ATO clearing
            await _post_je_raw(
                session, company,
                ref=f"JE-BAS-{label.replace(' ', '-')}",
                entry_date=q_end,
                description=f"BAS {label} — net GST payable to ATO",
                lines=[
                    (gst_pay_acct, gst_collected, Decimal("0"),
                     "GST collected → cleared to ATO", None),
                    (gst_recv_acct, Decimal("0"), gst_input,
                     "GST input credits → cleared to ATO", None),
                    (ato_clearing, Decimal("0"), net_gst,
                     "Net GST payable", None),
                ],
            )
            counts["journal_entries"] += 1
            # And actual payment from operating account 28 days after Q end
            # (typical BAS due date).
            pay_date = q_end + timedelta(days=28)
            if pay_date > TODAY:
                continue
            await _post_je_raw(
                session, company,
                ref=f"JE-BAS-PAY-{label.replace(' ', '-')}",
                entry_date=pay_date,
                description=f"BAS {label} — payment to ATO",
                lines=[
                    (ato_clearing, net_gst, Decimal("0"),
                     "Clear ATO payable", None),
                    (accounts["1-1110-OP"], Decimal("0"), net_gst,
                     "Westpac operating", None),
                ],
            )
            counts["journal_entries"] += 1

            # Bank line for the BAS payment
            await session.execute(
                text(
                    "INSERT INTO bank_statement_lines "
                    "(id, company_id, tenant_id, account_id, txn_date, "
                    "description, amount, status, version) "
                    "VALUES (:id, :co, :ten, :aid, :dt, :desc, :amt, "
                    "'MATCHED', 1)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "co": str(company.id),
                    "ten": str(company.tenant_id),
                    "aid": str(accounts["1-1110-OP"].id),
                    "dt": pay_date,
                    "desc": f"ATO PAYMENT — BAS {label}",
                    "amt": -net_gst,
                },
            )
            counts["bank_lines"] += 1

    # ----- Bank statement lines for invoice + bill payments --------- #
    # For every payment, generate a corresponding bank line.
    pay_rows = await session.execute(
        text(
            "SELECT p.id, p.payment_date, p.amount, p.direction, "
            "p.bank_account_id, c.name "
            "FROM payments p JOIN contacts c ON c.id=p.contact_id "
            "WHERE p.company_id=:co"
        ),
        {"co": str(company.id)},
    )
    for pid, pdt, amt, direction, bank_id, name in pay_rows.fetchall():
        signed = amt if direction == "INCOMING" else -amt
        # Status: MATCHED if before reconciled-through, else UNMATCHED
        status_str = "MATCHED" if pdt <= RECONCILED_THROUGH else "UNMATCHED"
        await session.execute(
            text(
                "INSERT INTO bank_statement_lines "
                "(id, company_id, tenant_id, account_id, txn_date, "
                "description, amount, status, version) "
                "VALUES (:id, :co, :ten, :aid, :dt, :desc, :amt, :st, 1)"
            ),
            {
                "id": str(uuid.uuid4()),
                "co": str(company.id),
                "ten": str(company.tenant_id),
                "aid": str(bank_id),
                "dt": pdt,
                "desc": (
                    f"Customer payment — {name}"
                    if direction == "INCOMING"
                    else f"Vendor payment — {name}"
                ),
                "amt": signed,
                "st": status_str,
            },
        )
        counts["bank_lines"] += 1

    # ----- Unmatched bank lines (the user reconciles these) --------- #
    unmatched_lines = [
        (date(2026, 4, 1), "INTEREST EARNED", Decimal("142.30"), "1-1110-SV"),
        (date(2026, 4, 30), "INTEREST EARNED", Decimal("148.10"), "1-1110-SV"),
        (date(2026, 4, 5), "BANK FEE - MONTHLY", Decimal("-15.00"), "1-1110-OP"),
        (date(2026, 5, 5), "BANK FEE - MONTHLY", Decimal("-15.00"), "1-1110-OP"),
        (date(2026, 4, 18), "ATO REFUND - PAYG WHHTAX",
         Decimal("412.00"), "1-1110-OP"),
        (date(2026, 4, 22), "OWNER DRAWINGS", Decimal("-2500.00"), "1-1110-OP"),
        (date(2026, 5, 1), "TRANSFER FROM SAVINGS",
         Decimal("5000.00"), "1-1110-OP"),
        (date(2026, 5, 1), "TRANSFER TO OPERATING",
         Decimal("-5000.00"), "1-1110-SV"),
    ]
    for d, desc, amt, code in unmatched_lines:
        if d > TODAY:
            continue
        await session.execute(
            text(
                "INSERT INTO bank_statement_lines "
                "(id, company_id, tenant_id, account_id, txn_date, "
                "description, amount, status, version) "
                "VALUES (:id, :co, :ten, :aid, :dt, :desc, :amt, "
                "'UNMATCHED', 1)"
            ),
            {
                "id": str(uuid.uuid4()),
                "co": str(company.id),
                "ten": str(company.tenant_id),
                "aid": str(accounts[code].id),
                "dt": d,
                "desc": desc,
                "amt": amt,
            },
        )
        counts["bank_lines"] += 1

    # ----- Demo user --------------------------------------------------- #
    demo_user = User(
        tenant_id=DEFAULT_TENANT_ID,
        username="demo",
        display_name="Demo Bookkeeper",
        email=DEMO_USER_EMAIL,
        role=UserRole.BOOKKEEPER.value,
        password_hash=hash_password(DEMO_USER_PASSWORD),
        email_verified_at=datetime.now(UTC),
        version=1,
    )
    session.add(demo_user)
    await session.flush()

    return counts


async def _create_invoice_payment(
    session, company, accounts, customers, *,
    invoice, customer, pay_date, bank, ar_acct,
):
    """Create a customer payment + allocation + JE."""
    pay_id = uuid.uuid4()
    # Number per company: PMT-YYYY-NNNN
    seq = await session.execute(
        text("SELECT COUNT(*) FROM payments WHERE company_id=:co"),
        {"co": str(company.id)},
    )
    n = seq.scalar_one() + 1
    number = f"PMT-{pay_date.year}-{n:04d}"
    p = Payment(
        id=pay_id,
        company_id=company.id,
        tenant_id=company.tenant_id,
        contact_id=customer.id,
        bank_account_id=bank.id,
        number=number,
        direction="INCOMING",
        method="eft",
        status="POSTED",
        payment_date=pay_date,
        amount=invoice.total,
        base_amount=invoice.total,
        currency="AUD",
        fx_rate=Decimal("1"),
        posted_at=datetime.combine(pay_date, datetime.min.time(), tzinfo=UTC),
        posted_by="seed",
        reference=f"Invoice {invoice.number}",
        version=1,
    )
    session.add(p)
    await session.flush()
    await session.execute(
        text(
            "INSERT INTO payment_allocations "
            "(id, payment_id, invoice_id, amount) "
            "VALUES (:id, :pid, :iid, :amt)"
        ),
        {
            "id": str(uuid.uuid4()),
            "pid": str(pay_id),
            "iid": str(invoice.id),
            "amt": invoice.total,
        },
    )
    je_id = await _post_je_raw(
        session, company,
        ref=f"JE-{number}",
        entry_date=pay_date,
        description=f"Receipt — {invoice.number}",
        lines=[
            (bank, invoice.total, Decimal("0"),
             f"Receipt from {customer.name}", None),
            (ar_acct, Decimal("0"), invoice.total,
             f"Clear AR — {invoice.number}", None),
        ],
    )
    await session.execute(
        text("UPDATE payments SET journal_entry_id=:je WHERE id=:id"),
        {"je": str(je_id), "id": str(pay_id)},
    )


async def _create_bill_payment(
    session, company, accounts, *,
    bill, vendor, pay_date, bank, ap_acct,
):
    """Create a vendor payment + allocation + JE."""
    pay_id = uuid.uuid4()
    seq = await session.execute(
        text("SELECT COUNT(*) FROM payments WHERE company_id=:co"),
        {"co": str(company.id)},
    )
    n = seq.scalar_one() + 1
    number = f"PMT-{pay_date.year}-{n:04d}"
    p = Payment(
        id=pay_id,
        company_id=company.id,
        tenant_id=company.tenant_id,
        contact_id=vendor.id,
        bank_account_id=bank.id,
        number=number,
        direction="OUTGOING",
        method="eft",
        status="POSTED",
        payment_date=pay_date,
        amount=bill.total,
        base_amount=bill.total,
        currency="AUD",
        fx_rate=Decimal("1"),
        posted_at=datetime.combine(pay_date, datetime.min.time(), tzinfo=UTC),
        posted_by="seed",
        reference=f"Bill {bill.number}",
        version=1,
    )
    session.add(p)
    await session.flush()
    await session.execute(
        text(
            "INSERT INTO payment_allocations "
            "(id, payment_id, bill_id, amount) "
            "VALUES (:id, :pid, :bid, :amt)"
        ),
        {
            "id": str(uuid.uuid4()),
            "pid": str(pay_id),
            "bid": str(bill.id),
            "amt": bill.total,
        },
    )
    je_id = await _post_je_raw(
        session, company,
        ref=f"JE-{number}",
        entry_date=pay_date,
        description=f"Vendor payment — {bill.number}",
        lines=[
            (ap_acct, bill.total, Decimal("0"),
             f"Clear AP — {bill.number}", None),
            (bank, Decimal("0"), bill.total,
             f"Payment to {vendor.name}", None),
        ],
    )
    await session.execute(
        text("UPDATE payments SET journal_entry_id=:je WHERE id=:id"),
        {"je": str(je_id), "id": str(pay_id)},
    )


# ----- Verification ---------------------------------------------------- #


async def _verify(session: AsyncSession) -> dict:
    """Final invariant checks. Returns a dict for the report."""
    out: dict = {}
    out["tenants"] = (await session.execute(
        text("SELECT COUNT(*) FROM tenants WHERE archived_at IS NULL")
    )).scalar_one()
    out["users"] = (await session.execute(
        text("SELECT COUNT(*) FROM users WHERE archived_at IS NULL")
    )).scalar_one()
    out["companies"] = (await session.execute(
        text("SELECT COUNT(*) FROM companies WHERE archived_at IS NULL")
    )).scalar_one()
    out["contacts"] = (await session.execute(
        text("SELECT COUNT(*) FROM contacts")
    )).scalar_one()
    out["invoices"] = (await session.execute(
        text("SELECT COUNT(*) FROM invoices")
    )).scalar_one()
    out["bills"] = (await session.execute(
        text("SELECT COUNT(*) FROM bills")
    )).scalar_one()
    out["payments"] = (await session.execute(
        text("SELECT COUNT(*) FROM payments")
    )).scalar_one()
    out["journal_entries"] = (await session.execute(
        text("SELECT COUNT(*) FROM journal_entries")
    )).scalar_one()
    out["bank_lines"] = (await session.execute(
        text("SELECT COUNT(*) FROM bank_statement_lines")
    )).scalar_one()
    out["fixed_assets"] = (await session.execute(
        text("SELECT COUNT(*) FROM fixed_assets")
    )).scalar_one()
    out["purchase_orders"] = (await session.execute(
        text("SELECT COUNT(*) FROM purchase_orders")
    )).scalar_one()

    # Trial balance (the load-bearing check)
    tb = (await session.execute(
        text(
            "SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) "
            "FROM journal_lines jl "
            "JOIN journal_entries je ON je.id = jl.entry_id "
            "WHERE je.company_id=:co AND je.status='POSTED'"
        ),
        {"co": str(DEMO_COMPANY_ID)},
    )).scalar_one()
    out["trial_balance"] = tb
    return out


# ----- Entry point ----------------------------------------------------- #


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed the public demo (DESTRUCTIVE — wipes every tenant).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag, the script reports what "
        "it would do and exits.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    SessionLocal = _owner_session_factory()

    if not args.apply:
        async with SessionLocal() as session:
            t = (await session.execute(
                text("SELECT COUNT(*) FROM tenants")
            )).scalar_one()
            c = (await session.execute(
                text("SELECT COUNT(*) FROM companies")
            )).scalar_one()
            i = (await session.execute(
                text("SELECT COUNT(*) FROM invoices")
            )).scalar_one()
            print(
                f"[dry-run] would WIPE {t} tenants, {c} companies, "
                f"{i} invoices, and rebuild Bondi Coastal Joinery.",
                file=sys.stderr,
            )
            print(
                "Re-run with --apply to commit the changes.",
                file=sys.stderr,
            )
        return 0

    async with SessionLocal() as session:
        try:
            await _wipe_everything(session)
            counts = await _build_demo(session)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Seed failed; transaction rolled back.")
            return 1

    # Re-open a session for verification (commit released the txn).
    async with SessionLocal() as session:
        report = await _verify(session)

    print("\n=== Seed applied ===", file=sys.stderr)
    for k, v in counts.items():
        print(f"  {k:18} {v}", file=sys.stderr)

    print("\n=== Verification ===", file=sys.stderr)
    for k, v in report.items():
        print(f"  {k:18} {v}", file=sys.stderr)

    tb = Decimal(str(report["trial_balance"]))
    if tb != Decimal("0"):
        print(
            f"\n[FAIL] Trial balance is {tb}, expected 0.00",
            file=sys.stderr,
        )
        return 2

    if report["tenants"] != 1:
        print(
            f"\n[FAIL] Expected 1 tenant, got {report['tenants']}",
            file=sys.stderr,
        )
        return 3
    if report["users"] != 2:
        print(
            f"\n[FAIL] Expected 2 users, got {report['users']}",
            file=sys.stderr,
        )
        return 4

    print("\n[OK] Demo seeded. Trial balance = 0.00", file=sys.stderr)
    print(
        f"     Login: {DEMO_USER_EMAIL} / {DEMO_USER_PASSWORD}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
