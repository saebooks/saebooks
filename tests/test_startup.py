"""Startup smoke tests — verify REST + gRPC both start inside the FastAPI lifespan.

The gRPC integration test is skipped unless SAEBOOKS_INTEGRATION_TESTS=1 is set,
because it binds a real port and requires no conflicting server on 50051.
"""
from __future__ import annotations

import os

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# REST — OpenAPI schema reachable (uses shared conftest client fixture)
# ---------------------------------------------------------------------------


async def test_openapi_json_returns_200(client: AsyncClient) -> None:
    """Prove REST API is functional after wiring in the gRPC lifespan."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert "openapi" in data
    assert data.get("info", {}).get("title") == "SAE Books"


# ---------------------------------------------------------------------------
# gRPC lifespan module — importable + serve() is callable
# ---------------------------------------------------------------------------


async def test_grpc_server_importable() -> None:
    """grpc_server module and serve() callable exist — no port binding needed."""
    from saebooks.grpc_server import SAEBooksServicer, serve

    assert callable(serve)
    assert SAEBooksServicer is not None


# ---------------------------------------------------------------------------
# gRPC Heartbeat — integration only (real port bind, skip on normal pytest run)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("SAEBOOKS_INTEGRATION_TESTS"),
    reason="Binds real gRPC port — set SAEBOOKS_INTEGRATION_TESTS=1 to run",
)
async def test_grpc_heartbeat_live() -> None:
    """Open a real gRPC channel to a live server and call Heartbeat."""
    from grpc import aio
    from saebooks.grpc_gen import saebooks_pb2, saebooks_pb2_grpc

    from saebooks.grpc_server import serve

    port = 50098  # non-default to avoid clash with dev server
    server = await serve(port)
    try:
        async with aio.insecure_channel(f"localhost:{port}") as channel:
            stub = saebooks_pb2_grpc.SAEBooksStub(channel)
            response = await stub.Heartbeat(saebooks_pb2.HeartbeatRequest())
        assert response.status == "ok"
    finally:
        await server.stop(grace=2)
