"""EE cashbook — the Estonian jurisdiction profile lifts the AU-only v1 gate.

Coverage:
- EUR entries accepted for an EE cashbook company (the old
  ``cashbook_currency_unsupported`` 409 for EUR is gone).
- Käibemaks split: registered company, STD (24%) income → 3-line JE with
  the VAT line auto-posted to the collected account; INPUT_STD expense →
  VAT line on the paid account; trial balance holds.
- Exempt category (Pangakulud / INPUT_EXEMPT, rate 0) posts a clean
  2-line JE.
- Non-registered EE company posts 2-line JEs (no KM line).
- An EE company with a non-EUR base currency is refused with the same
  typed error code AU uses for non-AUD.
- A jurisdiction with NO registered cashbook profile is refused
  (preserves v1 behaviour for everything that isn't AU/EE).
- EE picker taxonomy is served by ``all_defaults("EE")`` with Estonian
  labels and EE tax codes.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import JournalEntry
from saebooks.models.tax_code import TaxCode
from saebooks.services import settings as settings_svc
from saebooks.services.cashbook import (
    CashbookCurrencyError,
    record_cashbook_entry,
)
from saebooks.services.cashbook_categories import (
    all_defaults,
    get_default,
)

pytestmark = pytest.mark.postgres_only


async def _make_ee_cashbook_company(
    *,
    tax_registered: bool = True,
    base_currency: str = "EUR",
    jurisdiction: str = "EE",
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Minimal EE cashbook company: EE-chart-coded accounts the EE
    category profile points at, per-company EE tax codes, VAT settings."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        # Company row FIRST — the tenant_coherence trigger on accounts
        # asserts the parent company exists at insert time. The cashbook
        # bank FK is set after the accounts flush (the paired CHECK
        # constraint sees the consistent end state at commit).
        company = Company(
            id=company_id, tenant_id=DEFAULT_TENANT_ID,
            name=f"Kassa OU {company_id.hex[:8]}",
            base_currency=base_currency,
            fin_year_start_month=1, audit_mode="immutable",
            jurisdiction=jurisdiction,
        )
        session.add(company)
        await session.flush()
        accounts = {
            "bank": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="1100", name="Pank — arvelduskonto",
                account_type=AccountType.ASSET,
            ),
            "services": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="4100", name="Müügitulu — teenused",
                account_type=AccountType.INCOME,
            ),
            "utilities": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="6300", name="Kommunaalkulud",
                account_type=AccountType.EXPENSE,
            ),
            "bank_fees": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="6600", name="Pangakulud",
                account_type=AccountType.EXPENSE,
            ),
            "vat_receivable": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="1400", name="Käibemaks (VAT) Receivable",
                account_type=AccountType.ASSET,
            ),
            "vat_payable": Account(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="2200", name="Käibemaks (VAT) Payable",
                account_type=AccountType.LIABILITY,
            ),
        }
        for a in accounts.values():
            session.add(a)
        await session.flush()

        company.bookkeeping_mode = "cashbook"
        company.cashbook_default_bank_account_id = accounts["bank"].id
        company.tax_registered = tax_registered
        session.add(
            TaxCode(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="STD", name="Käibemaks — üldine määr (24%)",
                rate=Decimal("24.000"), tax_system="VAT",
                jurisdiction="EE", reporting_type="taxable",
            )
        )
        session.add(
            TaxCode(
                company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
                code="INPUT_STD", name="Sisendkäibemaks — üldine määr (24%)",
                rate=Decimal("24.000"), tax_system="VAT",
                jurisdiction="EE", reporting_type="taxable",
            )
        )
        await settings_svc.set(session, "gst_collected_account_code", "2200")
        await settings_svc.set(session, "gst_paid_account_code", "1400")
        await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        acct_ids = {k: a.id for k, a in accounts.items()}
    return DEFAULT_TENANT_ID, company_id, acct_ids


def _new_key(prefix: str = "ee") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _trial_balance(entry: JournalEntry) -> tuple[Decimal, Decimal]:
    debits = sum((ln.debit for ln in entry.lines), Decimal("0"))
    credits = sum((ln.credit for ln in entry.lines), Decimal("0"))
    return debits, credits


async def test_ee_income_registered_km_three_lines() -> None:
    tenant_id, company_id, accts = await _make_ee_cashbook_company()
    async with AsyncSessionLocal() as session:
        entry = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 7, 10),
            description="Teenuse müük",
            amount=Decimal("124.00"),
            direction="income",
            category_code="INC_SERVICES",
            idempotency_key=_new_key("km-inc"),
            actor="pytest",
        )

    je = entry
    assert len(je.lines) == 3
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("124.00")
    vat_lines = [
        ln for ln in je.lines if ln.account_id == accts["vat_payable"]
    ]
    assert len(vat_lines) == 1
    # 24/124 of the gross: 124.00 → 24.00 käibemaks, 100.00 net.
    assert vat_lines[0].credit == Decimal("24.00")
    income_lines = [
        ln for ln in je.lines if ln.account_id == accts["services"]
    ]
    assert income_lines[0].credit == Decimal("100.00")


async def test_ee_expense_registered_km_paid_side() -> None:
    tenant_id, company_id, accts = await _make_ee_cashbook_company()
    async with AsyncSessionLocal() as session:
        entry = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 7, 11),
            description="Elekter",
            amount=Decimal("62.00"),
            direction="expense",
            category_code="EXP_UTILITIES",
            idempotency_key=_new_key("km-exp"),
            actor="pytest",
        )

    je = entry
    assert len(je.lines) == 3
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("62.00")
    vat_lines = [
        ln for ln in je.lines if ln.account_id == accts["vat_receivable"]
    ]
    assert len(vat_lines) == 1
    assert vat_lines[0].debit == Decimal("12.00")  # 62 × 24/124


async def test_ee_exempt_category_two_lines() -> None:
    tenant_id, company_id, _accts = await _make_ee_cashbook_company()
    async with AsyncSessionLocal() as session:
        entry = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 7, 12),
            description="Kontohaldustasu",
            amount=Decimal("8.50"),
            direction="expense",
            category_code="EXP_BANK_FEES",
            idempotency_key=_new_key("km-exempt"),
            actor="pytest",
        )

    je = entry
    assert len(je.lines) == 2
    debits, credits = _trial_balance(je)
    assert debits == credits == Decimal("8.50")


async def test_ee_non_registered_two_lines() -> None:
    tenant_id, company_id, _ = await _make_ee_cashbook_company(
        tax_registered=False
    )
    async with AsyncSessionLocal() as session:
        entry = await record_cashbook_entry(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            entry_date=date(2026, 7, 13),
            description="Müük (KMV-välise ettevõtte)",
            amount=Decimal("50.00"),
            direction="income",
            category_code="INC_SERVICES",
            idempotency_key=_new_key("km-nonreg"),
            actor="pytest",
        )

    je = entry
    assert len(je.lines) == 2


async def test_ee_non_eur_currency_refused() -> None:
    tenant_id, company_id, _ = await _make_ee_cashbook_company(
        base_currency="AUD"
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookCurrencyError) as exc:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 7, 14),
                description="x",
                amount=Decimal("10.00"),
                direction="expense",
                category_code="EXP_UTILITIES",
                idempotency_key=_new_key("badccy"),
                actor="pytest",
            )
    assert exc.value.code == "cashbook_currency_unsupported"


async def test_unregistered_jurisdiction_refused() -> None:
    tenant_id, company_id, _ = await _make_ee_cashbook_company(
        jurisdiction="DE", base_currency="EUR"
    )
    async with AsyncSessionLocal() as session:
        with pytest.raises(CashbookCurrencyError) as exc:
            await record_cashbook_entry(
                db=session,
                tenant_id=tenant_id,
                company_id=company_id,
                entry_date=date(2026, 7, 14),
                description="x",
                amount=Decimal("10.00"),
                direction="expense",
                category_code="EXP_UTILITIES",
                idempotency_key=_new_key("nojuris"),
                actor="pytest",
            )
    assert exc.value.code == "cashbook_currency_unsupported"


def test_ee_picker_taxonomy() -> None:
    codes = [c.code for c in all_defaults("EE")]
    assert "INC_SERVICES" in codes and "EXP_BANK_FEES" in codes
    assert get_default("EXP_VEHICLE", "EE").gst_default == Decimal("0.24")
    assert get_default("INC_SERVICES", "EE").label == "Müügitulu — teenused"
    # AU untouched.
    assert get_default("INC_SALES").gst_default == Decimal("0.10")
