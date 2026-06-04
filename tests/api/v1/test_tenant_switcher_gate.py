"""A3 — tenant-switcher regression guard.

``saebooks/api/v1/auth.py::resolve_tenant_id`` triple-gates the
``X-Active-Tenant`` override:

    header present  AND  FLAG_TENANT_SWITCHER enabled  AND  role >= ADMIN

``FLAG_TENANT_SWITCHER`` lives in ``_DEVELOPER_FLAGS`` ONLY (the developer
edition). On a NON-developer instance (the flag is off) the header must be
ignored entirely, so an admin for tenant A cannot pull tenant B's data by
setting the header. There was no test pinning this; this file adds one
(plus the companion "it DOES switch under developer edition").

These are unit tests of ``resolve_tenant_id`` against a synthetic
``Request`` — no DB, no HTTP stack — so they isolate the gating logic.
The edition is controlled via the ``saebooks.config.settings`` singleton
(which ``resolve_tenant_id`` reads through ``is_enabled(..., settings=_s)``).
"""
from __future__ import annotations

import uuid

import pytest
from starlette.requests import Request

from saebooks.api.v1 import auth as auth_mod
from saebooks.config import settings as _settings

pytestmark = pytest.mark.asyncio


_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _make_request(*, active_tenant: str | None, role: str | None, jwt_tenant: uuid.UUID) -> Request:
    """Build a synthetic Starlette Request with the given header + state.

    The JWT claim carries tenant A; the ``X-Active-Tenant`` header (if
    set) requests a switch to tenant B.
    """
    headers: list[tuple[bytes, bytes]] = []
    if active_tenant is not None:
        headers.append((b"x-active-tenant", active_tenant.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/anything",
        "headers": headers,
    }
    request = Request(scope)
    request.state.jwt_claims = {"tenant_id": str(jwt_tenant)}
    if role is not None:
        request.state.role = role
    return request


async def test_tenant_switch_ignored_on_non_developer_edition(monkeypatch) -> None:
    """Non-developer edition (flag off): X-Active-Tenant header is IGNORED.

    Admin authenticated for tenant A + header X-Active-Tenant=tenant_B
    -> resolved tenant stays A. Cross-tenant data must NOT be reachable.
    """
    # Community is the non-developer default; force it explicitly so the
    # test is robust against the singleton's ambient value.
    monkeypatch.setattr(_settings, "edition", "community", raising=False)

    request = _make_request(
        active_tenant=str(_TENANT_B),
        role="admin",
        jwt_tenant=_TENANT_A,
    )
    resolved = auth_mod.resolve_tenant_id(request)
    assert resolved == _TENANT_A, (
        f"Non-developer edition must ignore X-Active-Tenant; resolved "
        f"{resolved}, expected tenant A {_TENANT_A}"
    )
    assert resolved != _TENANT_B, "Cross-tenant switch leaked on a non-developer edition"


async def test_tenant_switch_honoured_on_developer_edition(monkeypatch) -> None:
    """Developer edition (flag on) + admin + header -> switches to tenant B.

    Companion to the regression guard above: proves the gate's positive
    path still works, so the negative test is meaningful (not vacuous
    because the feature is dead everywhere).
    """
    monkeypatch.setattr(_settings, "edition", "developer", raising=False)

    request = _make_request(
        active_tenant=str(_TENANT_B),
        role="admin",
        jwt_tenant=_TENANT_A,
    )
    resolved = auth_mod.resolve_tenant_id(request)
    assert resolved == _TENANT_B, (
        f"Developer edition + admin + header should switch to tenant B; "
        f"resolved {resolved}"
    )


async def test_tenant_switch_ignored_for_non_admin_even_on_developer(monkeypatch) -> None:
    """Even on developer edition, a non-admin role cannot switch tenants.

    Role gate is the third leg of the triple-gate. A viewer for tenant A
    with X-Active-Tenant=tenant_B stays on A.
    """
    monkeypatch.setattr(_settings, "edition", "developer", raising=False)

    request = _make_request(
        active_tenant=str(_TENANT_B),
        role="viewer",
        jwt_tenant=_TENANT_A,
    )
    resolved = auth_mod.resolve_tenant_id(request)
    assert resolved == _TENANT_A, (
        f"Non-admin must not switch tenants even on developer edition; "
        f"resolved {resolved}"
    )
