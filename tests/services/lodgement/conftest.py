"""Shared fixtures for lodgement tests.

The fixtures here keep tests free of two recurring concerns:

* ``LicenseService.current_token`` reading from a real cache file on
  disk — the suite never wants that. We monkey-patch it to return
  a stub token string.
* ``LODGE_SERVER_URL`` env var possibly set by the developer — we
  pin a known base URL so respx matches and the lodge.saebooks.com.au
  default is never accidentally hit during tests.
"""
from __future__ import annotations

import pytest

from saebooks.services.licence import LicenseService


TEST_BASE_URL = "https://lodge.test"
TEST_TOKEN = "test.licence.token"


@pytest.fixture(autouse=True)
def _stub_licence_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``LicenseService.current_token`` to return a fixture token.

    ``autouse`` so every test in this directory gets it without
    boilerplate. Tests that want to exercise the no-token path can
    override with their own monkeypatch.
    """
    monkeypatch.setattr(
        LicenseService, "current_token", classmethod(lambda cls: TEST_TOKEN)
    )


@pytest.fixture(autouse=True)
def _pin_lodge_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``LODGE_SERVER_URL`` so respx routes match deterministically."""
    monkeypatch.setenv("LODGE_SERVER_URL", TEST_BASE_URL)
