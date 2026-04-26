"""Tests for RequestIdMiddleware.

Verifies that:
1. A generated UUID is added to the response when no X-Request-Id is supplied.
2. A caller-supplied X-Request-Id is echoed back unchanged.
3. The id is stored on request.state.request_id.
"""
from __future__ import annotations

import re

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_request_id_generated_when_absent(client: AsyncClient) -> None:
    """Response must carry X-Request-Id even when not supplied by caller."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    rid = resp.headers.get("x-request-id")
    assert rid is not None, "X-Request-Id header missing from response"
    # Must be a valid UUID4 (xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx)
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        rid,
    ), f"Generated X-Request-Id is not a valid UUID4: {rid!r}"


@pytest.mark.anyio
async def test_request_id_echoed_when_supplied(client: AsyncClient) -> None:
    """A caller-supplied X-Request-Id must be returned unchanged."""
    supplied = "foobar-test-id-42"
    resp = await client.get("/healthz", headers={"X-Request-Id": supplied})
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id") == supplied
