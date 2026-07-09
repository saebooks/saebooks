"""Delegation facades for the platform / identity module env-flag (#32 wave 1).

When ``settings.platform_base_url`` is set, the engine's billing-webhook and
signup / magic-link route handlers route their work here. Everything is a
route-level proxy (the capture ``mirror_post`` pattern): the engine forwards
the request to the module and returns a ``JSONResponse`` that mirrors the
module's status code and JSON body verbatim, so the API client sees the
identical response it would from the in-process path. The module owns ALL the
logic in delegated mode (validation, DB writes, promo mint, email dispatch,
Stripe-signature verification); the engine handler is a pure pass-through once
its edge dependencies (rate-limit for signup, nothing for the webhook) have
run.

Two shapes, matching how each moved flow carries its request:

* ``mirror_post_json`` — for the signup / magic-link routes, whose entire
  request is captured by a pydantic body model. The engine re-serialises the
  validated model to JSON and forwards it; the module re-validates and does the
  work. (Re-serialising the model, rather than forwarding the raw request,
  means the engine handler needs no ``Request`` parameter and its signature is
  unchanged.)
* ``mirror_post_raw`` — for the Stripe webhook, whose signature is computed
  over the exact request bytes. The engine forwards the raw body plus the
  ``Stripe-Signature`` header; the module re-verifies the signature in-process.

Imports lazily to avoid an import cycle (the moved route modules import THIS
module at load time).
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from saebooks.services import platform_client as _client

# Response headers worth preserving when mirroring a module response back to
# the original API client. ``Retry-After`` matters for any rate-limit 429 the
# module could emit; ``WWW-Authenticate`` for auth challenges.
_FORWARD_RESPONSE_HEADERS = ("retry-after", "www-authenticate")


def _mirror(resp: Any, path: str) -> JSONResponse:
    """Turn a module ``httpx.Response`` into a mirrored ``JSONResponse``."""
    body = _client.json_body(resp, path)
    headers = {
        k: resp.headers[k]
        for k in _FORWARD_RESPONSE_HEADERS
        if k in resp.headers
    }
    return JSONResponse(
        content=body, status_code=resp.status_code, headers=headers or None
    )


async def mirror_post_json(
    path: str,
    payload: dict[str, Any],
    *,
    forward_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Forward a JSON body to the module and mirror the module response.

    ``forward_headers`` carries any per-request authenticity header the moved
    flow verifies itself inside the module — e.g. the OAuth handoff's
    ``X-OAuth-Handoff-Secret`` (the module re-checks it against its own env, so
    the engine forwards the caller's presented value verbatim). None-valued
    entries are dropped by the client so a missing header stays ABSENT and the
    module's own presence check returns the identical error the in-process path
    would.
    """
    resp = await _client.post_json(path, payload, extra_headers=forward_headers)
    return _mirror(resp, path)


async def mirror_post_raw(
    path: str,
    raw_body: bytes,
    *,
    content_type: str = "application/json",
    forward_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Forward a raw request body (+ selected headers) verbatim and mirror the
    module response. Used for the Stripe webhook."""
    resp = await _client.post_raw(
        path,
        raw_body,
        content_type=content_type,
        extra_headers=forward_headers,
    )
    return _mirror(resp, path)
