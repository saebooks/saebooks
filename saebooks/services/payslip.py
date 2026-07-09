"""Payslip data assembly (no rendering).

Produces a dict shaped per the ATO STP Phase 2 payslip schema —
suitable for direct HTML render (see ``saebooks-web/templates/payslips/
single.html``) or future PDF generation.

Pure data; PDF generation is OUT OF SCOPE for this pass.

The output dict has the following top-level keys:

    employer:           {abn, name, branch_code, address}
    employee:           {id, employee_number, name, tfn_masked,
                         super_fund, super_member_number,
                         pay_frequency, pay_basis}
    period:             {start, end, payment_date}
    earnings:           [{description, hours, rate, amount, stp_code}]
    allowances:         [{type, amount, stp_code}]
    deductions:         [{type, amount, stp_code}]
    paid_leave:         [{leave_type, hours, amount}]
    lump_sums:          [{category, amount}]
    tax:                {payg, stsl (optional)}
    super:              {amount, rate, fund_name, usi, member_number}
    totals:             {gross, payg, super, deductions, net}
    ytd:                {gross, payg, super}
    breakdown:          {payg_explanation, super_explanation}

Caller is responsible for fetching the Company / Employee / SuperFund
rows; this module receives them as arguments to keep the function
pure and unit-testable.

References:
    - Fair Work Regulations 2009 r 3.46 — payslip required fields
    - ATO STP Phase 2 employer reporting guidelines § 1.7 (payslip
      vs STP payload contents)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from saebooks.models.company import Company
from saebooks.models.employee import Employee
from saebooks.models.pay_run import PayRun, PayRunLine
from saebooks.models.super_fund import SuperFund


def _mask_tfn(tfn_plain: str | None) -> str:
    """Mask a TFN for display — last 3 digits only."""
    if not tfn_plain:
        return ""
    digits = "".join(ch for ch in tfn_plain if ch.isdigit())
    if len(digits) < 3:
        return "XXX-XXX-XXX"
    return f"XXX-XXX-{digits[-3:]}"


def _employee_display_name(employee: Employee) -> str:
    """Construct a display name — fall back gracefully when Contact lacks data.

    For Phase 2 we trust the caller to have eager-loaded
    ``employee.contact`` if it's needed. When unavailable, we use
    ``employee_number`` as the visible label.
    """
    contact = getattr(employee, "contact", None)
    if contact is not None:
        name = getattr(contact, "name", None)
        if name:
            return name
    return employee.employee_number


def build_payslip(
    *,
    company: Company,
    employee: Employee,
    pay_run: PayRun,
    line: PayRunLine,
    super_fund: SuperFund | None,
    tfn_plain: str | None = None,
    payg_breakdown: str | None = None,
    super_breakdown: str | None = None,
) -> dict[str, Any]:
    """Assemble a payslip data dict for one employee in one pay run.

    Parameters
    ----------
    company, employee, pay_run, line, super_fund
        Already-loaded ORM rows. The function does NO database work.
    tfn_plain
        Optional plaintext TFN — when supplied, the masked form
        appears on the payslip. Default omitted entirely.
    payg_breakdown, super_breakdown
        Optional explanation strings (output of
        ``WithholdingResult.breakdown_note`` /
        ``SuperResult.breakdown_note``) for the printable breakdown
        block.
    """
    # Extended pay-run-line fields are jsonb on the post-1B schema.
    # Until the ORM declares them, we access via getattr with
    # defaults so the module is forward-compatible.
    allowances = getattr(line, "allowances", None) or []
    deductions = getattr(line, "deductions", None) or []
    paid_leave = getattr(line, "paid_leave", None) or []
    lump_sums = getattr(line, "lump_sums", None) or []
    ordinary_hours = getattr(line, "ordinary_hours", Decimal("0")) or Decimal("0")
    overtime_hours = getattr(line, "overtime_hours", Decimal("0")) or Decimal("0")
    ytd_gross = getattr(line, "ytd_gross", Decimal("0")) or Decimal("0")
    ytd_tax = getattr(line, "ytd_tax", Decimal("0")) or Decimal("0")
    ytd_super = getattr(line, "ytd_super", Decimal("0")) or Decimal("0")

    deductions_total = sum(
        (Decimal(str(d["amount"])) for d in deductions),
        start=Decimal("0"),
    )

    earnings: list[dict[str, Any]] = []
    if ordinary_hours > 0:
        earnings.append({
            "description": "Ordinary hours",
            "hours": float(ordinary_hours),
            "rate": float(employee.base_rate),
            "amount": float(
                Decimal(str(ordinary_hours)) * Decimal(str(employee.base_rate))
            ),
            "stp_code": "GROSS",
        })
    if overtime_hours > 0:
        earnings.append({
            "description": "Overtime",
            "hours": float(overtime_hours),
            "rate": None,
            "amount": None,
            "stp_code": "OT",
        })

    return {
        "employer": {
            "abn": getattr(company, "abn", None),
            "name": company.name,
            "branch_code": employee.payg_branch_code,
            "address": {
                "line1": getattr(company, "address_line1", None),
                "line2": getattr(company, "address_line2", None),
                "suburb": getattr(company, "suburb", None),
                "state": getattr(company, "state", None),
                "postcode": getattr(company, "postcode", None),
            },
        },
        "employee": {
            "id": str(employee.id),
            "employee_number": employee.employee_number,
            "name": _employee_display_name(employee),
            "tfn_masked": _mask_tfn(tfn_plain) if tfn_plain else None,
            "pay_frequency": employee.pay_frequency,
            "pay_basis": employee.pay_basis,
            "address": {
                "line1": employee.address_line1,
                "line2": employee.address_line2,
                "suburb": employee.suburb,
                "state": employee.state,
                "postcode": employee.postcode,
            },
        },
        "period": {
            "start": pay_run.period_start.isoformat(),
            "end": pay_run.period_end.isoformat(),
            "payment_date": pay_run.payment_date.isoformat(),
        },
        "earnings": earnings,
        "allowances": [
            {
                "type": a.get("type"),
                "amount": float(Decimal(str(a["amount"]))),
                "stp_code": a.get("type"),
            }
            for a in allowances
        ],
        "deductions": [
            {
                "type": d.get("type"),
                "amount": float(Decimal(str(d["amount"]))),
                "stp_code": d.get("type"),
            }
            for d in deductions
        ],
        "paid_leave": [
            {
                "leave_type": pl.get("leave_type"),
                "hours": float(Decimal(str(pl.get("hours", 0)))),
                "amount": float(Decimal(str(pl["amount"]))),
            }
            for pl in paid_leave
        ],
        "lump_sums": [
            {
                "category": ls.get("category"),
                "amount": float(Decimal(str(ls["amount"]))),
            }
            for ls in lump_sums
        ],
        "tax": {
            "payg": float(line.tax),
        },
        "super": {
            "amount": float(line.super_amount),
            "fund_name": super_fund.name if super_fund else None,
            "usi": super_fund.usi if super_fund else None,
            "member_number": employee.super_member_number,
            "is_smsf": super_fund.is_smsf if super_fund else None,
        },
        "totals": {
            "gross": float(line.gross),
            "payg": float(line.tax),
            "super": float(line.super_amount),
            "deductions": float(deductions_total),
            "net": float(line.net),
        },
        "ytd": {
            "gross": float(ytd_gross),
            "payg": float(ytd_tax),
            "super": float(ytd_super),
        },
        "breakdown": {
            "payg_explanation": payg_breakdown,
            "super_explanation": super_breakdown,
        },
    }


__all__ = ["build_payslip"]
