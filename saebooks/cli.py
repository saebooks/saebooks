"""Command-line entry points for SAE Books background jobs.

Invoked like::

    python -m saebooks.cli sync-feeds
    python -m saebooks.cli refresh-feed-issues
    python -m saebooks.cli sync-feeds --company-id <uuid>
    python -m saebooks.cli generate-recurring
    python -m saebooks.cli generate-recurring --company-id <uuid>

Designed to be kicked by plain cron — no long-running worker, no queue
runtime. Exits 0 on success, 1 on total failure; per-account errors are
logged but don't kill the whole run (so one flakey bank doesn't stop
the others from syncing).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import date

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import recurrence
from saebooks.services.bank_feeds import health, onboarding

logger = logging.getLogger("saebooks.cli")


async def _sync_feeds(company_id: str | None) -> int:
    """Run sync_all_active; return exit code."""
    cid = uuid.UUID(company_id) if company_id else None
    try:
        async with AsyncSessionLocal() as session:
            outcomes = await onboarding.sync_all_active(
                session,
                company_id=cid,
                settings=settings,
            )
            await session.commit()
    except onboarding.SissNotConfiguredError as exc:
        logger.error("sync-feeds: %s", exc)
        return 1

    total_new = sum(o.lines_inserted for o in outcomes)
    total_seen = sum(o.transactions_seen for o in outcomes)
    logger.info(
        "sync-feeds: %d account(s), %d txns seen, %d new lines",
        len(outcomes),
        total_seen,
        total_new,
    )
    for outcome in outcomes:
        logger.info(
            "  account=%s seen=%d new=%d cursor=%s",
            outcome.bank_feed_account_id,
            outcome.transactions_seen,
            outcome.lines_inserted,
            outcome.cursor_advanced_to or "(unchanged)",
        )
    return 0


async def _generate_recurring(
    company_id: str | None, as_of: str | None
) -> int:
    """Materialise every due RecurringInvoice; return exit code."""
    cid = uuid.UUID(company_id) if company_id else None
    as_of_date = date.fromisoformat(as_of) if as_of else None
    async with AsyncSessionLocal() as session:
        invoices = await recurrence.run_due(
            session, as_of=as_of_date, company_id=cid
        )
    logger.info(
        "generate-recurring: materialised %d invoice(s) as of %s",
        len(invoices),
        (as_of_date or date.today()).isoformat(),
    )
    for inv in invoices:
        logger.info(
            "  invoice=%s contact=%s total=%s status=%s",
            inv.id,
            inv.contact_id,
            inv.total,
            inv.status.value,
        )
    return 0


async def _refresh_feed_issues() -> int:
    try:
        result = await health.refresh_feed_issues(settings=settings)
    except onboarding.SissNotConfiguredError as exc:
        logger.error("refresh-feed-issues: %s", exc)
        return 1
    logger.info(
        "refresh-feed-issues: fetched=%d cached=%d as_of=%s",
        result.fetched,
        result.cached,
        result.as_of.isoformat(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="saebooks.cli",
        description="SAE Books background-job entry points.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser(
        "sync-feeds",
        help="Pull new transactions for every active BankFeedAccount.",
    )
    sync.add_argument(
        "--company-id",
        default=None,
        help="Limit to one company's feeds. Default: all active feeds.",
    )

    sub.add_parser(
        "refresh-feed-issues",
        help="Cache /sds/feedissues into bank_feed_issues.",
    )

    gen = sub.add_parser(
        "generate-recurring",
        help="Materialise every due RecurringInvoice as a DRAFT (or POSTED if auto_post).",
    )
    gen.add_argument(
        "--company-id",
        default=None,
        help="Limit to one company's templates. Default: all companies.",
    )
    gen.add_argument(
        "--as-of",
        default=None,
        help="Override today's date (ISO-format) — useful for catch-up runs.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "sync-feeds":
        return asyncio.run(_sync_feeds(args.company_id))
    if args.command == "refresh-feed-issues":
        return asyncio.run(_refresh_feed_issues())
    if args.command == "generate-recurring":
        return asyncio.run(
            _generate_recurring(args.company_id, args.as_of)
        )
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
