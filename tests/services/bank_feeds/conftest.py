"""Shared fixtures for bank-feeds remote-client tests.

Mirrors ``tests/services/lodgement/conftest.py`` so the two suites stay
in step. We keep two concerns out of every test:

* ``LicenseService.current_token()`` reaching for a real cache file —
  the suite never wants that. Patched to return a stub string.
* ``FEEDS_SERVER_URL`` env var possibly set by the developer — pinned
  to a known base URL so respx routes match deterministically.

The pre-existing tests in this directory (test_client / test_endpoints
/ test_repo / etc.) target the legacy SISS-direct stack and don't share
any imports with these new tests, so we don't need to coordinate
fixtures across the two test surfaces.
"""
from __future__ import annotations

import pytest

from saebooks.services.licence import LicenseService


TEST_BASE_URL = "https://feeds.test"
TEST_TOKEN = "test.licence.token"


@pytest.fixture(autouse=True)
def _stub_licence_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LicenseService, "current_token", classmethod(lambda cls: TEST_TOKEN)
    )


@pytest.fixture(autouse=True)
def _pin_feeds_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEEDS_SERVER_URL", TEST_BASE_URL)
