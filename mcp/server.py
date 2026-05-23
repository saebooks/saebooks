"""Standalone MCP server entry point — shim that delegates to
``saebooks.mcp.server``.

The canonical FastMCP instance and all 145 tool registrations live in
the ``saebooks.mcp.server`` module so the same code can run as either:

  - a separate container (this entry point — useful for old-style
    deployments + air-gapped operator workstations that can't reach
    the API directly)
  - an in-process ASGI mount at ``/mcp`` on the saebooks API
    (preferred; see ``saebooks/main.py``)

This file exists so the existing ``mcp/`` compose stack and the
``mcp:0.x`` images keep working. New deployments should use the
in-tree mount and skip this container entirely.
"""
from __future__ import annotations

from saebooks.mcp.server import main

if __name__ == "__main__":
    main()
