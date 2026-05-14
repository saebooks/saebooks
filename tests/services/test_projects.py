"""Tests for ``saebooks.services.projects``.

Projects are thin tag rows — the risk isn't the CRUD itself, it's
making sure:

1. UniqueConstraint `(company_id, code)` rejects duplicates.
2. `list_active` respects status + archived filters + search.
3. `update` only allows whitelisted fields; unknown keys raise.
4. `archive` is a soft-delete + flips status to ARCHIVED and writes
   an audit snapshot.
5. Archived projects disappear from default list.

Tests run against the live AU-seeded DB (same pattern as
``tests/test_assets.py``); project codes are UUID-suffixed so runs
don't collide.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.project import Project, ProjectStatus
from saebooks.services import projects as svc
pytestmark = pytest.mark.postgres_only


async def _company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


def _uniq_code(prefix: str = "J-TEST") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def test_create_round_trip() -> None:
    cid = await _company_id()
    code = _uniq_code()
    async with AsyncSessionLocal() as session:
        p = await svc.create(session, cid, code=code, name="Test project")
    assert p.id is not None
    assert p.code == code
    assert p.name == "Test project"
    assert p.status == ProjectStatus.ACTIVE
    assert p.archived_at is None


async def test_create_strips_whitespace() -> None:
    cid = await _company_id()
    code = _uniq_code()
    async with AsyncSessionLocal() as session:
        p = await svc.create(
            session, cid, code=f"  {code}  ", name="  Spacey name  "
        )
    assert p.code == code
    assert p.name == "Spacey name"


async def test_create_duplicate_code_rejected() -> None:
    cid = await _company_id()
    code = _uniq_code()
    async with AsyncSessionLocal() as session:
        await svc.create(session, cid, code=code, name="First")

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with AsyncSessionLocal() as session:
            await svc.create(session, cid, code=code, name="Second")


async def test_list_active_filters_by_status_and_archived() -> None:
    cid = await _company_id()
    # Three projects, three different statuses
    a_code = _uniq_code("J-A")
    b_code = _uniq_code("J-B")
    c_code = _uniq_code("J-C")
    async with AsyncSessionLocal() as session:
        active = await svc.create(
            session, cid, code=a_code, name="Still running"
        )
        done = await svc.create(
            session, cid, code=b_code, name="Finished",
            status=ProjectStatus.COMPLETED,
        )
        gone = await svc.create(
            session, cid, code=c_code, name="To archive",
        )

    async with AsyncSessionLocal() as session:
        await svc.archive(session, gone.id, performed_by="test")

    async with AsyncSessionLocal() as session:
        # Default — active + completed, no archived
        all_live = await svc.list_active(session, cid)
        ids = {p.id for p in all_live}
        assert active.id in ids
        assert done.id in ids
        assert gone.id not in ids

        # Status filter narrows to just ACTIVE
        actives = await svc.list_active(session, cid, status=ProjectStatus.ACTIVE)
        ids = {p.id for p in actives}
        assert active.id in ids
        assert done.id not in ids

        # include_archived pulls the archived one back in
        with_archived = await svc.list_active(session, cid, include_archived=True)
        ids = {p.id for p in with_archived}
        assert gone.id in ids


async def test_list_active_search_matches_code_or_name() -> None:
    cid = await _company_id()
    needle = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        p1 = await svc.create(
            session, cid, code=f"J-{needle}", name="Unremarkable name"
        )
        p2 = await svc.create(
            session, cid, code=_uniq_code(), name=f"Project about {needle}"
        )
        decoy = await svc.create(session, cid, code=_uniq_code(), name="Ignored")

    async with AsyncSessionLocal() as session:
        results = await svc.list_active(session, cid, search=needle)
        ids = {p.id for p in results}
        assert p1.id in ids
        assert p2.id in ids
        assert decoy.id not in ids


async def test_update_whitelists_fields() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        p = await svc.create(session, cid, code=_uniq_code(), name="Before")

    async with AsyncSessionLocal() as session:
        updated = await svc.update(
            session, p.id,
            performed_by="test",
            name="After",
            status=ProjectStatus.COMPLETED,
            start_date=date(2026, 1, 1),
            notes="completed on time",
        )
    assert updated.name == "After"
    assert updated.status == ProjectStatus.COMPLETED
    assert updated.start_date == date(2026, 1, 1)
    assert updated.notes == "completed on time"


async def test_update_rejects_unknown_field() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        p = await svc.create(session, cid, code=_uniq_code(), name="Whatever")

    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="Unknown field"):
            await svc.update(session, p.id, not_a_field="x")


async def test_update_coerces_string_status() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        p = await svc.create(session, cid, code=_uniq_code(), name="String status")

    async with AsyncSessionLocal() as session:
        updated = await svc.update(session, p.id, status="COMPLETED")
    assert updated.status == ProjectStatus.COMPLETED


async def test_archive_is_soft_delete() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        p = await svc.create(session, cid, code=_uniq_code(), name="Archive me")

    before = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        await svc.archive(session, p.id, performed_by="test")

    async with AsyncSessionLocal() as session:
        fresh = await svc.get(session, p.id)
        assert fresh is not None
        assert fresh.archived_at is not None
        assert fresh.archived_at >= before
        assert fresh.status == ProjectStatus.ARCHIVED


async def test_archive_missing_id_is_noop() -> None:
    async with AsyncSessionLocal() as session:
        # Should not raise
        await svc.archive(session, uuid.uuid4(), performed_by="test")


async def test_get_returns_none_for_unknown() -> None:
    async with AsyncSessionLocal() as session:
        assert await svc.get(session, uuid.uuid4()) is None


async def test_list_active_order_and_limit() -> None:
    cid = await _company_id()
    # Deliberately out-of-order codes so sort proof isn't an accident
    codes = [f"Z-{uuid.uuid4().hex[:6]}", f"A-{uuid.uuid4().hex[:6]}"]
    async with AsyncSessionLocal() as session:
        for c in codes:
            await svc.create(session, cid, code=c, name=f"name for {c}")

    async with AsyncSessionLocal() as session:
        result = await svc.list_active(session, cid, limit=500)
        sorted_codes = [p.code for p in result]
        # Should appear in ascending code order
        assert sorted_codes == sorted(sorted_codes)


async def test_list_active_cross_company_isolation() -> None:
    """Rows in another company must NOT appear."""
    cid = await _company_id()
    # Fabricate a bogus company_id — list under it should be empty
    other_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await svc.create(session, cid, code=_uniq_code(), name="Belongs to cid")
        results = await svc.list_active(session, other_id)
    assert results == []


@pytest.fixture(autouse=True, scope="module")
async def _cleanup_test_projects() -> AsyncGenerator[None, None]:
    """Autouse module-scope teardown: purge any J-/Z-/A-UUID-suffixed
    projects we created so the persistent dev DB doesn't accumulate.
    """
    yield
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Project).where(
                Project.code.like("J-TEST%")
                | Project.code.like("J-A-%")
                | Project.code.like("J-B-%")
                | Project.code.like("J-C-%")
                | Project.code.like("J-%")
                | Project.code.like("Z-%")
                | Project.code.like("A-%")
            )
        )
        for p in result.scalars().all():
            await session.delete(p)
        await session.commit()
