"""MODE dispatcher tests for the pre-accounting module entrypoint.

M2 wave 2a (#32 P0b(b)) added ``preaccounting_app/__main__.py`` — the
pre-accounting module was previously missing the ``MODE``-based single-image
entrypoint dispatcher that ``capture_app`` / ``platform_app`` already have
(``python -m capture_app`` / ``python -m platform_app``). This mirrors
``platform_app/__main__.py``'s shape exactly: ``MODE=web`` (default) runs
uvicorn; anything else is a hard ``SystemExit(2)``. Pre-accounting has no
``worker.py`` (unlike capture), so — like platform — this is web-only.

No DB / ``grpc_gen`` needed: ``uvicorn.run`` is monkeypatched so the test
never actually binds a socket, matching the wave-1 lesson to test observable
behaviour (call args, exit codes) rather than framework internals.
"""
from __future__ import annotations

import pytest

from preaccounting_app import __main__ as dispatcher


def test_default_mode_runs_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    dispatcher.main()

    assert calls == [("preaccounting_app.main:app", {"host": "0.0.0.0", "port": 8080})]


def test_mode_web_explicit_case_insensitive_and_custom_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODE", "WEB")
    monkeypatch.setenv("PORT", "9090")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    dispatcher.main()

    assert calls == [("preaccounting_app.main:app", {"host": "0.0.0.0", "port": 9090})]


def test_unknown_mode_exits_nonzero_without_starting_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODE", "worker")  # pre-accounting has no worker role
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append((target, kw)))

    with pytest.raises(SystemExit) as exc_info:
        dispatcher.main()

    assert exc_info.value.code == 2
    assert calls == []
