"""M1.5 · T8 — generic return calculator: box-definition parsing +
reference-DB-driven aggregation + AU BAS thin-wrapper equivalence.

Four groups of tests:

* Pure-unit, no DB — ``_parse_box_definition`` grammar (the aggregation
  string format documented in the AU seed YAML) and the 2-letter ->
  3-letter jurisdiction mapping. Fast, deterministic, catch a typo in
  the parser or a seed row before either ever touches a database.
* ``postgres_only`` — ``generate_return`` reproduces
  ``jurisdictions.au.tax.bas_report``'s pre-T8 numbers exactly for a
  GST-inclusive taxable sale (the same scenario
  ``tests/services/test_cashbook_bas.py``'s keystone test pins), proving
  the thin-wrapper refactor is behaviour-preserving.
* ``postgres_only`` — with no ``REFERENCE_DATABASE_URL`` configured (the
  standard test/CI shape — see ``docker-compose.test.yml``),
  ``generate_return`` still succeeds via the embedded fallback box set,
  proving AU BAS reporting does not go down when the reference DB is
  absent (MODULARITY — no capability's fault should cascade from an
  unrelated/optional module being unavailable).
* Reference-DB integration (skipped unless both ``REFERENCE_DATABASE_URL``
  and ``REFERENCE_MIGRATION_DATABASE_URL`` are configured, same gate as
  ``tests/integration/test_cross_db_join.py``) — proves
  ``generate_return`` actually READS ``TaxReturnBoxDefinition`` rather
  than hardcoding box literals: a temporary, uniquely-named box row is
  inserted, ``generate_return`` returns it verbatim, and the row is
  removed again in a ``finally``.
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal, ReferenceSession
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.services.reports import REPORTABLE_STATUSES
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import settings as settings_svc
from saebooks.jurisdictions.au.tax import bas_report
from saebooks.services.tax_return_generator import (
    FormulaSyntaxError,
    TaxReturnBoxResult,
    TaxReturnResult,
    _aggregate_ledger_by_box,
    _BoxDefRow,
    _evaluate_formula_boxes,
    _ParsedBox,
    _parse_box_definition,
    _to_engine_jurisdiction,
    _to_reference_jurisdiction,
    generate_return,
    persist_return,
)

pytestmark = pytest.mark.postgres_only

_FROM = date(2026, 1, 1)
_TO = date(2026, 12, 31)
_ENTRY_DATE = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Pure-unit — box-definition parsing (no DB; runs even without postgres_only,
# but marked at module level above for consistency with the file's other
# tests; these particular functions don't touch a session).
# ---------------------------------------------------------------------------


def test_parse_box_definition_sum_taxable_inclusive() -> None:
    row = _BoxDefRow("G1", "Total sales", "sum_taxable_for_codes:income:gst_inclusive", ["taxable"], 1)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_taxable_for_codes"
    assert parsed.bucket == "income"
    assert parsed.inclusive is True
    assert parsed.feeder_codes == frozenset({"taxable"})


def test_parse_box_definition_sum_taxable_exclusive() -> None:
    row = _BoxDefRow("G2", "Export sales", "sum_taxable_for_codes:income:gst_exclusive", ["export"], 2)
    parsed = _parse_box_definition(row)
    assert parsed.inclusive is False


def test_parse_box_definition_sum_tax_amount() -> None:
    row = _BoxDefRow("1A", "GST collected", "sum_tax_amount_for_codes:income", ["taxable"], 6)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_tax_amount_for_codes"
    assert parsed.bucket == "income"


def test_parse_box_definition_manual() -> None:
    row = _BoxDefRow("9", "Manual adjustment", "manual", [], 9)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "manual"
    assert parsed.bucket is None


def test_parse_box_definition_formula_kind() -> None:
    """KMD-formula support Packet 1 — aggregation='formula' is the new
    discriminator; the expression lives in the dedicated 'formula'
    column (row.formula), not inlined into aggregation."""
    row = _BoxDefRow("4", "Output VAT", "formula", [], 12, formula="0.24*KMD:1 + 0.09*KMD:2")
    parsed = _parse_box_definition(row)
    assert parsed.kind == "formula"
    assert parsed.formula == "0.24*KMD:1 + 0.09*KMD:2"
    assert parsed.bucket is None
    assert parsed.feeder_codes == frozenset()


def test_parse_box_definition_formula_kind_requires_expression() -> None:
    row = _BoxDefRow("4", "Output VAT", "formula", [], 12, formula=None)
    with pytest.raises(ValueError, match="no expression"):
        _parse_box_definition(row)

    row_blank = _BoxDefRow("4", "Output VAT", "formula", [], 12, formula="   ")
    with pytest.raises(ValueError, match="no expression"):
        _parse_box_definition(row_blank)


def test_parse_box_definition_legacy_inline_formula_prefix_rejected() -> None:
    """Behaviour change (Packet 1, flagged): the reserved inline
    'formula:<expr>' prefix used to raise NotImplementedError as an
    unbuilt placeholder — no seeded box ever used it. Packet 1 commits
    to the dedicated-column form (aggregation='formula', expr in the
    'formula' column — see the scope's §3.1 "builder does not choose")
    and now actively rejects the legacy inline form with a ValueError
    pointing at the correct usage, rather than leaving it unimplemented."""
    row = _BoxDefRow("X1", "Some formula box", "formula:g1-g11", [], 1)
    with pytest.raises(ValueError, match="dedicated 'formula' column"):
        _parse_box_definition(row)


def test_parse_box_definition_output_bucket() -> None:
    """KMD-formula support Packet 3 — the role-based "output" bucket
    (matches a component's direction, not the owning account's type)."""
    row = _BoxDefRow("1_RC", "RC output base", "sum_taxable_for_codes:output:gst_exclusive", ["rc_eu_acq_goods"], 102)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_taxable_for_codes"
    assert parsed.bucket == "output"
    assert parsed.inclusive is False


def test_parse_box_definition_input_bucket() -> None:
    row = _BoxDefRow("5_RC", "RC input VAT", "sum_tax_amount_for_codes:input", ["rc_eu_acq_goods"], 142)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_tax_amount_for_codes"
    assert parsed.bucket == "input"


def test_parse_box_definition_missing_bucket_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "sum_taxable_for_codes", ["taxable"], 1)
    with pytest.raises(ValueError, match="income"):
        _parse_box_definition(row)


def test_parse_box_definition_missing_inclusive_modifier_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "sum_taxable_for_codes:income", ["taxable"], 1)
    with pytest.raises(ValueError, match="gst_inclusive"):
        _parse_box_definition(row)


def test_parse_box_definition_unknown_kind_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "count_the_beans:income", ["taxable"], 1)
    with pytest.raises(ValueError, match="unknown aggregation kind"):
        _parse_box_definition(row)


def test_to_reference_jurisdiction_maps_au_to_aus() -> None:
    assert _to_reference_jurisdiction("AU") == "AUS"
    assert _to_reference_jurisdiction("au") == "AUS"


def test_to_reference_jurisdiction_passthrough_unknown() -> None:
    assert _to_reference_jurisdiction("XX") == "XX"


def test_to_engine_jurisdiction_maps_aus_to_au() -> None:
    """Bug fix (round 6) — the reverse mapping used by the embedded
    fallback lookup must undo _to_reference_jurisdiction so a caller
    supplying the reference-DB-canonical 3-char code ('AUS') still
    resolves to the fallback's 2-letter key ('AU')."""
    assert _to_engine_jurisdiction("AUS") == "AU"
    assert _to_engine_jurisdiction("aus") == "AU"


def test_to_engine_jurisdiction_passthrough_already_engine_code() -> None:
    assert _to_engine_jurisdiction("AU") == "AU"
    assert _to_engine_jurisdiction("au") == "AU"


def test_to_engine_jurisdiction_passthrough_unknown() -> None:
    assert _to_engine_jurisdiction("XX") == "XX"


# ---------------------------------------------------------------------------
# KMD-formula support Packet 1 — the ``formula:`` aggregation engine
# (parser + safe AST evaluator + topological sort + cycle detection +
# per-box cent rounding). All pure-unit, no DB — see
# ~/.claude/plans/kmd-formula-support-scope.md §3.1/§6 for the design
# these tests pin, including the exact "golden mini-network" figures from
# the scope's §6 domestic-only KMD test plan.
# ---------------------------------------------------------------------------


def _mbox(code: str, *, order: int = 0) -> _ParsedBox:
    """A 'manual'-kind box standing in for an already-ledger-aggregated
    box in these formula-engine tests (its value comes from the
    ``ledger_amounts`` dict passed to ``_evaluate_formula_boxes``, not
    from its own aggregation recipe — only ``kind`` and ``box_code``
    matter to the formula pass)."""
    return _parse_box_definition(_BoxDefRow(code, code, "manual", [], order))


def _fbox(code: str, formula: str, *, order: int = 0) -> _ParsedBox:
    return _parse_box_definition(_BoxDefRow(code, code, "formula", [], order, formula=formula))


def test_formula_box_reference_and_addition() -> None:
    parsed = [_mbox("1"), _mbox("2"), _fbox("3", "KMD:1 + KMD:2")]
    ledger = {"1": Decimal("100.00"), "2": Decimal("50.00"), "3": Decimal("0")}
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert amounts["3"] == Decimal("150.00")


def test_formula_bare_box_ref_resolves_within_current_return_type() -> None:
    parsed = [_mbox("1"), _fbox("3", "1")]  # bare ref, no RETURN_TYPE: prefix
    amounts = _evaluate_formula_boxes(parsed, {"1": Decimal("42.00")}, return_type="KMD")
    assert amounts["3"] == Decimal("42.00")


def test_formula_rate_multiplication() -> None:
    """RATE-FORMULA — rate x base coefficient (scope's box-4 finding)."""
    parsed = [_mbox("1"), _fbox("4", "0.24*KMD:1")]
    ledger = {"1": Decimal("10000.00"), "4": Decimal("0")}
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert amounts["4"] == Decimal("2400.00")


def test_formula_subtraction_and_unary_minus() -> None:
    parsed = [_mbox("A"), _mbox("B"), _fbox("C", "-(KMD:A - KMD:B)")]
    ledger = {"A": Decimal("10.00"), "B": Decimal("30.00"), "C": Decimal("0")}
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert amounts["C"] == Decimal("20.00")


def test_formula_max_zero_payable_and_refund_split() -> None:
    """BOX-FORMULA — the decisive box 12/13 finding: one signed net N,
    split as max(0,N) / max(0,-N); exactly one side is non-zero."""
    parsed = [
        _mbox("4"), _mbox("5"),
        _fbox("12", "max(0, KMD:4 - KMD:5)"),
        _fbox("13", "max(0, -(KMD:4 - KMD:5))"),
    ]
    payable = {
        "4": Decimal("2710.00"), "5": Decimal("840.00"), "12": Decimal("0"), "13": Decimal("0"),
    }
    amounts = _evaluate_formula_boxes(parsed, payable, return_type="KMD")
    assert amounts["12"] == Decimal("1870.00")
    assert amounts["13"] == Decimal("0.00")

    refund = {
        "4": Decimal("840.00"), "5": Decimal("2710.00"), "12": Decimal("0"), "13": Decimal("0"),
    }
    amounts2 = _evaluate_formula_boxes(parsed, refund, return_type="KMD")
    assert amounts2["12"] == Decimal("0.00")
    assert amounts2["13"] == Decimal("1870.00")


def test_formula_golden_kmd_mini_network_payable_and_refund() -> None:
    """The scope's §6 golden-file domestic-only KMD period, as a
    pure-unit formula-engine test (Packet 1 scope — the DB-posting
    version of this same period lands in Packet 2 once the EE seed
    flips boxes 4/12/13 to these exact formulas). Box 4 rate-derived
    over 24%/9%/13%; box 12/13 the payable/refund split, both
    directions."""
    parsed = [
        _mbox("1"), _mbox("2"), _mbox("2-2"), _mbox("5"), _mbox("10"), _mbox("11"),
        _fbox("4", "0.24*KMD:1 + 0.09*KMD:2 + 0.13*KMD:2-2"),
        _fbox("12", "max(0, KMD:4 - KMD:5 + KMD:10 - KMD:11)"),
        _fbox("13", "max(0, -(KMD:4 - KMD:5 + KMD:10 - KMD:11))"),
    ]
    base_ledger = {
        "1": Decimal("10000.00"), "2": Decimal("2000.00"), "2-2": Decimal("1000.00"),
        "5": Decimal("840.00"), "10": Decimal("0.00"), "11": Decimal("0.00"),
        "4": Decimal("0"), "12": Decimal("0"), "13": Decimal("0"),
    }
    amounts = _evaluate_formula_boxes(parsed, base_ledger, return_type="KMD")
    assert amounts["4"] == Decimal("2710.00")
    assert amounts["12"] == Decimal("1870.00")
    assert amounts["13"] == Decimal("0.00")

    refund_ledger = dict(base_ledger, **{"5": Decimal("3000.00")})
    amounts2 = _evaluate_formula_boxes(parsed, refund_ledger, return_type="KMD")
    assert amounts2["4"] == Decimal("2710.00")
    assert amounts2["12"] == Decimal("0.00")
    assert amounts2["13"] == Decimal("290.00")


def test_formula_manual_box_absent_is_zero_and_supplied_value_flows() -> None:
    """Finding 7: boxes 4-1/10/11 are manual-by-design but FEED the box
    12/13 formula. An ABSENT manual value is an explicit 0 (box 12
    computes correctly for the common no-import/no-adjustment case); a
    filer-supplied 4-1 flows into 12/13 via ``manual_values``; and a
    ``manual_values`` entry for a non-manual (ledger/formula) box is
    rejected so a typo can't clobber a computed box."""
    parsed = [
        _mbox("1"), _mbox("5"), _mbox("4-1"), _mbox("10"), _mbox("11"),
        _fbox("4", "0.24*KMD:1"),
        _fbox("12", "max(0, KMD:4 + KMD:4-1 - KMD:5 + KMD:10 - KMD:11)"),
        _fbox("13", "max(0, -(KMD:4 + KMD:4-1 - KMD:5 + KMD:10 - KMD:11))"),
    ]
    ledger = {
        "1": Decimal("10000.00"), "5": Decimal("840.00"),
        "4-1": Decimal("0"), "10": Decimal("0"), "11": Decimal("0"),
        "4": Decimal("0"), "12": Decimal("0"), "13": Decimal("0"),
    }
    # Absent 4-1 → explicit 0: box 12 = max(0, 2400 + 0 - 840) = 1560.
    absent = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert absent["12"] == Decimal("1560.00")

    # Filer supplies box 4-1 = 500 → box 12 = max(0, 2400 + 500 - 840) = 2060.
    supplied = _evaluate_formula_boxes(
        parsed, ledger, return_type="KMD", manual_values={"4-1": Decimal("500.00")}
    )
    assert supplied["4-1"] == Decimal("500.00")
    assert supplied["12"] == Decimal("2060.00")
    assert supplied["13"] == Decimal("0.00")

    # A manual_values entry for a non-manual box is rejected loudly.
    with pytest.raises(ValueError, match="only override 'manual'"):
        _evaluate_formula_boxes(
            parsed, ledger, return_type="KMD", manual_values={"4": Decimal("1")}
        )


def test_formula_topological_order_three_deep_chain_ignores_display_order() -> None:
    """1 (ledger) -> 4 (formula) -> 12 (formula), depth 3 — matches the
    scope's stated EE dependency chain shape. display_order is
    deliberately shuffled/out of dependency order to prove evaluation
    order comes from the dependency graph, not seed row order."""
    parsed = [
        _mbox("1", order=1),
        _fbox("12", "max(0, KMD:4)", order=5),
        _fbox("4", "0.24*KMD:1", order=90),
    ]
    ledger = {"1": Decimal("1000.00"), "4": Decimal("0"), "12": Decimal("0")}
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert amounts["4"] == Decimal("240.00")
    assert amounts["12"] == Decimal("240.00")


def test_formula_cycle_detected_names_the_cycle() -> None:
    parsed = [_fbox("4", "KMD:12"), _fbox("12", "KMD:4")]
    with pytest.raises(ValueError, match="formula cycle") as excinfo:
        _evaluate_formula_boxes(parsed, {}, return_type="KMD")
    # Names both boxes in the cycle path, not just "a cycle exists".
    assert "4" in str(excinfo.value)
    assert "12" in str(excinfo.value)


def test_formula_unknown_box_ref_raises_value_error_not_key_error() -> None:
    parsed = [_mbox("1"), _fbox("4", "KMD:99")]
    with pytest.raises(ValueError, match="unknown box"):
        _evaluate_formula_boxes(parsed, {"1": Decimal("0")}, return_type="KMD")


def test_formula_foreign_return_type_prefix_rejected() -> None:
    parsed = [_mbox("1"), _fbox("4", "BAS:1A + KMD:1")]
    with pytest.raises(ValueError, match="return_type"):
        _evaluate_formula_boxes(parsed, {"1": Decimal("10.00")}, return_type="KMD")


def test_formula_max_requires_literal_zero_first_arg() -> None:
    parsed = [_mbox("1"), _fbox("4", "max(1, KMD:1)")]
    with pytest.raises(ValueError, match="literal 0"):
        _evaluate_formula_boxes(parsed, {"1": Decimal("10.00")}, return_type="KMD")


def test_formula_max_rejects_non_max_function_name() -> None:
    """'min(...)' isn't a recognised token at all — the grammar has
    exactly one function (max(0,·)); _evaluate_formula_boxes wraps the
    parser's FormulaSyntaxError into a ValueError (see the box-level
    'invalid formula' wrapping), so callers only ever see ValueError."""
    parsed = [_mbox("1"), _fbox("4", "min(0, KMD:1)")]
    with pytest.raises(ValueError, match="unexpected token"):
        _evaluate_formula_boxes(parsed, {"1": Decimal("10.00")}, return_type="KMD")


def test_formula_parser_raises_formula_syntax_error_directly() -> None:
    """FormulaSyntaxError itself (the parser's own exception type,
    exported for direct/unit use) is a ValueError subclass and is what
    _FormulaParser.parse() raises before _evaluate_formula_boxes wraps
    it."""
    from saebooks.services.tax_return_generator import _FormulaParser

    with pytest.raises(FormulaSyntaxError):
        _FormulaParser("min(0, KMD:1)", return_type="KMD", known_codes=frozenset({"1"})).parse()


def test_formula_rounding_half_up_on_exact_half_cent() -> None:
    """Rounding DIRECTION is UNVERIFIED per scope §3.2 (source confirms
    cent precision only); pins the scope's stated pragmatic default,
    ROUND_HALF_UP, at the one place it can bite: an exact half-cent tie
    from a rate x base multiplication."""
    parsed = [_mbox("1"), _fbox("4", "0.01*KMD:1")]
    ledger = {"1": Decimal("12.5"), "4": Decimal("0")}  # 0.01 * 12.5 = 0.125
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="KMD")
    assert amounts["4"] == Decimal("0.13")


def test_formula_rounds_every_box_not_just_formula_boxes() -> None:
    """Scope §3.2: round per box after aggregation/evaluation — applies
    uniformly, including ledger-sourced boxes that pass through a
    formula-bearing return unchanged."""
    parsed = [_mbox("1")]
    amounts = _evaluate_formula_boxes(parsed, {"1": Decimal("10")}, return_type="KMD")
    assert amounts["1"] == Decimal("10.00")


def test_formula_no_formula_boxes_is_a_pure_pass_through() -> None:
    """AU/NZ/UK regression guard: a box set with zero formula boxes
    (AU's BAS shape today) must not be perturbed by the formula pass
    beyond the uniform 2dp rounding step."""
    parsed = [_mbox("G1"), _mbox("1A")]
    ledger = {"G1": Decimal("1100.00"), "1A": Decimal("100.00")}
    amounts = _evaluate_formula_boxes(parsed, ledger, return_type="BAS")
    assert amounts == {"G1": Decimal("1100.00"), "1A": Decimal("100.00")}


# ---------------------------------------------------------------------------
# Helpers shared by the DB-backed tests below.
# ---------------------------------------------------------------------------


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, asset_account_id, income_account_id, gst_taxcode_id)
    scoped to the shared seed company, mirroring the fixture pattern in
    tests/api/v1/test_reports_bas_cashflow.py."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None, "seed company not found"

        async def _first_of(t: AccountType) -> uuid.UUID:
            acct = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id,
                        Account.account_type == t,
                        Account.is_header.is_(False),
                    ).order_by(Account.code)
                )
            ).scalars().first()
            assert acct is not None
            return acct.id

        gst_code = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "GST",
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == company.id,
                )
            )
        ).scalars().first()
        assert gst_code is not None, "Seed AU tax code GST not found"

        return (
            company.id,
            await _first_of(AccountType.ASSET),
            await _first_of(AccountType.INCOME),
            gst_code.id,
        )


async def _post_gst_sale(
    company_id: uuid.UUID,
    *,
    asset_id: uuid.UUID,
    income_id: uuid.UUID,
    gst_code_id: uuid.UUID,
    net: Decimal,
    gst: Decimal,
) -> None:
    """Dr Asset (net+gst) / Cr Income (net, tax_code_id=GST, gst_amount=gst)."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=_ENTRY_DATE,
            description="T8 generate_return test sale",
            lines=[
                {"account_id": asset_id, "debit": net + gst, "credit": Decimal("0")},
                {
                    "account_id": income_id,
                    "debit": Decimal("0"),
                    "credit": net,
                    "tax_code_id": gst_code_id,
                    "gst_amount": gst,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-t8")


# ---------------------------------------------------------------------------
# generate_return reproduces au.bas_report's pre-T8 numbers (thin-wrapper
# equivalence — the critical acceptance test for this theme).
# ---------------------------------------------------------------------------


async def test_generate_return_matches_au_bas_report_keystone_delta() -> None:
    """A $1,100 GST-inclusive taxable sale must move G1 by 1100 (inclusive)
    and 1A by 100 — the same keystone scenario
    tests/services/test_cashbook_bas.py pins for au.bas_report, now
    reproduced directly against generate_return (both AU's box-definition
    source and au.bas_report's underlying engine, since bas_report is a
    thin wrapper over it as of T8)."""
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    async def _g1_1a() -> tuple[Decimal, Decimal]:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AU", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        return result.amount("G1"), result.amount("1A")

    before_g1, before_1a = await _g1_1a()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("1000.00"), gst=Decimal("100.00"),
    )

    after_g1, after_1a = await _g1_1a()

    assert after_g1 - before_g1 == Decimal("1100.00"), (
        "generate_return G1 should move by the GST-inclusive 1100, "
        f"moved by {after_g1 - before_g1}"
    )
    assert after_1a - before_1a == Decimal("100.00"), (
        f"generate_return 1A should move by 100, moved by {after_1a - before_1a}"
    )


async def test_au_bas_report_thin_wrapper_matches_generate_return_directly() -> None:
    """au.bas_report(...) and generate_return(..., jurisdiction='AU',
    return_type='BAS') must agree box-for-box over the same window —
    bas_report is now a thin wrapper over generate_return, so this is a
    direct equivalence check, not just a delta comparison."""
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("500.00"), gst=Decimal("50.00"),
    )

    async with AsyncSessionLocal() as session:
        report = await bas_report(session, company_id, from_date=_FROM, to_date=_TO)
    async with AsyncSessionLocal() as session:
        result = await generate_return(
            session, company_id,
            jurisdiction="AU", return_type="BAS",
            from_date=_FROM, to_date=_TO,
        )

    assert report.g1.amount == result.amount("G1")
    assert report.g2.amount == result.amount("G2")
    assert report.g3.amount == result.amount("G3")
    assert report.g10.amount == result.amount("G10")
    assert report.g11.amount == result.amount("G11")
    assert report.label_1a.amount == result.amount("1A")
    assert report.label_1b.amount == result.amount("1B")
    assert report.g1.description == result.boxes["G1"].box_label


# ---------------------------------------------------------------------------
# Resilience — the standard test/CI environment does NOT configure
# REFERENCE_DATABASE_URL (only REFERENCE_MIGRATION_DATABASE_URL, for
# migrations — see docker-compose.test.yml), so this exercises the
# embedded-fallback path every other BAS test in the suite transitively
# relies on. Explicit here so a regression shows up as a T8-scoped
# failure, not a mysterious BAS-wide outage.
# ---------------------------------------------------------------------------


async def test_generate_return_falls_back_when_reference_db_not_configured() -> None:
    if ReferenceSession is not None:
        pytest.skip(
            "REFERENCE_DATABASE_URL is configured in this environment — "
            "the fallback path isn't exercised here; see "
            "test_generate_return_reads_reference_table_when_configured "
            "for the reference-DB-present case."
        )
    company_id, _asset_id, _income_id, _gst_code_id = await _ctx()
    async with AsyncSessionLocal() as session:
        result = await generate_return(
            session, company_id,
            jurisdiction="AU", return_type="BAS",
            from_date=_FROM, to_date=_TO,
        )
    assert result.source == "embedded_fallback"
    # The fallback box set must still be the full AU BAS box list.
    assert set(result.boxes) == {"G1", "G2", "G3", "G10", "G11", "1A", "1B"}


async def test_generate_return_falls_back_with_reference_db_canonical_jurisdiction_code() -> None:
    """Bug fix (round 6) — a caller supplying the reference-DB's own
    3-char canonical code ('AUS', per models/reference/jurisdiction.py)
    must still hit the embedded fallback, not a spurious 'no box
    definitions' ValueError. Previously the fallback lookup used the
    raw un-mapped jurisdiction, which only ever matched the 2-letter
    engine code.

    Goes beyond a box-codes-only check: posts the same keystone GST
    sale as test_generate_return_matches_au_bas_report_keystone_delta
    and asserts the G1/1A DELTA is identical — proving 'AUS' doesn't
    just avoid the ValueError but produces the correct AU BAS numbers
    (aggregation is company_id/date-scoped, not jurisdiction-filtered,
    so this also guards against a future change coupling the two)."""
    if ReferenceSession is not None:
        pytest.skip(
            "REFERENCE_DATABASE_URL is configured in this environment — "
            "the fallback path isn't exercised here."
        )
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    async def _g1_1a() -> tuple[Decimal, Decimal]:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AUS", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        assert result.source == "embedded_fallback"
        assert set(result.boxes) == {"G1", "G2", "G3", "G10", "G11", "1A", "1B"}
        return result.amount("G1"), result.amount("1A")

    before_g1, before_1a = await _g1_1a()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("1000.00"), gst=Decimal("100.00"),
    )

    after_g1, after_1a = await _g1_1a()

    assert after_g1 - before_g1 == Decimal("1100.00"), (
        "generate_return(jurisdiction='AUS') G1 should move by the "
        f"GST-inclusive 1100, moved by {after_g1 - before_g1}"
    )
    assert after_1a - before_1a == Decimal("100.00"), (
        "generate_return(jurisdiction='AUS') 1A should move by 100, "
        f"moved by {after_1a - before_1a}"
    )


# ---------------------------------------------------------------------------
# Proves generate_return actually reads TaxReturnBoxDefinition (not a
# hardcoded box list) — inserts a temporary, uniquely-named box and
# confirms it comes back verbatim. Skipped in the standard test/CI shape
# (same gate as tests/integration/test_cross_db_join.py).
# ---------------------------------------------------------------------------

_reference_db_configured = bool(
    os.environ.get("REFERENCE_DATABASE_URL") and os.environ.get("REFERENCE_MIGRATION_DATABASE_URL")
)


@pytest.mark.skipif(
    not _reference_db_configured,
    reason="REFERENCE_DATABASE_URL / REFERENCE_MIGRATION_DATABASE_URL not both configured",
)
async def test_generate_return_reads_reference_table_when_configured() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.models.reference.tax_return_box_definition import TaxReturnBoxDefinition
    from saebooks.services.reference.loader import load_seeds

    await load_seeds("AU", version_tag="test-t8")

    company_id, _asset_id, _income_id, _gst_code_id = await _ctx()

    test_box_id = uuid.uuid4()
    assert ReferenceMigrationSession is not None
    async with ReferenceMigrationSession() as ref:
        ref.add(
            TaxReturnBoxDefinition(
                id=test_box_id,
                jurisdiction="AUS",
                return_type="BAS",
                box_code="ZZTEST8",
                box_label="T8 proof-of-read box",
                aggregation="manual",
                feeder_tax_codes=[],
                display_order=999,
            )
        )
        await ref.commit()

    try:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AU", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        assert result.source == "reference_db"
        assert "ZZTEST8" in result.boxes, (
            "generate_return did not surface the temporary reference-DB "
            "row — it is not actually reading TaxReturnBoxDefinition"
        )
        assert result.boxes["ZZTEST8"].box_label == "T8 proof-of-read box"
        assert result.boxes["ZZTEST8"].amount == Decimal("0")  # manual box
    finally:
        async with ReferenceMigrationSession() as ref:
            row = await ref.get(TaxReturnBoxDefinition, test_box_id)
            if row is not None:
                await ref.delete(row)
                await ref.commit()


# ---------------------------------------------------------------------------
# Bug fix (round 3) — indirect_tax_rate_percent must pick AU's GST (10%)
# as the "standard vat_gst rate", not excise-type codes (WET 29%, LCT
# 33%) that also live in the AU tax_codes seed with a higher
# rate_percent. Before the fix, WET/LCT had no explicit tax_family in
# the seed YAML, so they defaulted (via the RefTaxCode server_default)
# to 'vat_gst' — the same family as GST — and the highest-rate-wins
# query picked LCT's 33% instead of GST's 10%. The fix tags WET/LCT as
# tax_family='excise' in the seed so the vat_gst filter genuinely
# narrows to GST-family codes only.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _reference_db_configured,
    reason="REFERENCE_DATABASE_URL / REFERENCE_MIGRATION_DATABASE_URL not both configured",
)
async def test_indirect_tax_rate_percent_picks_gst_not_wet_or_lct() -> None:
    from saebooks.services.reference.loader import load_seeds
    from saebooks.services.tax_return_generator import (
        indirect_tax_inclusive_fraction,
        indirect_tax_rate_percent,
    )

    await load_seeds("AU", version_tag="test-t8-rate")

    rate = await indirect_tax_rate_percent("AU")
    assert rate == Decimal("10.0000"), (
        "expected AU's standard vat_gst rate to be GST's 10%, not an "
        "excise-family code (WET 29% / LCT 33%) sharing the vat_gst "
        f"family via an untagged seed row; got {rate}"
    )

    fraction = await indirect_tax_inclusive_fraction("AU")
    assert fraction == Decimal("10.0000") / Decimal("110.0000")


# ---------------------------------------------------------------------------
# KMD-formula support Packet 2 — EE domestic golden-file test (see
# ~/.claude/plans/kmd-formula-support-scope.md §6 for the hand-computed
# figures this pins).
#
# Why this doesn't go through generate_return(jurisdiction="EE", ...):
# REFERENCE_DATABASE_URL is never configured in this test harness (only
# REFERENCE_MIGRATION_DATABASE_URL — see docker-compose.test.yml and the
# module docstring's "Resilience" section above), so ReferenceSession is
# always None here and generate_return falls back to
# _FALLBACK_BOX_DEFINITIONS, which only has an AU/BAS entry — there is no
# embedded EE fallback (by design; EE is reference-DB-only). Rather than
# skip the strongest achievable test, this reads the REAL EE KMD seed
# YAML directly (same file the reference DB loader applies verbatim —
# tests/seeds/test_tax_return_box_definitions_seed.py's EE tests pin its
# grammar/shape) and drives the real `_aggregate_ledger_by_box` +
# `_evaluate_formula_boxes` — the exact two passes `generate_return`
# composes — against a fresh throwaway company's real posted ledger. The
# only thing bypassed is the reference-DB row *fetch* itself, which
# Packet 1's own `test_generate_return_reads_reference_table_when_configured`
# already covers generically (gated the same way, for AU).
# ---------------------------------------------------------------------------

_EE_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "EE"
    / "tax_return_box_definitions.yaml"
)


def _ee_kmd_parsed_boxes() -> list[_ParsedBox]:
    doc = yaml.safe_load(_EE_SEED_PATH.read_text())
    rows = [r for r in doc["rows"] if r["return_type"] == "KMD"]
    return [
        _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r.get("feeder_tax_codes") or [],
                display_order=r["display_order"],
                formula=r.get("formula"),
            )
        )
        for r in rows
    ]


# The scope's §6 golden-file domestic-only period, in full.
_EE_GOLDEN_EXPECTED: dict[str, Decimal] = {
    "1": Decimal("10000.00"), "1-1": Decimal("0.00"), "1-2": Decimal("0.00"),
    "2": Decimal("2000.00"), "2-1": Decimal("0.00"), "2-2": Decimal("1000.00"),
    "3": Decimal("5000.00"), "3.1": Decimal("0.00"), "3.1.1": Decimal("0.00"),
    "3.2": Decimal("5000.00"), "3.2.1": Decimal("0.00"),
    "4": Decimal("2710.00"), "4-1": Decimal("0.00"),
    "5": Decimal("840.00"), "5.1": Decimal("0.00"), "5.2": Decimal("240.00"),
    "5.3": Decimal("0.00"), "5.4": Decimal("0.00"),
    "6": Decimal("0.00"), "6.1": Decimal("0.00"), "7": Decimal("0.00"), "7.1": Decimal("0.00"),
    "8": Decimal("500.00"),
    "9": Decimal("0.00"), "10": Decimal("0.00"), "11": Decimal("0.00"),
    "12": Decimal("1870.00"), "13": Decimal("0.00"),
}


async def _make_ee_company(*, jurisdiction: str = "AU") -> uuid.UUID:
    """A throwaway company (own chart, own EE tax codes) so this golden
    test's absolute box totals are never contaminated by any other
    module's postings against the shared AU seed company (the isolation
    concern test_cashbook_bas.py's docstring flags for that shared
    company) — every box here is an absolute value, not a delta, so
    isolation matters more than in the delta-based AU tests.

    ``jurisdiction`` defaults to "AU" (Company.jurisdiction's own column
    default, unchanged from pre-Packet-3) — the domestic-only golden
    tests below never touch reverse-charge codes, so which engine
    ``services.journal._apply_tax_treatment`` dispatches to makes no
    numerical difference for them (AU's and EE's ``compute()`` derive
    base/tax/direction identically for an ordinary line — see
    tests/services/test_tax_engine_ee.py), and leaving the default
    untouched keeps those two tests byte-for-byte unperturbed by Packet
    3. The RC-FANOUT test below passes ``jurisdiction="EE"`` explicitly
    — it NEEDS the real EETaxEngine's reverse-charge fan-out, which the
    AU engine does not have."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"KMD Golden {company_id.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                jurisdiction=jurisdiction,
            )
        )
        await session.flush()

        # GST auto-post settings are a GLOBAL table (services/settings.py
        # has no company scoping), not per-company — set them to the same
        # convention tests/services/test_cashbook_bas.py already
        # establishes (2-1310 / 2-1330) so this is idempotent alongside
        # every other test that also sets them, and so this company's OWN
        # matching-coded accounts (below) resolve regardless of test
        # execution order.
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")

        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Bank", account_type=AccountType.ASSET),
            "income": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="4-1000", name="Sales", account_type=AccountType.INCOME),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "fixed_asset": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1500", name="Fixed Assets", account_type=AccountType.ASSET),
            "gst_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="GST Collected", account_type=AccountType.LIABILITY),
            "gst_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="GST Paid", account_type=AccountType.ASSET),
            # KMD-formula support Packet 3 — the reverse-charge test posts
            # the OUTPUT-side self-assessed liability to this account
            # explicitly (gst_svc.auto_post_gst_lines only auto-adds ONE
            # line per taxable line, keyed off account_type — it handles
            # the INPUT side (GST Paid) automatically for an EXPENSE-bucket
            # line, same as any ordinary domestic purchase, but has no
            # concept of a reverse charge needing a SECOND, output-side
            # auto-line; extending it is out of this packet's scope — see
            # this packet's build report). Manually balancing the entry
            # this way keeps the GL double-entry correct without touching
            # gst_svc.auto_post_gst_lines.
            "vat_rc_payable": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1350", name="VAT self-assessed (reverse charge)", account_type=AccountType.LIABILITY),
        }
        for acct in accounts.values():
            session.add(acct)
        await session.flush()

        tax_codes = {
            "standard": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-STD", name="EE 24%", rate=Decimal("24.000"), tax_system="VAT", jurisdiction="EE", reporting_type="standard"),
            "reduced_9": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-RED9", name="EE 9%", rate=Decimal("9.000"), tax_system="VAT", jurisdiction="EE", reporting_type="reduced_9"),
            "reduced_13": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-RED13", name="EE 13%", rate=Decimal("13.000"), tax_system="VAT", jurisdiction="EE", reporting_type="reduced_13"),
            "zero_export": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-ZEXP", name="EE export 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="EE", reporting_type="zero_export"),
            "exempt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-EXEMPT", name="EE exempt", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="EE", reporting_type="exempt"),
            "capital": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EE-CAP", name="EE capital 24%", rate=Decimal("24.000"), tax_system="VAT", jurisdiction="EE", reporting_type="capital"),
            # KMD-formula support Packet 3 — reverse-charge EU-acquisition
            # of goods, current standard rate. direction/reverse_charge
            # aren't columns on the company-side TaxCode model (see
            # services.tax_engine.ee's module docstring on why RC
            # detection keys off reporting_type, not a schema flag); this
            # row just needs to exist so a posted line can reference it.
            "rc_eu_acq_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="RC-EUACQ", name="EE reverse charge — EU acquisition of goods (24%)", rate=Decimal("24.000"), tax_system="VAT", jurisdiction="EE", reporting_type="rc_eu_acq_goods"),
        }
        for tc in tax_codes.values():
            session.add(tc)
        await session.commit()

    return company_id


async def _post_ee_two_line(
    company_id: uuid.UUID,
    *,
    entry_date: date,
    description: str,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
    debit_amount: Decimal,
    credit_amount: Decimal,
    tax_line: str,  # "debit" | "credit" — which side carries tax_code_id/gst_amount
    tax_code_id: uuid.UUID | None,
    gst: Decimal,
) -> None:
    """Post a fully-balanced 2-line entry; gst_svc.auto_post_gst_lines adds
    the 3rd (GST Collected/Paid) line at post time when gst != 0, matching
    tests/services/test_tax_return_generator.py's existing _post_gst_sale
    shape (sale side) and its purchase-side mirror image."""
    async with AsyncSessionLocal() as session:
        debit_line: dict[str, object] = {"account_id": debit_account_id, "debit": debit_amount, "credit": Decimal("0")}
        credit_line: dict[str, object] = {"account_id": credit_account_id, "debit": Decimal("0"), "credit": credit_amount}
        if tax_line == "debit":
            debit_line["tax_code_id"] = tax_code_id
            debit_line["gst_amount"] = gst
        else:
            credit_line["tax_code_id"] = tax_code_id
            credit_line["gst_amount"] = gst

        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=description,
            lines=[debit_line, credit_line],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-kmd-golden")


async def _post_ee_sale(company_id, accounts, tax_code_id, *, entry_date, net, gst, label) -> None:
    await _post_ee_two_line(
        company_id, entry_date=entry_date, description=f"KMD golden — {label}",
        debit_account_id=accounts["bank"], credit_account_id=accounts["income"],
        debit_amount=net + gst, credit_amount=net,
        tax_line="credit", tax_code_id=tax_code_id, gst=gst,
    )


async def _post_ee_purchase(company_id, accounts, tax_code_id, *, entry_date, expense_account_id, net, gst, label) -> None:
    await _post_ee_two_line(
        company_id, entry_date=entry_date, description=f"KMD golden — {label}",
        debit_account_id=expense_account_id, credit_account_id=accounts["bank"],
        debit_amount=net, credit_amount=net + gst,
        tax_line="debit", tax_code_id=tax_code_id, gst=gst,
    )


async def _kmd_box_vector(
    company_id: uuid.UUID, *, from_date: date, to_date: date,
    statuses: tuple[EntryStatus, ...] = (EntryStatus.POSTED,),
) -> dict[str, Decimal]:
    # Reversal-aware callers pass REPORTABLE_STATUSES (POSTED + REVERSED)
    # so BOTH the reversed original and its reversal are in the window and
    # net — POSTED-only excludes the REVERSED original but keeps the
    # (oppositely-signed) reversal, which cannot net a pair. Non-reversal
    # golden tests keep the POSTED-only default (unchanged).
    parsed = _ee_kmd_parsed_boxes()
    async with AsyncSessionLocal() as session:
        ledger_amounts = await _aggregate_ledger_by_box(
            session, parsed,
            company_id=company_id, tenant_id=None,
            from_date=from_date, to_date=to_date,
            statuses=statuses, exclude_archived=False,
        )
    return _evaluate_formula_boxes(parsed, ledger_amounts, return_type="KMD")


async def test_ee_kmd_domestic_golden_period_payable() -> None:
    """The scope's §6 domestic-only golden period, posted for real and
    read back through the real box-set (feeder_tax_codes, box 4's
    rate-formula, box 12/13's max(0,·) split) — asserts every one of the
    28 KMD boxes, not just the ones this period touches, so a box that
    should read 0 but doesn't (feeder collision) fails loudly too."""
    company_id = await _make_ee_company()
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        accounts_result = await session.execute(
            select(Account.code, Account.id).where(Account.company_id == company_id)
        )
        by_code = {code: aid for code, aid in accounts_result.all()}
        tax_result = await session.execute(
            select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
        )
        tax_by_type = {rt: tid for rt, tid in tax_result.all()}
        assert company is not None

    accounts = {
        "bank": by_code["1-1110"], "income": by_code["4-1000"],
        "expense": by_code["5-1000"], "fixed_asset": by_code["1-1500"],
    }

    period_from, period_to = date(2026, 1, 1), date(2026, 1, 31)
    entry_date = date(2026, 1, 15)

    await _post_ee_sale(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, net=Decimal("10000.00"), gst=Decimal("2400.00"), label="standard 24%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_9"], entry_date=entry_date, net=Decimal("2000.00"), gst=Decimal("180.00"), label="reduced 9%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_13"], entry_date=entry_date, net=Decimal("1000.00"), gst=Decimal("130.00"), label="reduced 13%")
    await _post_ee_sale(company_id, accounts, tax_by_type["zero_export"], entry_date=entry_date, net=Decimal("5000.00"), gst=Decimal("0.00"), label="export")
    await _post_ee_sale(company_id, accounts, tax_by_type["exempt"], entry_date=entry_date, net=Decimal("500.00"), gst=Decimal("0.00"), label="exempt")
    await _post_ee_purchase(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, expense_account_id=accounts["expense"], net=Decimal("2500.00"), gst=Decimal("600.00"), label="standard-rate purchase")
    await _post_ee_purchase(company_id, accounts, tax_by_type["capital"], entry_date=entry_date, expense_account_id=accounts["fixed_asset"], net=Decimal("1000.00"), gst=Decimal("240.00"), label="fixed-asset purchase")

    amounts = await _kmd_box_vector(company_id, from_date=period_from, to_date=period_to)

    for box_code, expected in _EE_GOLDEN_EXPECTED.items():
        assert amounts.get(box_code) == expected, (
            f"KMD box {box_code!r} expected {expected}, got {amounts.get(box_code)}"
        )


async def test_ee_kmd_domestic_golden_period_refund() -> None:
    """Refund variant (scope §6): same output VAT (box 4 = 2710.00), but
    input VAT of 3,000.00 flips the net negative — box 12 = 0.00, box 13
    = 290.00. Exercises the max(0,·) split's other branch against real
    posted data, in a disjoint date window on the SAME company/chart/tax
    codes as the payable test (no cross-contamination — different
    from_date/to_date window, mirroring this file's existing delta-test
    isolation convention)."""
    company_id = await _make_ee_company()
    async with AsyncSessionLocal() as session:
        accounts_result = await session.execute(
            select(Account.code, Account.id).where(Account.company_id == company_id)
        )
        by_code = {code: aid for code, aid in accounts_result.all()}
        tax_result = await session.execute(
            select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
        )
        tax_by_type = {rt: tid for rt, tid in tax_result.all()}

    accounts = {"bank": by_code["1-1110"], "income": by_code["4-1000"], "expense": by_code["5-1000"]}
    entry_date = date(2026, 3, 15)

    await _post_ee_sale(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, net=Decimal("10000.00"), gst=Decimal("2400.00"), label="refund period standard 24%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_9"], entry_date=entry_date, net=Decimal("2000.00"), gst=Decimal("180.00"), label="refund period reduced 9%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_13"], entry_date=entry_date, net=Decimal("1000.00"), gst=Decimal("130.00"), label="refund period reduced 13%")
    await _post_ee_purchase(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, expense_account_id=accounts["expense"], net=Decimal("12500.00"), gst=Decimal("3000.00"), label="refund period large purchase")

    amounts = await _kmd_box_vector(company_id, from_date=date(2026, 3, 1), to_date=date(2026, 3, 31))

    assert amounts["4"] == Decimal("2710.00")
    assert amounts["5"] == Decimal("3000.00")
    assert amounts["12"] == Decimal("0.00")
    assert amounts["13"] == Decimal("290.00")


# ---------------------------------------------------------------------------
# KMD-formula support Packet 3 — reverse-charge fan-out (RC-FANOUT, scope
# §3.4). Posts a REAL EU-acquisition purchase line through the full
# per-jurisdiction dispatch path (services.journal.post ->
# _apply_tax_treatment -> get_engine("EE") -> EETaxEngine.compute_components
# -> two JournalLineTaxComponent rows) and reads the KMD box vector back —
# the same real-posting-plus-real-aggregation shape as the domestic golden
# tests above, not a bypassed/synthetic one.
# ---------------------------------------------------------------------------


async def _post_ee_reverse_charge_purchase(
    company_id: uuid.UUID,
    accounts: dict[str, uuid.UUID],
    tax_code_id: uuid.UUID,
    *,
    entry_date: date,
    net: Decimal,
    self_assessed_vat: Decimal,
    label: str,
) -> None:
    """Post a balanced EU-acquisition reverse-charge purchase.

    Three explicit lines + one gst_svc.auto_post_gst_lines auto-line:
      DR expense          net                (tax_code_id=RC, gst_amount=vat — triggers
                                                the dispatcher's dual-component fan-out AND
                                                auto_post_gst_lines' single INPUT-side line)
      CR bank              net                (the foreign supplier is paid the net invoice
                                                amount only — no VAT was charged by them)
      CR vat_rc_payable     vat                (the OUTPUT-side self-assessed liability,
                                                booked explicitly — see _make_ee_company's
                                                account comment for why this isn't auto-posted)
      DR GST Paid           vat  (AUTO, from gst_svc.auto_post_gst_lines — the INPUT side)
    Balances: DR = net + vat; CR = net + vat.
    """
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"KMD RC-FANOUT — {label}",
            lines=[
                {
                    "account_id": accounts["expense"],
                    "debit": net,
                    "credit": Decimal("0"),
                    "tax_code_id": tax_code_id,
                    "gst_amount": self_assessed_vat,
                },
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net},
                {
                    "account_id": accounts["vat_rc_payable"],
                    "debit": Decimal("0"),
                    "credit": self_assessed_vat,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-kmd-rc")


async def test_ee_kmd_reverse_charge_eu_acquisition_lands_output_and_input_roles() -> None:
    """The scope's §6 RC golden period (base €4,000, self-assessed at
    24% = €960.00), posted through the REAL dispatcher/engine — asserts
    BOTH roles land in the right boxes:
      * box 1 (output taxable base) includes the €4,000 RC base
      * box 4 (rate-formula) auto-includes the €960.00 output VAT —
        needed NO formula change, just box 1 carrying the RC base
      * box 5 (input VAT) includes the €960.00 deductible input VAT
      * box 6 / 6.1 (informative acquisition totals) = €4,000.00 —
        unchanged from Packet 2, still the plain purchase-bucket read
      * box 12/13 net UNCHANGED by the RC (output 960 == input 960) —
        the scope's own "canonical proof the fan-out is balanced"

    Uses a company with jurisdiction="EE" explicitly (see
    _make_ee_company's docstring) — this is the one test in this file
    that actually exercises EETaxEngine's reverse-charge fan-out via
    the real per-jurisdiction dispatcher in
    services.journal._apply_tax_treatment.
    """
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    accounts = {
        "bank": by_code["1-1110"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }

    period_from, period_to = date(2026, 5, 1), date(2026, 5, 31)
    await _post_ee_reverse_charge_purchase(
        company_id, accounts, tax_by_type["rc_eu_acq_goods"],
        entry_date=date(2026, 5, 15), net=Decimal("4000.00"),
        self_assessed_vat=Decimal("960.00"), label="EU acquisition of goods",
    )

    amounts = await _kmd_box_vector(company_id, from_date=period_from, to_date=period_to)

    assert amounts["1"] == Decimal("4000.00"), "box 1 should carry the RC acquisition base"
    assert amounts["1_DOMESTIC"] == Decimal("0.00")
    assert amounts["1_RC"] == Decimal("4000.00")
    assert amounts["4"] == Decimal("960.00"), "box 4's rate-formula should auto-include the RC output VAT"
    assert amounts["5"] == Decimal("960.00"), "box 5 should carry the RC deductible input VAT"
    assert amounts["5_DOMESTIC"] == Decimal("0.00")
    assert amounts["5_RC"] == Decimal("960.00")
    assert amounts["6"] == Decimal("4000.00"), "box 6 (informative) unchanged from Packet 2"
    assert amounts["6.1"] == Decimal("4000.00")
    assert amounts["12"] == Decimal("0.00"), "balanced RC: output == input, net payable unaffected"
    assert amounts["13"] == Decimal("0.00")


async def test_ee_kmd_reverse_charge_9pct_lands_in_box_2_not_box_1() -> None:
    """Finding 1 (rate-aware RC routing): a 9% EU-acquisition (books from
    an EU publisher), SAME rc_eu_acq_goods reporting_type tag as the 24%
    case but rate 9%, must land in box 2 (9%), NOT box 1 (24%) — the rate
    discriminates. box 4's rate-formula then taxes it at 0.09 (= €180),
    box 5 deducts the €180 input VAT, and box 12/13 net to 0 (balanced)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        # A 9%-rate EU acquisition carrying the SAME rc_eu_acq_goods
        # reporting_type tag as the 24% code — created here (not in the
        # shared fixture) so it can't collide with the reporting_type→id
        # lookup the 24% tests use. The rate, not the tag, routes it.
        rc9 = TaxCode(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="RC-EUACQ9",
            name="EE reverse charge — EU acquisition of books (9%)",
            rate=Decimal("9.000"), tax_system="VAT", jurisdiction="EE",
            reporting_type="rc_eu_acq_goods",
        )
        session.add(rc9)
        await session.flush()
        rc9_tc_id = rc9.id
        await session.commit()

    accounts = {
        "bank": by_code["1-1110"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }
    period_from, period_to = date(2026, 5, 1), date(2026, 5, 31)
    await _post_ee_reverse_charge_purchase(
        company_id, accounts, rc9_tc_id,
        entry_date=date(2026, 5, 15), net=Decimal("2000.00"),
        self_assessed_vat=Decimal("180.00"), label="EU acquisition of books (9%)",
    )

    amounts = await _kmd_box_vector(company_id, from_date=period_from, to_date=period_to)

    assert amounts["1"] == Decimal("0.00"), "9% RC must NOT land in box 1 (24%)"
    assert amounts["1_RC"] == Decimal("0.00")
    assert amounts["2"] == Decimal("2000.00"), "9% RC base lands in box 2"
    assert amounts["2_RC"] == Decimal("2000.00")
    assert amounts["2_DOMESTIC"] == Decimal("0.00")
    assert amounts["4"] == Decimal("180.00"), "box 4 taxes the 9% base at 0.09"
    assert amounts["5"] == Decimal("180.00"), "box 5 deducts the 9% input VAT"
    assert amounts["5_RC"] == Decimal("180.00")
    assert amounts["6"] == Decimal("2000.00"), "box 6 informative acquisition base"
    assert amounts["6.1"] == Decimal("2000.00")
    assert amounts["12"] == Decimal("0.00"), "balanced RC: output == input"
    assert amounts["13"] == Decimal("0.00")


async def test_ee_kmd_reverse_charge_reversal_nets_all_boxes() -> None:
    """Finding 3 — reversing a posted reverse-charge acquisition nets the
    role-keyed boxes (1_RC output base, 5_RC input VAT) back to zero in
    lock-step with the account-type box 6, so boxes 1/4/5 stay consistent
    with 6/6.1 across the void (previously the base nulled but the tax
    boxes stayed overstated)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        rc_tc_id = (
            await session.execute(
                select(TaxCode.id).where(
                    TaxCode.company_id == company_id, TaxCode.code == "RC-EUACQ"
                )
            )
        ).scalars().one()

    accounts = {"bank": by_code["1-1110"], "expense": by_code["5-1000"], "vat_rc_payable": by_code["2-1350"]}
    period_from, period_to = date(2026, 5, 1), date(2026, 5, 31)
    await _post_ee_reverse_charge_purchase(
        company_id, accounts, rc_tc_id,
        entry_date=date(2026, 5, 15), net=Decimal("4000.00"),
        self_assessed_vat=Decimal("960.00"), label="EU acquisition (to be reversed)",
    )

    # Find and reverse the RC entry.
    from saebooks.models.journal import JournalEntry, JournalLine
    async with AsyncSessionLocal() as session:
        rc_entry_id = (
            await session.execute(
                select(JournalEntry.id)
                .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
                .where(JournalEntry.company_id == company_id,
                       JournalLine.account_id == accounts["expense"],
                       JournalLine.tax_code_id == rc_tc_id)
            )
        ).scalars().first()
        assert rc_entry_id is not None
    async with AsyncSessionLocal() as session:
        await journal_svc.reverse(session, rc_entry_id, posted_by="pytest-rc-void", tenant_id=DEFAULT_TENANT_ID)

    amounts = await _kmd_box_vector(
        company_id, from_date=period_from, to_date=period_to,
        statuses=REPORTABLE_STATUSES,
    )
    for box in ("1", "1_RC", "4", "5", "5_RC", "6", "6.1", "12", "13"):
        assert amounts[box] == Decimal("0.00"), f"box {box} should net to 0 after reversal, got {amounts[box]}"


async def test_ee_kmd_reverse_charge_posts_two_components_output_and_input() -> None:
    """Lower-level companion to the box-vector test above: asserts the
    ACTUAL JournalLineTaxComponent rows the dispatcher wrote — two
    components on the one RC line, component_role
    'reverse_charge_output'/'reverse_charge_input', both tax=960.00,
    directions 'output'/'input' — the concrete database-level proof of
    the scope's §3.4 point 2 design, one layer beneath the box totals."""
    from saebooks.models.journal import JournalLine
    from saebooks.models.journal_line_tax_component import JournalLineTaxComponent

    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    accounts = {
        "bank": by_code["1-1110"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }
    entry_date = date(2026, 6, 15)
    await _post_ee_reverse_charge_purchase(
        company_id, accounts, tax_by_type["rc_eu_acq_goods"],
        entry_date=entry_date, net=Decimal("1000.00"),
        self_assessed_vat=Decimal("240.00"), label="single-line component proof",
    )

    async with AsyncSessionLocal() as session:
        rc_line_id = (
            await session.execute(
                select(JournalLine.id).where(
                    JournalLine.account_id == accounts["expense"],
                    JournalLine.tax_code_id == tax_by_type["rc_eu_acq_goods"],
                )
            )
        ).scalars().first()
        assert rc_line_id is not None

        components = (
            await session.execute(
                select(JournalLineTaxComponent).where(
                    JournalLineTaxComponent.journal_line_id == rc_line_id
                )
            )
        ).scalars().all()

    assert len(components) == 2, f"expected exactly 2 components on the RC line, got {len(components)}"
    by_role = {c.component_role: c for c in components}
    assert set(by_role) == {"reverse_charge_output", "reverse_charge_input"}

    output = by_role["reverse_charge_output"]
    input_ = by_role["reverse_charge_input"]
    assert output.direction == "output"
    assert input_.direction == "input"
    assert output.tax_amount == Decimal("240.00")
    assert input_.tax_amount == Decimal("240.00")
    assert output.base_amount == input_.base_amount == Decimal("1000.00")
    assert output.sequence == 0
    assert input_.sequence == 1
    assert output.ref_tax_code == input_.ref_tax_code == "RC-EUACQ"


async def test_ee_kmd_reverse_charge_gst_amount_none_still_lands_in_boxes() -> None:
    """Critic-round-3 fix: the NATURAL shape of a fresh EU-acquisition
    reverse-charge post is gst_amount=None — the foreign supplier's
    invoice carries no VAT to copy in, self-assessment is exactly what
    reverse charge means (EETaxEngine._compute_reverse_charge's
    ``_derive_tax`` already falls back to base*rate/100 for this case).
    Before this fix, ``journal._apply_tax_treatment``'s component gate
    required ``gst_amount is not None`` unconditionally, so a line posted
    this way got NO JournalLineTaxComponent rows at all — KMD boxes
    1_RC/5_RC (component-only, no gst_amount fallback) silently read 0
    while box 6 (plain purchase-bucket net) still picked up the base,
    producing a filable-but-wrong return. Same golden shape as
    ``test_ee_kmd_reverse_charge_eu_acquisition_lands_output_and_input_roles``
    but with gst_amount omitted on the expense line and no explicit
    vat_rc_payable line (nothing to balance against — the entry is just
    DR expense / CR bank, net-only)."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    bank_id = by_code["1-1110"]
    expense_id = by_code["5-1000"]
    rc_tax_code_id = tax_by_type["rc_eu_acq_goods"]

    period_from, period_to = date(2026, 5, 1), date(2026, 5, 31)
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 5, 15),
            description="KMD RC-FANOUT — gst_amount=None natural self-assessment shape",
            lines=[
                {
                    "account_id": expense_id,
                    "debit": Decimal("4000.00"),
                    "credit": Decimal("0"),
                    "tax_code_id": rc_tax_code_id,
                    "gst_amount": None,
                },
                {"account_id": bank_id, "debit": Decimal("0"), "credit": Decimal("4000.00")},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-kmd-rc-none")

    amounts = await _kmd_box_vector(company_id, from_date=period_from, to_date=period_to)

    assert amounts["1"] == Decimal("4000.00"), "box 1 must carry the RC base even with gst_amount=None"
    assert amounts["1_RC"] == Decimal("4000.00")
    assert amounts["4"] == Decimal("960.00"), "box 4 must auto-include the derived RC output VAT"
    assert amounts["5"] == Decimal("960.00"), "box 5 must carry the derived RC deductible input VAT"
    assert amounts["5_RC"] == Decimal("960.00")
    assert amounts["6"] == Decimal("4000.00")
    assert amounts["12"] == Decimal("0.00"), "balanced RC: derived output == derived input"
    assert amounts["13"] == Decimal("0.00")


async def test_ee_kmd_reverse_charge_reversal_does_not_double_count() -> None:
    """Finding 3 (updated): reversing a posted RC line MIRRORS the
    original's components onto the reversal (so the tax boxes net), and
    the aggregator signs the reversal negative — so over the reversal-
    aware window (REPORTABLE_STATUSES: original REVERSED + reversal
    POSTED) box 1_RC/5_RC net to 0, they neither double nor stay
    overstated. This supersedes the earlier 'reversal emits no
    components' rule that left the tax un-netted."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    accounts = {
        "bank": by_code["1-1110"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }
    period_from, period_to = date(2026, 7, 1), date(2026, 7, 31)
    entry_date = date(2026, 7, 15)

    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description="KMD RC-FANOUT — reversal double-count regression",
            lines=[
                {
                    "account_id": accounts["expense"],
                    "debit": Decimal("1000.00"),
                    "credit": Decimal("0"),
                    "tax_code_id": tax_by_type["rc_eu_acq_goods"],
                    "gst_amount": Decimal("240.00"),
                },
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": Decimal("1000.00")},
                {
                    "account_id": accounts["vat_rc_payable"],
                    "debit": Decimal("0"),
                    "credit": Decimal("240.00"),
                },
            ],
        )
        posted = await journal_svc.post(session, entry.id, posted_by="pytest-kmd-rc-reverse")
        entry_id = posted.id

    async with AsyncSessionLocal() as session:
        await journal_svc.reverse(session, entry_id, posted_by="pytest-kmd-rc-reverse", tenant_id=DEFAULT_TENANT_ID)

    amounts = await _kmd_box_vector(
        company_id, from_date=period_from, to_date=period_to,
        statuses=REPORTABLE_STATUSES,
    )

    assert amounts["1_RC"] == Decimal("0.00"), "reversal nets the RC output base"
    assert amounts["5_RC"] == Decimal("0.00"), "reversal nets the RC input base"
    assert amounts["1"] == Decimal("0.00")
    assert amounts["5"] == Decimal("0.00")
    assert amounts["6"] == Decimal("0.00"), "account-type base nets too"


# ---------------------------------------------------------------------------
# KMD-formula support Packet 4 — persist_return (tax_returns persistence,
# scope §4/§7 Packet 4). Jurisdiction/return-type generic — this section
# exercises it with the same EE/KMD golden vector as the tests above, but
# nothing in persist_return itself is EE-specific (see its docstring).
# ---------------------------------------------------------------------------


def _make_kmd_result(amounts: dict[str, Decimal]) -> TaxReturnResult:
    """Wrap a box_code -> Decimal vector (e.g. from ``_kmd_box_vector``)
    into a ``TaxReturnResult``, without routing EE through ``generate_return``
    (whose embedded fallback is AU-only — see this file's module docstring
    on the reference-DB gate). Labels/display_order are read from the real
    EE seed's parsed boxes so persisted figures carry real labels."""
    parsed_by_code = {b.box_code: b for b in _ee_kmd_parsed_boxes()}
    boxes = {
        code: TaxReturnBoxResult(
            box_code=code,
            box_label=parsed_by_code[code].box_label if code in parsed_by_code else code,
            amount=amount,
            display_order=parsed_by_code[code].display_order if code in parsed_by_code else 0,
        )
        for code, amount in amounts.items()
    }
    return TaxReturnResult(
        jurisdiction="EE",
        return_type="KMD",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 31),
        boxes=boxes,
        source="embedded_fallback",
    )


async def test_persist_return_writes_tax_returns_row_with_ready_status() -> None:
    from sqlalchemy import select as sa_select

    from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
    from saebooks.models.tax_return import TaxReturn, TaxReturnStatus

    company_id = await _make_ee_company(jurisdiction="EE")
    period_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EST",
                period_type=TaxPeriodType.MONTHLY,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            )
        )
        await session.commit()

    result = _make_kmd_result(_EE_GOLDEN_EXPECTED)

    async with AsyncSessionLocal() as session:
        row = await persist_return(
            session, result,
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, period_id=period_id,
        )
        await session.commit()
        persisted_id = row.id

    async with AsyncSessionLocal() as session:
        persisted = (
            await session.execute(sa_select(TaxReturn).where(TaxReturn.id == persisted_id))
        ).scalar_one()

    assert persisted.jurisdiction == "EE"
    assert persisted.return_type == "KMD"
    assert persisted.status == TaxReturnStatus.READY
    assert persisted.period_id == period_id
    assert persisted.figures["1"]["amount"] == "10000.00"
    assert persisted.figures["4"]["amount"] == "2710.00"
    assert persisted.figures["12"]["amount"] == "1870.00"
    assert persisted.figures["13"]["amount"] == "0.00"


async def test_persist_return_figures_round_trip_through_kmd_serializer() -> None:
    """The exact regression the scope's persistence step must not break:
    a persisted return's ``figures`` JSONB, read back and passed through
    ``KmdFigures.from_figures_json``, must reproduce the original amounts —
    proving the persisted shape and the KMD serializer's read shape agree."""
    from sqlalchemy import select as sa_select

    from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
    from saebooks.models.tax_return import TaxReturn
    from saebooks.services.lodgement.kmd import KmdFigures

    company_id = await _make_ee_company(jurisdiction="EE")
    period_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EST",
                period_type=TaxPeriodType.MONTHLY,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            )
        )
        await session.commit()

    result = _make_kmd_result(_EE_GOLDEN_EXPECTED)
    async with AsyncSessionLocal() as session:
        row = await persist_return(
            session, result,
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, period_id=period_id,
        )
        await session.commit()
        persisted_id = row.id

    async with AsyncSessionLocal() as session:
        persisted = (
            await session.execute(sa_select(TaxReturn).where(TaxReturn.id == persisted_id))
        ).scalar_one()
        figures_json = persisted.figures

    figs = KmdFigures.from_figures_json(figures_json)
    for box_code, expected in _EE_GOLDEN_EXPECTED.items():
        if box_code in figs.boxes:
            assert figs.amount(box_code) == expected, box_code


async def test_persist_return_filters_internal_aggregation_legs() -> None:
    """Finding 5: a persisted return must contain EXACTLY the official
    form boxes — the engine-internal aggregation legs (1_DOMESTIC/1_RC,
    2_DOMESTIC/2_RC, 2-2_DOMESTIC/2-2_RC, 5_DOMESTIC/5_RC — display_order
    >= 100) that feed the box-1/2/2-2/5 BOX-FORMULAs must NOT leak into
    ``figures``, or the generic GET /tax_returns reader would surface
    non-form '(internal) …' codes."""
    from sqlalchemy import select as sa_select

    from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
    from saebooks.models.tax_return import TaxReturn

    company_id = await _make_ee_company(jurisdiction="EE")
    period_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(TaxPeriod(
            id=period_id, company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            jurisdiction="EST", period_type=TaxPeriodType.MONTHLY,
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
        ))
        await session.commit()

    internal_legs = {
        "1_DOMESTIC": Decimal("10000.00"), "1_RC": Decimal("0.00"),
        "2_DOMESTIC": Decimal("2000.00"), "2_RC": Decimal("0.00"),
        "2-2_DOMESTIC": Decimal("1000.00"), "2-2_RC": Decimal("0.00"),
        "5_DOMESTIC": Decimal("840.00"), "5_RC": Decimal("0.00"),
    }
    result = _make_kmd_result({**_EE_GOLDEN_EXPECTED, **internal_legs})

    async with AsyncSessionLocal() as session:
        row = await persist_return(
            session, result,
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, period_id=period_id,
        )
        await session.commit()
        persisted_id = row.id

    async with AsyncSessionLocal() as session:
        figures = (
            await session.execute(sa_select(TaxReturn).where(TaxReturn.id == persisted_id))
        ).scalar_one().figures

    for leg in internal_legs:
        assert leg not in figures, f"internal leg {leg} leaked into persisted figures"
    assert set(figures) == set(_EE_GOLDEN_EXPECTED), "persisted figures must be exactly the 28 official boxes"
    assert len(figures) == 28
