"""Contract tests for ``GET /api/v1/modules`` (M2 §5 steps 3-6).

Unauthenticated, static-only module catalogue. Covers:

* no auth required, response shape is stable;
* the six developer-only flags and the internal "developer" tier never
  appear;
* planned (unbacked) flags show ``state="planned"``;
* delegated modules (capture/preaccounting/platform) are present with
  ``kind="delegated"`` (the cashbook ``kind="mode"`` entry is added in
  M2 §5 step 6 -- see ``tests/api/v1/test_modules_cashbook_mode.py``);
* NEVER returns edition / effective_edition / entitled / health — those
  are the bearer-gated ``/modules/usage`` endpoint's job.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.main import app
from saebooks.services.module_registry import DEVELOPER_ONLY_FLAGS, PLANNED_FLAGS


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_no_auth_required(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    assert r.status_code == 200


async def test_ignores_bogus_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get(
        "/api/v1/modules", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert r.status_code == 200


async def test_response_shape(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    body = r.json()
    assert set(body.keys()) == {"modules", "caps"}
    assert isinstance(body["modules"], list)
    assert len(body["modules"]) > 0
    for entry in body["modules"]:
        assert set(entry.keys()) == {
            "id", "label", "kind", "group", "tier_membership", "state",
        }
        assert entry["kind"] in {"flag", "delegated", "mode"}
        assert entry["state"] in {"enforced", "planned"}
        assert entry["tier_membership"] in {
            "community", "offline", "business", "pro", "enterprise",
        }


async def test_never_returns_per_user_state(unauth_client: AsyncClient) -> None:
    """Static-only contract: no edition/effective_edition/entitled/health
    anywhere in the payload -- those require an authenticated caller."""
    r = await unauth_client.get("/api/v1/modules")
    raw = r.text
    for forbidden in ("effective_edition", "entitled", "\"health\""):
        assert forbidden not in raw, f"{forbidden!r} leaked into unauth /modules"
    body = r.json()
    assert "edition" not in body


async def test_excludes_developer_only_flags(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    ids = {m["id"] for m in r.json()["modules"]}
    for dev_flag in DEVELOPER_ONLY_FLAGS:
        assert dev_flag not in ids


async def test_excludes_developer_tier(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    body = r.json()
    assert "developer" not in body["caps"]
    for m in body["modules"]:
        assert m["tier_membership"] != "developer"


async def test_planned_flags_marked_planned(unauth_client: AsyncClient) -> None:
    """Assert against ``PLANNED_FLAGS`` (source of truth) so this test
    doesn't rot as Wave-style work moves flags from planned to enforced.

    Wave A (2026-07-10) moved multi_currency / abr_lookup /
    projects_budgets / asset_v2 from planned to enforced -- see
    ``saebooks/services/module_registry.py`` PLANNED_FLAGS docstring.
    """
    r = await unauth_client.get("/api/v1/modules")
    by_id = {m["id"]: m for m in r.json()["modules"]}
    for planned_id in PLANNED_FLAGS:
        assert by_id[planned_id]["state"] == "planned"


async def test_wave_a_flags_marked_enforced(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    by_id = {m["id"]: m for m in r.json()["modules"]}
    for enforced_id in (
        "multi_currency", "abr_lookup", "projects_budgets", "asset_v2",
    ):
        assert by_id[enforced_id]["state"] == "enforced"


async def test_enforced_flag_marked_enforced(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    by_id = {m["id"]: m for m in r.json()["modules"]}
    assert by_id["bank_feeds"]["state"] == "enforced"
    assert by_id["bank_feeds"]["tier_membership"] == "business"


async def test_delegated_modules_present(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/modules")
    by_id = {m["id"]: m for m in r.json()["modules"]}
    for module_id in ("capture", "preaccounting", "platform"):
        assert by_id[module_id]["kind"] == "delegated"


async def test_caps_matrix_present_for_every_public_tier(
    unauth_client: AsyncClient,
) -> None:
    r = await unauth_client.get("/api/v1/modules")
    caps = r.json()["caps"]
    assert set(caps.keys()) == {
        "community", "offline", "business", "pro", "enterprise",
    }
    for edition_caps in caps.values():
        assert set(edition_caps.keys()) == {
            "admin_seats", "employee_seats", "companies", "seat_cap_kind",
        }
