"""Tests for ``saebooks.services.observability``.

Three things to verify:

* ``configure_json_logging`` replaces the root handler's formatter
  with a JSON one, emits valid JSON, and is idempotent.
* ``configure_sentry`` is a no-op on empty DSN and is safe to call
  twice.
* ``configure`` reads the Settings object and routes to both paths
  correctly.
"""
from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import pytest

from saebooks.config import Settings
from saebooks.services import observability

pytestmark = pytest.mark.postgres_only


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> None:
    """Snapshot + restore the root logger handlers around each test.

    The module under test reconfigures the global root logger, which
    would otherwise contaminate the rest of the test suite.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        # Close any test-added handlers, then restore the snapshot.
        for h in list(root.handlers):
            if h not in saved_handlers:
                h.close()
                root.removeHandler(h)
        # Re-add any that tests dropped.
        for h in saved_handlers:
            if h not in root.handlers:
                root.addHandler(h)
        root.setLevel(saved_level)


# ---------------------------------------------------------------------- #
# JSON logging                                                            #
# ---------------------------------------------------------------------- #


def test_json_logging_emits_valid_json() -> None:
    """After configure_json_logging, each log record serialises to a
    JSON line containing our canonical fields (ts/level/logger/msg)."""
    root = logging.getLogger()
    # Replace handlers with a stream we can inspect.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    observability.configure_json_logging(force=True)

    logging.getLogger("saebooks.test").info("hello world", extra={"k": "v"})
    handler.flush()

    line = buf.getvalue().strip()
    assert line, "No log output captured"
    record = json.loads(line)  # proves it's valid JSON
    assert record["msg"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "saebooks.test"
    assert record["k"] == "v"  # extras bubble up as top-level keys
    assert "ts" in record


def test_json_logging_is_idempotent() -> None:
    """Calling configure_json_logging twice doesn't re-wrap output.

    Regression guard: if the second call reattached the formatter to
    an already-wrapped handler, each record would be double-encoded.
    """
    root = logging.getLogger()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    observability.configure_json_logging(force=True)
    # Second call should be a no-op (returns False).
    result = observability.configure_json_logging()
    assert result is False

    logging.getLogger("saebooks.test").info("once")
    handler.flush()
    out = buf.getvalue().strip()
    # If double-wrapping were happening, we'd see "once" nested inside
    # another JSON object as a string. Parse once and assert the msg
    # field is a plain string, not another JSON doc.
    record = json.loads(out)
    assert record["msg"] == "once"
    assert not record["msg"].startswith("{")


# ---------------------------------------------------------------------- #
# Sentry                                                                  #
# ---------------------------------------------------------------------- #


def test_sentry_noop_on_empty_dsn() -> None:
    """Empty DSN must never touch sentry_sdk.init."""
    with patch("sentry_sdk.init") as mock_init:
        result = observability.configure_sentry("")
    assert result is False
    mock_init.assert_not_called()


def test_sentry_inits_with_dsn() -> None:
    """Non-empty DSN runs sentry_sdk.init with conservative defaults."""
    with patch("sentry_sdk.init") as mock_init:
        result = observability.configure_sentry(
            "https://abc@sentry.example/1", environment="community"
        )
    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://abc@sentry.example/1"
    assert kwargs["environment"] == "community"
    # Defence-in-depth: PII must not be shipped by default.
    assert kwargs["send_default_pii"] is False
    # Traces must be off by default (cost + noise).
    assert kwargs["traces_sample_rate"] == 0.0


# ---------------------------------------------------------------------- #
# Top-level configure()                                                   #
# ---------------------------------------------------------------------- #


def test_configure_both_off_by_default() -> None:
    """Default Settings (no SAEBOOKS_LOG_JSON, no SENTRY_DSN) → neither
    side effect runs."""
    s = Settings(SAEBOOKS_LOG_JSON=False, SENTRY_DSN="")
    with patch("sentry_sdk.init") as mock_init:
        status = observability.configure(s)
    assert status == {"json_logging": False, "sentry": False}
    mock_init.assert_not_called()


def test_configure_turns_on_json_when_requested() -> None:
    s = Settings(SAEBOOKS_LOG_JSON=True, SENTRY_DSN="")
    status = observability.configure(s)
    assert status["json_logging"] is True
    assert status["sentry"] is False


def test_configure_inits_sentry_when_dsn_set() -> None:
    s = Settings(SAEBOOKS_LOG_JSON=False, SENTRY_DSN="https://x@s.example/1")
    with patch("sentry_sdk.init") as mock_init:
        status = observability.configure(s)
    assert status["sentry"] is True
    mock_init.assert_called_once()
