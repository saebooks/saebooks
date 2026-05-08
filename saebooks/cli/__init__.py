"""Command-line entry points for SAE Books background jobs.

Invoked like::

    python -m saebooks.cli sync-feeds
    python -m saebooks.cli refresh-feed-issues
    python -m saebooks.cli sync-feeds --company-id <uuid>
    python -m saebooks.cli generate-recurring
    python -m saebooks.cli generate-recurring --company-id <uuid>
    python -m saebooks.cli reconcile-feeds
    python -m saebooks.cli reconcile-feeds --company-id <uuid>
    python -m saebooks.cli fx-revalue
    python -m saebooks.cli fx-revalue --through 2026-03-31
    python -m saebooks.cli fx-revalue --company-id <uuid> --through 2026-03-31
    python -m saebooks.cli bootstrap-admin --email you@example.com --name "You"

Designed to be kicked by plain cron — no long-running worker, no queue
runtime. Exits 0 on success, 1 on total failure; per-account errors are
logged but don't kill the whole run (so one flakey bank doesn't stop
the others from syncing). ``reconcile-feeds`` is an exception: it exits
non-zero if any account's variance exceeds :data:`.reconcile.VARIANCE_TOLERANCE`
so cron alerting can page when feeds drift from the GL.

RLS plumbing (``sync-feeds``, ``refresh-feed-issues``)
------------------------------------------------------
The two cross-tenant CLI walkers run as the non-BYPASSRLS
``saebooks_app`` role and set ``app.current_tenant`` per group via
``SET LOCAL`` inside a transaction. They refuse to start if the
connecting role can bypass RLS — passing ``--allow-bypass`` is the
explicit override (used in dev or a one-off owner-role rescue).

Cross-tenant enumeration goes through the SECURITY DEFINER function
``bank_feeds_active_accounts_for_sync()`` (migration 0084) so the
NOBYPASSRLS role can still discover what tenants need iterating.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func as sa_func
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saebooks.config import settings
from saebooks.db import AppSessionLocal, AsyncSessionLocal
from saebooks.services import recurrence
from saebooks.services.bank_feeds import health, onboarding, reconcile
from saebooks.services.fx import reval as fx_reval

logger = logging.getLogger("saebooks.cli")


# --------------------------------------------------------------------------- #
# RLS guards                                                                  #
# --------------------------------------------------------------------------- #


class BypassRoleRefused(RuntimeError):
    """Raised when a cross-tenant CLI walker is invoked under a BYPASSRLS role
    without the ``--allow-bypass`` opt-out.
    """


async def _assert_not_bypass(session: AsyncSession) -> None:
    """Refuse to keep going if the connected role bypasses RLS.

    Background: once migration 0056 split the DB role, the runtime path
    must connect as ``saebooks_app`` (NOBYPASSRLS). If the CLI is run
    against an owner-role URL by mistake, the per-tenant
    ``SET LOCAL app.current_tenant`` becomes decorative — every SELECT
    would still return cross-tenant data — and the operator would not
    notice until a tenant complains. Failing loudly here is the point.

    The query reads ``rolbypassrls`` from ``pg_roles`` for
    ``current_user``. Superuser short-circuits BYPASSRLS in Postgres
    so we OR ``is_superuser`` into the check.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT rolsuper, rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
        )
    ).first()
    if row is None:
        # Defensive: current_user must exist in pg_roles. If not,
        # something is very wrong; bail loudly.
        raise BypassRoleRefused(
            "could not resolve current_user against pg_roles — refusing to run"
        )
    rolsuper, rolbypassrls = bool(row[0]), bool(row[1])
    if rolsuper or rolbypassrls:
        raise BypassRoleRefused(
            "Refusing to run cross-tenant CLI under a BYPASSRLS / SUPERUSER role "
            f"(current_user has rolsuper={rolsuper}, rolbypassrls={rolbypassrls}). "
            "Set SAEBOOKS_APP_DATABASE_URL to a saebooks_app URL, or pass "
            "--allow-bypass to override (NOT for production)."
        )


def _resolve_session_factory(
    *, allow_bypass: bool
) -> async_sessionmaker[AsyncSession]:
    """Pick the session factory for cross-tenant CLI work.

    Default: the strict ``AppSessionLocal`` keyed off
    ``SAEBOOKS_APP_DATABASE_URL``. Missing env var = exit early with
    a clear message; we will not silently use the BYPASSRLS owner.

    With ``--allow-bypass``: fall back to ``AsyncSessionLocal`` which
    follows the regular fallback chain. The bypass-role refusal is
    also lifted (see callers).
    """
    if AppSessionLocal is None:
        if allow_bypass:
            logger.warning(
                "SAEBOOKS_APP_DATABASE_URL is unset; --allow-bypass is set so "
                "falling back to DATABASE_URL. RLS enforcement is OFF for this run."
            )
            return AsyncSessionLocal
        raise RuntimeError(
            "SAEBOOKS_APP_DATABASE_URL is not set. The sync-feeds CLI refuses "
            "to run under the BYPASSRLS owner role. Set the env var to a "
            "saebooks_app DSN, or pass --allow-bypass to override (NOT for production)."
        )
    return AppSessionLocal


# --------------------------------------------------------------------------- #
# sync-feeds                                                                  #
# --------------------------------------------------------------------------- #


async def _enumerate_active_groups(
    session: AsyncSession,
    *,
    company_id: uuid.UUID | None,
) -> dict[tuple[uuid.UUID, uuid.UUID], list[uuid.UUID]]:
    """Return ``{(company_id, tenant_id): [account_id, ...]}`` for all active
    feed accounts, optionally filtered to one company.

    Uses the SECURITY DEFINER enumerator
    ``bank_feeds_active_accounts_for_sync()`` so the NOBYPASSRLS
    ``saebooks_app`` role can discover cross-tenant rows. Filtering by
    company is applied client-side (not via SQL parameter into the
    function) — the function returns ``(company_id, tenant_id, account_id)``
    triples and we group + filter in Python. This keeps the function's
    contract minimal; the cost is one extra row per call which is trivial
    versus the cost of a sync pass.
    """
    rows = (
        await session.execute(
            text("SELECT * FROM bank_feeds_active_accounts_for_sync()")
        )
    ).all()
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list[uuid.UUID]] = defaultdict(list)
    for row in rows:
        cid, tid, aid = row[0], row[1], row[2]
        if company_id is not None and cid != company_id:
            continue
        grouped[(cid, tid)].append(aid)
    return grouped


async def _sync_feeds(company_id: str | None, *, allow_bypass: bool) -> int:
    """Run sync_all_active grouped per-tenant under RLS; return exit code.

    Algorithm:
        1. Open a session against the strict app role.
        2. Refuse to continue if the role bypasses RLS (unless
           ``--allow-bypass`` was passed).
        3. Enumerate ``(company_id, tenant_id, account_id)`` via the
           SECDEF function.
        4. For each ``(company_id, tenant_id)`` group: open a fresh
           transaction, ``SET LOCAL app.current_tenant``, call the
           existing per-company ``sync_all_active`` so per-account
           errors get their normal logging path, commit.
        5. Aggregate outcomes for the summary log line.
    """
    cid = uuid.UUID(company_id) if company_id else None

    try:
        SessionFactory = _resolve_session_factory(allow_bypass=allow_bypass)
    except RuntimeError as exc:
        logger.error("sync-feeds: %s", exc)
        return 2

    async with SessionFactory() as enum_session:
        if not allow_bypass:
            try:
                await _assert_not_bypass(enum_session)
            except BypassRoleRefused as exc:
                logger.error("sync-feeds: %s", exc)
                return 2
        try:
            who = (
                await enum_session.execute(text("SELECT current_user"))
            ).scalar_one()
            logger.info("sync-feeds: connected as DB role=%s", who)
        except Exception:  # pragma: no cover — diagnostic only
            who = "<unknown>"

        try:
            groups = await _enumerate_active_groups(
                enum_session, company_id=cid
            )
        except onboarding.SissNotConfiguredError as exc:
            logger.error("sync-feeds: %s", exc)
            return 1

    if not groups:
        logger.info(
            "sync-feeds: 0 active feed accounts (company filter=%s)",
            company_id or "all",
        )
        return 0

    logger.info(
        "sync-feeds: %d (company,tenant) group(s), %d account(s) total",
        len(groups),
        sum(len(v) for v in groups.values()),
    )

    all_outcomes: list[onboarding.SyncOutcome] = []
    failed_groups = 0

    for (group_company_id, tenant_id), account_ids in groups.items():
        try:
            async with SessionFactory() as session:
                # SET LOCAL only lasts for the current transaction. We
                # open one explicitly so the GUC is bound, then the
                # nested sync_all_active runs all its writes inside
                # the same transaction and commits at the bottom.
                async with session.begin():
                    await session.execute(
                        text(
                            "SELECT set_config('app.current_tenant', :tid, true)"
                        ),
                        {"tid": str(tenant_id)},
                    )
                    logger.info(
                        "sync-feeds: company=%s tenant=%s accounts=%d",
                        group_company_id,
                        tenant_id,
                        len(account_ids),
                    )
                    outcomes = await onboarding.sync_all_active(
                        session,
                        company_id=group_company_id,
                        settings=settings,
                    )
                    all_outcomes.extend(outcomes)
        except onboarding.SissNotConfiguredError as exc:
            # SISS misconfig is global — abort, rather than logging
            # the same error N times once per tenant.
            logger.error("sync-feeds: %s", exc)
            return 1
        except Exception as exc:  # noqa: BLE001
            failed_groups += 1
            logger.exception(
                "sync-feeds: company=%s tenant=%s failed: %s",
                group_company_id,
                tenant_id,
                exc,
            )

    total_new = sum(o.lines_inserted for o in all_outcomes)
    total_seen = sum(o.transactions_seen for o in all_outcomes)
    logger.info(
        "sync-feeds: %d account(s), %d txns seen, %d new lines, %d group(s) failed",
        len(all_outcomes),
        total_seen,
        total_new,
        failed_groups,
    )
    for outcome in all_outcomes:
        logger.info(
            "  account=%s seen=%d new=%d cursor=%s",
            outcome.bank_feed_account_id,
            outcome.transactions_seen,
            outcome.lines_inserted,
            outcome.cursor_advanced_to or "(unchanged)",
        )
    return 1 if failed_groups else 0


# --------------------------------------------------------------------------- #
# generate-recurring (unchanged — uses owner session for now)                 #
# --------------------------------------------------------------------------- #


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


async def _reconcile_feeds(company_id: str | None) -> int:
    """Walk every active BankFeedAccount and log health per account.

    Exits 1 if any account has a variance >$0.01 or is stale — so cron
    can alert on feed drift. Per-company errors don't abort the loop.
    """

    cid = uuid.UUID(company_id) if company_id else None
    worst = "ok"
    async with AsyncSessionLocal() as session:
        if cid is not None:
            reports = [await reconcile.sweep(session, company_id=cid)]
        else:
            reports = await reconcile.sweep_all_companies(session)

    total_accounts = sum(len(r.accounts) for r in reports)
    logger.info(
        "reconcile-feeds: %d company/ies, %d feed account(s)",
        len(reports),
        total_accounts,
    )
    for r in reports:
        for a in r.accounts:
            log = logger.error if a.severity == "error" else logger.info
            log("  company=%s %s", r.company_id, reconcile._fmt_report_line(a))
            if a.severity == "error":
                worst = "error"
            elif a.severity == "warn" and worst == "ok":
                worst = "warn"
    return 1 if worst == "error" else 0


async def _fx_revalue(company_id: str | None, through: str | None) -> int:
    """Run ``revalue_company`` (or all companies) — return exit code."""
    cid = uuid.UUID(company_id) if company_id else None
    through_date = date.fromisoformat(through) if through else date.today()
    try:
        async with AsyncSessionLocal() as session:
            if cid is not None:
                results: dict[uuid.UUID, fx_reval.RevalResult] = {
                    cid: await fx_reval.revalue_company(
                        session,
                        company_id=cid,
                        through_date=through_date,
                    )
                }
            else:
                results = await fx_reval.revalue_all_companies(
                    session, through_date=through_date
                )
            await session.commit()
    except fx_reval.FxRevalError as exc:
        logger.error("fx-revalue: %s", exc)
        return 1

    total_posted = sum(r.posted_count for r in results.values())
    logger.info(
        "fx-revalue: %d company/ies, %d adjustment pair(s) posted through %s",
        len(results),
        total_posted,
        through_date.isoformat(),
    )
    for company_id_, result in results.items():
        logger.info(
            "  company=%s posted=%d skipped=%s zero=%s",
            company_id_,
            result.posted_count,
            result.skipped_currencies or "-",
            result.zero_currencies or "-",
        )
    return 0


async def _bootstrap_admin(
    *,
    email: str,
    name: str | None,
    ttl_hours: int,
    force: bool,
) -> int:
    """Create the first owner user + Default tenant; print a one-time bearer token.

    Idempotency: refuses if any users already exist (override with ``--force``).
    Self-hosters run this once after ``docker compose up`` to get a JWT they
    can paste into the desktop client's first-run wizard or use as a
    ``curl -H 'Authorization: Bearer …'`` header.

    Exit codes:
        0 — created and token printed
        1 — already bootstrapped (users exist)
        2 — misconfigured (bad email, no SAEBOOKS_SECRET_KEY, DB unreachable)
    """
    # Late imports — keep startup cost off the other CLI commands.
    from saebooks.models.tenant import Tenant
    from saebooks.models.user import User, UserRole
    from saebooks.services.jwt_tokens import make_access_token

    email_lc = (email or "").strip().lower()
    if "@" not in email_lc or len(email_lc) < 3:
        logger.error("bootstrap-admin: invalid email %r", email)
        return 2

    # Without a stable secret key the printed token signs under an
    # ephemeral per-process key and the API server (different process)
    # can't verify it. Make this loud.
    if not settings.secret_key.strip():
        logger.error(
            "bootstrap-admin: SAEBOOKS_SECRET_KEY is not set. The token printed "
            "would be signed under an ephemeral per-process key and the API server "
            "could not verify it. Set SAEBOOKS_SECRET_KEY (a 32+ char random string) "
            "in the API container's environment, restart, then rerun this command."
        )
        return 2

    DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    try:
        async with AsyncSessionLocal() as session:
            # Idempotency guard — refuse if any user rows exist.
            existing_count = (
                await session.execute(
                    select(sa_func.count()).select_from(User)
                )
            ).scalar_one()
            if existing_count > 0 and not force:
                logger.error(
                    "bootstrap-admin: refusing to run — %d user row(s) already "
                    "exist. Use --force to create another owner anyway "
                    "(NOT recommended for normal use).",
                    existing_count,
                )
                return 1

            # Reuse the Default tenant if migrations seeded it; otherwise create it.
            tenant = (
                await session.execute(
                    select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)
                )
            ).scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(
                    id=DEFAULT_TENANT_ID,
                    name=name or "Default",
                    slug="default",
                )
                session.add(tenant)
                await session.flush()

            # If --force and the email already exists, surface a clean error
            # rather than letting the unique-constraint blow up the session.
            existing_user = (
                await session.execute(
                    select(User).where(User.username == email_lc)
                )
            ).scalar_one_or_none()
            if existing_user is not None:
                logger.error(
                    "bootstrap-admin: a user with username=%s already exists "
                    "(id=%s, role=%s). Pick a different email or remove the "
                    "row first.",
                    email_lc,
                    existing_user.id,
                    existing_user.role,
                )
                return 1

            user = User(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                username=email_lc,
                email=email_lc,
                display_name=name or email_lc.split("@", 1)[0],
                role=UserRole.OWNER.value,
                # Pre-verified — the operator runs this from a trusted shell;
                # forcing them through the email-verification flow would be
                # daft when no SMTP is wired up yet.
                email_verified_at=datetime.now(timezone.utc),
                password_version=0,
                version=1,
            )
            session.add(user)
            await session.commit()

            token = make_access_token(
                user, expires_in_seconds=ttl_hours * 3600
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("bootstrap-admin: %s", exc)
        return 2

    bar = "=" * 72
    print()
    print(bar)
    print("SAE Books — bootstrap successful")
    print(bar)
    print(f"  Email     : {email_lc}")
    print(f"  Tenant    : {tenant.name}  ({tenant.id})")
    print(f"  User ID   : {user.id}")
    print("  Role      : owner")
    print(f"  Token TTL : {ttl_hours}h")
    print(bar)
    print()
    print("Bearer token (paste into the desktop first-run wizard, or use as")
    print("`Authorization: Bearer <token>`):")
    print()
    print(f"  {token}")
    print()
    print("Smoke test:")
    print(f"  curl -H 'Authorization: Bearer {token}' \\")
    print("       http://localhost:8000/api/v1/companies")
    print()
    print("Token rotates with the next password reset. To mint a fresh one")
    print("use the /auth/login endpoint after setting a password in the UI.")
    print()
    return 0


async def _refresh_feed_issues(*, allow_bypass: bool) -> int:
    """Cache /sds/feedissues into bank_feed_issues.

    ``health.refresh_feed_issues`` opens its own session internally so
    the RLS guard here is informational rather than enforcing. We still
    log the role for parity with sync-feeds.
    """
    if not allow_bypass and AppSessionLocal is not None:
        async with AppSessionLocal() as session:
            try:
                await _assert_not_bypass(session)
            except BypassRoleRefused as exc:
                logger.error("refresh-feed-issues: %s", exc)
                return 2
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
    sync.add_argument(
        "--allow-bypass",
        action="store_true",
        default=False,
        help="Override the BYPASSRLS-role refusal. NOT for production use.",
    )

    rfi = sub.add_parser(
        "refresh-feed-issues",
        help="Cache /sds/feedissues into bank_feed_issues.",
    )
    rfi.add_argument(
        "--allow-bypass",
        action="store_true",
        default=False,
        help="Override the BYPASSRLS-role refusal. NOT for production use.",
    )

    rec = sub.add_parser(
        "reconcile-feeds",
        help="Walk active feeds, flag stale accounts + GL variance. "
        "Exits 1 if any account has variance >$0.01.",
    )
    rec.add_argument(
        "--company-id",
        default=None,
        help="Limit to one company. Default: all active feeds.",
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

    boot = sub.add_parser(
        "bootstrap-admin",
        help="Create the first owner user + Default tenant and print a "
        "long-lived bearer token. Self-host first-run.",
    )
    boot.add_argument(
        "--email",
        required=True,
        help="Owner email address (becomes the username).",
    )
    boot.add_argument(
        "--name",
        default=None,
        help="Display name for the owner / Default tenant. Default: local-part of email.",
    )
    boot.add_argument(
        "--ttl-hours",
        type=int,
        default=24 * 30,
        help="Bearer token lifetime in hours. Default: 720 (30 days).",
    )
    boot.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Create another owner even if users already exist. NOT recommended.",
    )

    fx = sub.add_parser(
        "fx-revalue",
        help="Post month-end FX revaluation (adjusting + reversing pair per "
        "foreign currency with open AR/AP).",
    )
    fx.add_argument(
        "--company-id",
        default=None,
        help="Limit to one company. Default: all active companies.",
    )
    fx.add_argument(
        "--through",
        default=None,
        help="Revaluation date (ISO-format). Default: today.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "sync-feeds":
        return asyncio.run(
            _sync_feeds(args.company_id, allow_bypass=args.allow_bypass)
        )
    if args.command == "refresh-feed-issues":
        return asyncio.run(
            _refresh_feed_issues(allow_bypass=args.allow_bypass)
        )
    if args.command == "generate-recurring":
        return asyncio.run(
            _generate_recurring(args.company_id, args.as_of)
        )
    if args.command == "reconcile-feeds":
        return asyncio.run(_reconcile_feeds(args.company_id))
    if args.command == "fx-revalue":
        return asyncio.run(_fx_revalue(args.company_id, args.through))
    if args.command == "bootstrap-admin":
        return asyncio.run(
            _bootstrap_admin(
                email=args.email,
                name=args.name,
                ttl_hours=args.ttl_hours,
                force=args.force,
            )
        )
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
