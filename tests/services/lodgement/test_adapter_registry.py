"""Tests for the per-jurisdiction lodgement-adapter registry.

Covers:

- AU returns a fully-wired adapter that delegates to the existing
  licence-gated relay chain.
- NZ/UK/EE return stub adapters whose every call raises
  ``NotImplementedError`` keyed to M1/M2/M3.
- Unknown jurisdiction raises ``UnknownJurisdiction`` (KeyError).
- Unknown AU route raises ``UnknownRoute`` (LookupError).
"""
from __future__ import annotations

import pytest

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
from saebooks.services.lodgement.adapters.nz import NZLodgementAdapter
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
async def test_get_adapter_nz_lodge_raises_not_implemented_m1() -> None:
    adapter = get_adapter("NZ")
    with pytest.raises(NotImplementedError, match="M1"):
        await adapter.lodge("gst101", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_nz_route_arg_passes_through() -> None:
    """Per-route validation is intentionally lax for stub jurisdictions —
    any route string accepted at registry level; adapter raises on call."""
    adapter = get_adapter("NZ", route="gst101")
    with pytest.raises(NotImplementedError, match="M1"):
        await adapter.lodge("gst101", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_uk_lodge_raises_not_implemented_m2() -> None:
    adapter = get_adapter("UK")
    assert isinstance(adapter, UKLodgementAdapter)
    with pytest.raises(NotImplementedError, match="M2"):
        await adapter.lodge("vat100", b"<xml/>", "id-1", {})


@pytest.mark.asyncio
async def test_get_adapter_ee_lodge_raises_not_implemented_m3() -> None:
    adapter = get_adapter("EE")
    assert isinstance(adapter, EELodgementAdapter)
    with pytest.raises(NotImplementedError, match="M3"):
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
