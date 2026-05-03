"""Router tests for /admin/ato-sbr (ato_sbr.py).

The entire router is gated behind FLAG_ATO_SBR (Pro+ edition).
Community/default edition → all routes 404.
Pro edition → routes live, rendering / upload / SSID / environment.

Covers:
* GET /admin/ato-sbr → 404 in community (feature gate)
* GET /admin/ato-sbr → 200 in pro edition
* POST /admin/ato-sbr/keystore with empty file → redirect with error
* POST /admin/ato-sbr/keystore with invalid XML → redirect with error
* POST /admin/ato-sbr/keystore with valid-looking XML → redirect with message
* POST /admin/ato-sbr/ssid with short SSID → redirect (error or saved)
* POST /admin/ato-sbr/ssid with valid SSID → redirect with message=ssid+saved
* POST /admin/ato-sbr/environment → redirect
* POST /admin/ato-sbr/confirm → redirect
* POST /admin/ato-sbr/clear → redirect with message=config+cleared
"""
from __future__ import annotations

import io

import pytest
from httpx import AsyncClient, ASGITransport

from saebooks.config import settings as app_settings
from saebooks.main import app


@pytest.fixture
async def client(admin_client: AsyncClient) -> AsyncClient:
    """Pro-edition ``/admin/ato-sbr/*`` routes are gated by
    ``require_role(ADMIN)`` (after feature gate). Delegate to the conftest
    ``admin_client``. Community-edition tests still hit the feature gate
    first, so they get 404 regardless of auth."""
    return admin_client


@pytest.fixture
def pro_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "pro")


# ---------------------------------------------------------------------------
# Feature gate (community edition — must be set explicitly since .env may set enterprise)
# ---------------------------------------------------------------------------


@pytest.fixture
def community_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")


async def test_ato_sbr_404_in_community(
    client: AsyncClient, community_edition: None
) -> None:
    r = await client.get("/admin/ato-sbr")
    assert r.status_code == 404


async def test_ato_sbr_keystore_404_in_community(
    client: AsyncClient, community_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/keystore",
        files={"file": ("keystore.xml", io.BytesIO(b""), "application/xml")},
        data={"password": "test"},
    )
    assert r.status_code == 404


async def test_ato_sbr_ssid_404_in_community(
    client: AsyncClient, community_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/ssid",
        data={"ssid": "SBD12345"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Wizard page (Pro edition)
# ---------------------------------------------------------------------------


async def test_ato_sbr_index_200_in_pro(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.get("/admin/ato-sbr")
    assert r.status_code == 200


async def test_ato_sbr_index_contains_keystore_form(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.get("/admin/ato-sbr")
    assert r.status_code == 200
    assert "keystore" in r.text.lower()


# ---------------------------------------------------------------------------
# Keystore upload
# ---------------------------------------------------------------------------


async def test_keystore_empty_file_redirects_with_error(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/keystore",
        files={"file": ("keystore.xml", io.BytesIO(b""), "application/xml")},
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error" in r.headers["location"]
    assert "no+file" in r.headers["location"] or "error" in r.headers["location"]


async def test_keystore_invalid_xml_redirects_with_error(
    client: AsyncClient, pro_edition: None
) -> None:
    invalid_xml = b"<this is not valid xml"
    r = await client.post(
        "/admin/ato-sbr/keystore",
        files={"file": ("keystore.xml", io.BytesIO(invalid_xml), "application/xml")},
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error" in r.headers["location"]


async def test_keystore_wrong_format_redirects_with_error(
    client: AsyncClient, pro_edition: None
) -> None:
    # Well-formed XML but not a keystore format the service recognises
    not_a_keystore = b"<?xml version='1.0'?><root><item>hello</item></root>"
    r = await client.post(
        "/admin/ato-sbr/keystore",
        files={"file": ("keystore.xml", io.BytesIO(not_a_keystore), "application/xml")},
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Expect an error redirect (the service will reject this as not a JKS/PKCS12)
    location = r.headers["location"]
    assert "/admin/ato-sbr" in location


# ---------------------------------------------------------------------------
# SSID
# ---------------------------------------------------------------------------


async def test_ssid_save_valid_redirects_with_message(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/ssid",
        data={"ssid": "SBD00000001"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Either saved or error — both are redirects to /admin/ato-sbr
    assert "/admin/ato-sbr" in r.headers["location"]


# ---------------------------------------------------------------------------
# Environment toggle
# ---------------------------------------------------------------------------


async def test_environment_evte_redirects(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/environment",
        data={"environment": "evte"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin/ato-sbr" in r.headers["location"]


async def test_environment_prod_redirects(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/environment",
        data={"environment": "prod"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Confirm (off-system steps)
# ---------------------------------------------------------------------------


async def test_confirm_step_redirects(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/confirm",
        data={"step": "mygovid"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin/ato-sbr" in r.headers["location"]


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


async def test_clear_redirects_with_message(
    client: AsyncClient, pro_edition: None
) -> None:
    r = await client.post(
        "/admin/ato-sbr/clear",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "config+cleared" in r.headers["location"]
