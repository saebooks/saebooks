"""D1 — unhandled 5xx errors are normalised to RFC 7807 problem+json.

``saebooks/api/errors.py::register_handlers`` historically installed
handlers ONLY for ``HTTPException`` + ``RequestValidationError``. An
unhandled exception therefore fell through to Starlette's default
``{"detail": "Internal Server Error"}`` body — violating the documented
"all non-2xx JSON responses normalised to problem+json" contract for the
5xx surface.

These tests build a throwaway FastAPI app with a route that raises a bare
``RuntimeError``, register the production handlers on it, and assert the
500 response is ``application/problem+json`` with ``code=internal_error``
— NOT Starlette's default ``{"detail": ...}``. The throwaway route is
mounted only on the test app and never ships in the real application.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.api.errors import register_handlers

pytestmark = pytest.mark.asyncio


def _app_with_boom() -> FastAPI:
    """Throwaway app: one route that raises, production handlers attached."""
    app = FastAPI()

    @app.get("/_test_boom")
    async def _boom() -> dict:
        raise RuntimeError("intentional test-only explosion")

    register_handlers(app)
    return app


async def test_unhandled_5xx_is_problem_json() -> None:
    """A bare RuntimeError → 500 application/problem+json, code=internal_error."""
    app = _app_with_boom()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/_test_boom", headers={"Accept": "application/json"})

    assert r.status_code == 500, r.text
    assert r.headers["content-type"].startswith("application/problem+json"), (
        f"Expected application/problem+json, got {r.headers.get('content-type')!r}"
    )
    body = r.json()
    assert body.get("code") == "internal_error", body
    assert body.get("status") == 500, body
    # RFC 7807 mandatory fields present.
    assert "type" in body and "title" in body, body
    # The traceback / exception message must NOT leak into the body.
    assert "intentional test-only explosion" not in r.text, (
        "Internal exception detail leaked into the problem+json body"
    )
    # Must NOT be Starlette's default shape.
    assert body.get("detail") != "Internal Server Error", (
        "Got Starlette's default 500 body, not problem+json"
    )


async def test_unhandled_5xx_html_caller_unaffected() -> None:
    """A non-JSON (HTML) caller does NOT get problem+json — content negotiation.

    The 5xx handler is gated on ``_wants_json()`` like the existing
    HTTPException / validation handlers, so browser callers keep the
    default Starlette behaviour.
    """
    app = _app_with_boom()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/_test_boom", headers={"Accept": "text/html"})

    assert r.status_code == 500, r.text
    # Browser path: NOT problem+json.
    assert not r.headers["content-type"].startswith("application/problem+json"), (
        "HTML caller should not receive problem+json on 5xx"
    )
