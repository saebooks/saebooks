"""Tests for the per-jurisdiction lodgement-adapter registry.

Covers:

- AU returns a fully-wired adapter that delegates to the existing
  licence-gated relay chain.
- UK returns a stub adapter whose every call raises
  ``NotImplementedError`` keyed to M2.
- NZ returns the shaped, live-gated adapter (NZ jurisdiction module):
  known targets validated, then ``NZLiveCredentialsMissing`` before any
  socket (no IR gateway credentials are provisioned).
- Unknown jurisdiction raises ``UnknownJurisdiction`` (KeyError).
- Unknown AU route raises ``UnknownRoute`` (LookupError).
"""
from __future__ import annotations

import pytest

from saebooks.jurisdictions.nz.lodgement import NZLodgementAdapter
from saebooks.services.licence import LicenseService
from saebooks.services.lodgement import (
    NullLodgementService,
    RemoteLodgementService,
    UnknownJurisdiction,
    UnknownRoute,
    get_adapter,
)
from saebooks.services.lodgement.adapters.au import AULodgementAdapter
from saebooks.services.lodgement.adapters.ee import EELodgementAdapter
from saebooks.services.lodgement.adapters.uk import UKLodgementAdapter


def test_get_adapter_au_returns_au_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LicenseService, "has_feature", classmethod(lambda cls, flag: True)
    )
    adapter = get_adapter("AU")
    assert isinstance(adapter, AULodgementAdapter)
    assert adapter.jurisdiction == "AU"
    # Should wrap a RemoteLodgementService when feature is on.
    assert isinstance(adapter.service, RemoteLodgementService)


def test_get_adapter_au_with_feature_off_wraps_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LicenseService, "has_feature", classmethod(lambda cls, flag: False)
    )
    adapter = get_adapter("AU")
    assert isinstance(adapter, AULodgementAdapter)
    assert isinstance(adapter.service, NullLodgementService)


def test_get_adapter_au_known_route_validates() -> None:
    # Should not raise — bas is a known AU route.
    adapter = get_adapter("AU", route="bas")
    assert isinstance(adapter, AULodgementAdapter)


def test_get_adapter_au_unknown_route_raises() -> None:
    with pytest.raises(UnknownRoute, match="bogus_route"):
        get_adapter("AU", route="bogus_route")


def test_get_adapter_nz_returns_stub() -> None:
    adapter = get_adapter("NZ")
    assert isinstance(adapter, NZLodgementAdapter)
    assert adapter.jurisdiction == "NZ"


@pytest.mark.asyncio
async def test_get_adapter_nz_lodge_gates_loudly_before_any_socket() -> None:
    # NZ jurisdiction module: the target is validated, then the live
    # gate fires (no IR gateway credentials are provisioned) — the
    # ee.py fail-loud pattern, zero network egress.
    from saebooks.services.lodgement.exceptions import NZLiveCredentialsMissing

    adapter = get_adapter("NZ")
    with pytest.raises(NZLiveCredentialsMissing):
        await adapter.lodge("gst101", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_nz_route_arg_passes_through() -> None:
    """Per-route validation at registry level is applied only to AU —
    the NZ adapter validates its own targets at call time."""
    from saebooks.services.lodgement.exceptions import NZLiveCredentialsMissing

    adapter = get_adapter("NZ", route="gst101")
    with pytest.raises(NZLiveCredentialsMissing):
        await adapter.lodge("gst101", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_uk_lodge_gated_on_live_creds() -> None:
    """The UK jurisdiction module reshaped the M2 stub: the adapter is
    wired with named targets, but with no HMRC credentials (and no UK
    transport built this wave) a live ``lodge`` fails loud with
    ``UKLiveCredentialsMissing`` (before any network) instead of the old
    M2 ``NotImplementedError`` stub — the same shape as EE's live gate
    below."""
    from saebooks.services.lodgement.exceptions import UKLiveCredentialsMissing

    adapter = get_adapter("UK")
    assert isinstance(adapter, UKLodgementAdapter)
    with pytest.raises(UKLiveCredentialsMissing):
        await adapter.lodge("vat100", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_ee_lodge_gated_on_live_creds() -> None:
    """M3 is now real: the EE adapter is wired, but with no X-Road mTLS creds
    provisioned a live ``lodge`` fails loud with ``EELiveCredentialsMissing``
    (before any network) instead of the old M3 ``NotImplementedError`` stub."""
    from saebooks.services.lodgement import EELiveCredentialsMissing

    adapter = get_adapter("EE")
    assert isinstance(adapter, EELodgementAdapter)
    with pytest.raises(EELiveCredentialsMissing):
        await adapter.lodge("vat_kmd", b"<xml/>", "id-1", {})


def test_get_adapter_unknown_jurisdiction_raises() -> None:
    with pytest.raises(UnknownJurisdiction, match="XX"):
        get_adapter("XX")


def test_au_adapter_with_injected_service() -> None:
    """Adapters can be constructed with an explicit ``LodgementService``
    for testing — bypasses the licence factory entirely."""
    null_svc = NullLodgementService()
    adapter = AULodgementAdapter(service=null_svc)
    assert adapter.service is null_svc


@pytest.mark.asyncio
async def test_au_adapter_dispatches_to_underlying_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AU adapter's ``lodge('bas', ...)`` calls
    ``service.lodge_bas(...)``."""
    calls: list[tuple] = []

    class _FakeService:
        async def lodge_bas(self, envelope, period_id, metadata):
            calls.append(("bas", envelope, period_id, metadata))
            return "ok"

        async def lodge_stp(self, envelope, payevent_id, metadata):
            calls.append(("stp", envelope, payevent_id, metadata))
            return "ok"

    adapter = AULodgementAdapter(service=_FakeService())  # type: ignore[arg-type]
    await adapter.lodge("bas", b"env", "p1", {"k": "v"})
    await adapter.lodge("stp", b"env2", "pe1", {})

    assert calls[0] == ("bas", b"env", "p1", {"k": "v"})
    assert calls[1] == ("stp", b"env2", "pe1", {})


@pytest.mark.asyncio
async def test_au_adapter_unknown_route_raises_typed_error() -> None:
    from saebooks.services.lodgement.adapters.au import (
        UnknownRoute as AUUnknownRoute,
    )

    adapter = AULodgementAdapter(service=NullLodgementService())
    with pytest.raises(AUUnknownRoute):
        await adapter.lodge("not_a_route", b"x", "id", {})
