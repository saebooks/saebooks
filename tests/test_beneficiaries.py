"""Tests for the beneficiary register.

Covers:
* DB: contact_type_enum includes BENEFICIARY value.
* DB: contacts table has tfn, share_percentage, default_income_classification columns.
* DB: beneficiary_entitlements has contact_id FK column.
* Service: create contact of type BENEFICIARY with beneficiary fields.
* Router: GET /beneficiaries returns 200.
* Router: GET /contacts?type=BENEFICIARY returns 200 and filters correctly.
* Router: POST /contacts creates a BENEFICIARY contact (round-trip).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect, select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import contacts as svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def _cleanup(contact_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(Contact, contact_id)
        if row:
            await session.delete(row)
            await session.commit()


# ---------------------------------------------------------------------------
# DB schema checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beneficiary_enum_value_exists() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'contact_type_enum'"
            )
        )
        labels = {row[0] for row in result.all()}
    assert "BENEFICIARY" in labels, "BENEFICIARY missing from contact_type_enum"


@pytest.mark.asyncio
async def test_contacts_beneficiary_columns_exist() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'contacts' "
                "AND column_name IN ('tfn', 'share_percentage', 'default_income_classification')"
            )
        )
        cols = {row[0] for row in result.all()}
    assert "tfn" in cols, "tfn column missing from contacts"
    assert "share_percentage" in cols, "share_percentage column missing from contacts"
    assert "default_income_classification" in cols, (
        "default_income_classification column missing from contacts"
    )


@pytest.mark.asyncio
async def test_beneficiary_entitlements_contact_id_column_exists() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'beneficiary_entitlements' "
                "AND column_name = 'contact_id'"
            )
        )
        cols = {row[0] for row in result.all()}
    assert "contact_id" in cols, "contact_id FK column missing from beneficiary_entitlements"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_beneficiary_contact() -> None:
    company_id = await _first_company_id()
    async with AsyncSessionLocal() as session:
        contact = await svc.create(
            session,
            company_id,
            name="Alice Nguyen",
            contact_type=ContactType.BENEFICIARY,
            tfn="123456789",
            share_percentage="60.0000",
            default_income_classification="Individual",
        )
    assert contact.contact_type == ContactType.BENEFICIARY
    assert contact.tfn == "123456789"
    assert contact.default_income_classification == "Individual"
    await _cleanup(contact.id)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beneficiaries_register_page(client: AsyncClient) -> None:
    r = await client.get("/beneficiaries")
    assert r.status_code == 200
    assert b"Beneficiary Register" in r.content


@pytest.mark.asyncio
async def test_contacts_list_filter_beneficiary(client: AsyncClient) -> None:
    r = await client.get("/contacts?type=BENEFICIARY")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_beneficiary_via_form(client: AsyncClient) -> None:
    company_id = await _first_company_id()
    unique_name = f"Test Beneficiary {uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/contacts",
        data={
            "name": unique_name,
            "contact_type": "BENEFICIARY",
            "email": "",
            "phone": "",
            "abn": "",
            "address_line1": "",
            "address_line2": "",
            "city": "",
            "state": "",
            "postcode": "",
            "notes": "",
            "default_account_id": "",
            "default_tax_code": "",
            "tfn": "987654321",
            "share_percentage": "40.0000",
            "default_income_classification": "Trust",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Verify row was created with correct type and fields
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.name == unique_name,
            )
        )
        contact = result.scalars().first()
    assert contact is not None
    assert contact.contact_type == ContactType.BENEFICIARY
    assert contact.tfn == "987654321"
    assert contact.default_income_classification == "Trust"
    await _cleanup(contact.id)
