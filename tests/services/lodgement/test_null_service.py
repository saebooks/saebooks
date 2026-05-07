"""NullLodgementService refuses every method."""
from __future__ import annotations

import pytest

from saebooks.services.lodgement import (
    LodgementUnsupportedEdition,
    NullLodgementService,
)


@pytest.fixture
def svc() -> NullLodgementService:
    return NullLodgementService()


async def test_lodge_stp_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition, match="ato_sbr"):
        await svc.lodge_stp(b"x", "p1", {})


async def test_lodge_bas_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition):
        await svc.lodge_bas(b"x", "2026-Q3", {})


async def test_lodge_tpar_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition):
        await svc.lodge_tpar(b"x", "FY2026", {})


async def test_send_superstream_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition):
        await svc.send_superstream(b"x", "msg-1", {})


async def test_lookup_abr_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition):
        await svc.lookup_abr("12345678901")


async def test_my_audit_log_refuses(svc: NullLodgementService) -> None:
    with pytest.raises(LodgementUnsupportedEdition):
        await svc.my_audit_log()


def test_message_names_required_edition() -> None:
    """The error message must name the edition so the UI can prompt for an upgrade."""
    err = LodgementUnsupportedEdition(required_edition="pro", flag="ato_sbr")
    assert "Pro" in str(err)
    assert "ato_sbr" in str(err)
    assert err.required_edition == "pro"
    assert err.flag == "ato_sbr"
