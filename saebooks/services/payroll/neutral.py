"""Neutral (null-object) payroll engine — the "zero modules" floor.

Jurisdiction-module architecture Phase 0 (design doc §3.3): the
payroll-side sibling of ``tax_engine/neutral.py``. A company with no
registered payroll module (the reserved ``"XX"`` sentinel, or any
unregistered jurisdiction) still computes a pay line — take-home is
gross minus caller-supplied deductions, with **zero statutory
add-ons**: no withholding, no retirement contribution, empty component
list. Null-object pattern (``lodgement/null.py`` template), not a
circuit-breaker — every call succeeds with an empty answer.
"""
from __future__ import annotations

from typing import Any

from saebooks.money import round_money
from saebooks.services.payroll.types import (
    PayrollContext,
    PayrollPostingProfile,
    PayrollResult,
)

#: Reserved sentinel meaning "no jurisdiction module" — same value as
#: ``tax_engine.NEUTRAL_JURISDICTION`` (re-declared, not imported, so
#: the payroll package stays importable independently of tax_engine).
NEUTRAL_JURISDICTION = "XX"

#: Posting-profile floor for jurisdictions with no registered payroll
#: module (jurisdiction-module Phase 1). A neutral pay run posts as a
#: plain wage expense / net-clearing credit — no statutory roles at
#: all, matching ``NeutralPayrollEngine``'s empty component tuple. The
#: two codes are the core CoA-seed conventions ``pay_runs_v2`` has
#: always used for the wages debit (6-2110 "Wages — gross") and the
#: net-pay credit (2-1150 "Payments — pending", cleared by ABA
#: processing). If a line somehow carries a statutory amount under
#: this profile, ``finalize_with_je`` refuses loudly rather than drop
#: the leg.
NEUTRAL_POSTING_PROFILE = PayrollPostingProfile(
    wages_account_code="6-2110",
    net_account_code="2-1150",
    role_accounts=(),
)


class NeutralPayrollEngine:
    """Null-object ``PayrollEngine`` — pays gross, computes nothing."""

    jurisdiction = NEUTRAL_JURISDICTION

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        net = round_money(ctx.gross - ctx.deductions_total)
        return PayrollResult(
            jurisdiction=NEUTRAL_JURISDICTION,
            gross=ctx.gross,
            net=net,
            components=(),
        )
