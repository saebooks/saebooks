"""Payment-terms → due-date derivation.

A contact may carry default payment terms (``payment_terms_basis`` +
``payment_terms_days``). When a bill or invoice is created without an explicit
due_date, the engine derives it from the issuing/billing contact's terms via
``compute_due_date``. NULL terms → returns None (caller keeps due_date explicit).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from saebooks.models.contact import PaymentTermsBasis


def end_of_month(d: date) -> date:
    """Last calendar day of ``d``'s month."""
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last_day)


def compute_due_date(
    issue_date: date,
    basis: PaymentTermsBasis | None,
    days: int | None,
) -> date | None:
    """Derive a due date from an issue date and a contact's default terms.

    * ``DAYS`` → ``issue_date + days`` (net N from the invoice date).
    * ``EOM``  → ``end_of_month(issue_date) + days`` (N days after month end —
      Australian "30-day EOM": a 14 May invoice with EOM 30 → 31 May + 30 = 30 Jun).

    Returns ``None`` when the contact has no usable terms (basis or days NULL),
    so the caller can fall back to an explicit/issue-date due date.
    """
    if basis is None or days is None:
        return None
    if basis == PaymentTermsBasis.DAYS:
        return issue_date + timedelta(days=days)
    if basis == PaymentTermsBasis.EOM:
        return end_of_month(issue_date) + timedelta(days=days)
    return None
