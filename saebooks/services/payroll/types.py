"""Neutral types for the payroll-engine seam (jurisdiction-module Phase 0).

Mirrors ``tax_engine/types.py``'s role for the tax seam: the core owns
these jurisdiction-neutral dataclasses; each per-jurisdiction
``PayrollEngine`` consumes a ``PayrollContext`` and returns a
``PayrollResult`` whose components carry **numbers + account-role
tags** — never accounts, never a posted JE. Which liability/expense
account a role maps to (and the double-entry itself) is the core's
business (``pay_runs_v2``), not the module's.

Unlike ``PostingContext``/``TaxTreatment`` (sync, no-I/O by design),
the payroll seam is **async and the engine owns its own reference
reads** — AU withholding reads the ``payg_tax_scales``/
``stsl_coefficients`` reference tables and EE payroll reads the
reference DB directly (with embedded fallback). Keeping "which tables
to read" inside the module is deliberate (design doc §2.4): it makes
each engine own its own reference-DB-absent degradation.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


class PayrollComponentRole(enum.StrEnum):
    """Jurisdiction-neutral account roles a payroll component can land in.

    The core maps each role to the company's configured account when it
    posts the pay-run JE. AU uses withholding + retirement (PAYG WH /
    Super); EE will add the employer-social roles when it registers
    through this seam (Phase 5 of the design doc).
    """

    WITHHOLDING_LIABILITY = "withholding_liability"
    RETIREMENT_LIABILITY = "retirement_liability"
    RETIREMENT_EXPENSE = "retirement_expense"
    EMPLOYER_SOCIAL_LIABILITY = "employer_social_liability"
    EMPLOYER_SOCIAL_EXPENSE = "employer_social_expense"

    @property
    def posts_debit(self) -> bool:
        """Which side of the pay-run JE this role lands on.

        ``*_EXPENSE`` roles are additional employer cost — an expense
        debit paired with their ``*_LIABILITY`` credit. ``*_LIABILITY``
        roles credit the statutory payable (withholding is carved out
        of gross, so it has no expense leg of its own — the wages debit
        already carries it).
        """
        return self.value.endswith("_expense")


@dataclass(frozen=True, slots=True)
class PayrollComponent:
    """One statutory contribution/withholding amount, role-tagged.

    ``note`` is the human-readable breakdown snapshot (same audit
    purpose as ``TaxTreatment.notes`` — survives later rate changes).
    """

    role: PayrollComponentRole
    amount: Decimal
    note: str = ""


@dataclass(frozen=True, slots=True)
class PayrollContext:
    """Inputs a payroll engine needs to compute one pay-run line.

    Frozen for the same reason as ``PostingContext`` — the result is
    snapshotted onto the pay-run line, so the inputs must be immutable.

    ``gross`` and ``ote`` are the neutral aggregates the core assembles
    from the raw line input (ordinary/overtime/allowances/leave/lump
    sums) — that arithmetic is plain double-entry bookkeeping, not
    jurisdiction compute, so it stays in ``pay_runs_v2._compute``.
    ``employee`` carries the ORM row for engine-specific statutory
    flags (AU: TFN/STSL/Medicare fields; EE: pillar-II election) —
    neutral core code never reads it.
    """

    company_id: uuid.UUID
    employee_id: uuid.UUID
    pay_run_id: uuid.UUID
    period: str            # pay frequency ("WEEKLY"/"FORTNIGHTLY"/"MONTHLY"/...)
    period_start: date
    period_end: date
    effective_date: date   # payment date — drives rate-table selection
    gross: Decimal         # taxable gross for the period
    ote: Decimal           # retirement-contribution base (AU: OTE per SGR 2009/2)
    deductions_total: Decimal
    employee: Any = None
    medicare_exemption: str = "NONE"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PayrollResult:
    """What a payroll engine computed for one line.

    ``net`` is take-home after withholding + caller-supplied
    deductions. ``components`` are the statutory add-ons, role-tagged;
    the core sums roles it recognises and (Phase 1, design §5.3) will
    post the JE from them. An engine with nothing to add (the neutral
    null object) returns ``net == gross - deductions_total`` and an
    empty component tuple.
    """

    jurisdiction: str
    gross: Decimal
    net: Decimal
    components: tuple[PayrollComponent, ...] = ()

    def total_for(self, role: PayrollComponentRole) -> Decimal:
        return sum(
            (c.amount for c in self.components if c.role is role),
            start=Decimal("0"),
        )

    def note_for(self, role: PayrollComponentRole) -> str:
        for c in self.components:
            if c.role is role and c.note:
                return c.note
        return ""


@dataclass(frozen=True, slots=True)
class PayrollRoleAccount:
    """One role's posting target: which chart account (by code) a
    ``PayrollComponentRole`` books to, and the JE-line description
    prefix it carries (``f"{label}: {employee}"``). Direction comes
    from the role itself (``PayrollComponentRole.posts_debit``)."""

    role: PayrollComponentRole
    account_code: str
    label: str


@dataclass(frozen=True, slots=True)
class PayrollPostingProfile:
    """How a jurisdiction's role-tagged payroll amounts land in the GL.

    Jurisdiction-module Phase 1 (design doc §5.3): the core owns the
    pay-run double-entry — Dr wages(gross), Dr each ``*_EXPENSE``
    component, Cr each ``*_LIABILITY`` component, Cr net clearing —
    and this profile supplies the only jurisdiction-varying part:
    which account code (and line label) each role maps to. Modules
    register one via ``register_jurisdiction_module``; an unregistered
    jurisdiction falls back to ``neutral.NEUTRAL_POSTING_PROFILE``
    (wages + net only, no statutory roles).

    ``role_accounts`` order is significant twice over and must be
    preserved by implementations: it is the account-resolution order
    (so which missing-account error fires first is stable) AND the
    per-employee JE line order after the wages debit.
    """

    wages_account_code: str
    net_account_code: str
    role_accounts: tuple[PayrollRoleAccount, ...] = ()
    wages_label: str = "Wages"
    net_label: str = "Net pay"
