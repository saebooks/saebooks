"""Guardrails that keep the MCP surface from making the manual-journal-entry
shortcut easy.

Background: the handover hard rule + ``feedback_no-manual-journal-entries`` say
Claude must never author a manual JE as a shortcut — the engine derives the
ledger from real records, and a record-type tool carries provenance + a real
audit trail while a hand-written JE does not. The MCP is the tool surface
Claude uses, so the steer has to live in-band here, not only in memory.

These tests pin three things:

  1. ``create_journal_entry`` requires an explicit ``reason`` and its
     description frames it as the exception path / bad practice.
  2. The common record-type tools exist (coverage) and each tells the model to
     use it instead of a manual JE.
  3. The server-level instructions carry the golden rule.

They introspect the live FastMCP registry rather than importing the functions
directly, so they exercise exactly what an MCP client sees.
"""
from __future__ import annotations

import pytest


def _tools() -> dict:
    from saebooks.mcp.server import mcp

    tm = getattr(mcp, "_tool_manager", None)
    assert tm is not None, "FastMCP missing _tool_manager"
    return getattr(tm, "_tools", None) or {}


def test_manual_je_requires_reason() -> None:
    """The manual JE tool must demand an explicit ``reason`` argument so the
    exception is self-documenting and can't be invoked as a silent shortcut.
    """
    tool = _tools().get("create_journal_entry")
    assert tool is not None, "create_journal_entry tool missing"
    required = set(tool.parameters.get("required", []))
    assert "reason" in required, (
        "create_journal_entry must require a 'reason' argument — got "
        f"required={sorted(required)}"
    )


def test_manual_je_description_frames_it_as_exception() -> None:
    """The description Claude reads must steer toward record types and flag a
    manual JE as the exception path / weaker practice.
    """
    tool = _tools().get("create_journal_entry")
    assert tool is not None
    desc = (tool.description or "").lower()
    assert "exception" in desc, "manual JE description must call itself the exception path"
    # It must point at the proper record types.
    for word in ("invoice", "bill", "expense", "payment"):
        assert word in desc, f"manual JE description should redirect to {word}"
    # It must label manual JEs as poor practice / mention the manual stamp.
    assert "manual" in desc
    assert "audit" in desc or "practice" in desc


@pytest.mark.parametrize(
    "name",
    [
        "create_invoice",
        "create_credit_note",
        "create_bill",
        "create_expense",
        "create_payment",
    ],
)
def test_record_type_tools_exist(name: str) -> None:
    """Every common economic event must have a first-class record-type tool
    so there is always a non-manual path."""
    assert name in _tools(), f"missing record-type tool: {name}"


@pytest.mark.parametrize(
    "name",
    [
        "create_invoice",
        "create_credit_note",
        "create_bill",
        "create_expense",
        "create_payment",
    ],
)
def test_record_type_tools_steer_away_from_manual_je(name: str) -> None:
    """Each record-type tool's description tells the model to use it instead
    of a manual journal entry."""
    tool = _tools().get(name)
    assert tool is not None, f"missing tool {name}"
    desc = (tool.description or "").lower()
    assert "not a manual journal entry" in desc, (
        f"{name} description should explicitly steer away from manual JEs; "
        f"got: {desc!r}"
    )


def test_server_instructions_carry_golden_rule() -> None:
    """The top-level server instructions (what every client sees on connect)
    must carry the no-manual-JE golden rule and name the record types."""
    from saebooks.mcp.server import mcp

    instr = (mcp.instructions or "").lower()
    assert "never author a manual journal entry" in instr
    assert "exception path" in instr
    assert "derived" in instr
    for word in ("create_invoice", "create_bill", "create_payment"):
        assert word in instr, f"instructions should name {word}"
