"""Regression: the gRPC sidecar must honour SAEBOOKS_BIND_HOST.

The one-click server sets ``SAEBOOKS_BIND_HOST=127.0.0.1`` so every listener
stays on loopback. The gRPC server historically hardcoded ``[::]`` and so was
externally reachable on bare-metal / one-click installs (a security
regression — verified: a TCP connect from outside the VM succeeded). These
tests pin the contract: ``serve()`` binds ``{settings.bind_host}:{port}`` and
never falls back to an all-interfaces literal.
"""
from __future__ import annotations

import pytest

from saebooks import grpc_server
from saebooks.config import settings as _settings


class _FakeServer:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def add_insecure_port(self, addr: str) -> int:
        self.bound.append(addr)
        return 0

    async def start(self) -> None:
        return None


@pytest.mark.parametrize(
    ("bind_host", "port"),
    [("127.0.0.1", 18962), ("0.0.0.0", 50051)],
)
async def test_serve_binds_configured_host(
    monkeypatch: pytest.MonkeyPatch, bind_host: str, port: int
) -> None:
    fake = _FakeServer()
    monkeypatch.setattr(_settings, "bind_host", bind_host, raising=False)
    monkeypatch.setattr(grpc_server.aio, "server", lambda *a, **k: fake)
    monkeypatch.setattr(
        grpc_server.saebooks_pb2_grpc,
        "add_SAEBooksServicer_to_server",
        lambda *a, **k: None,
    )

    await grpc_server.serve(port=port)

    assert fake.bound == [f"{bind_host}:{port}"]
    # Never the historical all-interfaces literal when a host is configured.
    assert not any(a.startswith("[::]") for a in fake.bound)


async def test_serve_explicit_host_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeServer()
    monkeypatch.setattr(_settings, "bind_host", "0.0.0.0", raising=False)
    monkeypatch.setattr(grpc_server.aio, "server", lambda *a, **k: fake)
    monkeypatch.setattr(
        grpc_server.saebooks_pb2_grpc,
        "add_SAEBooksServicer_to_server",
        lambda *a, **k: None,
    )

    await grpc_server.serve(port=18962, host="127.0.0.1")

    assert fake.bound == ["127.0.0.1:18962"]
