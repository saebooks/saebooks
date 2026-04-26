"""Tests for RFC 7807 Problem Details error contract.

Verifies that:
1. A 404 on an unknown route returns ``application/problem+json``
   when ``Accept: application/json`` is set.
2. Without Accept header the default FastAPI JSON format is returned.
3. The ``code``, ``type``, ``title``, and ``status`` fields are present.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_unknown_route_404_problem_json(client: AsyncClient) -> None:
    """GET an unknown /api/v1/ path with Accept: application/json -> problem+json 404."""
    resp = await client.get(
        "/api/v1/this-route-does-not-exist-at-all",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 404
    ct = resp.headers.get("content-type", "")
    assert "application/problem+json" in ct, (
        f"Expected application/problem+json content-type, got {ct!r}"
    )
    body = resp.json()
    assert body["status"] == 404, f"Expected status 404 in body, got {body}"
    assert "code" in body, f"Missing 'code' field in problem+json: {body}"
    assert "type" in body, f"Missing 'type' URI in problem+json: {body}"
    assert "title" in body, f"Missing 'title' field in problem+json: {body}"
    # type must start with our base URI
    assert body["type"].startswith("https://saebooks.io/problems/"), (
        f"'type' URI must start with https://saebooks.io/problems/, got {body['type']!r}"
    )


@pytest.mark.anyio
async def test_unknown_route_404_without_accept_header(client: AsyncClient) -> None:
    """GET an unknown path without Accept header -> plain JSON (backwards compat)."""
    resp = await client.get("/api/v1/this-route-does-not-exist-at-all")
    assert resp.status_code == 404
    # Must still return JSON (the default FastAPI shape), just not problem+json.
    body = resp.json()
    assert "detail" in body


@pytest.mark.anyio
async def test_problem_json_with_star_accept(client: AsyncClient) -> None:
    """Accept: */* also triggers problem+json (callers like curl)."""
    resp = await client.get(
        "/api/v1/nonexistent",
        headers={"Accept": "*/*"},
    )
    assert resp.status_code == 404
    ct = resp.headers.get("content-type", "")
    assert "application/problem+json" in ct
