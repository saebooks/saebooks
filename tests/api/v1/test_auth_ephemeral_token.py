"""Regression: the ephemeral dev API token value must not hit stdout in
one-click / user-facing mode.

The one-click server runs with a visible console and no SAEBOOKS_DEV_API_TOKEN,
so the startup path generates a random bearer. Printing that bearer in
cleartext is a secret-to-stdout leak (noisy and scary in a user console). The
value is logged ONLY in an explicit dev/test environment; otherwise the log
records that a token was generated without the value.
"""
from __future__ import annotations

import logging

import pytest

from saebooks.api.v1 import auth as auth_mod


def _call_resolve(monkeypatch: pytest.MonkeyPatch, env_value: str | None) -> str:
    monkeypatch.delenv(auth_mod._ENV_VAR, raising=False)
    if env_value is None:
        monkeypatch.delenv(auth_mod._DEV_ENV_GUARD, raising=False)
    else:
        monkeypatch.setenv(auth_mod._DEV_ENV_GUARD, env_value)
    return auth_mod._resolve_token()


@pytest.mark.parametrize("env_value", [None, "production", "prod", "community"])
def test_ephemeral_token_value_suppressed_outside_dev(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env_value: str | None,
) -> None:
    with caplog.at_level(logging.INFO, logger="saebooks.api.auth"):
        token = _call_resolve(monkeypatch, env_value)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert token and token not in joined, "token value leaked to logs"
    assert "value suppressed" in joined
    assert "ephemeral API token" in joined


@pytest.mark.parametrize("env_value", ["dev", "development", "test"])
def test_ephemeral_token_value_shown_in_dev(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env_value: str,
) -> None:
    with caplog.at_level(logging.INFO, logger="saebooks.api.auth"):
        token = _call_resolve(monkeypatch, env_value)

    joined = "\n".join(r.getMessage() for r in caplog.records)
    # On a real dev box the value is a documented convenience.
    assert token in joined
