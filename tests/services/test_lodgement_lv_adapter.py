"""LV lodgement adapter — the fail-loud gate (zero network egress).

Mirrors the NZ/UK adapter gate tests: route validation is real, every
network-needing call refuses BEFORE any socket, and a configured-but-
unbuilt transport is distinguishable from a credential problem.
"""
from __future__ import annotations

import pytest

from saebooks.services.lodgement.adapters.lv import (
    KNOWN_TARGETS,
    EdsConfig,
    LVLodgementAdapter,
)
from saebooks.services.lodgement.exceptions import (
    LodgementError,
    LVLiveCredentialsMissing,
    LVLodgementError,
)


def test_lv_exception_family_shape() -> None:
    assert issubclass(LVLodgementError, LodgementError)
    assert issubclass(LVLiveCredentialsMissing, LVLodgementError)


def test_known_targets_are_the_three_company_filings() -> None:
    assert {"pvn", "employer_report", "uin"} == KNOWN_TARGETS


@pytest.mark.parametrize("route", sorted({"pvn", "employer_report", "uin"}))
async def test_lodge_refuses_loudly_before_any_socket(route: str) -> None:
    adapter = LVLodgementAdapter(config=EdsConfig())  # nothing provisioned
    with pytest.raises(LVLiveCredentialsMissing, match="Refusing to open a connection"):
        await adapter.lodge(route, b"<payload/>", "idem-1", {})


async def test_unknown_target_is_a_caller_bug_not_a_credential_problem() -> None:
    adapter = LVLodgementAdapter(config=EdsConfig())
    with pytest.raises(ValueError, match="does not support lodge target"):
        await adapter.lodge("kmd", b"", "idem-2", {})  # an EE route, not LV


async def test_annual_income_return_rejected_with_guidance() -> None:
    adapter = LVLodgementAdapter(config=EdsConfig())
    with pytest.raises(ValueError, match="gada ienākumu deklarācija"):
        await adapter.lodge("annual_income_return", b"", "idem-3", {})


async def test_configured_but_unbuilt_transport_is_not_a_credential_error() -> None:
    adapter = LVLodgementAdapter(
        config=EdsConfig(
            base_url="https://eds.vid.gov.lv/api",
            client_id="client",
            client_secret_path="/nonexistent/secret",
        )
    )
    with pytest.raises(NotImplementedError, match="later phase"):
        await adapter.lodge("pvn", b"<payload/>", "idem-4", {})
