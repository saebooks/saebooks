"""Tests for ``saebooks.services.payg`` and ``services.super_calc``.

These tests verify the FORMULA APPLICATION + scale resolution against
the seeded ``payg_tax_scales`` / ``stsl_coefficients`` rows. The
SEEDED COEFFICIENTS THEMSELVES are derived from the published ATO
marginal-rate brackets (FY25-26) but have NOT been round-tripped
against the ATO "Tax tables Excel calculator" — that's a Phase 2B
verification step. See ``alembic/versions/0112_payg_tables.py``.

What we DO verify here:

1. ``resolve_scale_no`` selects the right scale (1–8) for every
   combination of (resident, TFT, TFN status, WHM, medicare exempt).
2. ``apply_formula`` rounds correctly (NAT 1004 §6: weekly to whole
   dollars, half-up).
3. ``compute_withholding`` end-to-end:
   - Returns $0 when below the tax-free band.
   - Returns the expected WH for a known-derived weekly gross.
   - WHM employees taxed at 15% flat under $45k/year (~$865.38/wk).
   - No-TFN scales (4 / 8) override every other flag.
   - STSL top-up is additive and zero below threshold.
   - Fortnightly / monthly periods scale weekly WH correctly.
4. ``compute_super`` end-to-end:
   - 12% from 1 Jul 2025, 11.5% before.
   - MSCB cap applied per-period.
   - Pre-2014 effective_date raises.

Negative tests for missing band data + unsupported periods are
included so the engine fails loudly rather than producing wrong WH.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.employee import TfnStatus
from saebooks.models.payg import PaygTaxScale, StslCoefficient
from saebooks.services.payg import (
    PaygDataMissing,
    apply_formula,
    compute_withholding,
    resolve_scale_no,
)
from saebooks.services.super_calc import (
    compute_super,
    current_sg_rate,
    quarterly_cap,
)

# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #


def _fake_employee(
    *,
    resident: bool = True,
    tft: bool = True,
    tfn_status: str = TfnStatus.PROVIDED.value,
    whm: bool = False,
    stsl: bool = False,
    base_rate: Decimal = Decimal("35.00"),
    pay_frequency: str = "WEEKLY",
    pay_basis: str = "HOURLY",
    weekly_hours: Decimal = Decimal("38.00"),
) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like an Employee.

    Avoids a DB round-trip: ``compute_withholding`` only reads the
    flag attributes, never relationship attributes. Stops the test
    from depending on the full Employee table being seeded.
    """
    return SimpleNamespace(
        id=None,
        is_australian_resident=resident,
        claims_tax_free_threshold=tft,
        tfn_status=tfn_status,
        working_holiday_maker=whm,
        study_training_support_loan=stsl,
        base_rate=base_rate,
        pay_frequency=pay_frequency,
        pay_basis=pay_basis,
        weekly_hours=weekly_hours,
    )


# Must be on or after 2025-09-24 so the new NAT 3539 Schedule 8
# STSL coefficients (seeded in 0120) are in effect for the
# STSL test cases below.
_FY25_26_DATE = date(2025, 10, 1)  # within FY25-26, STSL effective
_FY24_25_DATE = date(2025, 6, 1)   # within FY24-25 (rate = 11.5%)


# --------------------------------------------------------------------- #
# Scale resolution                                                      #
# --------------------------------------------------------------------- #


class TestResolveScale:

    def test_resident_with_tft(self) -> None:
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=True,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=False,
        ) == 2

    def test_resident_without_tft(self) -> None:
        # ATO Scale 1 — resident, no tax-free threshold claimed
        # (second job). Was internal pseudo-scale 3 before 0120;
        # service constants swapped to match ATO numbering.
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=False,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=False,
        ) == 1

    def test_non_resident(self) -> None:
        # ATO Scale 3 — foreign residents (no TFT, no Medicare).
        # Was internal pseudo-scale 1 before 0120.
        assert resolve_scale_no(
            is_australian_resident=False,
            claims_tax_free_threshold=False,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=False,
        ) == 3

    def test_no_tfn_resident(self) -> None:
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=True,
            tfn_status=TfnStatus.NOT_PROVIDED.value,
            working_holiday_maker=False,
        ) == 4

    def test_no_tfn_nonresident(self) -> None:
        assert resolve_scale_no(
            is_australian_resident=False,
            claims_tax_free_threshold=False,
            tfn_status=TfnStatus.NOT_PROVIDED.value,
            working_holiday_maker=False,
        ) == 8

    def test_whm_overrides_no_tfn(self) -> None:
        """A WHM without a TFN is still WHM (scale 7), not flat 47%."""
        assert resolve_scale_no(
            is_australian_resident=False,
            claims_tax_free_threshold=False,
            tfn_status=TfnStatus.NOT_PROVIDED.value,
            working_holiday_maker=True,
        ) == 7

    def test_whm_overrides_resident(self) -> None:
        # WHM can be resident-for-tax-purposes after 6 months, but
        # the WHM schedule still applies because of the WHM flag.
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=True,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=True,
        ) == 7

    def test_medicare_full_exemption(self) -> None:
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=True,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=False,
            medicare_exemption="FULL",
        ) == 5

    def test_medicare_half_exemption(self) -> None:
        assert resolve_scale_no(
            is_australian_resident=True,
            claims_tax_free_threshold=True,
            tfn_status=TfnStatus.PROVIDED.value,
            working_holiday_maker=False,
            medicare_exemption="HALF",
        ) == 6


# --------------------------------------------------------------------- #
# Formula application (pure)                                            #
# --------------------------------------------------------------------- #


class TestApplyFormula:

    def test_below_threshold(self) -> None:
        # Scale 2 first band: 0 .. 350.00 has a=0, b=0 → WH = 0
        assert apply_formula(
            weekly_gross=Decimal("250.00"),
            coef_a=Decimal("0"),
            coef_b=Decimal("0"),
        ) == Decimal("0")

    def test_band_2_round_trip(self) -> None:
        # Scale 2 band 2: a=0.18 b=63.00
        # weekly_gross = 500.00 → x = floor(500) + 0.99 = 500.99
        # raw = 0.18 * 500.99 - 63 = 90.1782 - 63 = 27.1782
        # whole-dollar half-up → $27
        assert apply_formula(
            weekly_gross=Decimal("500.00"),
            coef_a=Decimal("0.18"),
            coef_b=Decimal("63"),
        ) == Decimal("27")

    def test_negative_clamped_to_zero(self) -> None:
        # Negative raw amounts (a*x - b < 0) clamp to 0 — never
        # generate a "refund" via PAYG.
        assert apply_formula(
            weekly_gross=Decimal("100.00"),
            coef_a=Decimal("0.18"),
            coef_b=Decimal("63"),
        ) == Decimal("0")

    def test_half_dollar_rounds_up(self) -> None:
        # Construct a value whose unrounded result is exactly N.50
        # weekly_gross = 100 → x = 100.99
        # WH = 0.01 * 100.99 + 0 = 1.0099 → round to 1
        # Verify half-up: x=100.99 * 0.01 - (-0.495) = 1.5049 → 2
        result = apply_formula(
            weekly_gross=Decimal("100.00"),
            coef_a=Decimal("0.01"),
            coef_b=Decimal("-0.495"),
        )
        # 0.01 * 100.99 = 1.0099 ; - (-0.495) = 1.5049 → round to 2
        assert result == Decimal("2")


# --------------------------------------------------------------------- #
# End-to-end (DB-backed)                                                #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestComputeWithholding:

    async def test_below_tax_free_threshold_scale_2(self) -> None:
        """A resident with TFT earning <$350/wk pays $0 PAYG."""
        emp = _fake_employee()
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("300.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        assert result.payg_amount == Decimal("0.00")
        assert result.scale_used == 2
        assert result.stsl_amount == Decimal("0.00")

    async def test_scale_2_band_2_known_calc(self) -> None:
        """Resident + TFT, $500/wk → exercises Scale 2 band 3 (ATO)."""
        emp = _fake_employee()
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("500.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        # NAT 1004 Scale 2 FY25-26 band (500.00, 625.00): a=0.26 b=107.8462
        # x = floor(500) + 0.99 = 500.99
        # wh = 0.26 * 500.99 - 107.8462 = 130.2574 - 107.8462 = 22.4112 → $22
        assert result.payg_amount == Decimal("22.00")
        assert result.scale_used == 2

    async def test_scale_1_no_tft_taxes_from_dollar_one(self) -> None:
        """Resident no TFT → ATO Scale 1 (second-job schedule).

        Constants swap (0120): old DB scale 3 was the resident-no-TFT
        slot; ATO numbering reserves scale 1 for it. $300/wk lands in
        band 2 of NAT 1004 Scale 1 — LITO shade-out is included so
        the marginal rate from $150 is 21.17%, not the old 18%.
        """
        emp = _fake_employee(tft=False)
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("300.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        # NAT 1004 Scale 1 FY25-26 band (150.00, 371.00): a=0.2117 b=7.755
        # x = floor(300) + 0.99 = 300.99
        # wh = 0.2117 * 300.99 - 7.755 = 63.7196 - 7.755 = 55.9646 → $56
        assert result.scale_used == 1
        assert result.payg_amount == Decimal("56.00")

    async def test_scale_4_no_tfn_flat_47pct(self) -> None:
        """Resident, no TFN provided → flat 47% (scale 4)."""
        emp = _fake_employee(tfn_status=TfnStatus.NOT_PROVIDED.value)
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("1000.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        # 0.47 * 1000.99 = 470.4653 → $470
        assert result.scale_used == 4
        assert result.payg_amount == Decimal("470.00")

    async def test_scale_7_whm_15pct_under_threshold(self) -> None:
        """WHM earning under $45k annualised → flat 15%."""
        emp = _fake_employee(
            whm=True,
            tfn_status=TfnStatus.PROVIDED.value,
            tft=False,
            resident=False,
        )
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("500.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        # Scale 7 band 1: a=0.15 b=0 → 0.15 * 500.99 = 75.1485 → $75
        assert result.scale_used == 7
        assert result.payg_amount == Decimal("75.00")

    async def test_stsl_additive(self) -> None:
        """With STSL flag, withholding = base + STSL top-up.

        Test target: $1,500/wk resident with TFT + STSL on/after
        2025-09-24 (the effective_from for NAT 3539 Schedule 8).

        Base ATO Scale 2 band (1282, 2596): a=0.32 b=176.5769
            x = floor(1500) + 0.99 = 1500.99
            wh = 0.32 * 1500.99 - 176.5769 = 480.3168 - 176.5769
               = 303.7399 → $304
        STSL Schedule 8 band 2 (1288, 2403): a=0.15 b=193.2692
            stsl = 0.15 * 1500.99 - 193.2692 = 225.1485 - 193.2692
                 = 31.8793 → $32
        Total = $304 + $32 = $336
        """
        emp_no_stsl = _fake_employee()
        emp_stsl = _fake_employee(stsl=True)
        async with AsyncSessionLocal() as session:
            base = await compute_withholding(
                session,
                gross_per_period=Decimal("1500.00"),
                period="WEEKLY",
                employee=emp_no_stsl,
                effective_date=_FY25_26_DATE,
            )
            top = await compute_withholding(
                session,
                gross_per_period=Decimal("1500.00"),
                period="WEEKLY",
                employee=emp_stsl,
                effective_date=_FY25_26_DATE,
            )
        assert top.payg_amount > base.payg_amount
        assert top.stsl_amount > 0
        # STSL component is the difference.
        assert top.stsl_amount == top.payg_amount - top.base_payg_amount
        # Sanity: full payg = base + stsl exactly (no double-rounding).
        assert top.payg_amount == base.payg_amount + top.stsl_amount

    async def test_stsl_below_threshold_zero(self) -> None:
        """Earning below the STSL minimum ($1,046/wk ≈ $54,435/yr) → 0 top-up."""
        emp = _fake_employee(stsl=True)
        async with AsyncSessionLocal() as session:
            result = await compute_withholding(
                session,
                gross_per_period=Decimal("800.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        assert result.stsl_amount == Decimal("0.00")

    async def test_fortnightly_doubles_weekly(self) -> None:
        """Fortnightly $1,000 should ≈ weekly $500 × 2."""
        emp = _fake_employee()
        async with AsyncSessionLocal() as session:
            weekly = await compute_withholding(
                session,
                gross_per_period=Decimal("500.00"),
                period="WEEKLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
            fortnight = await compute_withholding(
                session,
                gross_per_period=Decimal("1000.00"),
                period="FORTNIGHTLY",
                employee=emp,
                effective_date=_FY25_26_DATE,
            )
        assert fortnight.payg_amount == weekly.payg_amount * 2

    async def test_unsupported_period_rejected(self) -> None:
        emp = _fake_employee()
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError, match="WEEKLY"):
                await compute_withholding(
                    session,
                    gross_per_period=Decimal("100.00"),
                    period="QUARTERLY",
                    employee=emp,
                    effective_date=_FY25_26_DATE,
                )

    async def test_negative_gross_rejected(self) -> None:
        emp = _fake_employee()
        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError, match="non-negative"):
                await compute_withholding(
                    session,
                    gross_per_period=Decimal("-1.00"),
                    period="WEEKLY",
                    employee=emp,
                    effective_date=_FY25_26_DATE,
                )

    async def test_missing_band_raises(self) -> None:
        emp = _fake_employee()
        # Pre-FY25-26 effective date → no seeded rows.
        async with AsyncSessionLocal() as session:
            with pytest.raises(PaygDataMissing):
                await compute_withholding(
                    session,
                    gross_per_period=Decimal("500.00"),
                    period="WEEKLY",
                    employee=emp,
                    effective_date=date(2020, 1, 1),
                )


# --------------------------------------------------------------------- #
# Super calc                                                            #
# --------------------------------------------------------------------- #


class TestSuperCalc:

    def test_12pct_from_july_2025(self) -> None:
        result = compute_super(
            ote=Decimal("1000.00"),
            period="WEEKLY",
            effective_date=date(2025, 7, 1),
        )
        assert result.rate == Decimal("0.1200")
        assert result.sg_amount == Decimal("120.00")
        assert not result.cap_applied

    def test_115pct_in_june_2025(self) -> None:
        result = compute_super(
            ote=Decimal("1000.00"),
            period="WEEKLY",
            effective_date=date(2025, 6, 30),
        )
        assert result.rate == Decimal("0.1150")
        assert result.sg_amount == Decimal("115.00")

    def test_step_at_july_1_2024(self) -> None:
        # FY23-24 ends 30 June 2024: rate = 11%
        before = compute_super(
            ote=Decimal("1000.00"),
            period="WEEKLY",
            effective_date=date(2024, 6, 30),
        )
        after = compute_super(
            ote=Decimal("1000.00"),
            period="WEEKLY",
            effective_date=date(2024, 7, 1),
        )
        assert before.rate == Decimal("0.1100")
        assert after.rate == Decimal("0.1150")

    def test_mscb_cap_applied_per_quarter(self) -> None:
        """Weekly OTE above MSCB / 13 → cap kicks in."""
        # FY25-26 MSCB = $65,070 / quarter → $5,005.38 / wk
        result = compute_super(
            ote=Decimal("10000.00"),  # well above cap
            period="WEEKLY",
            effective_date=_FY25_26_DATE,
        )
        assert result.cap_applied
        # 12% × $5,005.38 ≈ $600.65
        assert result.sg_amount == Decimal("600.65")
        # Total weekly cap should match 65070 / 13 = 5005.3846… → 5005.38
        assert result.period_cap == Decimal("5005.38")

    def test_no_cap_under_threshold(self) -> None:
        result = compute_super(
            ote=Decimal("2000.00"),
            period="WEEKLY",
            effective_date=_FY25_26_DATE,
        )
        assert not result.cap_applied
        assert result.sg_amount == Decimal("240.00")  # 12% × 2000

    def test_monthly_cap_one_third_of_quarter(self) -> None:
        result = compute_super(
            ote=Decimal("30000.00"),
            period="MONTHLY",
            effective_date=_FY25_26_DATE,
        )
        # Monthly cap = 65070 / 3 = $21,690
        assert result.period_cap == Decimal("21690.00")
        assert result.cap_applied
        assert result.sg_amount == Decimal("2602.80")  # 12% × 21,690

    def test_negative_ote_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_super(
                ote=Decimal("-1.00"),
                period="WEEKLY",
                effective_date=_FY25_26_DATE,
            )

    def test_pre_2014_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_super(
                ote=Decimal("1000.00"),
                period="WEEKLY",
                effective_date=date(2013, 1, 1),
            )

    def test_explicit_max_base_override(self) -> None:
        result = compute_super(
            ote=Decimal("1000.00"),
            period="WEEKLY",
            effective_date=_FY25_26_DATE,
            max_base_quarter=Decimal("13000"),  # → $1000/wk
        )
        assert result.period_cap == Decimal("1000.00")
        assert not result.cap_applied
        # SG = 12% × 1000 = 120
        assert result.sg_amount == Decimal("120.00")

    def test_current_sg_rate_helper(self) -> None:
        assert current_sg_rate(date(2025, 7, 1)) == Decimal("0.1200")
        assert current_sg_rate(date(2024, 7, 1)) == Decimal("0.1150")

    def test_quarterly_cap_helper(self) -> None:
        assert quarterly_cap(date(2025, 7, 1)) == Decimal("65070")
        assert quarterly_cap(date(2024, 7, 1)) == Decimal("65070")
        assert quarterly_cap(date(2023, 7, 1)) == Decimal("62270")


# --------------------------------------------------------------------- #
# Seed-data sanity                                                      #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_payg_seed_present() -> None:
    """At least one band per scale 1–8 for FY25-26 must be loaded."""
    async with AsyncSessionLocal() as session:
        for scale_no in (1, 2, 3, 4, 5, 6, 7, 8):
            result = await session.execute(
                select(PaygTaxScale).where(
                    PaygTaxScale.scale_no == scale_no,
                    PaygTaxScale.effective_from <= _FY25_26_DATE,
                )
            )
            rows = result.scalars().all()
            assert rows, f"No bands seeded for scale_no={scale_no}"


@pytest.mark.asyncio
async def test_stsl_seed_present() -> None:
    """STSL bands must cover the 1%–10% range."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(StslCoefficient).order_by(StslCoefficient.earnings_floor)
        )
        rows = list(result.scalars().all())
        # NAT 3539 Schedule 8 collapsed the old 19 percentage bands
        # into 4 marginal bands effective 2025-09-24:
        #   nil (< $1,288/wk), 15% ($1,288-$2,403), 17% ($2,403-$3,447),
        #   10% (above $3,447 — flat HELP repayment cap).
        assert len(rows) >= 4
        assert rows[0].coef_a == Decimal("0.000000")
        # Top band: HELP repayment cap at 10% above $3,447/wk.
        assert rows[-1].coef_a == Decimal("0.100000")
