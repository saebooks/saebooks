"""LT lodgement adapter — fail-loud live gate (the EE/NZ/UK
test_*_adapter_live_gate.py parallel).

Proves every network-needing call raises ``LTLiveCredentialsMissing``
BEFORE any socket (there is no transport to open — no i.MAS/EDS
client exists this wave), that the route surface matches the staged LT
build targets, and that an unknown route is a ``ValueError`` at the
adapter boundary.
"""
from __future__ import annotations

import pytest

from saebooks.services.lodgement.adapters.lt import (
    KNOWN_ROUTES,
    LTLodgementAdapter,
)
from saebooks.services.lodgement.exceptions import (
    LodgementError,
    LTLiveCredentialsMissing,
    LTLodgementError,
)


def test_route_surface_is_the_staged_target_list() -> None:
    assert KNOWN_ROUTES == {
        "fr0600",
        "gpm313",
        "isaf",
        "cit_annual",
    }


def test_exception_family_nests_under_lodgement_error() -> None:
    assert issubclass(LTLodgementError, LodgementError)
    assert issubclass(LTLiveCredentialsMissing, LTLodgementError)


@pytest.mark.parametrize("route", sorted(KNOWN_ROUTES))
async def test_every_route_fails_loud_before_any_socket(route: str) -> None:
    adapter = LTLodgementAdapter()
    with pytest.raises(LTLiveCredentialsMissing):
        await adapter.lodge(route, b"{}", f"idem-{route}", {})


async def test_unknown_route_is_a_value_error() -> None:
    adapter = LTLodgementAdapter()
    with pytest.raises(ValueError, match="bogus"):
        await adapter.lodge("bogus", b"{}", "idem-x", {})


async def test_gate_fires_even_with_env_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials alone must NOT open a path — no i.MAS/EDS client
    exists this wave; the gate fires with a sharper message but fires
    regardless."""
    monkeypatch.setenv("LT_IMAS_CLIENT_ID", "x")
    monkeypatch.setenv("LT_IMAS_CLIENT_SECRET", "x")
    adapter = LTLodgementAdapter()
    with pytest.raises(LTLiveCredentialsMissing, match="not built"):
        await adapter.lodge("fr0600", b"{}", "idem-env", {})
