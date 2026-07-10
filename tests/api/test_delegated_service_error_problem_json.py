"""Delegated-module failures normalise to RFC 7807 problem+json 503 (M2 wave
2a, P0a).

Mirrors ``test_errors_5xx_problem_json.py``'s throwaway-app pattern. Before
this wave, ``CaptureServiceError`` / ``PreAccountingServiceError`` /
``PlatformServiceError`` had no dedicated handler and fell through to the
generic 500 catch-all — a delegated transport failure looked identical to a
genuine internal-server bug. ``saebooks.api.errors.register_handlers`` now
also installs a handler on the shared ``DelegatedServiceError`` base
(Starlette matches by MRO, so one registration covers all three concrete
subclasses), mapping to a 503 that carries the failing ``module`` id.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from saebooks.api.errors import register_handlers
from saebooks.services.capture_client import CaptureServiceError
from saebooks.services.platform_client import PlatformServiceError
from saebooks.services.preaccounting_client import PreAccountingServiceError

pytestmark = pytest.mark.asyncio


def _app_raising(exc: Exception) -> FastAPI:
    app = FastAPI()

    @app.get("/_test_delegated_boom")
    async def _boom() -> dict:
        raise exc

    register_handlers(app)
    return app


@pytest.mark.parametrize(
    "exc_cls,module_id",
    [
        (CaptureServiceError, "capture"),
        (PreAccountingServiceError, "preaccounting"),
        (PlatformServiceError, "platform"),
    ],
)
async def test_delegated_service_error_is_503_problem_json(
    exc_cls: type[Exception], module_id: str
) -> None:
    app = _app_raising(exc_cls("simulated transport failure"))
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/_test_delegated_boom", headers={"Accept": "application/json"})

    assert r.status_code == 503, r.text
    assert r.headers["content-type"].startswith("application/problem+json"), r.headers
    body = r.json()
    assert body["code"] == "module_unavailable", body
    assert body["status"] == 503, body  # RFC 7807 mandatory field stays numeric
    assert body["module"] == module_id, body
    assert "type" in body and "title" in body, body
    # Internal exception text must not leak.
    assert "simulated transport failure" not in r.text


async def test_delegated_service_error_html_caller_unaffected() -> None:
    app = _app_raising(CaptureServiceError("boom"))
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/_test_delegated_boom", headers={"Accept": "text/html"})

    assert r.status_code == 503, r.text
    assert not r.headers["content-type"].startswith("application/problem+json")


async def test_delegated_service_error_beats_generic_catch_all() -> None:
    """A DelegatedServiceError must be resolved by the specific handler, not
    fall through to the ``Exception`` catch-all (which would report 500 +
    ``code=internal_error`` instead of 503 + ``module_unavailable``)."""
    app = _app_raising(PreAccountingServiceError("boom"))
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/_test_delegated_boom", headers={"Accept": "application/json"})

    body = r.json()
    assert r.status_code == 503
    assert body["code"] != "internal_error"
