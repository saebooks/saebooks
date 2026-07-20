"""KMD 2027 — silent-drop coverage guard (findings transfer from feat/kmd3-2027).

The donor build's critic loop found its generator SILENTLY DROPPED
``ic_acq_exempt`` (KMDTYYP ``S_106``) lines: a purchase-side reporting_type that
was mapped to a leaf in the seed but never reached by the generator, so the
transaction vanished from the export with neither a row nor a data-quality flag.

This module encodes the invariant that would have caught it, and confirms the
canonical build does NOT have the same gap:

  * ``test_every_mapped_engine_source_is_reachable`` (pure) — for EVERY
    ``(reporting_type, role)`` the seed maps to a KMDTYYP leaf, the generator
    has a code path that emits a row for it. A mapped pair the generator can
    never reach is a silent drop by construction. This is a structural guard
    over ``kmdtyyp_mapping.yaml`` × the generator's dispatch tag-sets, so seeding
    a new mapped input/acquisition tag without wiring the generator fails loudly.
  * ``test_ic_acq_exempt_produces_s106_row`` (postgres_only) — the donor's exact
    finding: a posted ``ic_acq_exempt`` bill produces an ``S_106`` row (not
    dropped, not flagged).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services.lodgement.kmd_2027 import generator as gen
from saebooks.services.lodgement.kmd_2027 import kmdtyyp
from saebooks.services.lodgement.kmd_2027.generator import generate_kmd_2027
from tests.services.lodgement.test_kmd_inf_generator import _contact, _post_bill
from tests.services.test_tax_return_generator import _make_ee_company

_D = Decimal

# The generator's role → reachable-reporting_type dispatch, as a function of its
# module-level tag-sets. A mapped (reporting_type, role) NOT covered here is a
# transaction the generator can never emit — a silent drop.
_INPUT_REACHABLE = gen._ORDINARY_INPUT_TAGS | gen._RC_FANOUT_INPUT_TAGS


def test_every_mapped_engine_source_is_reachable() -> None:
    """No mapped (reporting_type, role) pair is unreachable by the generator."""
    loaded = kmdtyyp._load()
    unreachable: list[tuple[str, str, str]] = []
    for (reporting_type, role), leaf in loaded.forward.items():
        if role == "sale":
            reachable = True  # every invoice/credit-note group calls resolve(rt, "sale")
        elif role == "acquisition":
            reachable = reporting_type in gen._RC_ACQUISITION_TAGS
        elif role == "input":
            reachable = reporting_type in _INPUT_REACHABLE
        else:  # "accounting" — the generator has no accounting-entry path
            reachable = False
        if not reachable:
            unreachable.append((reporting_type, role, leaf))
    assert not unreachable, (
        "mapped engine sources the generator can never emit (silent drop): "
        f"{unreachable}"
    )


def test_ic_acq_exempt_is_mapped_and_reachable() -> None:
    """The donor's exact gap, at the mapping+dispatch level: ic_acq_exempt
    resolves to S_106 AND the generator routes it through the acquisition path."""
    assert kmdtyyp.resolve_kmdtyyp("ic_acq_exempt", "acquisition") == "S_106"
    assert "ic_acq_exempt" in gen._RC_ACQUISITION_TAGS
    # …and S_106 is NOT fanned out to an O_ input row (exempt IC acq has none).
    assert "ic_acq_exempt" not in gen._RC_FANOUT_INPUT_TAGS


@pytest.mark.postgres_only
async def test_ic_acq_exempt_produces_s106_row() -> None:
    """A posted ic_acq_exempt bill is exported as an S_106 row — the donor
    silently dropped it; the canonical build must not."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY))
        session.add(TaxCode(company_id=company_id, code="EE-IC-ACQ-EXEMPT", name="EE IC acquisition exempt", rate=_D("0.000"), tax_system="VAT", jurisdiction="EE", reporting_type="ic_acq_exempt"))
        await session.commit()
        expense = (await session.execute(select(Account.id).where(Account.company_id == company_id, Account.code == "5-1000"))).scalar_one()
        tax_id = (await session.execute(select(TaxCode.id).where(TaxCode.company_id == company_id, TaxCode.reporting_type == "ic_acq_exempt"))).scalar_one()

    supplier = await _contact(company_id, "EU Exempt Acq Supplier", ContactType.SUPPLIER, None)
    await _post_bill(company_id, supplier, expense, tax_id, net=_D("2000.00"), issue_date=date(2026, 2, 10))

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(session, company_id=company_id, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28))

    assert not listing.errors, [e.message for e in listing.errors]
    s106 = [r for r in listing.rows if r.kmdtyyp_code == "S_106"]
    assert len(s106) == 1, f"ic_acq_exempt must emit exactly one S_106 row, got {[(r.kmdtyyp_code, str(r.amount)) for r in listing.rows]}"
    assert s106[0].amount == _D("2000.00")
