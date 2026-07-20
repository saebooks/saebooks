"""OSS-Q generator tests (EE-frontier build plan, Module 2).

Two groups, mirroring ``test_kmd_inf_generator.py``'s own split:

* Pure-unit, no DB — ``aggregate_oss_cells`` (the member-state x rate
  aggregation) and ``mapping.normalize_member_state``.
* ``postgres_only`` — one end-to-end scenario proving ``generate_oss_q``
  reads posted invoices, resolves the destination member state + rate,
  and reconciles against the pure aggregator.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.services.lodgement.oss_q.generator import (
    OssQCompanyConfigError,
    OssSaleLine,
    aggregate_oss_cells,
    generate_oss_q,
)
from saebooks.services.lodgement.oss_q.mapping import (
    MEMBER_STATE_NAMES,
    OSS_REPORTING_TYPE,
    alpha3_to_alpha2,
    normalize_member_state,
)

_D = Decimal


# ---------------------------------------------------------------------------
# Pure-unit — no DB.
# ---------------------------------------------------------------------------


def test_aggregate_oss_cells_groups_by_member_state_and_rate() -> None:
    lines = [
        OssSaleLine(member_state_code="DE", taxable_base=_D("100.00"), vat_rate_percent=_D("19.0000")),
        OssSaleLine(member_state_code="DE", taxable_base=_D("50.00"), vat_rate_percent=_D("19.0000")),
        OssSaleLine(member_state_code="FR", taxable_base=_D("200.00"), vat_rate_percent=_D("20.0000")),
    ]
    cells = aggregate_oss_cells(lines)
    assert len(cells) == 2

    de_cell = next(c for c in cells if c.member_state_code == "DE")
    assert de_cell.taxable_base == _D("150.00")
    assert de_cell.vat_amount == _D("28.50")  # 150 * 19% = 28.50
    assert de_cell.member_state_name == "Germany"

    fr_cell = next(c for c in cells if c.member_state_code == "FR")
    assert fr_cell.taxable_base == _D("200.00")
    assert fr_cell.vat_amount == _D("40.00")  # 200 * 20% = 40.00


def test_aggregate_oss_cells_keeps_distinct_rates_separate() -> None:
    """Same member state, two different rates (a company-provisioned
    reduced-rate override alongside the standard rate) must NOT be
    netted into one cell — mirrors kmd_inf.generator's critic-round-5
    "key on (reporting_type, rate)" fix."""
    lines = [
        OssSaleLine(member_state_code="FR", taxable_base=_D("100.00"), vat_rate_percent=_D("20.0000")),
        OssSaleLine(member_state_code="FR", taxable_base=_D("100.00"), vat_rate_percent=_D("5.5000")),
    ]
    cells = aggregate_oss_cells(lines)
    assert len(cells) == 2
    rates = {c.vat_rate_percent for c in cells}
    assert rates == {_D("20.0000"), _D("5.5000")}


def test_aggregate_oss_cells_rounds_at_cell_level_not_per_line() -> None:
    """VAT is derived from the SUMMED (already cent-rounded) base, not a
    sum of independently-rounded per-line VAT amounts — matches the
    module docstring's "round per box after aggregation" convention."""
    lines = [
        OssSaleLine(member_state_code="DE", taxable_base=_D("10.01"), vat_rate_percent=_D("19.0000")),
        OssSaleLine(member_state_code="DE", taxable_base=_D("10.01"), vat_rate_percent=_D("19.0000")),
        OssSaleLine(member_state_code="DE", taxable_base=_D("10.01"), vat_rate_percent=_D("19.0000")),
    ]
    cells = aggregate_oss_cells(lines)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.taxable_base == _D("30.03")
    # 30.03 * 0.19 = 5.7057 -> half-up to 5.71 (not 3 x round(1.9019) = 3x1.90=5.70)
    assert cell.vat_amount == _D("5.71")


def test_aggregate_oss_cells_empty_input() -> None:
    assert aggregate_oss_cells([]) == []


def test_aggregate_oss_cells_deterministic_order() -> None:
    """Output order is (member_state_code, rate) ascending — stable for
    golden/snapshot use."""
    lines = [
        OssSaleLine(member_state_code="FR", taxable_base=_D("1"), vat_rate_percent=_D("20")),
        OssSaleLine(member_state_code="DE", taxable_base=_D("1"), vat_rate_percent=_D("19")),
    ]
    cells = aggregate_oss_cells(lines)
    assert [c.member_state_code for c in cells] == ["DE", "FR"]


def test_member_state_names_covers_every_seeded_country() -> None:
    """Kept in lock-step with EE/oss_member_state_rates.yaml — see
    mapping.py's own comment on this discipline."""
    assert set(MEMBER_STATE_NAMES) == {
        "DE", "FR", "IT", "ES", "PT", "NL", "BE", "LU", "AT", "IE",
        "FI", "SE", "DK", "PL", "CZ", "LV", "LT",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Germany", "DE"),
        ("germany", "DE"),
        ("GERMANY", "DE"),
        ("DE", "DE"),
        ("de", "DE"),
        ("DEU", "DE"),
        ("France", "FR"),
        ("  ", None),
        (None, None),
        ("Atlantis", None),
        ("Australia", None),  # Contact.country's own default — never an OSS destination
    ],
)
def test_normalize_member_state(text: str | None, expected: str | None) -> None:
    assert normalize_member_state(text) == expected


def test_alpha3_to_alpha2_round_trips_every_seeded_state() -> None:
    """Every alpha-2 name in MEMBER_STATE_NAMES has a resolvable alpha-3
    counterpart — the code oss_member_state_rates rows use (countries.code
    is alpha-3)."""
    from saebooks.services.lodgement.oss_q.mapping import ALPHA2_TO_ALPHA3

    assert set(ALPHA2_TO_ALPHA3) == set(MEMBER_STATE_NAMES)
    for alpha2, alpha3 in ALPHA2_TO_ALPHA3.items():
        assert alpha3_to_alpha2(alpha3) == alpha2


def test_alpha3_to_alpha2_unknown_code_returns_none() -> None:
    assert alpha3_to_alpha2("XXX") is None


def test_oss_reporting_type_constant() -> None:
    assert OSS_REPORTING_TYPE == "oss_eu_b2c"


# ---------------------------------------------------------------------------
# postgres_only — end-to-end via generate_oss_q.
# ---------------------------------------------------------------------------

_PERIOD_START = date(2026, 4, 1)
_PERIOD_END = date(2026, 6, 30)


@pytest.mark.postgres_only
async def test_generate_oss_q_refuses_non_eur_base_currency() -> None:
    from sqlalchemy import update

    from saebooks.db import AsyncSessionLocal
    from saebooks.models.company import Company
    from tests.services.test_tax_return_generator import _make_ee_company

    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Company).where(Company.id == company_id).values(base_currency="AUD")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(OssQCompanyConfigError, match="EUR"):
            await generate_oss_q(
                session, company_id=company_id,
                period_start=_PERIOD_START, period_end=_PERIOD_END,
            )


@pytest.mark.postgres_only
async def test_generate_oss_q_golden_period() -> None:
    """One German B2C sale + one French B2C sale, posted as ordinary
    invoices with an OSS-tagged company TaxCode (rate=0 -> falls back to
    the embedded/reference standard rate) -> two cells reconciling
    against ``aggregate_oss_cells`` called directly on the same facts."""
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.account import Account, AccountType
    from saebooks.models.contact import Contact, ContactType
    from saebooks.models.tax_code import TaxCode
    from saebooks.services import invoices as invoices_svc
    from tests.services.test_tax_return_generator import _make_ee_company

    company_id = await _make_ee_company(jurisdiction="EE")

    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid for code, aid in (
                await session.execute(
                    select(Account.code, Account.id)
                    .where(Account.company_id == company_id)
                )
            ).all()
        }
        oss_tc = TaxCode(
            company_id=company_id,
            code="OSS-B2C", name="OSS EU B2C (Union scheme)", rate=Decimal("0.000"),
            tax_system="VAT", jurisdiction="EE", reporting_type=OSS_REPORTING_TYPE,
            input_credit_recoverable=False,
        )
        session.add(oss_tc)
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        await session.commit()
        oss_tc_id = oss_tc.id

    income_acct = by_code["4-1000"]

    async def _contact(name: str, country: str) -> uuid.UUID:
        async with AsyncSessionLocal() as session:
            c = Contact(company_id=company_id, name=name, contact_type=ContactType.CUSTOMER, country=country)
            session.add(c)
            await session.commit()
            return c.id

    async def _post(contact_id: uuid.UUID, net: Decimal, issue_date: date) -> None:
        async with AsyncSessionLocal() as session:
            inv = await invoices_svc.create_draft(
                session, company_id=company_id, contact_id=contact_id,
                issue_date=issue_date, due_date=issue_date, currency="EUR",
                lines=[{
                    "description": "OSS-Q golden sale", "account_id": income_acct,
                    "tax_code_id": oss_tc_id, "quantity": Decimal("1"), "unit_price": net,
                }],
            )
            await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-oss-q")

    de_customer = await _contact("DE Consumer", "Germany")
    fr_customer = await _contact("FR Consumer", "France")

    await _post(de_customer, Decimal("100.00"), date(2026, 5, 1))
    await _post(de_customer, Decimal("50.00"), date(2026, 5, 2))
    await _post(fr_customer, Decimal("200.00"), date(2026, 5, 3))

    async with AsyncSessionLocal() as session:
        listing = await generate_oss_q(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert not listing.errors
    assert len(listing.cells) == 2

    de_cell = next(c for c in listing.cells if c.member_state_code == "DE")
    assert de_cell.taxable_base == Decimal("150.00")
    assert de_cell.vat_amount == Decimal("28.50")  # 19% embedded/reference rate

    fr_cell = next(c for c in listing.cells if c.member_state_code == "FR")
    assert fr_cell.taxable_base == Decimal("200.00")
    assert fr_cell.vat_amount == Decimal("40.00")  # 20% embedded/reference rate

    assert listing.total_taxable_base() == Decimal("350.00")
    assert listing.total_vat_payable() == Decimal("68.50")


@pytest.mark.postgres_only
async def test_generate_oss_q_flags_unrecognised_country() -> None:
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.account import Account, AccountType
    from saebooks.models.contact import Contact, ContactType
    from saebooks.models.tax_code import TaxCode
    from saebooks.services import invoices as invoices_svc
    from tests.services.test_tax_return_generator import _make_ee_company

    company_id = await _make_ee_company(jurisdiction="EE")

    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid for code, aid in (
                await session.execute(
                    select(Account.code, Account.id)
                    .where(Account.company_id == company_id)
                )
            ).all()
        }
        oss_tc = TaxCode(
            company_id=company_id,
            code="OSS-B2C-2", name="OSS EU B2C (Union scheme)", rate=Decimal("0.000"),
            tax_system="VAT", jurisdiction="EE", reporting_type=OSS_REPORTING_TYPE,
            input_credit_recoverable=False,
        )
        session.add(oss_tc)
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        await session.commit()
        oss_tc_id = oss_tc.id

    income_acct = by_code["4-1000"]

    async with AsyncSessionLocal() as session:
        c = Contact(company_id=company_id, name="Unmapped Country Customer", contact_type=ContactType.CUSTOMER, country="Atlantis")
        session.add(c)
        await session.commit()
        contact_id = c.id

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 5, 1), due_date=date(2026, 5, 1), currency="EUR",
            lines=[{
                "description": "OSS-Q unmapped-country sale", "account_id": income_acct,
                "tax_code_id": oss_tc_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-oss-q")

    async with AsyncSessionLocal() as session:
        listing = await generate_oss_q(
            session, company_id=company_id,
            period_start=_PERIOD_START, period_end=_PERIOD_END,
        )

    assert listing.cells == []
    assert len(listing.errors) == 1
    assert listing.errors[0].kind == "unmapped_country"
