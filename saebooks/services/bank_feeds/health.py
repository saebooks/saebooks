"""Feed-health — cache ``/sds/feedissues`` and surface active issues in the UI.

Batch J. The SISS ``/sds/feedissues`` endpoint returns outstanding
issues per institution (outages, schema-incompat announcements, etc.).
We pull it opportunistically — every ~6h via the daily-sync CLI, or
manually via the "Refresh" button on ``/admin/bank-feeds`` — and cache
into the ``bank_feed_issues`` table (migration 0016).

The dashboard then surfaces active rows as a yellow banner so the admin
sees issues against institutions they're actually connected to.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.bank_feed import (
    BankFeedAccount,
    BankFeedIssue,
    BankFeedIssueStatus,
)
from saebooks.services.bank_feeds import endpoints, onboarding, repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshOutcome:
    fetched: int          # total issue rows returned by SISS
    cached: int           # rows newly-upserted into our cache
    as_of: datetime


async def refresh_feed_issues(
    *,
    settings: Settings | None = None,
) -> RefreshOutcome:
    """Pull ``list_feed_issues`` and upsert into ``bank_feed_issues``.

    Pulls only the first page (100 rows) — SISS rarely has more active
    issues than that. Upsert is idempotent on ``sds_feed_issue_id``.
    """
    from saebooks.db import AsyncSessionLocal

    async with onboarding.siss_client(settings) as client:
        envelope = await endpoints.list_feed_issues(client, page=1, page_size=100)
    data = envelope.get("data") or {}
    issues = data.get("issues") or data.get("feedIssues") or []

    cached = 0
    async with AsyncSessionLocal() as session:
        for raw in issues:
            await repo.upsert_feed_issue(session, issue=raw)
            cached += 1
        await session.commit()
    return RefreshOutcome(fetched=len(issues), cached=cached, as_of=datetime.now())


async def active_issues_for_company(
    session: AsyncSession, company_id: uuid.UUID
) -> list[BankFeedIssue]:
    """Return active issues scoped to institutions this company is linked to.

    Used by the dashboard banner so we don't annoy the user with issues
    that don't affect them. If the company has no feeds, returns an
    empty list.
    """
    account_rows = (
        await session.execute(
            select(BankFeedAccount.sds_institution_id).where(
                BankFeedAccount.company_id == company_id,
                BankFeedAccount.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    institution_ids = {row for row in account_rows if row}
    if not institution_ids:
        return []
    rows = await session.execute(
        select(BankFeedIssue)
        .where(
            BankFeedIssue.status == BankFeedIssueStatus.ACTIVE.value,
            BankFeedIssue.sds_institution_id.in_(institution_ids),
        )
        .order_by(BankFeedIssue.creation_datetime.desc())
    )
    return list(rows.scalars().all())
