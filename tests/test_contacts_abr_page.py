"""Router smoke tests for the ABR lookup endpoints on /contacts.

Covers the Enterprise feature-gate (404 on Community) and the HTMX
fragments returned by the lookup/apply routes.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def configured_abr(monkeypatch: pytest.MonkeyPatch, enterprise: None) -> None:
    monkeypatch.setattr(app_settings, "abr_api_guid", "test-guid")
    monkeypatch.setattr(app_settings, "abr_api_base", "https://abr.example/json")


async def _first_company_id() -> object:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def _create_contact(company_id: object) -> Contact:
    async with AsyncSessionLocal() as session:
        contact = Contact(
            company_id=company_id,
            name="Scratch",
            contact_type=ContactType.SUPPLIER,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        return contact


async def _cleanup_contact(contact_id: object) -> None:
    async with AsyncSessionLocal() as session:
        contact = await session.get(Contact, contact_id)
        if contact:
            await session.delete(contact)
            await session.commit()


async def test_abr_lookup_404_in_community(client: AsyncClient) -> None:
    r = await client.post("/contacts/abr-lookup", data={"abn": "87744586592"})
    assert r.status_code == 404


async def test_abr_lookup_502_when_unconfigured(
    client: AsyncClient, enterprise: None
) -> None:
    # Enterprise but no guid -> AbrNotConfiguredError -> 502
    r = await client.post("/contacts/abr-lookup", data={"abn": "87744586592"})
    assert r.status_code == 502
    assert "not configured" in r.text


@respx.mock
async def test_abr_lookup_returns_result_fragment(
    client: AsyncClient, configured_abr: None
) -> None:
    payload = (
        'callback({'
        '"Abn":"87744586592","AbnStatus":"Active",'
        '"EntityName":"Sauer Pty Ltd",'
        '"BusinessName":["SAE Engineering"],'
        '"AddressState":"QLD","AddressPostcode":"4350",'
        '"Gst":"2024-02-15"'
        '})'
    )
    respx.get("https://abr.example/json/AbnDetails.aspx").mock(
        return_value=httpx.Response(200, text=payload)
    )
    r = await client.post(
        "/contacts/abr-lookup", data={"abn": "87 744 586 592"}
    )
    assert r.status_code == 200
    assert "ABR match" in r.text
    assert "SAE Engineering" in r.text
    assert "QLD" in r.text


@respx.mock
async def test_abr_lookup_400_on_invalid_abn(
    client: AsyncClient, configured_abr: None
) -> None:
    payload = (
        'callback({"Abn":"","AbnStatus":"",'
        '"Message":"Search text is not a valid ABN or ACN"})'
    )
    respx.get("https://abr.example/json/AbnDetails.aspx").mock(
        return_value=httpx.Response(200, text=payload)
    )
    r = await client.post(
        "/contacts/abr-lookup", data={"abn": "12345678901"}
    )
    assert r.status_code == 400
    assert "ABR lookup failed" in r.text


@respx.mock
async def test_abr_apply_merges_and_persists(
    client: AsyncClient, configured_abr: None
) -> None:
    company_id = await _first_company_id()
    contact = await _create_contact(company_id)
    try:
        payload = (
            'callback({'
            '"Abn":"87744586592","AbnStatus":"Active",'
            '"EntityName":"Sauer Pty Ltd",'
            '"AddressState":"QLD","AddressPostcode":"4350",'
            '"Gst":"2024-02-15"'
            '})'
        )
        respx.get("https://abr.example/json/AbnDetails.aspx").mock(
            return_value=httpx.Response(200, text=payload)
        )
        r = await client.post(
            f"/contacts/{contact.id}/abr-apply",
            data={"abn": "87 744 586 592"},
        )
        assert r.status_code == 200
        assert "ABR applied" in r.text
        # Verify the merge actually hit the DB
        async with AsyncSessionLocal() as session:
            refreshed = await session.get(Contact, contact.id)
            assert refreshed is not None
            assert refreshed.state == "QLD"
            assert refreshed.postcode == "4350"
            assert refreshed.abn == "87 744 586 592"
    finally:
        await _cleanup_contact(contact.id)


async def test_abr_apply_404_in_community(client: AsyncClient) -> None:
    import uuid as _u

    r = await client.post(
        f"/contacts/{_u.uuid4()}/abr-apply", data={"abn": "87744586592"}
    )
    assert r.status_code == 404
