"""Tests for saebooks.services.latex_pdf — the render-service client.

Presentation (Jinja templates + the latex_escape filter + the XeLaTeX client)
moved out of the engine into the app render service (#31/#32). This module is
now a thin HTTP client, so the tests assert the client contract:

* the request shape — POST {RENDER_SERVICE_URL}/internal/render/{template},
  ctx serialised as the JSON body, X-Render-Token sent only when configured;
* the response mapping — 200 → bytes, 422 → LatexCompileError(log_tail),
  connection error / timeout / other status → LatexServiceError.

latex-api is gone; there is no /compile round-trip and no Jinja env to reset.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from saebooks.services.latex_pdf import (
    LatexCompileError,
    LatexServiceError,
    render_latex,
)

# The default RENDER_SERVICE_URL (no env override in the test harness).
_RENDER_BASE = "http://web:8080"
_FAKE_PDF = b"%PDF-1.5 fake pdf content"


@pytest.fixture(autouse=True)
def _clear_render_token(monkeypatch: pytest.MonkeyPatch):
    """Default to no token so header assertions start from a clean slate."""
    monkeypatch.setattr("saebooks.config.settings.render_service_token", "", raising=False)
    monkeypatch.setattr("saebooks.config.settings.render_service_url", _RENDER_BASE, raising=False)


# ---------------------------------------------------------------------------
# 200 → bytes, and request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_latex_success_returns_bytes(respx_mock: respx.MockRouter) -> None:
    """200 from the render service → the PDF bytes are returned verbatim."""
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    pdf = await render_latex("document", {"number": "4042", "total": "110.00"})

    assert pdf == _FAKE_PDF
    assert route.called
    # ctx is posted verbatim as the JSON body.
    posted = route.calls[0].request
    assert posted.headers["content-type"].startswith("application/json")
    import json as _json

    assert _json.loads(posted.content.decode()) == {"number": "4042", "total": "110.00"}


@pytest.mark.asyncio
async def test_render_latex_template_in_path(respx_mock: respx.MockRouter) -> None:
    """The template name is the final URL path segment."""
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/quote").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )
    await render_latex("quote", {"number": "1019"})
    assert route.called


@pytest.mark.asyncio
async def test_render_latex_token_header_sent_when_configured(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty RENDER_SERVICE_TOKEN is sent as X-Render-Token."""
    monkeypatch.setattr("saebooks.config.settings.render_service_token", "s3cr3t", raising=False)
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    await render_latex("document", {})

    assert route.calls[0].request.headers.get("x-render-token") == "s3cr3t"


@pytest.mark.asyncio
async def test_render_latex_token_header_absent_when_empty(
    respx_mock: respx.MockRouter
) -> None:
    """An empty token means the X-Render-Token header is not sent at all."""
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    await render_latex("document", {})

    assert "x-render-token" not in route.calls[0].request.headers


# ---------------------------------------------------------------------------
# 422 → LatexCompileError(log_tail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_latex_compile_error_uses_log_tail(
    respx_mock: respx.MockRouter
) -> None:
    """422 → LatexCompileError; log_tail comes from the JSON 'log_tail' key."""
    log_tail = "! Undefined control sequence.\nl.5 \\badcommand"
    respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(422, json={"log_tail": log_tail, "detail": "nope"})
    )

    with pytest.raises(LatexCompileError) as exc_info:
        await render_latex("document", {})

    assert exc_info.value.log_tail == log_tail


@pytest.mark.asyncio
async def test_render_latex_compile_error_falls_back_to_detail(
    respx_mock: respx.MockRouter
) -> None:
    """422 without log_tail → LatexCompileError falls back to the 'detail' key."""
    respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(422, json={"detail": "compile blew up"})
    )

    with pytest.raises(LatexCompileError) as exc_info:
        await render_latex("document", {})

    assert exc_info.value.log_tail == "compile blew up"


# ---------------------------------------------------------------------------
# connection error / timeout / other status → LatexServiceError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_latex_connection_error(respx_mock: respx.MockRouter) -> None:
    """A transport-level connect failure → LatexServiceError (not raw httpx)."""
    respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(LatexServiceError):
        await render_latex("document", {})


@pytest.mark.asyncio
async def test_render_latex_timeout(respx_mock: respx.MockRouter) -> None:
    """A timeout → LatexServiceError."""
    respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        side_effect=httpx.ReadTimeout("too slow")
    )

    with pytest.raises(LatexServiceError):
        await render_latex("document", {})


@pytest.mark.asyncio
async def test_render_latex_unexpected_status(respx_mock: respx.MockRouter) -> None:
    """Any non-200/422 status → LatexServiceError."""
    respx_mock.post(f"{_RENDER_BASE}/internal/render/document").mock(
        return_value=Response(500, text="internal error")
    )

    with pytest.raises(LatexServiceError):
        await render_latex("document", {})
