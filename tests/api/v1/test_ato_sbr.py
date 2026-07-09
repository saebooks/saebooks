"""Tests for /api/v1/ato_sbr — Cat-C greenfield router (W3).

Covers:
* 401 unauthenticated requests
* 404 feature-gate (community edition)
* Keystore upload (happy path, empty file, bad keystore)
* Keystore list
* Keystore soft-delete (happy, 404 if not found, 409 if already cleared)
* Wizard start (machine_credential + ssid_link flows, bad flow)
* Wizard step advance (happy path, 409 stale-step, 422 missing answer, 404 gone)
* Ping against stubbed lodge-server

Note on database
----------------
These tests require a live Postgres DB (the same one other API tests use).
If run on a dev machine without a local Postgres, skip with:

    pytest tests/api/v1/test_ato_sbr.py -x \
        --ignore-glob="*test_ato_sbr.py"

On a CI host with Postgres available, run via:

    sudo docker compose -f /path/to/saebooks/docker-compose.yml \
        exec -T saebooks2-app-1 pytest tests/api/v1/test_ato_sbr.py -x

Note on keystore fixture
-------------------------
We cannot upload a real ATO RAM Machine Credential in CI because the
format requires live cert bytes. Tests that exercise the keystore
upload path use a mock of ``load_keystore`` to bypass the real parser.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import saebooks.services.features as features_mod
from saebooks.api.v1.auth import current_token
from saebooks.main import app
from saebooks.services.ato_sbr.keystore import KeystoreError, LoadedKeystore

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    """Authenticated client with a valid bearer token."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def anon_client() -> AsyncClient:
    """Unauthenticated client (no Authorization header)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture
def pro_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force Pro edition so FLAG_ATO_SBR is active."""
    monkeypatch.setattr(features_mod._default_settings, "edition", "pro")


@pytest.fixture
def community_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force Community edition — FLAG_ATO_SBR disabled."""
    monkeypatch.setattr(features_mod._default_settings, "edition", "community")


_FAKE_LOADED_KEYSTORE = LoadedKeystore(
    subject_cn="TEST ABN 12345678901",
    issuer_cn="ATO Machine Credential CA",
    serial="deadbeef1234",
    not_before=datetime(2025, 1, 1, tzinfo=UTC),
    not_after=datetime(2026, 1, 1, tzinfo=UTC),
)


# ---------------------------------------------------------------------------
# 401 — unauthenticated
# ---------------------------------------------------------------------------


async def test_keystore_upload_401(anon_client: AsyncClient) -> None:
    r = await anon_client.post("/api/v1/ato_sbr/keystore", data={})
    assert r.status_code == 401


async def test_keystore_list_401(anon_client: AsyncClient) -> None:
    r = await anon_client.get("/api/v1/ato_sbr/keystore")
    assert r.status_code == 401


async def test_keystore_delete_401(anon_client: AsyncClient) -> None:
    r = await anon_client.delete(f"/api/v1/ato_sbr/keystore/{uuid.uuid4()}")
    assert r.status_code == 401


async def test_wizard_start_401(anon_client: AsyncClient) -> None:
    r = await anon_client.post("/api/v1/ato_sbr/onboarding/wizards", json={"flow": "machine_credential"})
    assert r.status_code == 401


async def test_ping_401(anon_client: AsyncClient) -> None:
    r = await anon_client.post("/api/v1/ato_sbr/ping", json={"keystore_id": str(uuid.uuid4())})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 404 — feature gate (community edition)
# ---------------------------------------------------------------------------


async def test_keystore_upload_404_community(
    api_client: AsyncClient, community_edition: None
) -> None:
    r = await api_client.post("/api/v1/ato_sbr/keystore", data={})
    assert r.status_code == 404


async def test_keystore_list_404_community(
    api_client: AsyncClient, community_edition: None
) -> None:
    r = await api_client.get("/api/v1/ato_sbr/keystore")
    assert r.status_code == 404


async def test_wizard_start_404_community(
    api_client: AsyncClient, community_edition: None
) -> None:
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards", json={"flow": "machine_credential"}
    )
    assert r.status_code == 404


async def test_ping_404_community(
    api_client: AsyncClient, community_edition: None
) -> None:
    r = await api_client.post(
        "/api/v1/ato_sbr/ping", json={"keystore_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Keystore upload — happy path (mocked parser + crypto)
# ---------------------------------------------------------------------------


async def test_keystore_upload_happy_path(
    api_client: AsyncClient, pro_edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload a minimal keystore file with a mocked parser."""
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.load_keystore",
        lambda data, password: _FAKE_LOADED_KEYSTORE,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.is_configured",
        lambda s: True,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.encrypt_field",
        lambda v, settings: f"enc:{v[:8]}",
    )

    r = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"<fake/>", "application/xml")},
        data={"password": "hunter2", "label": "Test Credential"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    assert body["abn_or_name"] == "TEST ABN 12345678901"
    assert "2026-01-01" in body["expires_at"]


# ---------------------------------------------------------------------------
# Keystore upload — validation errors
# ---------------------------------------------------------------------------


async def test_keystore_upload_empty_file(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Empty file content returns 422."""
    r = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"", "application/xml")},
        data={"password": "hunter2"},
    )
    assert r.status_code == 422


async def test_keystore_upload_bad_parse(
    api_client: AsyncClient, pro_edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parser failure returns 422 with explanation."""
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.is_configured",
        lambda s: True,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.load_keystore",
        lambda data, password: (_ for _ in ()).throw(KeystoreError("bad format")),
    )

    r = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"<notakeystore/>", "application/xml")},
        data={"password": "wrong"},
    )
    assert r.status_code == 422
    assert "Keystore parse failed" in r.text


async def test_keystore_upload_no_encryption(
    api_client: AsyncClient, pro_edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When encryption is not configured, upload returns 503."""
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.is_configured",
        lambda s: False,
    )

    r = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"<x/>", "application/xml")},
        data={"password": "pw"},
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Keystore list
# ---------------------------------------------------------------------------


async def test_keystore_list_returns_items_key(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """List endpoint returns a dict with an 'items' key."""
    r = await api_client.get("/api/v1/ato_sbr/keystore")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


# ---------------------------------------------------------------------------
# Keystore soft-delete
# ---------------------------------------------------------------------------


async def test_keystore_delete_not_found(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """DELETE with unknown id returns 404."""
    r = await api_client.delete(f"/api/v1/ato_sbr/keystore/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_keystore_delete_happy_then_409(
    api_client: AsyncClient, pro_edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload a keystore, delete it (204), then confirm re-delete gives 409."""
    # Upload first.
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.load_keystore",
        lambda data, password: _FAKE_LOADED_KEYSTORE,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.is_configured",
        lambda s: True,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.encrypt_field",
        lambda v, settings: f"enc:{v[:8]}",
    )

    r = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"<fake/>", "application/xml")},
        data={"password": "pw"},
    )
    assert r.status_code == 201
    entry_id = r.json()["id"]

    # First delete — should succeed.
    r2 = await api_client.delete(f"/api/v1/ato_sbr/keystore/{entry_id}")
    assert r2.status_code == 204

    # Second delete — already cleared, should be 409.
    r3 = await api_client.delete(f"/api/v1/ato_sbr/keystore/{entry_id}")
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# Wizard — start
# ---------------------------------------------------------------------------


async def test_wizard_start_machine_credential(
    api_client: AsyncClient, pro_edition: None
) -> None:
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "machine_credential"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["flow"] == "machine_credential"
    assert "wizard_id" in body
    assert body["step_index"] == 0


async def test_wizard_start_ssid_link(
    api_client: AsyncClient, pro_edition: None
) -> None:
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "ssid_link"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["flow"] == "ssid_link"


async def test_wizard_start_bad_flow(
    api_client: AsyncClient, pro_edition: None
) -> None:
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "nonexistent_flow"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Wizard — step advance (happy path)
# ---------------------------------------------------------------------------


async def test_wizard_step_advance_happy_path(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Start a ssid_link wizard and submit the first step."""
    # Start.
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "ssid_link"},
    )
    assert r.status_code == 201
    wizard_id = r.json()["wizard_id"]

    # Step 0: provide ssid.
    r2 = await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
        json={"answers": {"ssid": "SBD00000001"}},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    # Should advance to step 1.
    assert body2["step_index"] == 1 or body2.get("status") == "complete"


async def test_wizard_completes_after_all_steps(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """ssid_link wizard completes after both steps are submitted."""
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "ssid_link"},
    )
    wizard_id = r.json()["wizard_id"]

    # Step 0: ssid entry.
    await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
        json={"answers": {"ssid": "SBD00000002"}},
    )
    # Step 1: confirmation.
    r2 = await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
        json={"answers": {"confirmed": True}},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("status") == "complete"


# ---------------------------------------------------------------------------
# Wizard — 409 stale-step (optimistic locking)
# ---------------------------------------------------------------------------


async def test_wizard_step_409_stale_version(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Submitting with wrong step index in If-Match returns 409."""
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "ssid_link"},
    )
    wizard_id = r.json()["wizard_id"]

    # Submit with If-Match: 99 (wrong step).
    r2 = await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
        json={"answers": {"ssid": "SBD00000001"}},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "stale_step"
    assert body["current_step"] == 0
    assert body["submitted_step"] == 99


# ---------------------------------------------------------------------------
# Wizard — 422 missing required answer
# ---------------------------------------------------------------------------


async def test_wizard_step_422_missing_answer(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Submitting step without required fields returns 422."""
    r = await api_client.post(
        "/api/v1/ato_sbr/onboarding/wizards",
        json={"flow": "ssid_link"},
    )
    wizard_id = r.json()["wizard_id"]

    # Step 0 requires 'ssid' but we send empty.
    r2 = await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{wizard_id}/step",
        json={"answers": {"ssid": ""}},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Wizard — 404 not found
# ---------------------------------------------------------------------------


async def test_wizard_step_404_not_found(
    api_client: AsyncClient, pro_edition: None
) -> None:
    r = await api_client.post(
        f"/api/v1/ato_sbr/onboarding/wizards/{uuid.uuid4()}/step",
        json={"answers": {}},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Ping — against stubbed lodge-server
# ---------------------------------------------------------------------------


async def test_ping_keystore_not_found(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Ping with unknown keystore_id returns 404."""
    r = await api_client.post(
        "/api/v1/ato_sbr/ping",
        json={"keystore_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


async def test_ping_missing_keystore_id(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Ping without keystore_id returns 422."""
    r = await api_client.post("/api/v1/ato_sbr/ping", json={})
    assert r.status_code == 422


async def test_ping_invalid_keystore_id_uuid(
    api_client: AsyncClient, pro_edition: None
) -> None:
    """Ping with non-UUID keystore_id returns 422."""
    r = await api_client.post(
        "/api/v1/ato_sbr/ping",
        json={"keystore_id": "not-a-uuid"},
    )
    assert r.status_code == 422


async def test_ping_stub_mode(
    api_client: AsyncClient, pro_edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When lodge-server returns stub-mode (501), ping returns ok=false, reason=stub."""
    from saebooks.services.lodgement.exceptions import LodgementUpstreamUnavailable

    # First upload a keystore so we have a valid id.
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.load_keystore",
        lambda data, password: _FAKE_LOADED_KEYSTORE,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.is_configured",
        lambda s: True,
    )
    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.crypto_svc.encrypt_field",
        lambda v, settings: f"enc:{v[:8]}",
    )

    r_upload = await api_client.post(
        "/api/v1/ato_sbr/keystore",
        files={"file": ("keystore.xml", b"<fake/>", "application/xml")},
        data={"password": "pw"},
    )
    assert r_upload.status_code == 201
    keystore_id = r_upload.json()["id"]

    # Stub the remote service to behave like a 501 stub-mode lodge-server.
    async def _stub_audit_log(self, limit=100):
        raise LodgementUpstreamUnavailable(status=501, detail="stub mode")

    monkeypatch.setattr(
        "saebooks.api.v1.ato_sbr.RemoteLodgementService.my_audit_log",
        _stub_audit_log,
    )

    r = await api_client.post(
        "/api/v1/ato_sbr/ping",
        json={"keystore_id": keystore_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "lodge_server_stub_mode"
    assert "latency_ms" in body
