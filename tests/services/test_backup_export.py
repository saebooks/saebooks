"""Tests for ``saebooks.services.backup_export`` (Wave E) — the
data-isolation-critical core of scheduled backups.

Two things are proven here:

1. **Completeness** (``test_every_table_is_classified``) — every table
   currently in ``Base.metadata`` lands in exactly one of
   TENANT_DIRECT / CHILD_TABLES / GLOBAL_EXCLUDE. A future table that
   isn't classified fails THIS test loudly, by name — see the module
   docstring's "completeness test" section for why that matters.

2. **Zero cross-tenant leakage** (``test_export_contains_zero_foreign_
   tenant_rows`` and friends) — runs on the DEFAULT (SQLite) backend,
   deliberately NOT ``@pytest.mark.postgres_only``. SQLite has no RLS
   at all, so a green result here proves the explicit
   WHERE/JOIN-on-tenant_id filter ALONE is sufficient — this is the
   "provably incapable of leaking" guarantee Richard asked for, not
   "RLS happens to also catch it." A Postgres-only RLS probe for the
   two new tables (scheduled_backup_configs/runs themselves) lives
   separately in tests/test_rls_scheduled_backups.py.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.payment import (
    Payment,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.models.tenant import Tenant
from saebooks.services.backup_export import (
    CHILD_TABLES,
    GLOBAL_EXCLUDE,
    UnclassifiedTableError,
    classify_all_tables,
    export_tenant_data,
)

# ---------------------------------------------------------------------- #
# 1. Completeness                                                        #
# ---------------------------------------------------------------------- #


def test_every_table_is_classified() -> None:
    """Does not raise UnclassifiedTableError for any table currently in
    Base.metadata. This is the "a new table can't silently fall through
    the cracks" guarantee — see module docstring."""
    classification = classify_all_tables()
    from saebooks.db import Base

    assert set(classification) == set(Base.metadata.tables)


def test_child_tables_point_at_a_direct_or_reachable_ancestor() -> None:
    """Every CHILD_TABLES parent must itself be classified 'direct' —
    single-hop only, per module docstring (no CHILD_TABLES entry whose
    parent is itself a child, avoiding an unbounded join chain)."""
    classification = classify_all_tables()
    for child, spec in CHILD_TABLES.items():
        assert classification[spec.parent_table].kind == "direct", (
            f"{child}'s ancestor {spec.parent_table!r} is not TENANT_DIRECT "
            "— CHILD_TABLES only supports a single hop to a directly "
            "tenant-scoped table"
        )


def test_global_exclude_tables_have_no_tenant_id_leak_path() -> None:
    """Every GLOBAL_EXCLUDE table is confirmed absent from CHILD_TABLES
    too (no accidental double-classification)."""
    assert GLOBAL_EXCLUDE.isdisjoint(CHILD_TABLES.keys())


def test_audit_snapshots_is_excluded() -> None:
    """Pin: audit_snapshots is currently missing tenant_id/RLS (tracked
    for Wave C) and MUST stay excluded until that migration lands —
    regression guard against silently including a cross-tenant-unsafe
    table."""
    classification = classify_all_tables()
    assert classification["audit_snapshots"].kind == "excluded"


# ---------------------------------------------------------------------- #
# 2. Zero cross-tenant leakage — SQLite, no RLS                          #
# ---------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def two_tenants() -> dict[str, dict]:
    """Seed two tenants, each with a company + one row across a
    representative mix of DIRECT tables (companies, contacts, accounts,
    tax_codes, invoices, bills, payments, journal_entries) AND CHILD
    tables (invoice_lines via invoice_id, journal_lines via the
    non-obvious entry_id column — the highest-risk CHILD_TABLES entry
    since its FK column name doesn't match the table-name-prefix
    pattern the other line tables follow).
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, dict] = {}
    async with AsyncSessionLocal() as session:
        for label in ("tenant_a", "tenant_b"):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()
            contact_id = uuid.uuid4()
            account_id = uuid.uuid4()
            tax_code_id = uuid.uuid4()
            invoice_id = uuid.uuid4()
            invoice_line_id = uuid.uuid4()
            bill_id = uuid.uuid4()
            payment_id = uuid.uuid4()
            journal_entry_id = uuid.uuid4()
            journal_line_id = uuid.uuid4()

            session.add(
                Tenant(id=tenant_id, name=f"BX-{label}-{suffix}", slug=f"bx-{label}-{suffix}")
            )
            await session.flush()
            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"BX-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()
            session.add(
                Contact(
                    id=contact_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    name=f"BX-Contact-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            session.add(
                Account(
                    id=account_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"X{suffix[:4]}{label[-1]}",
                    name="BX Income",
                    account_type=AccountType.INCOME,
                )
            )
            session.add(
                TaxCode(
                    id=tax_code_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=f"X{suffix[:3]}{label[-1]}",
                    name="BX GST",
                    rate=Decimal("10.000"),
                    tax_system="GST",
                    reporting_type="taxable",
                )
            )
            await session.flush()
            session.add(
                Invoice(
                    id=invoice_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    number=f"INV-BX-{suffix}-{label[-1]}",
                    issue_date=date.today(),
                    due_date=date.today(),
                    status=InvoiceStatus.DRAFT,
                    subtotal=Decimal("100.00"),
                    tax_total=Decimal("10.00"),
                    total=Decimal("110.00"),
                    currency="AUD",
                )
            )
            session.add(
                Bill(
                    id=bill_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    number=f"BILL-BX-{suffix}-{label[-1]}",
                    issue_date=date.today(),
                    due_date=date.today(),
                    status=BillStatus.DRAFT,
                    subtotal=Decimal("0"),
                    tax_total=Decimal("0"),
                    total=Decimal("0"),
                    currency="AUD",
                )
            )
            session.add(
                Payment(
                    id=payment_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    contact_id=contact_id,
                    bank_account_id=account_id,
                    number=f"PAY-BX-{suffix}-{label[-1]}",
                    direction=PaymentDirection.INCOMING,
                    method=PaymentMethod.EFT,
                    status=PaymentStatus.DRAFT,
                    payment_date=date.today(),
                    amount=Decimal("100.00"),
                    currency="AUD",
                )
            )
            session.add(
                JournalEntry(
                    id=journal_entry_id,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    ref=f"JE-BX-{suffix}-{label[-1]}",
                    entry_date=date.today(),
                    status=EntryStatus.DRAFT,
                )
            )
            await session.flush()
            session.add(
                InvoiceLine(
                    id=invoice_line_id,
                    invoice_id=invoice_id,
                    line_no=1,
                    description=f"BX line {label}",
                    account_id=account_id,
                    quantity=Decimal("1"),
                    unit_price=Decimal("100.00"),
                    line_subtotal=Decimal("100.00"),
                    line_tax=Decimal("10.00"),
                    line_total=Decimal("110.00"),
                )
            )
            session.add(
                JournalLine(
                    id=journal_line_id,
                    entry_id=journal_entry_id,
                    company_id=company_id,
                    line_no=1,
                    account_id=account_id,
                    debit=Decimal("110.00"),
                    credit=Decimal("0"),
                    description=f"BX journal line {label}",
                )
            )
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "ids": {
                    "contact": contact_id,
                    "account": account_id,
                    "tax_code": tax_code_id,
                    "invoice": invoice_id,
                    "invoice_line": invoice_line_id,
                    "bill": bill_id,
                    "payment": payment_id,
                    "journal_entry": journal_entry_id,
                    "journal_line": journal_line_id,
                },
            }
        await session.commit()
    return out


def _all_ids_in_export(export_json_str: str) -> set[str]:
    """Crude but effective: every UUID-shaped substring in the export."""
    import re

    return set(re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", export_json_str))


@pytest.mark.asyncio
async def test_export_contains_zero_foreign_tenant_rows(two_tenants: dict) -> None:
    a = two_tenants["tenant_a"]
    b = two_tenants["tenant_b"]

    async with AsyncSessionLocal() as session:
        result = await export_tenant_data(session, a["tenant_id"])

    export_str = result.to_json_bytes().decode("utf-8")
    found_ids = _all_ids_in_export(export_str)

    # Positive check: tenant A's own rows ARE present.
    assert str(a["tenant_id"]) in found_ids
    assert str(a["company_id"]) in found_ids
    for name, row_id in a["ids"].items():
        assert str(row_id) in found_ids, f"tenant A's own {name} row missing from its export"

    # The guarantee: NONE of tenant B's identifiers appear anywhere.
    assert str(b["tenant_id"]) not in found_ids
    assert str(b["company_id"]) not in found_ids
    for name, row_id in b["ids"].items():
        assert str(row_id) not in found_ids, (
            f"LEAK: tenant B's {name} row ({row_id}) appeared in tenant A's export"
        )


@pytest.mark.asyncio
async def test_export_child_table_join_scopes_correctly(two_tenants: dict) -> None:
    """Targeted check on the CHILD_TABLES join path specifically (not
    just "no foreign UUID anywhere" — assert the actual table buckets
    are right), for both a name-matching child (invoice_lines) and the
    deliberately non-matching one (journal_lines -> entry_id)."""
    a = two_tenants["tenant_a"]
    b = two_tenants["tenant_b"]

    async with AsyncSessionLocal() as session:
        result = await export_tenant_data(session, a["tenant_id"])

    # Row "id" values are raw uuid.UUID objects (rows are dict(r._mapping)
    # straight off the SQLAlchemy Core select, stringified only at
    # to_json_bytes() time) — normalise to str here so the membership
    # checks below (str keys from the `two_tenants` fixture) compare
    # like-for-like instead of `str in {UUID, ...}`, which is always
    # False regardless of whether the row is actually present.
    invoice_line_ids = {str(r["id"]) for r in result.tables["invoice_lines"].rows}
    journal_line_ids = {str(r["id"]) for r in result.tables["journal_lines"].rows}

    assert str(a["ids"]["invoice_line"]) in invoice_line_ids
    assert str(b["ids"]["invoice_line"]) not in invoice_line_ids

    assert str(a["ids"]["journal_line"]) in journal_line_ids
    assert str(b["ids"]["journal_line"]) not in journal_line_ids


@pytest.mark.asyncio
async def test_excluded_tables_never_appear_in_export(two_tenants: dict) -> None:
    a = two_tenants["tenant_a"]
    async with AsyncSessionLocal() as session:
        result = await export_tenant_data(session, a["tenant_id"])
    assert "audit_snapshots" not in result.tables
    assert "tenants" not in result.tables
    assert "settings" not in result.tables


@pytest.mark.asyncio
async def test_export_of_tenant_with_no_data_is_empty_not_an_error() -> None:
    lonely_tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(id=lonely_tenant_id, name="Lonely", slug=f"lonely-{lonely_tenant_id.hex[:8]}")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await export_tenant_data(session, lonely_tenant_id)

    assert result.tables["companies"].row_count == 0
    assert all(r.row_count == 0 for r in result.tables.values())


def test_unclassified_table_raises_loud() -> None:
    """Simulates "a new table shipped without classification" by
    registering an extra bare table on the REAL ``Base.metadata`` (the
    same MetaData ``classify_all_tables`` reads) — proves the
    completeness check fails LOUD (an exception naming the table), not
    silently (an empty/omitted export bucket). Cleaned up in `finally`
    so it never leaks into another test."""
    import sqlalchemy as sa

    from saebooks.db import Base

    fake_table_name = "totally_new_unclassified_table_xyz"
    fake = sa.Table(
        fake_table_name,
        Base.metadata,
        sa.Column("id", sa.String, primary_key=True),
    )
    try:
        with pytest.raises(UnclassifiedTableError, match=fake_table_name):
            classify_all_tables()
    finally:
        Base.metadata.remove(fake)
