"""Tests for saebooks.cli — the cron-kicked background jobs.

Only exercises argparse plumbing + happy paths. The sync/health logic
is fully covered in tests/services/bank_feeds/. Here we just want to
know the CLI wires them up correctly.
"""
from __future__ import annotations

from typing import Any

import pytest

from saebooks import cli
from saebooks.services.bank_feeds import health, onboarding, reconcile


def test_cli_help_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    # argparse exits with 2 when no subcommand is given
    assert exc.value.code == 2


def test_cli_sync_feeds_calls_sync_all_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_sync_all(session: Any, **kwargs: Any) -> list[Any]:
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(onboarding, "sync_all_active", fake_sync_all)
    rc = cli.main(["sync-feeds"])
    assert rc == 0
    assert captured["kwargs"]["company_id"] is None


def test_cli_sync_feeds_passes_company_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    captured: dict[str, Any] = {}

    async def fake_sync_all(session: Any, **kwargs: Any) -> list[Any]:
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(onboarding, "sync_all_active", fake_sync_all)
    cid = str(uuid.uuid4())
    rc = cli.main(["sync-feeds", "--company-id", cid])
    assert rc == 0
    assert str(captured["kwargs"]["company_id"]) == cid


def test_cli_sync_feeds_returns_1_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sync_all(session: Any, **kwargs: Any) -> list[Any]:
        raise onboarding.SissNotConfiguredError("no creds")

    monkeypatch.setattr(onboarding, "sync_all_active", fake_sync_all)
    rc = cli.main(["sync-feeds"])
    assert rc == 1


def test_cli_refresh_feed_issues_calls_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    called: dict[str, int] = {"n": 0}

    async def fake_refresh(**kwargs: Any) -> health.RefreshOutcome:
        called["n"] += 1
        return health.RefreshOutcome(fetched=0, cached=0, as_of=datetime.now())

    monkeypatch.setattr(health, "refresh_feed_issues", fake_refresh)
    rc = cli.main(["refresh-feed-issues"])
    assert rc == 0
    assert called["n"] == 1


def test_cli_refresh_returns_1_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_refresh(**kwargs: Any) -> health.RefreshOutcome:
        raise onboarding.SissNotConfiguredError("no creds")

    monkeypatch.setattr(health, "refresh_feed_issues", fake_refresh)
    rc = cli.main(["refresh-feed-issues"])
    assert rc == 1


# ---------------------------------------------------------------------- #
# reconcile-feeds                                                        #
# ---------------------------------------------------------------------- #


def _mk_fake_report(severity: str) -> reconcile.ReconciliationReport:
    """Build a minimal ReconciliationReport whose worst-severity is seeded."""
    import uuid as _uuid
    from datetime import date as _date
    from decimal import Decimal as _D

    health_kwargs: dict[str, Any] = {
        "bank_feed_account_id": _uuid.uuid4(),
        "ledger_account_id": _uuid.uuid4(),
        "ledger_account_code": "1-1110",
        "ledger_account_name": "Bank",
        "display_name": None,
        "masked_number": None,
        "last_statement_date": _date(2026, 4, 20),
        "days_since_last_statement": 1,
        "stale": severity == "error",
        "unmatched_count": 2 if severity == "warn" else 0,
        "feed_total": _D("100.00"),
        "gl_total": _D("100.00"),
        "variance": _D("0.00"),
        "has_variance": False,
    }
    a = reconcile.AccountHealth(**health_kwargs)
    return reconcile.ReconciliationReport(
        company_id=_uuid.uuid4(),
        through_date=_date(2026, 4, 20),
        accounts=[a],
    )


def test_cli_reconcile_feeds_clean_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sweep_all(session: Any, **kwargs: Any) -> list[Any]:
        return [_mk_fake_report("ok")]

    monkeypatch.setattr(reconcile, "sweep_all_companies", fake_sweep_all)
    rc = cli.main(["reconcile-feeds"])
    assert rc == 0


def test_cli_reconcile_feeds_warn_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNMATCHED warnings are informational, not failures — still exit 0."""

    async def fake_sweep_all(session: Any, **kwargs: Any) -> list[Any]:
        return [_mk_fake_report("warn")]

    monkeypatch.setattr(reconcile, "sweep_all_companies", fake_sweep_all)
    rc = cli.main(["reconcile-feeds"])
    assert rc == 0


def test_cli_reconcile_feeds_error_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale or variance → exit 1 so cron alerting fires."""

    async def fake_sweep_all(session: Any, **kwargs: Any) -> list[Any]:
        return [_mk_fake_report("error")]

    monkeypatch.setattr(reconcile, "sweep_all_companies", fake_sweep_all)
    rc = cli.main(["reconcile-feeds"])
    assert rc == 1


def test_cli_reconcile_feeds_passes_company_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid as _uuid

    captured: dict[str, Any] = {}

    async def fake_sweep(session: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return _mk_fake_report("ok")

    async def fake_sweep_all(session: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("--company-id should NOT hit sweep_all_companies")

    monkeypatch.setattr(reconcile, "sweep", fake_sweep)
    monkeypatch.setattr(reconcile, "sweep_all_companies", fake_sweep_all)
    cid = str(_uuid.uuid4())
    rc = cli.main(["reconcile-feeds", "--company-id", cid])
    assert rc == 0
    assert str(captured["kwargs"]["company_id"]) == cid
