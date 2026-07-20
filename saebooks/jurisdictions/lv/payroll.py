"""LV payroll compute — monthly IIN withholding + VSAOI — behind the
neutral ``PayrollEngine`` seam.

LV jurisdiction module. 2026 values; every figure is a lock-step
snapshot of this module's seed files
(``seeds/jurisdictions/LV/withholding_tables.yaml``,
``social_contribution_schemes.yaml``) whose primary pulls are
documented there. The IIN parameters are REFERENCE-PREFERRED with this
embedded fallback (the ``payroll_ee._resolve_rates`` /
``uk.payroll._resolve_paye_table`` convention —
``REFERENCE_DATABASE_URL`` is unset in the standard test/CI harness, so
the embedded path is what tests exercise); the VSAOI rates are embedded
snapshots.

THE LOAD-BEARING MODEL FINDING (verified three ways — VID's own
monthly/annual rates split, LV portāls, aibirojs.lv): Latvian monthly
employer withholding is a SINGLE FLAT 25.5% on the IIN base. The 33%
band (annual income over EUR 105,300) and the additional 3% rate (over
EUR 200,000) are settled EXCLUSIVELY through the employee's annual
income tax return — they are not payroll bands. Encoding a monthly 33%
band at EUR 8,775 would be the classic wrong-model bug; the UK module's
banded-withholding machinery is therefore deliberately NOT reused here
— Latvia's statutory monthly algorithm is allowance-then-flat-rate.

Monthly algorithm (2026):

    ee_vsaoi   = gross * 10.50%
    er_vsaoi   = gross * 23.59%
    iin_base   = max(0, gross - ee_vsaoi
                         - (550 if tax book submitted else 0)
                         - 250 * dependants)          # tax book required
    iin        = iin_base * 25.5%
    net        = gross - ee_vsaoi - iin - caller deductions

LV statutory inputs ride in ``PayrollContext.extra["lv"]``
----------------------------------------------------------
The neutral ``PayrollContext``/``Employee`` carry no LV statutory
fields (adding employee columns is a schema change, out of module
scope — the UK precedent, flagged in the build report)::

    {
        "tax_book_submitted": True,   # required (bool) — algas nodokļa
                                      # grāmatiņa electronically submitted
                                      # to THIS employer
        "dependants": 1,              # optional, default 0
    }

Hard refusals — never a silent wrong number (the EE partial-month /
UK-unsupported-path precedent): a missing ``extra["lv"]`` block or
``tax_book_submitted`` key; any pay frequency other than MONTHLY (the
minimum/allowances/rates are statutorily monthly figures; no
apportionment rule was verified); pensioner or disability-pension
employees (the reported reduced 31.70% VSAOI rate is single-source
UNVERIFIED, and the pensioner non-taxable minimum interacts with VSAA);
micro-enterprise-taxpayer employers (MUN replaces payroll taxes);
royalty/board-member special regimes. All raise
:class:`LVPayrollUnsupported` with a clear message.

Known, stated gaps (documented, not silently wrong):

* The VSAOI annual cap (EUR 105,300) and the solidarity tax above it
  are NOT cap-managed in monthly payroll — correctly so: the employer
  keeps withholding at the full 34.09% on the excess all year and VID
  reconciles to the 25% solidarity rate after year-end (see the
  social_contribution_schemes.yaml header). Monthly compute at the
  ordinary rates IS the statutory employer behaviour.
* The minimum-contribution top-up for sub-minimum-wage earners
  (EUR 780/month floor, assessed quarterly by VSAA and charged to the
  employer separately) is not modelled — it is a separate VSAA
  assessment, not a payroll-line withholding; part-time lines compute
  correctly as posted.
* Statutory rounding: amounts are rounded half-up to the cent at each
  component; the exact statutory rounding rule was not primary-verified
  (the UK "within tolerance, stated approximation" posture).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from saebooks.jurisdictions.lv import (
    identifiers as _identifiers,  # noqa: F401  (registers lv_pvn/lv_regnum validators on first LV dispatch)
)
from saebooks.money import round_money
from saebooks.services.payroll.types import (
    PayrollComponent,
    PayrollComponentRole,
    PayrollContext,
    PayrollResult,
)


class LVPayrollError(Exception):
    """LV payroll compute failed."""


class LVPayrollUnsupported(LVPayrollError):
    """The input needs an LV payroll feature this wave deliberately does
    not implement — refuse loudly rather than emit a wrong number."""


_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Embedded 2026 parameter snapshot (lock-step with the LV seeds).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IinParameters:
    rate_percent: Decimal
    non_taxable_minimum_monthly: Decimal
    dependant_allowance_monthly: Decimal
    source: str = "embedded_fallback"


_FALLBACK_IIN = IinParameters(
    rate_percent=Decimal("25.5"),
    non_taxable_minimum_monthly=Decimal("550.00"),
    dependant_allowance_monthly=Decimal("250.00"),
)

_IIN_TABLE_CODE = "lv_iin_salary_paye"

_VSAOI_EMPLOYEE_PERCENT = Decimal("10.50")
_VSAOI_EMPLOYER_PERCENT = Decimal("23.59")


async def _resolve_iin_parameters(session: Any, effective_date: Any) -> IinParameters:
    """Reference-preferred / embedded-fallback IIN parameter lookup (the
    ``payroll_ee._resolve_rates`` convention). Any absence — reference
    DB unconfigured, row missing, parameters malformed — degrades to the
    embedded 2026 snapshot."""
    from saebooks.db import ReferenceSession

    if ReferenceSession is not None:
        from sqlalchemy import select

        from saebooks.models.reference.withholding_table import WithholdingTable

        async with ReferenceSession() as ref:
            row = (
                await ref.execute(
                    select(WithholdingTable)
                    .where(
                        WithholdingTable.jurisdiction == "LVA",
                        WithholdingTable.code == _IIN_TABLE_CODE,
                        WithholdingTable.effective_from <= effective_date,
                    )
                    .order_by(WithholdingTable.effective_from.desc())
                )
            ).scalars().first()
        if row is not None:
            params = row.parameters or {}
            try:
                return IinParameters(
                    rate_percent=Decimal(str(params["rate_percent"])),
                    non_taxable_minimum_monthly=Decimal(
                        str(params["non_taxable_minimum_monthly"])
                    ),
                    dependant_allowance_monthly=Decimal(
                        str(params["dependant_allowance_monthly"])
                    ),
                    source="reference_db",
                )
            except (KeyError, ArithmeticError, TypeError):
                pass  # malformed row -> embedded snapshot below
    return _FALLBACK_IIN


class LVPayrollEngine:
    """Flat-25.5% monthly IIN + VSAOI behind the neutral seam."""

    jurisdiction = "LV"

    async def compute_line(
        self, session: Any, ctx: PayrollContext
    ) -> PayrollResult:
        lv = (ctx.extra or {}).get("lv")
        if not isinstance(lv, dict):
            raise LVPayrollUnsupported(
                "LV payroll needs its statutory inputs in extra['lv'] "
                "(tax_book_submitted, dependants) — the neutral "
                "PayrollContext and the Employee model carry no LV "
                "fields. Refusing rather than assuming the non-taxable "
                "minimum applies."
            )
        if ctx.period != "MONTHLY":
            raise LVPayrollUnsupported(
                f"pay frequency {ctx.period!r} is not supported for LV "
                "payroll — the non-taxable minimum (EUR 550), dependant "
                "allowance (EUR 250) and the flat monthly withholding "
                "rate are statutorily MONTHLY figures and no "
                "apportionment rule for other frequencies was verified. "
                "Use MONTHLY."
            )
        if "tax_book_submitted" not in lv:
            raise LVPayrollUnsupported(
                "extra['lv']['tax_book_submitted'] (bool) is required — "
                "whether the algas nodokļa grāmatiņa names this employer "
                "decides whether the non-taxable minimum and dependant "
                "allowances apply; guessing either way mis-withholds."
            )
        if lv.get("pensioner") or lv.get("disability_pension"):
            raise LVPayrollUnsupported(
                "pensioner / disability-pension employees are not "
                "computed this wave — the reduced VSAOI rate reported "
                "for them (31.70%) is single-source UNVERIFIED and the "
                "pensioner non-taxable minimum (EUR 1,000) is "
                "VSAA-administered. Refusing rather than guessing."
            )
        if lv.get("micro_enterprise"):
            raise LVPayrollUnsupported(
                "micro-enterprise-taxpayer (MUN) employers do not run "
                "standard IIN/VSAOI payroll — MUN replaces payroll "
                "taxes; not modelled this wave."
            )
        if lv.get("royalty_regime") or lv.get("board_member"):
            raise LVPayrollUnsupported(
                "royalty-recipient / board-member special regimes are "
                "not computed this wave."
            )

        gross = ctx.gross
        if gross < 0:
            raise LVPayrollError("gross must be >= 0")
        dependants = int(lv.get("dependants", 0))
        if dependants < 0:
            raise LVPayrollError("dependants must be >= 0")
        tax_book = bool(lv["tax_book_submitted"])

        params = await _resolve_iin_parameters(session, ctx.effective_date)

        ee_vsaoi = _q2(gross * _VSAOI_EMPLOYEE_PERCENT / Decimal("100"))
        er_vsaoi = _q2(gross * _VSAOI_EMPLOYER_PERCENT / Decimal("100"))

        allowances = _ZERO
        if tax_book:
            allowances = (
                params.non_taxable_minimum_monthly
                + params.dependant_allowance_monthly * dependants
            )
        iin_base = max(_ZERO, gross - ee_vsaoi - allowances)
        iin = _q2(iin_base * params.rate_percent / Decimal("100"))

        net = round_money(gross - ee_vsaoi - iin - ctx.deductions_total)

        book_note = (
            f"tax book submitted; minimum {params.non_taxable_minimum_monthly}"
            f" + {dependants} dependant(s) x {params.dependant_allowance_monthly}"
            if tax_book
            else "no tax book — no minimum/allowances (rate unchanged at "
            f"{params.rate_percent}%)"
        )
        components = (
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=iin,
                note=(
                    f"IIN {params.rate_percent}% flat monthly on base "
                    f"{iin_base} ({book_note}); 33%/3% annual bands settle "
                    "via the annual return, not payroll"
                ),
            ),
            PayrollComponent(
                role=PayrollComponentRole.WITHHOLDING_LIABILITY,
                amount=ee_vsaoi,
                note=f"VSAOI employee share {_VSAOI_EMPLOYEE_PERCENT}%",
            ),
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_LIABILITY,
                amount=er_vsaoi,
                note=f"VSAOI employer share {_VSAOI_EMPLOYER_PERCENT}%",
            ),
            PayrollComponent(
                role=PayrollComponentRole.EMPLOYER_SOCIAL_EXPENSE,
                amount=er_vsaoi,
                note=f"VSAOI employer share {_VSAOI_EMPLOYER_PERCENT}%",
            ),
        )
        return PayrollResult(
            jurisdiction="LV",
            gross=gross,
            net=net,
            components=components,
        )


__all__ = [
    "IinParameters",
    "LVPayrollEngine",
    "LVPayrollError",
    "LVPayrollUnsupported",
]
