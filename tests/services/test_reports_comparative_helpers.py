"""Pure-function unit tests for the R7 comparative-report helpers in
``services/reports.py`` -- ``fy_bounds_for_company`` (Finding 1 fix) and
``merge_comparative_lines``'s account-code re-sort (Minor 4 fix).

No DB session involved -- these are plain functions of dates/dicts, so
they are covered here rather than only indirectly through the API-level
integration tests in ``tests/api/v1/test_reports_comparative.py``.
"""
from __future__ import annotations

from datetime import date

from saebooks.services import reports as reports_svc

# ---------------------------------------------------------------------------
# fy_bounds_for_company
# ---------------------------------------------------------------------------


def test_fy_bounds_for_company_july_default() -> None:
    """month=7, day=1 (the AU default) matches the pre-existing
    ``_current_fy_bounds`` semantics for both sides of the FY boundary."""
    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2026, 7, 18), 7, 1)
    assert fy_start == date(2026, 7, 1)
    assert fy_end == date(2027, 6, 30)

    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2026, 3, 1), 7, 1)
    assert fy_start == date(2025, 7, 1)
    assert fy_end == date(2026, 6, 30)


def test_fy_bounds_for_company_uk_april_anchor() -> None:
    """month=4, day=6 (the UK anchor) -- a date just before the anchor falls
    in the FY that started the PRIOR April; a date on/after the anchor
    falls in the FY that starts this April."""
    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2130, 3, 15), 4, 6)
    assert fy_start == date(2129, 4, 6)
    assert fy_end == date(2130, 4, 5)

    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2130, 4, 6), 4, 6)
    assert fy_start == date(2130, 4, 6)
    assert fy_end == date(2131, 4, 5)


def test_fy_bounds_for_company_clamps_short_month_non_leap() -> None:
    """An anchor day that does not exist in the anchor month for a given
    year (day 31 in February) clamps to that month's actual last day --
    28 Feb in a non-leap year."""
    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2027, 3, 1), 2, 31)
    assert fy_start == date(2027, 2, 28)
    # Next year's anchor is Feb 2028 (a leap year) -> clamps to 29, not 28.
    assert fy_end == date(2028, 2, 28)


def test_fy_bounds_for_company_clamps_short_month_leap() -> None:
    """Same clamp, but the anchor year itself is a leap year -> clamps to
    29 Feb, not 28."""
    fy_start, fy_end = reports_svc.fy_bounds_for_company(date(2028, 3, 1), 2, 31)
    assert fy_start == date(2028, 2, 29)
    assert fy_end == date(2029, 2, 27)


# ---------------------------------------------------------------------------
# merge_comparative_lines -- account-code re-sort
# ---------------------------------------------------------------------------


def test_merge_comparative_lines_resorts_by_code() -> None:
    """A comparative-only account (no current-period line) is appended
    after every current line -- pre-fix this broke the ``Account.code``
    ordering the raw queries already produce. The merge must re-sort so
    the comparative-only account lands between its neighbours by code,
    not at the tail."""
    current_lines = [
        {"account_id": "a1", "code": "1000", "amount": 100.0},
        {"account_id": "a3", "code": "3000", "amount": 300.0},
    ]
    comparative_lines = [
        {"account_id": "a1", "code": "1000", "amount": 90.0},
        {"account_id": "a2", "code": "2000", "amount": 50.0},
    ]

    merged = reports_svc.merge_comparative_lines(
        current_lines, comparative_lines, value_key="amount"
    )

    assert [ln["code"] for ln in merged] == ["1000", "2000", "3000"]
    assert merged[1]["account_id"] == "a2"
    assert merged[1]["amount"] == 0.0
    assert merged[1]["comparative"] == 50.0
