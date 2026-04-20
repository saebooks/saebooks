"""Tests for ``saebooks.services.dashboard``.

Pure-ish tests for the sparkline renderer + integration tests for
widget queries hitting Postgres via ``AsyncSessionLocal``.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services import dashboard as svc


async def _company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


# ---------------------------------------------------------------------- #
# Sparkline renderer (pure)                                               #
# ---------------------------------------------------------------------- #


def test_sparkline_svg_empty_series() -> None:
    cf = svc.CashflowSparkline(days=30, points=[])
    svg = svc.sparkline_svg(cf)
    assert svg.startswith("<svg")
    assert "No cashflow data" in svg
    assert "polyline" not in svg  # No line on empty data


def test_sparkline_svg_flat_series_renders_baseline() -> None:
    # All zeros — must not divide-by-zero.
    points = [(date(2026, 4, i + 1), Decimal("0")) for i in range(5)]
    cf = svc.CashflowSparkline(days=5, points=points)
    svg = svc.sparkline_svg(cf)
    assert "<polyline" in svg
    assert cf.max_abs == Decimal("0")
    # Still contains a dashed baseline line element.
    assert "stroke-dasharray" in svg


def test_sparkline_svg_with_positive_and_negative() -> None:
    points = [
        (date(2026, 4, 1), Decimal("100")),
        (date(2026, 4, 2), Decimal("-50")),
        (date(2026, 4, 3), Decimal("25")),
    ]
    cf = svc.CashflowSparkline(days=3, points=points)
    assert cf.max_abs == Decimal("100")
    svg = svc.sparkline_svg(cf, width=200, height=40)
    # Every coord pair appears once.
    assert svg.count(",") >= 3  # three coord pairs separated by commas
    assert 'viewBox="0 0 200 40"' in svg


# ---------------------------------------------------------------------- #
# Aged-AR snapshot wraps the full report                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_aged_ar_snapshot_returns_all_buckets_zero_on_empty_filter() -> None:
    """Using a far-future as_at still returns a well-formed snapshot.

    The dev DB has invoices spread across the year; using an
    as_at that lands them ALL as 'current' would require
    per-test data setup we're deliberately skipping for the
    dashboard layer. So we just verify the shape + invariants.
    """
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        snap = await svc.aged_ar_snapshot(
            session, cid, as_at=date(2020, 1, 1)
        )
    # Pre-2020 cutoff means no invoices match; all buckets zero.
    assert snap.current == Decimal("0")
    assert snap.d1_30 == Decimal("0")
    assert snap.d31_60 == Decimal("0")
    assert snap.d61_90 == Decimal("0")
    assert snap.d90_plus == Decimal("0")
    assert snap.total == Decimal("0")
    assert snap.overdue == Decimal("0")


# ---------------------------------------------------------------------- #
# Bank balances query returns a list of BankBalance                       #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bank_balances_returns_list_of_reconcile_accounts() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        balances = await svc.bank_balances(session, cid)
    # Must be a list (possibly empty on minimal installs, but the AU
    # seed marks at least one cash-at-bank account reconcilable). The
    # query is AccountType.ASSET + reconcile=True — code prefix is a
    # seed convention we don't hard-assert against because things
    # like BAS Receivable legitimately carry a "2-" code despite
    # being an asset for reconciliation.
    assert isinstance(balances, list)
    for b in balances:
        assert isinstance(b.code, str) and b.code
        assert isinstance(b.balance, Decimal)


# ---------------------------------------------------------------------- #
# Unmatched count                                                         #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unmatched_count_returns_int() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        count = await svc.unmatched_statement_lines_count(session, cid)
    assert isinstance(count, int)
    assert count >= 0


# ---------------------------------------------------------------------- #
# Cashflow 30-day query                                                   #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cashflow_30d_zero_fills_full_window() -> None:
    cid = await _company_id()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        cf = await svc.cashflow_30d(session, cid, as_of=today)
    assert cf.days == 30
    assert len(cf.points) == 30  # Zero-fill: always exactly `days` points
    # Points are dates in ascending order spanning exactly 30 days.
    assert cf.points[0][0] == today - timedelta(days=29)
    assert cf.points[-1][0] == today
    for _, v in cf.points:
        assert isinstance(v, Decimal)


@pytest.mark.asyncio
async def test_cashflow_custom_window() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        cf = await svc.cashflow_30d(
            session, cid, as_of=date(2026, 4, 20), days=7
        )
    assert cf.days == 7
    assert len(cf.points) == 7


# ---------------------------------------------------------------------- #
# Upcoming recurring                                                      #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upcoming_recurring_returns_list_respecting_limit() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        rows = await svc.upcoming_recurring(session, cid, limit=3)
    assert isinstance(rows, list)
    assert len(rows) <= 3
    if rows:
        # Sorted ascending by next_run.
        for a, b in pairwise(rows):
            assert a.next_run <= b.next_run


# ---------------------------------------------------------------------- #
# build_dashboard bundle                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_build_dashboard_returns_complete_bundle() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        bundle = await svc.build_dashboard(session, cid)
    assert isinstance(bundle.bank_balances, list)
    assert isinstance(bundle.aged_ar, svc.AgedArSnapshot)
    assert isinstance(bundle.unmatched_count, int)
    assert isinstance(bundle.cashflow, svc.CashflowSparkline)
    assert bundle.cashflow_svg.startswith("<svg")
    assert isinstance(bundle.upcoming, list)
