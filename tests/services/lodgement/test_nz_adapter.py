"""NZ lodgement adapter — fail-loud gate, target validation, no sockets.

(The registry-level dispatch test file, test_adapter_registry.py, is
--ignore'd by the docker harness — these adapter-direct tests are the
ones that run in CI.)
"""
from __future__ import annotations

import pytest

from saebooks.jurisdictions.nz.lodgement import (
    KNOWN_TARGETS,
    IrGatewayConfig,
    NZLodgementAdapter,
)
from saebooks.services.lodgement.exceptions import (
    LodgementError,
    NZLiveCredentialsMissing,
    NZLodgementError,
)


def test_exception_family_is_lodgement_error() -> None:
    # ``except LodgementError`` must mop up NZ failures too (the EE
    # family's contract).
    assert issubclass(NZLiveCredentialsMissing, NZLodgementError)
    assert issubclass(NZLodgementError, LodgementError)


def test_known_targets_shape() -> None:
    assert {
        "gst101", "employment_information", "ir3", "ir4", "ir6", "ir7",
    } == KNOWN_TARGETS


@pytest.mark.parametrize("target", sorted(KNOWN_TARGETS))
async def test_lodge_gates_loudly_before_any_socket(target: str) -> None:
    # No IR gateway credentials — every valid target refuses with the
    # typed live gate, zero network egress. (Explicit empty config so
    # stray NZ_IRD_* env vars can never flip this test.)
    adapter = NZLodgementAdapter(IrGatewayConfig())
    with pytest.raises(NZLiveCredentialsMissing):
        await adapter.lodge(target, b"<payload/>", "idem-1", {})


async def test_lodge_unknown_target_is_a_caller_bug() -> None:
    adapter = NZLodgementAdapter()
    with pytest.raises(ValueError, match="bogus"):
        await adapter.lodge("bogus", b"", "idem-1", {})


async def test_lodge_stale_ir348_points_at_payday_filing() -> None:
    # The old stub advertised ir348 (Employer Monthly Schedule) — stale;
    # payday EI replaced it. The error must say so.
    adapter = NZLodgementAdapter()
    with pytest.raises(ValueError, match="employment_information"):
        await adapter.lodge("ir348", b"", "idem-1", {})


async def test_lodge_complete_config_still_refuses_no_transport() -> None:
    # A configured-but-unbuilt transport must not masquerade as a
    # credential problem: the SOAP gateway client is a later phase.
    adapter = NZLodgementAdapter(
        IrGatewayConfig(
            client_cert_path="/nonexistent/cert.pem",
            client_key_path="/nonexistent/key.pem",
            gateway_base_url="https://gateway.example.invalid",
            oauth_client_id="client-id",
        )
    )
    with pytest.raises(NotImplementedError, match="later phase"):
        await adapter.lodge("gst101", b"<payload/>", "idem-1", {})


async def test_lookup_nzbn_validates_format_before_gating() -> None:
    adapter = NZLodgementAdapter()
    with pytest.raises(ValueError, match="structurally valid NZBN"):
        await adapter.lookup_nzbn("not-an-nzbn")


async def test_lookup_nzbn_gates_on_missing_mbie_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MBIE_NZBN_API_KEY", raising=False)
    adapter = NZLodgementAdapter()
    with pytest.raises(NZLiveCredentialsMissing, match="MBIE_NZBN_API_KEY"):
        await adapter.lookup_nzbn("9429041234563")


async def test_lookup_nzbn_with_key_refuses_no_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MBIE_NZBN_API_KEY", "test-key")
    adapter = NZLodgementAdapter()
    with pytest.raises(NotImplementedError, match="later phase"):
        await adapter.lookup_nzbn("9429041234563")
