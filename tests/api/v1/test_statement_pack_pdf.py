"""Tests for GET /api/v1/reports/statement_pack.pdf.

Uses respx to mock latex-api — never hits the live service.

Tests:
* test_statement_pack_pdf_returns_pdf — end-to-end: seeded DB + respx mock →
  200 application/pdf
"""
from __future__ import annotations

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from saebooks.api.v1.auth import current_token
from saebooks.main import app

pytestmark = pytest.mark.postgres_only

_FAKE_PDF = b"%PDF-1.5 fake pdf"
_FAKE_PDF_URL = "/files/test-statement.pdf"
_LATEX_API_BASE = "http://latex-api:8000"


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_statement_pack_pdf_returns_pdf(
    api_client: AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """GET /api/v1/reports/statement_pack.pdf → 200 application/pdf.

    latex-api is mocked with respx; the endpoint assembles context from
    the test DB (seeded company + empty GL) and renders via render_latex.
    """
    import os

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE

    # Invalidate the cached Jinja env so it picks up the test LATEX_API_URL.
    import saebooks.services.latex_pdf as _svc

    _svc._env = None

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(200, json={"status": "ok", "pdf_url": _FAKE_PDF_URL, "id": "t1"})
    )
    respx_mock.get(f"{_LATEX_API_BASE}{_FAKE_PDF_URL}").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    resp = await api_client.get(
        "/api/v1/reports/statement_pack.pdf",
        params={"from_date": "2020-07-01", "to_date": "2021-06-30", "comparative": "false"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _FAKE_PDF


@pytest.mark.asyncio
async def test_statement_pack_pdf_compile_error_returns_502(
    api_client: AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    """When latex-api returns 422, the endpoint returns HTTP 502."""
    import os

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE

    import saebooks.services.latex_pdf as _svc

    _svc._env = None

    log_tail = "! Undefined control sequence."
    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(422, json={"detail": log_tail})
    )

    resp = await api_client.get(
        "/api/v1/reports/statement_pack.pdf",
        params={"from_date": "2020-07-01", "to_date": "2021-06-30", "comparative": "false"},
    )

    assert resp.status_code == 502, resp.text
    assert "compile error" in resp.text.lower() or "latex" in resp.text.lower()
