"""X-Request-Id correlation middleware.

Ensures every inbound request has a ``X-Request-Id`` header and echoes
it back on every response, so log lines from different services can be
correlated by a single identifier.

Behaviour
---------
* If the caller supplies ``X-Request-Id``, that value is used as-is and
  echoed back — callers can set their own ids for end-to-end tracing.
* If the header is absent, a fresh ``uuid4()`` string is generated.
* The id is stored on ``request.state.request_id`` so downstream
  handlers and dependencies can read it without re-parsing headers.
* The id is added to every response as ``X-Request-Id``.
* A single ``saebooks.access`` log line is emitted per request at
  ``DEBUG`` level — format: ``<METHOD> <path> <status> req=<id>``.

The middleware is registered in ``saebooks.main.create_app`` before
the auth and metrics middleware so the id is available to everything
downstream.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_LOG = logging.getLogger("saebooks.access")

_HEADER = "x-request-id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach / propagate ``X-Request-Id`` on every request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Honour caller-supplied id; generate one if absent.
        request_id = request.headers.get(_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)

        response.headers["X-Request-Id"] = request_id
        _LOG.debug(
            "%s %s %s req=%s",
            request.method,
            request.url.path,
            response.status_code,
            request_id,
        )
        return response
