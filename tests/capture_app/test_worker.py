"""Tests for the capture background worker loop (#32 step 5).

* one-iteration test — with a positive interval, the worker runs
  ``_sync_feeds`` + ``_reconcile_feeds`` exactly once and reports their exit
  codes (jobs are mocked; no DB / relay is touched).
* idle-mode test — with ``CAPTURE_SYNC_INTERVAL_MINUTES=0`` (the default) the
  worker logs a heartbeat and runs NO jobs.

Per the 2026-07-04 test-hygiene rule, settings and the injected job functions
are patched by STRING path only.
"""
from __future__ import annotations

import logging

import pytest

from capture_app import worker


@pytest.fixture
def mock_jobs(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the two CLI jobs with counters. Patched by string path so a
    stale object reference can't leak between tests."""
    calls = {"sync": 0, "reconcile": 0}

    async def fake_sync(company_id, *, allow_bypass=False):
        calls["sync"] += 1
        # The worker must NOT pass allow_bypass=True — it respects NOBYPASSRLS.
        assert allow_bypass is False
        return 0

    async def fake_reconcile(company_id):
        calls["reconcile"] += 1
        return 0

    monkeypatch.setattr("capture_app.worker._sync_feeds", fake_sync)
    monkeypatch.setattr("capture_app.worker._reconcile_feeds", fake_reconcile)
    return calls


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_worker_runs_one_iteration(
    monkeypatch: pytest.MonkeyPatch, mock_jobs: dict[str, int]
) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_sync_interval_minutes", 5)

    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    await worker.run(max_iterations=1, sleep=record_sleep)

    assert mock_jobs == {"sync": 1, "reconcile": 1}
    # max_iterations reached → the loop breaks BEFORE sleeping.
    assert slept == []


async def test_worker_two_iterations_sleeps_between(
    monkeypatch: pytest.MonkeyPatch, mock_jobs: dict[str, int]
) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_sync_interval_minutes", 3)

    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    await worker.run(max_iterations=2, sleep=record_sleep)

    assert mock_jobs == {"sync": 2, "reconcile": 2}
    # One sleep between the two iterations, at interval*60 seconds.
    assert slept == [180.0]


async def test_worker_idle_mode_runs_no_jobs(
    monkeypatch: pytest.MonkeyPatch, mock_jobs: dict[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("saebooks.config.settings.capture_sync_interval_minutes", 0)

    with caplog.at_level(logging.INFO, logger="saebooks.capture.worker"):
        await worker.run(max_iterations=1, sleep=_no_sleep)

    assert mock_jobs == {"sync": 0, "reconcile": 0}
    assert any("idle heartbeat" in rec.message for rec in caplog.records)


async def test_worker_iteration_failure_does_not_kill_loop(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising job is logged and the loop continues to the next iteration."""
    calls = {"sync": 0}

    async def boom_sync(company_id, *, allow_bypass=False):
        calls["sync"] += 1
        raise RuntimeError("sync exploded")

    async def ok_reconcile(company_id):
        return 0

    monkeypatch.setattr("capture_app.worker._sync_feeds", boom_sync)
    monkeypatch.setattr("capture_app.worker._reconcile_feeds", ok_reconcile)
    monkeypatch.setattr("saebooks.config.settings.capture_sync_interval_minutes", 1)

    # Two iterations; both raise but the loop survives and completes.
    await worker.run(max_iterations=2, sleep=_no_sleep)
    assert calls["sync"] == 2
