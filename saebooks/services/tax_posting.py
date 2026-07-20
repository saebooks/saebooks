"""Auto-posting of the balancing tax line — jurisdiction-NEUTRAL core plumbing.

For a journal entry being posted, this reads each line's ``gst_amount`` and adds
the matching tax-account line (collected-side / paid-side) so the entry
balances. Despite the ``gst_*`` setting-key names (kept for backwards
compatibility — renaming them is a persisted-``Setting`` migration, deferred),
NOTHING here is Australia-specific: it is generic double-entry structure driven
by the neutral ``AccountType`` classification. Every jurisdiction that carries a
tax amount on a line relies on it (AU GST, UK/EE/NZ/LT/LV VAT) — the UK/LT/LV
golden tests post through this path and assert the resulting VAT line. It was
mis-filed inside ``jurisdictions/au/tax.py`` (jmod Phase 2); it belongs in the
neutral core so ``services.journal`` posts through it without importing a
jurisdiction module. ``jurisdictions.au.tax`` re-imports these names so its
AU-specific ``settle_bas`` / ``validate_gst_account_settings`` / ``AUTaxEngine``
and the ``jurisdictions.au.gst`` shim are unchanged.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.services import settings as settings_svc
from saebooks.services.tax_engine.types import (
    INPUT_ACCOUNT_TYPES as _INPUT_TYPES,
)
from saebooks.services.tax_engine.types import (
    OUTPUT_ACCOUNT_TYPES as _OUTPUT_TYPES,
)
from saebooks.services.tax_engine.types import (
    PostingError,
)


class TaxConfigError(PostingError):
    """GST/tax configuration is invalid and posting cannot proceed.

    Raised when a journal entry carries a taxable line (a line with a
    non-zero ``gst_amount``) but the GST account code configured in
    settings does not resolve to a real, non-archived account in the
    company chart. A taxable line with nowhere to post its GST is a
    configuration error — NOT a no-op. Silently dropping the GST line
    produces an unbalanced journal entry that then fails with a
    misleading 'unbalanced' error (the 2026-06-10 primary bug). Surfacing
    a clear config error here points the operator straight at the bad
    setting instead.
    """


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
    # producing an unbalanced JE (root cause of the 2026-06-10 primary bug:
    # gst_paid_account_code was '2-1330', which did not exist in that
    # tenant's chart, so this returned [] and the JE failed as 'unbalanced').
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
