"""MCP wiring for the Transfer + Intercompany record types.

Two record types (Transfer, Intercompany) now exist in the engine but had NO
MCP tool. These tests pin the freshly-wired tools the same way
``test_mcp_no_manual_je.py`` pins the rest of the surface: by introspecting the
live FastMCP registry (what an MCP client actually sees) rather than importing
the functions directly.

They assert three things:

  1. The tools are REGISTERED in the FastMCP registry with the right required
     arguments.
  2. Their descriptions steer the model toward them (and away from a manual JE)
     and name the origin they produce.
  3. Invoking the tool's underlying function POSTs to the correct REST endpoint
     — ``/api/v1/transfers`` (-> origin=TRANSFER) and ``/api/v1/intercompany``
     (-> origin=INTERCOMPANY) — with the caller's arguments forwarded verbatim.
     The endpoint is what determines the engine origin, so pinning the path is
     the unit-level proof that each tool produces the right provenance.
"""
from __future__ import annotations

from typing import Any

import pytest


def _tools() -> dict:
    from saebooks.mcp.server import mcp

    tm = getattr(mcp, "_tool_manager", None)
    assert tm is not None, "FastMCP missing _tool_manager"
    return getattr(tm, "_tools", None) or {}


# --------------------------------------------------------------------------- #
# Registration + required-arg surface
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "create_transfer",
        "reverse_transfer",
        "list_transfers",
        "get_transfer",
        "intercompany_post",
        "reverse_intercompany",
        "list_intercompany",
        "get_intercompany",
    ],
)
def test_tool_registered(name: str) -> None:
    assert name in _tools(), f"MCP tool not registered: {name}"


def test_create_transfer_required_args() -> None:
    tool = _tools().get("create_transfer")
    assert tool is not None
    required = set(tool.parameters.get("required", []))
    for arg in ("from_account_id", "to_account_id", "amount", "transfer_date"):
        assert arg in required, (
            f"create_transfer must require {arg!r} — got {sorted(required)}"
        )


def test_intercompany_post_required_args() -> None:
    tool = _tools().get("intercompany_post")
    assert tool is not None
    required = set(tool.parameters.get("required", []))
    for arg in (
        "originator_company_id",
        "counterparty_company_id",
        "amount",
        "entry_date",
        "originator_contra_account_id",
        "counterparty_contra_account_id",
    ):
        assert arg in required, (
            f"intercompany_post must require {arg!r} — got {sorted(required)}"
        )


# --------------------------------------------------------------------------- #
# Descriptions steer correctly + name the origin
# --------------------------------------------------------------------------- #
def test_create_transfer_description_steers_and_names_origin() -> None:
    tool = _tools().get("create_transfer")
    assert tool is not None
    desc = (tool.description or "").lower()
    assert "not a manual journal entry" in desc, (
        "create_transfer must steer away from manual JEs"
    )
    assert "transfer" in desc
    # The brief: bank->bank, card paydown, director-loan repayment.
    assert "credit-card paydown" in desc or "card paydown" in desc
    assert "director-loan" in desc or "director loan" in desc
    assert "origin=transfer" in desc


def test_intercompany_post_description_steers_and_names_origin() -> None:
    tool = _tools().get("intercompany_post")
    assert tool is not None
    desc = (tool.description or "").lower()
    assert "not two hand-balanced manual jes" in desc, (
        "intercompany_post must steer away from two hand-balanced manual JEs"
    )
    assert "reciprocal" in desc
    assert "two of your companies" in desc or "two companies" in desc
    assert "origin=intercompany" in desc


# --------------------------------------------------------------------------- #
# Invoking the tool POSTs to the right endpoint (== right origin)
# --------------------------------------------------------------------------- #
class _Capture:
    """Records the (path, body) of the single _post the tool makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __call__(
        self, ctx: Any, path: str, body: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        self.calls.append((path, body))
        return {"ok": True, "echo_path": path}


@pytest.mark.asyncio
async def test_create_transfer_posts_to_transfers_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.mcp import server

    cap = _Capture()
    monkeypatch.setattr(server, "_post", cap)

    out = await server.create_transfer(
        None,
        from_account_id="11111111-1111-1111-1111-111111111111",
        to_account_id="22222222-2222-2222-2222-222222222222",
        amount=320.0,
        transfer_date="2026-06-06",
        description="CC paydown",
        reference="REF-1",
    )
    assert out == {"ok": True, "echo_path": "/api/v1/transfers"}
    assert len(cap.calls) == 1
    path, body = cap.calls[0]
    # The endpoint is what stamps origin=TRANSFER on the posted JE.
    assert path == "/api/v1/transfers"
    assert body is not None
    assert body["from_account_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["to_account_id"] == "22222222-2222-2222-2222-222222222222"
    assert body["amount"] == 320.0
    assert body["transfer_date"] == "2026-06-06"
    assert body["description"] == "CC paydown"
    assert body["reference"] == "REF-1"


@pytest.mark.asyncio
async def test_intercompany_post_posts_to_intercompany_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.mcp import server

    cap = _Capture()
    monkeypatch.setattr(server, "_post", cap)

    out = await server.intercompany_post(
        None,
        originator_company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        counterparty_company_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        amount=5000.0,
        entry_date="2026-06-06",
        originator_contra_account_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        counterparty_contra_account_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        description="Director funds SAE",
    )
    assert out == {"ok": True, "echo_path": "/api/v1/intercompany"}
    assert len(cap.calls) == 1
    path, body = cap.calls[0]
    # The endpoint is what stamps origin=INTERCOMPANY on both posted legs.
    assert path == "/api/v1/intercompany"
    assert body is not None
    assert body["originator_company_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert body["counterparty_company_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert body["amount"] == 5000.0
    assert body["entry_date"] == "2026-06-06"
    assert body["originator_contra_account_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert (
        body["counterparty_contra_account_id"]
        == "dddddddd-dddd-dddd-dddd-dddddddddddd"
    )
    assert body["description"] == "Director funds SAE"


@pytest.mark.asyncio
async def test_reverse_transfer_posts_to_reverse_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.mcp import server

    cap = _Capture()
    monkeypatch.setattr(server, "_post", cap)
    await server.reverse_transfer(None, transfer_id="ffffffff-ffff-ffff-ffff-ffffffffffff")
    path, _ = cap.calls[0]
    assert path == "/api/v1/transfers/ffffffff-ffff-ffff-ffff-ffffffffffff/reverse"


@pytest.mark.asyncio
async def test_reverse_intercompany_posts_to_reverse_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.mcp import server

    cap = _Capture()
    monkeypatch.setattr(server, "_post", cap)
    await server.reverse_intercompany(
        None, ic_txn_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    )
    path, _ = cap.calls[0]
    assert path == "/api/v1/intercompany/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee/reverse"
