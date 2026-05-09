"""Tests for the CoA template registry / dispatcher."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services.templates import (
    UnknownTemplate,
    apply_template,
    known_templates,
)


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        return co.id


def test_registry_lists_all_four_jurisdictions() -> None:
    keys = known_templates()
    assert "au/default" in keys
    assert "nz/default" in keys
    assert "uk/default" in keys
    assert "ee/default" in keys


async def test_apply_nz_default_raises_not_implemented() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotImplementedError, match="M1"):
            await apply_template(session, company_id, "nz/default")


async def test_apply_uk_default_raises_not_implemented() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotImplementedError, match="M2"):
            await apply_template(session, company_id, "uk/default")


async def test_apply_ee_default_raises_not_implemented() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotImplementedError, match="M3"):
            await apply_template(session, company_id, "ee/default")


async def test_apply_unknown_template_raises_unknown() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(UnknownTemplate):
            await apply_template(session, company_id, "moon/default")


async def test_seed_company_has_au_default_template_key() -> None:
    """0103 backfill should leave the seed company at au/default."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        assert co.coa_template_key == "au/default"
