"""AU payroll engine — PAYG withholding + Super Guarantee compute.

Jurisdiction-module architecture Phase 1: the engine now lives in the
AU module package beside the compute it calls (``jurisdictions.au.payg``
/ ``jurisdictions.au.super_calc`` — the Phase 1 physical move of what
Phase 0's wrapper reached at ``services/payg.py`` /
``services/super_calc.py``). Nothing about WHAT is computed has changed
since the pre-seam inline calls — only where the code lives and how it
is reached.

The math mirrors ``pay_runs_v2._compute``'s pre-seam AU branch exactly:

    wh  = compute_withholding(gross, period, employee, date, medicare)
    sg  = compute_super(ote, period, date)
    net = round_money(gross - wh.payg_amount - deductions_total)
"""
from __future__ import annotations

from typing import Any

from saebooks.jurisdictions.au.payg import (
    WithholdingResult,
    compute_withholding,
)
from saebooks.jurisdictions.au.super_calc import SuperResult, compute_super
from saebooks.money import round_money
from saebooks.services.payroll.types import (
    PayrollComponent,
    PayrollComponentRole,
    PayrollContext,
    PayrollResult,
)


class AUPayrollEngine:
    """AU PAYG withholding + Super Guarantee behind the neutral seam."""

    jurisdiction = "AU"

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        wh: WithholdingResult = await compute_withholding(
            session,
            gross_per_period=ctx.gross,
            period=ctx.period,
            employee=ctx.employee,
            effective_date=ctx.effective_date,
            medicare_exemption=ctx.medicare_exemption,
        )
        sg: SuperResult = compute_super(
            ote=ctx.ote,
            period=ctx.period,
            effective_date=ctx.effective_date,
        )
        net = round_money(ctx.gross - wh.payg_amount - ctx.deductions_total)
        return PayrollResult(
            jurisdiction="AU",
            gross=ctx.gross,
            net=net,
            components=(
                PayrollComponent(
                    role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                    amount=wh.payg_amount,
                    note=wh.breakdown_note,
                ),
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_LIABILITY,
                    amount=sg.sg_amount,
                    note=sg.breakdown_note,
                ),
                PayrollComponent(
                    role=PayrollComponentRole.RETIREMENT_EXPENSE,
                    amount=sg.sg_amount,
                    note=sg.breakdown_note,
                ),
            ),
        )
