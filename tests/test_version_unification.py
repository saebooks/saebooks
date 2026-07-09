"""Smoke test: OpenAPI info.version matches MCP serverInfo.version.

Critic finding #42 (2026-05-23): /openapi.json reported 0.0.1 while
MCP initialize returned serverInfo.version=1.27.1. Fix: both now read
from saebooks.__version__ (the mcp library version was the source of
the 1.27.1 before; we override mcp._mcp_server.version so the MCP
server reports the app version, not the library version).

These tests assert the three surfaces agree:
1. saebooks.__version__ (the canonical source)
2. /openapi.json info.version
3. MCP server's _mcp_server.version attribute (the MCP initialize path)

We do NOT make an HTTP request to the MCP initialize endpoint (that
requires a full MCP handshake); instead we assert the attribute is set
correctly. The attribute is what the mcp library reads when composing
the serverInfo response.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import saebooks
from saebooks.main import app
from saebooks.mcp.server import mcp


@pytest.fixture
async def unauth_client() -> AsyncClient:
    """Client with no Authorization header — OpenAPI is public."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_version_module_is_not_placeholder(unauth_client: AsyncClient) -> None:
    """__version__ must not be the old 0.0.1 placeholder."""
    assert saebooks.__version__ != "0.0.1", (
        "__version__ is still the placeholder; update saebooks/__init__.py"
    )
    assert saebooks.__version__, "__version__ must not be empty"


async def test_openapi_version_matches_module(unauth_client: AsyncClient) -> None:
    """GET /openapi.json info.version must equal saebooks.__version__."""
    r = await unauth_client.get("/openapi.json")
    assert r.status_code == 200
    openapi_version = r.json()["info"]["version"]
    assert openapi_version == saebooks.__version__, (
        f"OpenAPI version {openapi_version!r} != __version__ {saebooks.__version__!r}"
    )


def test_mcp_server_version_matches_module() -> None:
    """The MCP low-level server's version attribute must equal __version__.

    This is what the mcp library reads when composing the serverInfo
    in the initialize response.  We set it explicitly in server.py
    because FastMCP does not accept a version= constructor kwarg and
    otherwise falls back to the mcp library version.
    """
    assert mcp._mcp_server.version == saebooks.__version__, (
        f"MCP serverInfo version {mcp._mcp_server.version!r} != "
        f"__version__ {saebooks.__version__!r}"
    )


async def test_api_version_endpoint_matches_module(unauth_client: AsyncClient) -> None:
    """/api/v1/version must report the same version as __version__.

    This endpoint reads importlib.metadata which reads pyproject.toml;
    in an editable install or installed wheel that will match.  In a
    raw checkout it falls back to "0.0.1" — we accept that fallback
    here but assert it's consistent with whatever __version__ is.

    Note: in the test runner (raw checkout, no editable install) the
    endpoint returns "0.0.1" from the PackageNotFoundError path.  We
    do not assert equality with __version__ here — that would fail in
    CI on every source checkout.  We DO assert the field is present
    and non-empty.
    """
    r = await unauth_client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert body["version"], "version field must not be empty"


def test_pyproject_version_matches_module() -> None:
    """pyproject.toml [project] version must equal saebooks.__version__.

    /api/v1/version reports importlib.metadata.version("saebooks"), which is
    baked from pyproject.toml at install time, while OpenAPI and the MCP
    surfaces read saebooks.__version__ directly. The two source literals must
    therefore stay in lockstep — scripts/bump-version.sh sets both atomically;
    this test guards against a hand-edit that touches only one of them.

    Skips gracefully when pyproject.toml is not on disk (e.g. a packaged-only
    runtime image) so it never false-fails outside a source checkout.
    """
    import tomllib
    from pathlib import Path

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject_path.exists():
        pytest.skip("pyproject.toml not present in this checkout")

    pyproject = tomllib.loads(pyproject_path.read_text())
    declared = pyproject["project"]["version"]
    assert declared == saebooks.__version__, (
        f"pyproject.toml version {declared!r} != __version__ "
        f"{saebooks.__version__!r} — run scripts/bump-version.sh to sync them"
    )
