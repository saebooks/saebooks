"""Shared types for the tax_engine package.

The protocol uses these for input/output. Each per-jurisdiction
engine consumes a ``PostingContext`` and returns a ``TaxTreatment``;
the period-summary side returns a ``dict[label, Decimal]`` so each
jurisdiction can use its own form-box vocabulary (BAS labels for AU,
VAT100 boxes for UK, GST101 boxes for NZ).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from saebooks.models.account import AccountType

# Jurisdiction-NEUTRAL classification of a GL account into purchase-side
# ("input") vs sale-side ("output") direction, from double-entry account
# semantics alone (debit-normal expense/asset = input; credit-normal income =
# output). This is bookkeeping structure, NOT a GST/VAT rule — every
# jurisdiction's tax engine reuses it to decide whether a line's tax component
# feeds the paid-side or collected-side bucket. Lives in the neutral core so no
# jurisdiction module has to reach into another's package for it.
INPUT_ACCOUNT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.EXPENSE,
    AccountType.COST_OF_SALES,
    AccountType.OTHER_EXPENSE,
    AccountType.ASSET,
})
OUTPUT_ACCOUNT_TYPES: frozenset[AccountType] = frozenset({
    AccountType.INCOME,
    AccountType.OTHER_INCOME,
})


class PostingError(Exception):
    """A journal entry cannot be posted.

    Defined here in the leaf ``types`` module (not in
    ``services.journal``) so both ``services.journal`` and the
    per-jurisdiction tax engines can subclass it without a circular
    import (journal imports the tax engine at module load, so the tax
    engine cannot import journal at module load). ``services.journal``
    re-exports it as ``journal.PostingError`` for backwards
    compatibility, so every existing ``except journal_svc.PostingError``
    handler is unchanged.
    """


@dataclass(frozen=True, slots=True)
class PostingContext:
    """Inputs the tax engine needs to determine treatment for one line.

    Constructed at journal-line assembly time. Frozen because the
    engine snapshots the result onto the line — the inputs must be
    immutable so a re-run produces the same answer.
    """

    company_id: uuid.UUID
    jurisdiction: str
    posting_date: date
    account_id: uuid.UUID
    account_type: AccountType
    amount: Decimal
    # Pre-computed GST/VAT amount on the line, when the caller already
    # knows it (AU invoices carry it explicitly). None = engine
    # derives.
    gst_amount: Decimal | None = None
    # Resolved tax-code (already looked up by code or id). The engine
    # uses this to pick rate + reporting bucket.
    tax_code: str | None = None
    tax_code_id: uuid.UUID | None = None
    rate: Decimal | None = None
    reporting_type: str | None = None
    # Optional metadata — counterparty-related fields used by some
    # engines (cross-border rules in UK/EU, GST-registered status in
    # NZ).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaxTreatment:
    """Snapshot of the tax determination applied to a journal line.

    Serialised onto ``journal_lines.tax_treatment`` (JSONB) at post
    time. Persisting the snapshot keeps historic returns self-
    consistent even if the underlying tax_code definition changes.
    """

    jurisdiction: str
    code: str          # canonical tax-code string (e.g. "GST", "FRE", "EXP")
    rate: Decimal
    base: Decimal      # tax-base amount (line amount excluding tax)
    tax: Decimal       # tax amount itself
    reporting_type: str
    direction: str     # "output" (sales) | "input" (purchases) | "none"
    notes: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        """Render as a JSON-safe dict for storage in JSONB.

        ``Decimal`` is serialised as a string to keep precision
        round-trip exact (Postgres JSONB accepts numbers but Python's
        ``json.dumps`` would convert via ``float`` and lose digits).
        """
        return {
            "jurisdiction": self.jurisdiction,
            "code": self.code,
            "rate": str(self.rate),
            "base": str(self.base),
            "tax": str(self.tax),
            "reporting_type": self.reporting_type,
            "direction": self.direction,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ValidationError:
    """Returned by ``TaxEngine.validate`` for any pre-post issue.

    Strings rather than typed enums to keep the protocol simple — the
    UI maps codes to messages for the user.
    """

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    """Date window for ``boxes`` queries. Used as a stand-in for the
    persisted ``tax_periods`` row in unit tests; production callers
    pass the row directly. The engine accesses ``.period_start`` and
    ``.period_end`` either way (duck-typed).
    """

    period_start: date
    period_end: date
