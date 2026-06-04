"""Forensic-trail test for ``services.journal.delete`` (audit M5).

The DELETE on a journal entry cascades to its lines via the
SQLAlchemy ``cascade='all, delete-orphan'`` relationship. The audit
M5 finding was that snapshotting only the parent header lost the
line detail, which is where the GL meaning actually lives. This
test pins the fix: every line gets its own ``audit_snapshots`` row
*before* the entry is removed.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.company import Company
from saebooks.services import journal as svc

pytestmark = pytest.mark.postgres_only


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        accts = (
            await session.execute(
                select(Account)
                .where(Account.company_id == co.id, Account.is_header.is_(False))
                .order_by(Account.code)
                .limit(2)
            )
        ).scalars().all()
        return co.id, accts[0].id, accts[1].id


@pytest.mark.asyncio
async def test_delete_snapshots_lines_and_header() -> None:
    company_id, a, b = await _ctx()
    async with AsyncSessionLocal() as session:
        entry = await svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 5, 10),
            description="Audit M5 cascade-snapshot test",
            lines=[
                {"account_id": a, "debit": Decimal("12.34"), "credit": 0},
                {"account_id": b, "debit": 0, "credit": Decimal("12.34")},
            ],
        )
    line_ids = [str(ln.id) for ln in entry.lines]

    async with AsyncSessionLocal() as session:
        await svc.delete(session, entry.id, performed_by="test-m5")

    async with AsyncSessionLocal() as session:
        header = (
            await session.execute(
                select(AuditSnapshot)
                .where(
                    AuditSnapshot.table_name == "journal_entries",
                    AuditSnapshot.row_id == str(entry.id),
                    AuditSnapshot.action == "delete",
                )
            )
        ).scalars().all()
        assert len(header) == 1, "exactly one audit row for the JE header"
        assert header[0].performed_by == "test-m5"
        assert header[0].before_data["id"] == str(entry.id)

        line_snaps = (
            await session.execute(
                select(AuditSnapshot)
                .where(
                    AuditSnapshot.table_name == "journal_lines",
                    AuditSnapshot.action == "delete",
                    AuditSnapshot.row_id.in_(line_ids),
                )
            )
        ).scalars().all()
        assert len(line_snaps) == 2, "every cascaded line is snapshotted"
        for s in line_snaps:
            assert s.performed_by == "test-m5"
            assert s.reason is not None
            assert str(entry.id) in s.reason
            assert s.before_data["entry_id"] == str(entry.id)
