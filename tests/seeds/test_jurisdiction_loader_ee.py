"""End-to-end: clean reference DB -> load EE -> verify row counts.

Mirrors tests/seeds/test_jurisdiction_loader.py's AU shape. Proves the
EE seed set (all 15 files) loads through the real loader, is idempotent,
and that the directory-name (EE) -> reference-code (EST) resolution the
task's TRAP asked for actually round-trips through a live database.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytest.mark.asyncio
async def test_load_ee_idempotent() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    assert ReferenceMigrationSession is not None

    counts1 = await load_seeds("EE", version_tag="test-ee-1")
    expected_files = {
        "_global/jurisdictions.yaml",
        "_global/currencies.yaml",
        "_global/countries.yaml",
        "EE/entity_structure_types.yaml",
        "EE/tax_codes.yaml",
        "EE/tax_return_box_definitions.yaml",
        "EE/withholding_tables.yaml",
        "EE/social_contribution_schemes.yaml",
        "EE/mandatory_contribution_rules.yaml",
        "EE/corporate_tax_rates.yaml",
        "EE/dividend_relief_mechanisms.yaml",
        "EE/capital_gains_tax_regimes.yaml",
        "EE/benefit_in_kind_rates.yaml",
        "EE/retirement_vehicle_types.yaml",
        "EE/duty_concessions.yaml",
        "EE/chart_template.yaml",
        "EE/fiscal_year_definitions.yaml",
        "EE/oss_member_state_rates.yaml",
    }
    assert expected_files.issubset(set(counts1)), (
        f"Loader skipped expected EE seed files: missing={expected_files - set(counts1)}"
    )

    # Second load — idempotent no-op.
    counts2 = await load_seeds("EE", version_tag="test-ee-2")
    assert counts1 == counts2, "Idempotent re-run should report identical row counts"

    async with ReferenceMigrationSession() as s:
        # The directory-name (EE) -> reference-code (EST) TRAP: every row
        # must land under jurisdiction='EST', not 'EE'.
        n_tax_codes = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        # 26, not 22: KMD-formula support Packet 2 (see
        # ~/.claude/plans/kmd-formula-support-scope.md §2/§7) split the
        # single ZERO code into its four KMD box-3.1/3.1.1/3.2/3.2.1
        # sub-total leaves (ZERO_IC_GOODS, ZERO_IC_SERVICES, ZERO_EXPORT,
        # ZERO_TRAVELLER — net +3 rows) and added one new INPUT_IMPORT
        # code feeding KMD box 5.1 (+1 row): 22 - 1 + 4 + 1 = 26. (Prior
        # history: 22, not 18 — INPUT_EXEMPT was added (purchase-
        # direction VAT-exempt code — chart_template.yaml's Bank Fees
        # account previously reused the sale-direction EXEMPT code, which
        # has the wrong direction for a purchase-bucket account). Then
        # dated legacy-rate rows were added (20% until
        # 2023-12-31, 22% 2024-01-01-2025-06-30) for RC_EU_ACQ and
        # INPUT_CAP, matching the STD/INPUT_STD dated-history pattern
        # (+4 rows: 18 -> 22).)
        # Finding 4: +7 — the informative/RC feeder tags that had no
        # backing RefTaxCode row now do, so a company can provision a tax
        # code that feeds KMD boxes 6/6.1/7/7.1/9 and disambiguate goods
        # vs services EU acquisitions: RC_EU_ACQ_SERVICES (3 dated rows,
        # mirroring RC_EU_ACQ's goods series) + IC_ACQ_EXEMPT +
        # RC_DOMESTIC_ACQ + EE_ACQ_FOREIGN + INSTALL_OTHER_MS (4). 26+7=33.
        assert n_tax_codes == 33, f"Expected 33 EE tax codes, got {n_tax_codes}"

        n_oss_rates = (
            await s.execute(
                text("SELECT count(*) FROM oss_member_state_rates")
            )
        ).scalar_one()
        # EE-frontier Module 2 (0011_oss_member_state_rates): 17 EU
        # member-state destination rates — every countries.yaml row with
        # in_oss=true EXCEPT Estonia itself (never its own OSS
        # destination). See oss_member_state_rates.yaml's header for the
        # named gap (9 EU states not yet in countries.yaml).
        assert n_oss_rates == 17, f"Expected 17 OSS member-state rates, got {n_oss_rates}"

        n_entities = (
            await s.execute(
                text("SELECT count(*) FROM entity_structure_types WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        assert n_entities == 8, f"Expected 8 EE entity structure types, got {n_entities}"

        n_boxes = (
            await s.execute(
                text("SELECT count(*) FROM tax_return_box_definitions WHERE jurisdiction = 'EST'")
            )
        ).scalar_one()
        # 48, not 40: KMD-formula support Packet 3 (RC-FANOUT, see
        # ~/.claude/plans/kmd-formula-support-scope.md §3.4) added 4
        # internal-only ledger boxes — "1_DOMESTIC"/"1_RC" (box-1
        # BOX-FORMULA) + "5_DOMESTIC"/"5_RC" (box-5 BOX-FORMULA). Finding 1
        # (rate-aware RC routing) added 4 more — "2_DOMESTIC"/"2_RC" and
        # "2-2_DOMESTIC"/"2-2_RC" — so a 9% (box 2) / 13% (box 2-2)
        # EU-acquisition reverse charge lands in the correct rate box, not
        # box 1. All eight are internal legs (display_order >= 100), not
        # real EMTA box codes: 40 + 4 + 4 = 48.
        # EE-frontier Module 2: OSS-Q's single deliberate STUB row was
        # replaced with 3 manual structural rows (MS_BREAKDOWN/
        # CORRECTIONS/TOTAL_VAT_PAYABLE — see tax_return_box_definitions.
        # yaml's OSS-Q header comment for why it's 3 manual rows, not a
        # box vector): net +2. 48 - 1 + 3 = 50.
        assert n_boxes == 50, f"Expected 50 EE tax-return box definitions, got {n_boxes}"

        # KMD-formula support Packet 2 (see
        # ~/.claude/plans/kmd-formula-support-scope.md §2/§7) flips 14 of
        # the 19 previously-manual KMD boxes to a real aggregation
        # (CODES-SEED or formula), while leaving 5 manual by design. Row
        # count for THOSE 40 original boxes is unchanged (Packet 3 only
        # ADDS 4 new internal boxes, asserted above) — this proves the
        # *disposition* changed, not just that rows still exist.
        kmd_agg = {
            r.box_code: (r.aggregation, r.formula)
            for r in (
                await s.execute(
                    text(
                        "SELECT box_code, aggregation, formula "
                        "FROM tax_return_box_definitions "
                        "WHERE jurisdiction = 'EST' AND return_type = 'KMD'"
                    )
                )
            ).all()
        }

        no_longer_manual = [
            "3.1", "3.1.1", "3.2", "3.2.1", "5.1", "5.2", "6", "6.1", "7", "7.1", "9",
        ]
        for box_code in no_longer_manual:
            agg, _formula = kmd_agg[box_code]
            assert agg != "manual", (
                f"KMD box {box_code!r} should have flipped off 'manual' in "
                f"Packet 2, still {agg!r}"
            )
            assert agg == "sum_taxable_for_codes:income:gst_exclusive" or (
                agg == "sum_taxable_for_codes:purchase:gst_exclusive"
            ) or agg == "sum_tax_amount_for_codes:purchase", (
                f"KMD box {box_code!r} has unexpected aggregation {agg!r}"
            )

        formula_boxes = ["4", "12", "13"]
        for box_code in formula_boxes:
            agg, formula = kmd_agg[box_code]
            assert agg == "formula", f"KMD box {box_code!r} should be aggregation='formula', got {agg!r}"
            assert formula, f"KMD box {box_code!r} is aggregation='formula' but has no formula expression"

        # Packet 3 + finding 1: boxes 1, 2, 2-2 and 5 are BOX-FORMULA
        # (summing their own "*_DOMESTIC"/"*_RC" internal boxes) — see
        # tax_return_box_definitions.yaml's box comments. Boxes 2/2-2 gain
        # the rate-pinned RC legs so a 9%/13% EU-acquisition reverse charge
        # lands in the correct rate box, not box 1.
        rc_formula_boxes = {
            "1": "KMD:1_DOMESTIC + KMD:1_RC",
            "2": "KMD:2_DOMESTIC + KMD:2_RC",
            "2-2": "KMD:2-2_DOMESTIC + KMD:2-2_RC",
            "5": "KMD:5_DOMESTIC + KMD:5_RC",
        }
        for box_code, expected_formula in rc_formula_boxes.items():
            agg, formula = kmd_agg[box_code]
            assert agg == "formula", f"KMD box {box_code!r} should be aggregation='formula', got {agg!r}"
            assert formula == expected_formula, (
                f"KMD box {box_code!r} formula {formula!r} != expected {expected_formula!r}"
            )

        # The 8 internal-only ledger boxes themselves — one
        # account-type-bucket box (the exact domestic recipe, unchanged)
        # and one rate-pinned role-based ("output@rate"/"input") box per
        # pair. box 1_RC pins @24, box 2_RC @9, box 2-2_RC @13 so the
        # applied rate routes the base to the right output box (finding 1).
        internal_rc_boxes = {
            "1_DOMESTIC": "sum_taxable_for_codes:income:gst_exclusive",
            "1_RC": "sum_taxable_for_codes:output@24:gst_exclusive",
            "2_DOMESTIC": "sum_taxable_for_codes:income:gst_exclusive",
            "2_RC": "sum_taxable_for_codes:output@9:gst_exclusive",
            "2-2_DOMESTIC": "sum_taxable_for_codes:income:gst_exclusive",
            "2-2_RC": "sum_taxable_for_codes:output@13:gst_exclusive",
            "5_DOMESTIC": "sum_tax_amount_for_codes:purchase",
            "5_RC": "sum_tax_amount_for_codes:input",
        }
        for box_code, expected_agg in internal_rc_boxes.items():
            agg, _formula = kmd_agg[box_code]
            assert agg == expected_agg, (
                f"KMD box {box_code!r} aggregation {agg!r} != expected {expected_agg!r}"
            )

        still_manual = ["4-1", "10", "11", "5.3", "5.4"]
        for box_code in still_manual:
            agg, formula = kmd_agg[box_code]
            assert agg == "manual", (
                f"KMD box {box_code!r} should stay 'manual' by design (Packet 2), got {agg!r}"
            )
            assert formula is None, f"KMD box {box_code!r} is manual but has a formula value {formula!r}"

        return_types = {
            r[0]
            for r in (
                await s.execute(
                    text(
                        "SELECT DISTINCT return_type FROM tax_return_box_definitions "
                        "WHERE jurisdiction = 'EST'"
                    )
                )
            ).all()
        }
        assert return_types == {"KMD", "KMD-INF", "TSD", "OSS-Q"}, return_types

        # jurisdiction='EE' (the directory name, not the reference code)
        # must NOT have leaked into any row.
        n_wrong_code = (
            await s.execute(
                text("SELECT count(*) FROM tax_codes WHERE jurisdiction = 'EE'")
            )
        ).scalar_one()
        assert n_wrong_code == 0, "jurisdiction='EE' leaked in — must be 'EST'"

        # kmd-inf-tsd scope Packet 3: social_contribution_schemes.
        # wage_base_floor actually landed in the row (not just that the
        # loader accepted the YAML column name — see migration
        # 0010_social_tax_floor + services.payroll_ee's embedded-fallback
        # docstring for why this is the ONLY test in the tree that
        # exercises the real seeded value; every payroll_ee test runs the
        # embedded-fallback path instead, since REFERENCE_DATABASE_URL
        # itself is never configured here).
        floor = (
            await s.execute(
                text(
                    "SELECT wage_base_floor FROM social_contribution_schemes "
                    "WHERE jurisdiction = 'EST' AND code = 'ee_social_tax'"
                )
            )
        ).scalar_one()
        assert floor == Decimal("886.00"), (
            f"ee_social_tax.wage_base_floor should be 886.00, got {floor!r}"
        )

        # Distributed-profit 22/78 gross-up row + paired 0% retained row.
        rates = {
            r.entity_scope: float(r.rate_percent)
            for r in (
                await s.execute(
                    text(
                        "SELECT entity_scope, rate_percent FROM corporate_tax_rates "
                        "WHERE jurisdiction = 'EST' AND tax_year = 2026"
                    )
                )
            ).all()
        }
        assert rates["retained_reinvested"] == 0.0
        assert rates["distributed_profit"] == 22.0

        # schema_meta reflects the second tag.
        tag = (
            await s.execute(text("SELECT version_tag FROM schema_meta WHERE id = 1"))
        ).scalar_one()
        assert tag == "test-ee-2"
