"""UK lodgement adapter — fail-loud live gate (the EE
test_ee_adapter_live_gate.py parallel).

Proves every network-needing call raises ``UKLiveCredentialsMissing``
BEFORE any socket (there is no transport to open — the OAuth client and
fraud-prevention-header middleware are a later phase), that the route
surface matches the staged UK build targets, and that an unknown route
is a ``ValueError`` at the adapter boundary.
"""
from __future__ import annotations

import pytest

from saebooks.services.lodgement.adapters.uk import (
    KNOWN_ROUTES,
    UKLodgementAdapter,
)
from saebooks.services.lodgement.exceptions import (
    LodgementError,
    UKLiveCredentialsMissing,
    UKLodgementError,
)


def test_route_surface_is_the_staged_target_list() -> None:
    assert KNOWN_ROUTES == {
        "vat100",
        "itsa_quarterly",
        "rti_fps",
        "ct600",
        "companies_house",
    }


def test_exception_family_nests_under_lodgement_error() -> None:
    assert issubclass(UKLodgementError, LodgementError)
    assert issubclass(UKLiveCredentialsMissing, UKLodgementError)


@pytest.mark.parametrize("route", sorted(KNOWN_ROUTES))
async def test_every_route_fails_loud_before_any_socket(route: str) -> None:
    adapter = UKLodgementAdapter()
    with pytest.raises(UKLiveCredentialsMissing):
        await adapter.lodge(route, b"{}", f"idem-{route}", {})


async def test_unknown_route_is_a_value_error() -> None:
    adapter = UKLodgementAdapter()
    with pytest.raises(ValueError, match="bogus"):
        await adapter.lodge("bogus", b"{}", "idem-x", {})


async def test_crn_lookup_gated() -> None:
    adapter = UKLodgementAdapter()
    with pytest.raises(UKLiveCredentialsMissing):
        await adapter.lookup_crn("SC123456")


async def test_gate_fires_even_with_env_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials alone must NOT open a path — the transport (OAuth +
    Gov-Client-* middleware) does not exist this wave; the gate fires
    with a sharper message but fires regardless."""
    monkeypatch.setenv("UK_HMRC_CLIENT_ID", "x")
    monkeypatch.setenv("UK_HMRC_CLIENT_SECRET", "x")
    monkeypatch.setenv("UK_HMRC_REDIRECT_URI", "https://localhost/cb")
    adapter = UKLodgementAdapter()
    with pytest.raises(UKLiveCredentialsMissing, match="not built"):
        await adapter.lodge("vat100", b"{}", "idem-env", {})
