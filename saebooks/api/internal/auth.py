"""Auth dependency for the internal module-to-engine API surface (#32).

The ``/internal/*`` endpoints in this package are called by sibling
*module* containers (e.g. the pre-accounting module) over the private
docker network — never by a browser through the public edge. They carry
real side effects, so they are gated by a shared secret presented as the
``X-Internal-Token`` header and compared in constant time against
``settings.internal_api_token`` (env ``INTERNAL_API_TOKEN``).

Fail-closed contract
--------------------
* env token EMPTY  → 503 in EVERY environment (including dev/test). These
  endpoints are not safe to run ungated — e.g. numbering burns a sequence
  value — so an unconfigured instance must refuse rather than open up.
* header MISSING / MISMATCH → 401.

This is deliberately stricter than the ephemeral-demo ``_guard`` (which is
dev-open when its secret is unset): a demo provision is idempotent-ish and
already unreachable from the edge, whereas an internal fact/numbering call
mutates durable state.
"""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from saebooks.config import settings


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Gate an internal endpoint on the ``X-Internal-Token`` shared secret.

    Raises 503 when no token is configured (fail-closed), 401 when the
    presented header is missing or does not match.
    """
    expected = settings.internal_api_token.strip()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "internal API disabled: INTERNAL_API_TOKEN is not configured",
        )
    presented = (x_internal_token or "").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing X-Internal-Token",
        )
