"""AU tax engine — GST + BAS.

Reference implementation. Moved here from ``services/gst.py`` (auto-
posting GST Collected / GST Paid lines on journal post) and
``services/bas.py`` (G1/G2/G3/G10/G11/1A/1B period summary).

The two old modules remain as thin re-export shims for one release —
existing callers continue to import ``services.gst`` /
``services.bas`` and get a ``DeprecationWarning`` at import. The
shims are dropped at M1 entry once internal callers move over.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.services import settings as settings_svc
from saebooks.services.tax_engine.types import (
    PostingContext,
    PostingError,
    TaxTreatment,
    ValidationError,
)


class TaxConfigError(PostingError):
    """GST/tax configuration is invalid and posting cannot proceed.

    Raised when a journal entry carries a taxable line (a line with a
    non-zero ``gst_amount``) but the GST account code configured in
    settings does not resolve to a real, non-archived account in the
    company chart. A taxable line with nowhere to post its GST is a
    configuration error — NOT a no-op. Silently dropping the GST line
    produces an unbalanced journal entry that then fails with a
    misleading 'unbalanced' error (a real production incident, 2026-06-10). Surfacing
    a clear config error here points the operator straight at the bad
    setting instead.
    """


# ---------------------------------------------------------------------------
# Account-type → tax-direction tables (shared between auto-post and
# the engine's ``compute``).
# ---------------------------------------------------------------------------

_INPUT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    AccountType.ASSET,
})
_OUTPUT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
})

# Account types considered "income" for BAS purposes.
_BAS_INCOME_TYPES = _OUTPUT_TYPES

# Account types considered "purchases" for BAS purposes.
_BAS_PURCHASE_TYPES = _INPUT_TYPES


# ---------------------------------------------------------------------------
# BAS report dataclasses (kept stable for callers re-importing from
# ``services.bas``).
# ---------------------------------------------------------------------------


@dataclass
class BASLine:
    label: str
    description: str
    amount: Decimal = Decimal("0")


@dataclass
class BASReport:
    period_from: date | None
    period_to: date | None
    g1: BASLine
    g2: BASLine
    g3: BASLine
    g10: BASLine
    g11: BASLine
    label_1a: BASLine
    label_1b: BASLine

    @property
    def gst_payable(self) -> Decimal:
        """Net GST: collected minus paid. Positive = owe ATO."""
        return self.label_1a.amount - self.label_1b.amount


# ---------------------------------------------------------------------------
# GST auto-post helpers — internal to the engine; exported for the
# ``services.gst`` shim.
# ---------------------------------------------------------------------------


async def is_auto_post_enabled(session: AsyncSession) -> bool:
    val = await settings_svc.get(session, "gst_auto_post", "true")
    return str(val).lower() in ("true", "1", "yes")


async def _get_gst_account(
    session: AsyncSession, company_id: uuid.UUID, setting_key: str
) -> Account | None:
    raw = await settings_svc.get(session, setting_key, "")
    if not raw:
        return None
    code = str(raw)
    if "-" not in code and len(code) >= 2 and code[0].isdigit():
        hyphenated = code[0] + "-" + code[1:]
    else:
        hyphenated = code
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_([code, hyphenated]),
            Account.archived_at.is_(None),
        )
    )
    return result.scalars().first()


async def auto_post_gst_lines(
    session: AsyncSession,
    entry: JournalEntry,
) -> list[JournalLine]:
    """Generate GST account lines for a journal entry being posted.

    For each line that has a gst_amount, creates a corresponding line
    on the appropriate GST account (Collected or Paid). Returns the
    list of new GST lines added — the caller flushes/commits.
    """
    if not await is_auto_post_enabled(session):
        return []

    collected_acct = await _get_gst_account(
        session, entry.company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, entry.company_id, "gst_paid_account_code"
    )

    # Classify the line account types up-front so we can tell whether any
    # taxable line actually NEEDS a GST account before deciding that an
    # unresolved code is fatal. An entry with no taxable line at all (e.g.
    # a balance-sheet transfer) must remain a clean no-op even if the GST
    # settings are blank — only a taxable line with nowhere to post GST is
    # a configuration error.
    acct_ids = {line.account_id for line in entry.lines}
    acct_types: dict[uuid.UUID, AccountType] = {}
    if acct_ids:
        result = await session.execute(
            select(Account.id, Account.account_type).where(Account.id.in_(acct_ids))
        )
        for row in result.all():
            acct_types[row[0]] = row[1]

    needs_output = False  # a taxable income/sales line needs GST Collected
    needs_input = False   # a taxable expense/asset line needs GST Paid
    for line in entry.lines:
        gst = line.gst_amount
        if not gst or gst == Decimal("0"):
            continue
        acct_type = acct_types.get(line.account_id)
        if acct_type in _OUTPUT_TYPES:
            needs_output = True
        elif acct_type in _INPUT_TYPES:
            needs_input = True

    # A taxable line whose GST account code does not resolve is a config
    # error — raise loudly instead of silently emitting no GST line and
    # producing an unbalanced JE (root cause of a real production incident,
    # 2026-06-10: gst_paid_account_code was '2-1330', which did not exist in
    # that tenant's chart, so this returned [] and the JE failed as 'unbalanced').
    if needs_output and collected_acct is None:
        raw = await settings_svc.get(session, "gst_collected_account_code", "")
        raise TaxConfigError(
            f"gst_collected_account_code {str(raw)!r} does not resolve to an "
            f"account in the chart — a taxable sales line cannot post its GST. "
            f"Set gst_collected_account_code to a real GST Collected account code."
        )
    if needs_input and paid_acct is None:
        raw = await settings_svc.get(session, "gst_paid_account_code", "")
        raise TaxConfigError(
            f"gst_paid_account_code {str(raw)!r} does not resolve to an "
            f"account in the chart — a taxable purchase line cannot post its "
            f"GST. Set gst_paid_account_code to a real GST Paid account code."
        )

    # No taxable line needs a GST account — nothing to auto-post.
    if not collected_acct and not paid_acct:
        return []

    new_lines: list[JournalLine] = []
    max_line_no = max((line.line_no for line in entry.lines), default=0)
    # IDs of the GST accounts themselves (skip GST-on-GST). Either may be
    # None here when only one direction is configured + only that
    # direction is taxable; the per-line branch below only dereferences
    # the account it actually needs for that line.
    gst_account_ids = {
        a.id for a in (collected_acct, paid_acct) if a is not None
    }

    for line in entry.lines:
        gst = line.gst_amount
        if not gst or gst == Decimal("0"):
            continue
        if line.account_id in gst_account_ids:
            continue
        acct_type = acct_types.get(line.account_id)
        if acct_type is None:
            continue

        if acct_type in _OUTPUT_TYPES and collected_acct is None:
            continue
        if acct_type in _INPUT_TYPES and paid_acct is None:
            continue
        max_line_no += 1
        if acct_type in _OUTPUT_TYPES:
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=collected_acct.id,
                description=f"GST on {line.description or 'sale'}",
                debit=Decimal("0"),
                credit=abs(gst),
            )
        elif acct_type in _INPUT_TYPES:
            gst_line = JournalLine(
                entry_id=entry.id,
                line_no=max_line_no,
                account_id=paid_acct.id,
                description=f"GST on {line.description or 'purchase'}",
                debit=abs(gst),
                credit=Decimal("0"),
            )
        else:
            continue

        session.add(gst_line)
        entry.lines.append(gst_line)
        new_lines.append(gst_line)

    return new_lines


# ---------------------------------------------------------------------------
# GST settings validation — callable on settings save so a bad GST account
# code is caught at configuration time, not at the first taxable post.
# ---------------------------------------------------------------------------

# The GST account-code settings the AU engine resolves at post time.
# ``gst_clearing_account_code`` is only used by the BAS settlement helper
# (``settle_bas``); the other two drive ``auto_post_gst_lines``.
GST_ACCOUNT_SETTING_KEYS: tuple[str, ...] = (
    "gst_collected_account_code",
    "gst_paid_account_code",
    "gst_clearing_account_code",
)


async def validate_gst_account_settings(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    keys: tuple[str, ...] = GST_ACCOUNT_SETTING_KEYS,
    require_set: bool = False,
) -> dict[str, str]:
    """Check that each configured GST account code resolves to a real account.

    Returns a mapping ``{setting_key: problem_message}`` for every key whose
    value is set but does NOT resolve to a non-archived account in the
    company chart. An empty dict means every configured GST account code is
    valid.

    A blank/unset value is tolerated by default (a company may legitimately
    not have wired up, say, the clearing account yet) — pass
    ``require_set=True`` to also flag blanks. This helper is the
    configuration-time counterpart to the post-time guard in
    ``auto_post_gst_lines``: call it on settings save to reject a bad code
    (e.g. the '2-1330' that did not exist in that tenant's chart) up-front
    instead of letting it sit dormant until the first taxable expense.
    """
    problems: dict[str, str] = {}
    for key in keys:
        raw = await settings_svc.get(session, key, "")
        if not raw:
            if require_set:
                problems[key] = f"{key} is not set"
            continue
        acct = await _get_gst_account(session, company_id, key)
        if acct is None:
            problems[key] = (
                f"{key} {str(raw)!r} does not resolve to an account in the "
                f"chart"
            )
    return problems


async def settle_bas(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    settlement_date: date,
    from_date: date | None = None,
    to_date: date | None = None,
) -> JournalEntry | None:
    """Create a draft BAS settlement journal entry."""
    from saebooks.services import journal as journal_svc

    collected_acct = await _get_gst_account(
        session, company_id, "gst_collected_account_code"
    )
    paid_acct = await _get_gst_account(
        session, company_id, "gst_paid_account_code"
    )
    clearing_acct = await _get_gst_account(
        session, company_id, "gst_clearing_account_code"
    )

    if not collected_acct or not paid_acct or not clearing_acct:
        return None

    from saebooks.services.reports import _account_balances

    balances = await _account_balances(
        session, company_id, from_date=from_date, to_date=to_date
    )

    collected_bal = Decimal("0")
    paid_bal = Decimal("0")

    for bal in balances:
        if bal.account_id == collected_acct.id:
            collected_bal = bal.balance
        elif bal.account_id == paid_acct.id:
            paid_bal = bal.balance

    if collected_bal == Decimal("0") and paid_bal == Decimal("0"):
        return None

    lines: list[dict[str, object]] = []

    if collected_bal != Decimal("0"):
        lines.append({
            "account_id": collected_acct.id,
            "description": "Clear GST Collected for BAS",
            "debit": abs(collected_bal),
            "credit": Decimal("0"),
        })

    if paid_bal != Decimal("0"):
        lines.append({
            "account_id": paid_acct.id,
            "description": "Clear GST Paid for BAS",
            "debit": Decimal("0"),
            "credit": abs(paid_bal),
        })

    net = abs(collected_bal) - paid_bal
    if net > Decimal("0"):
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST payable to ATO",
            "debit": Decimal("0"),
            "credit": net,
        })
    elif net < Decimal("0"):
        lines.append({
            "account_id": clearing_acct.id,
            "description": "Net GST refund from ATO",
            "debit": abs(net),
            "credit": Decimal("0"),
        })

    if not lines:
        return None

    period_label = ""
    if from_date and to_date:
        period_label = f" ({from_date} to {to_date})"

    entry = await journal_svc.create_draft(
        session,
        company_id=company_id,
        tenant_id=tenant_id,
        entry_date=settlement_date,
        description=f"BAS settlement{period_label}",
        lines=lines,
    )

    return entry


async def bas_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> BASReport:
    """Build a BAS report for the given period.

    M1.5 · T8 — thin wrapper over
    ``tax_return_generator.generate_return``, which reads the box
    definitions (which company ``TaxCode.reporting_type`` values feed
    G1/G2/G3/G10/G11/1A/1B, and how) from the jurisdiction-keyed
    ``TaxReturnBoxDefinition`` reference table instead of the
    G1/G2/G3/G10/G11/1A/1B literals that used to be hardcoded directly
    in this function. Local import to avoid a module-load cycle:
    ``tax_return_generator`` imports this module's account-type sets at
    import time, so this module cannot import it back at import time
    too — only at call time, once both modules have finished loading.
    See docs/multi-jurisdiction.md (M1.5)
    (theme T8).
    """
    from saebooks.services.tax_return_generator import generate_return

    result = await generate_return(
        session,
        company_id,
        jurisdiction="AU",
        return_type="BAS",
        from_date=from_date,
        to_date=to_date,
    )

    def _line(box_code: str, fallback_description: str) -> BASLine:
        box = result.boxes.get(box_code)
        if box is None:
            return BASLine(box_code, fallback_description, Decimal("0"))
        return BASLine(box_code, box.box_label, box.amount)

    return BASReport(
        period_from=from_date,
        period_to=to_date,
        g1=_line("G1", "Total sales (including any GST)"),
        g2=_line("G2", "Export sales"),
        g3=_line("G3", "Other GST-free sales"),
        g10=_line("G10", "Capital purchases (including any GST)"),
        g11=_line("G11", "Non-capital purchases (including any GST)"),
        label_1a=_line("1A", "GST collected on sales"),
        label_1b=_line("1B", "GST paid on purchases"),
    )


# ---------------------------------------------------------------------------
# AU TaxEngine — protocol-conforming class.
# ---------------------------------------------------------------------------


class AUTaxEngine:
    """Australia GST tax engine — implements the ``TaxEngine`` protocol.

    M0: ``compute`` is sync and pure (no DB). It uses the
    ``PostingContext`` fields the caller already filled in (rate,
    reporting_type, gst_amount) — the engine doesn't look up tax_code
    rows itself; that's the caller's job. This keeps the engine
    cheap to call inline from journal-line construction.

    ``boxes`` and ``validate`` keep their AU shape but are stubs at
    this layer — the existing async ``bas_report`` helper above is
    the AU period-summary path used by the reports router. ``boxes``
    here is a sync convenience that returns the BAS report shape
    given a pre-built ``BASReport`` (passed in via ``period.report``)
    so the protocol is uniform without forcing the engine to issue
    DB calls.
    """

    jurisdiction: str = "AU"

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        # ``rate`` is round-tripped from the input as-is. Production
        # callers fill this from ``TaxCode.rate`` which stores the rate
        # in percentage points (``10.000`` == 10%). Direct unit-test
        # callers may pass a fraction (``0.10``) — both are valid; the
        # engine stores what it's given. Tax derivation falls back to
        # ``gst_amount`` when supplied so the convention only matters
        # for tax_engine consumers reading the snapshot back.
        rate = ctx.rate if ctx.rate is not None else Decimal("0")
        reporting_type = ctx.reporting_type or "no_tax"
        code = ctx.tax_code or "GST"

        # Direction: sales (credit-normal income) → output; purchases
        # (debit-normal expense / asset) → input. Anything else is a
        # no-tax line.
        if ctx.account_type in _OUTPUT_TYPES:
            direction = "output"
        elif ctx.account_type in _INPUT_TYPES:
            direction = "input"
        else:
            direction = "none"

        # Derive base + tax. If the caller supplied gst_amount we
        # trust it; otherwise we compute base = amount, tax = base * rate
        # (the caller's amount is already the net for AU GST lines).
        base = ctx.amount
        if ctx.gst_amount is not None:
            tax = ctx.gst_amount
        elif rate and rate != Decimal("0"):
            tax = (base * rate).quantize(Decimal("0.01"))
        else:
            tax = Decimal("0")

        return TaxTreatment(
            jurisdiction="AU",
            code=code,
            rate=rate,
            base=base,
            tax=tax,
            reporting_type=reporting_type,
            direction=direction,
        )

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Return BAS labels for a period.

        Accepts either a pre-built ``BASReport`` (passed via
        ``period.report``) or any duck-typed object with the seven
        BAS attributes. The async DB-driven ``bas_report`` helper
        above is the production path; this method is the protocol-
        uniform entry point.
        """
        report: BASReport
        if isinstance(period, BASReport):
            report = period
        elif hasattr(period, "report") and isinstance(period.report, BASReport):
            report = period.report
        else:
            raise NotImplementedError(
                "AUTaxEngine.boxes requires a pre-built BASReport for now. "
                "Use saebooks.services.tax_engine.au.bas_report(...) to "
                "produce one and pass it in."
            )

        return {
            "G1": report.g1.amount,
            "G2": report.g2.amount,
            "G3": report.g3.amount,
            "G10": report.g10.amount,
            "G11": report.g11.amount,
            "1A": report.label_1a.amount,
            "1B": report.label_1b.amount,
        }

    def validate(self, invoice: Any) -> list[ValidationError]:
        """AU pre-post checks. Stub at M0 — every AU validation today
        lives in the calling service (services/invoices.py); this
        method exists to satisfy the protocol and is the hook NZ/UK
        will use."""
        return []
