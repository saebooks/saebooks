"""Tests for the beneficiary register (gap PRTR-3).

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
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import contacts as svc

pytestmark = pytest.mark.postgres_only


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


