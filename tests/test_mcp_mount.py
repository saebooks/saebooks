"""Smoke tests for the in-tree MCP mount at /mcp.

Full MCP protocol roundtrip is exercised via curl in deploy
verification (see the saebooks-verify script and the README). These
tests stay light: they confirm the module loads, the FastMCP instance
holds the expected tool count, and the Starlette ASGI app is
constructible — which is enough to catch breakage from SDK upgrades
or registry refactors.
"""
from __future__ import annotations


def test_mcp_module_imports_with_tools() -> None:
    from saebooks.mcp.server import mcp

    tool_mgr = getattr(mcp, "_tool_manager", None)
    assert tool_mgr is not None, "FastMCP missing _tool_manager attribute"

    tools = getattr(tool_mgr, "_tools", None) or {}
    # The full safety cap registers ~145 tools as of v0.3. Bound loose
    # so adding/removing a handful doesn't fail the test, but a wholesale
    # registry break (0 tools, half the tools) will.
    assert len(tools) >= 100, f"expected >=100 tools registered, got {len(tools)}"


def test_streamable_http_asgi_app_is_starlette() -> None:
    from saebooks.mcp.server import streamable_http_asgi_app

    app = streamable_http_asgi_app()
    # Starlette is duck-typed — check the ASGI shape rather than the
    # concrete class so SDK refactors don't break us.
    assert callable(app), "asgi app must be callable (asgi3 signature)"
    assert hasattr(app, "routes"), "asgi app missing .routes attribute"


def test_mcp_path_resets_to_root() -> None:
    """Inner streamable_http_path must be ``/`` so mounting at /mcp
    on the FastAPI app gives the final external path ``/mcp/`` rather
    than ``/mcp/mcp/`` (the SDK default).
    """
    from saebooks.mcp.server import mcp, streamable_http_asgi_app

    streamable_http_asgi_app()
    assert mcp.settings.streamable_http_path == "/"
