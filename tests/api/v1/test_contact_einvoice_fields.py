"""Round-trip contract tests for the e-invoicing contact fields.

Migration 0197 added ``Contact.e_invoice_recipient`` +
``Contact.peppol_participant_id`` but they were absent from every API
schema, so they could not round-trip. These tests pin that they now
persist through POST -> GET and toggle through PATCH.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.main import app


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


def _rand_name(prefix: str = "EInv Buyer") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


async def test_create_with_einvoice_fields_then_get_returns_them(
    api_client: AsyncClient,
) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={
            "name": _rand_name(),
            "contact_type": "CUSTOMER",
            "e_invoice_recipient": True,
            "peppol_participant_id": "0191:10137025",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["e_invoice_recipient"] is True
    assert created["peppol_participant_id"] == "0191:10137025"

    contact_id = created["id"]
    r2 = await api_client.get(f"/api/v1/contacts/{contact_id}")
    assert r2.status_code == 200
    fetched = r2.json()
    assert fetched["e_invoice_recipient"] is True
    assert fetched["peppol_participant_id"] == "0191:10137025"


async def test_create_defaults_flag_false(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("NoFlag"), "contact_type": "CUSTOMER"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["e_invoice_recipient"] is False
    assert created["peppol_participant_id"] is None


async def test_patch_toggles_flag_and_persists(api_client: AsyncClient) -> None:
    r = await api_client.post(
        "/api/v1/contacts",
        json={"name": _rand_name("Toggle"), "contact_type": "CUSTOMER"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    contact_id = created["id"]
    assert created["e_invoice_recipient"] is False

    r2 = await api_client.patch(
        f"/api/v1/contacts/{contact_id}",
        json={"e_invoice_recipient": True, "peppol_participant_id": "0191:10137025"},
        headers={"If-Match": str(created["version"])},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["e_invoice_recipient"] is True
    assert updated["peppol_participant_id"] == "0191:10137025"

    # Persisted, not just echoed.
    r3 = await api_client.get(f"/api/v1/contacts/{contact_id}")
    assert r3.status_code == 200
    fetched = r3.json()
    assert fetched["e_invoice_recipient"] is True
    assert fetched["peppol_participant_id"] == "0191:10137025"
