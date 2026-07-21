"""Test for ``Contact.industry_code`` (M1.5 P1 tail) — industry
classification linkage, free text (no FK), NULL by default.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def test_contact_industry_code_defaults_null_and_settable() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        contact = Contact(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            name=f"Pytest Industry Contact {uuid.uuid4()}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        assert contact.industry_code is None

        contact.industry_code = "6920"
        await session.commit()
        await session.refresh(contact)
        assert contact.industry_code == "6920"
