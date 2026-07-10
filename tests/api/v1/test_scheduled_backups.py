"""Contract tests for ``/api/v1/scheduled-backups`` (Wave E,
FLAG_SCHEDULED_BACKUPS Pro+).

Covers the API-layer contract only — bearer required, admin required,
tier gate (404 below Pro), config CRUD validation, and a full
trigger -> list -> detail -> download round trip that proves the
downloaded artifact is genuinely the SAEBKX01 envelope the client's
passphrase decrypts. The tenant-isolation GUARANTEE itself (zero
foreign-tenant rows in an export) is proven at the service layer in
``tests/services/test_backup_export.py`` and at the RLS layer in
``tests/test_rls_scheduled_backups.py`` — this file does not re-derive
that, it proves the HTTP contract sits correctly on top of it.
"""
from __future__ import annotations

import gzip
import json

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.main import app
from saebooks.services.backup_crypto import DecryptionError, decrypt_export

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]

_PASSPHRASE = "correct horse battery staple wave-e"


@pytest.fixture
async def admin_api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", "X-Admin": "true"},
    ) as ac:
        yield ac


@pytest.fixture
async def plain_api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _force_pro_edition(monkeypatch: pytest.MonkeyPatch):
    from saebooks.config import settings as _settings

    original = _settings.edition
    _settings.edition = "pro"
    yield
    _settings.edition = original


@pytest.fixture(autouse=True)
def _staging_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect the ciphertext staging dir to a pytest tmp_path so the
    test never depends on /app/scheduled-backups being writable inside
    whatever container runs the suite."""
    from saebooks.config import settings as _settings

    monkeypatch.setattr(_settings, "scheduled_backup_export_dir", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
async def _clean_default_tenant_backup_state():
    """DEFAULT_TENANT_ID is shared across this whole test file (and
    other suites) — start and end each test with no leftover
    scheduled_backup_configs/runs row for it, so "config unset" and
    "one config exists" assertions don't depend on test execution
    order or a previous local run's leftovers.

    Uses the OWNER engine (not the RLS-scoped runtime engine) — same
    reason tests/test_rls_scheduled_backups.py's seed fixture does:
    this cleanup must work regardless of ``app.current_tenant`` GUC
    state, which a plain AsyncSessionLocal session wouldn't have set."""
    from sqlalchemy import text

    from saebooks.db import engine as _owner_engine

    async def _wipe() -> None:
        async with _owner_engine.begin() as conn:
            # Bind the UUID object directly (not str(...)) — tenant_id is
            # a `uuid` column and this query has no `::uuid` cast, so an
            # unqualified str bind param gets inferred as VARCHAR by
            # asyncpg, producing `operator does not exist: uuid =
            # character varying` on every test in this file (this fixture
            # is autouse). Matches the working pattern in
            # tests/test_rls_scheduled_backups.py's teardown, which binds
            # the UUID object against the same uncast predicate shape.
            await conn.execute(
                text(
                    "DELETE FROM scheduled_backup_runs WHERE tenant_id = :tid"
                ).bindparams(tid=DEFAULT_TENANT_ID)
            )
            await conn.execute(
                text(
                    "DELETE FROM scheduled_backup_configs WHERE tenant_id = :tid"
                ).bindparams(tid=DEFAULT_TENANT_ID)
            )

    await _wipe()
    yield
    await _wipe()


# ---------------------------------------------------------------------------
# Auth / admin / tier gate
# ---------------------------------------------------------------------------


async def test_config_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/scheduled-backups/config")
    assert r.status_code == 401


async def test_config_requires_admin(plain_api_client: AsyncClient) -> None:
    r = await plain_api_client.get("/api/v1/scheduled-backups/config")
    assert r.status_code == 403


async def test_config_404_below_pro(
    admin_api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "business")
    r = await admin_api_client.get("/api/v1/scheduled-backups/config")
    assert r.status_code == 404


async def test_export_404_below_pro(
    admin_api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saebooks.config import settings as module_settings

    monkeypatch.setattr(module_settings, "edition", "community")
    r = await admin_api_client.post(
        "/api/v1/scheduled-backups/export", json={"passphrase": _PASSPHRASE}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------


async def test_get_config_when_unset_returns_null(admin_api_client: AsyncClient) -> None:
    r = await admin_api_client.get("/api/v1/scheduled-backups/config")
    assert r.status_code == 200
    assert r.json() is None


async def test_put_config_then_get_round_trips(admin_api_client: AsyncClient) -> None:
    body = {
        "enabled": True,
        "destination_type": "local_path",
        "destination_params": {"relative_path": "sub/dir"},
        "retention_keep_n": 5,
        "retention_keep_days": 30,
        "managed_by": "client",
    }
    r = await admin_api_client.put("/api/v1/scheduled-backups/config", json=body)
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["destination_type"] == "local_path"
    assert created["destination_params"] == {"relative_path": "sub/dir"}
    assert created["retention_keep_n"] == 5
    assert created["tenant_id"] == str(DEFAULT_TENANT_ID)

    r2 = await admin_api_client.get("/api/v1/scheduled-backups/config")
    assert r2.status_code == 200
    assert r2.json()["id"] == created["id"]


async def test_put_config_rejects_unknown_destination_type(
    admin_api_client: AsyncClient,
) -> None:
    r = await admin_api_client.put(
        "/api/v1/scheduled-backups/config",
        json={"destination_type": "ftp_1990s", "destination_params": {}},
    )
    assert r.status_code == 400


async def test_put_config_rejects_managed_by_sae_as_not_implemented(
    admin_api_client: AsyncClient,
) -> None:
    """Richard's decision 6 correction: certs aren't paywalled, SAE-
    assumed LIABILITY is. managed_by='sae' is the reserved extension
    point for that priced tier — refused, not silently accepted,
    because nothing implements SAE assuming that liability yet."""
    r = await admin_api_client.put(
        "/api/v1/scheduled-backups/config",
        json={
            "destination_type": "local_path",
            "destination_params": {"relative_path": "x"},
            "managed_by": "sae",
        },
    )
    assert r.status_code == 422


async def test_put_config_rejects_local_path_escaping_root(
    admin_api_client: AsyncClient,
) -> None:
    r = await admin_api_client.put(
        "/api/v1/scheduled-backups/config",
        json={
            "destination_type": "local_path",
            "destination_params": {"relative_path": "../../../etc"},
        },
    )
    assert r.status_code == 400


async def test_put_config_accepts_rclone_remote_shape(
    admin_api_client: AsyncClient,
) -> None:
    """The rclone destination is a STUBBED push (see
    services/backup_destinations.py) but config validation/storage IS
    real — a tenant can save the destination today; the push itself
    is what's stubbed (proven separately by test_export_with_rclone_
    destination_marks_push_stubbed below)."""
    r = await admin_api_client.put(
        "/api/v1/scheduled-backups/config",
        json={
            "destination_type": "rclone_remote",
            "destination_params": {"remote": "backblaze", "path": "/sae/backups"},
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Trigger export → list → detail → download round trip
# ---------------------------------------------------------------------------


async def test_export_rejects_weak_passphrase(admin_api_client: AsyncClient) -> None:
    r = await admin_api_client.post(
        "/api/v1/scheduled-backups/export", json={"passphrase": "short"}
    )
    assert r.status_code in (400, 422)


async def test_export_trigger_list_detail_download_round_trip(
    admin_api_client: AsyncClient,
) -> None:
    # Ensure no destination config → destination_type falls back to
    # "download_only" (no push attempted).
    r = await admin_api_client.post(
        "/api/v1/scheduled-backups/export", json={"passphrase": _PASSPHRASE}
    )
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "SUCCESS", run
    assert run["destination_type"] == "download_only"
    assert run["remote_push_status"] == "not_applicable"
    assert run["artifact_size_bytes"] and run["artifact_size_bytes"] > 0
    assert run["artifact_sha256"] and len(run["artifact_sha256"]) == 64
    assert run["table_counts"] is not None
    run_id = run["id"]

    r_list = await admin_api_client.get("/api/v1/scheduled-backups/runs")
    assert r_list.status_code == 200
    items = r_list.json()["items"]
    assert any(i["id"] == run_id for i in items)

    r_detail = await admin_api_client.get(f"/api/v1/scheduled-backups/runs/{run_id}")
    assert r_detail.status_code == 200
    assert r_detail.json()["id"] == run_id

    r_dl = await admin_api_client.get(
        f"/api/v1/scheduled-backups/runs/{run_id}/download"
    )
    assert r_dl.status_code == 200
    assert r_dl.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r_dl.headers.get("content-disposition", "")

    envelope = r_dl.content
    assert envelope.startswith(b"SAEBKX01")
    compressed = decrypt_export(envelope, _PASSPHRASE)
    plaintext = gzip.decompress(compressed)
    payload = json.loads(plaintext)
    assert payload["manifest"]["tenant_id"] == str(DEFAULT_TENANT_ID)
    assert "companies" in payload["manifest"]["tables"]

    # Wrong passphrase must fail closed — SAE Books never stored the
    # right one, so this is the only proof that matters.
    with pytest.raises(DecryptionError):
        decrypt_export(envelope, "definitely the wrong passphrase")


async def test_download_unknown_run_is_404(admin_api_client: AsyncClient) -> None:
    r = await admin_api_client.get(
        "/api/v1/scheduled-backups/runs/00000000-0000-0000-0000-000000000099"
    )
    assert r.status_code == 404


async def test_export_with_rclone_destination_marks_push_stubbed(
    admin_api_client: AsyncClient,
) -> None:
    r = await admin_api_client.put(
        "/api/v1/scheduled-backups/config",
        json={
            "destination_type": "rclone_remote",
            "destination_params": {"remote": "backblaze", "path": "/sae/backups"},
        },
    )
    assert r.status_code == 200, r.text

    r_export = await admin_api_client.post(
        "/api/v1/scheduled-backups/export", json={"passphrase": _PASSPHRASE}
    )
    assert r_export.status_code == 200, r_export.text
    run = r_export.json()
    # The LOCAL export still succeeds; only the remote push is stubbed.
    assert run["status"] == "SUCCESS"
    assert run["remote_push_status"] == "stubbed_not_implemented"
    assert run["artifact_size_bytes"] and run["artifact_size_bytes"] > 0
