"""Tests for saebooks.cli — the cron-kicked background jobs.

Only exercises argparse plumbing + happy paths. The sync/health logic
is fully covered in tests/services/bank_feeds/. Here we just want to
know the CLI wires them up correctly.
"""
from __future__ import annotations

from typing import Any

import pytest

from saebooks import cli
from saebooks.services.bank_feeds import health, onboarding


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
