"""Ephemeral per-visit demo tenants — provision + reap.

Public-preview hosts (app.saebooks.com.au etc.) drop the old shared-DB
basic-auth demo in favour of a *fresh isolated tenant per visit*. On a root
visit with no valid ``sb_demo`` cookie, the web container calls
``POST /internal/demo/provision`` over the docker network; this module:

1. Enforces the per-IP provision rate-limit and the global tenant cap
   (reaping the oldest-idle demo first when at cap).
2. Creates a BRAND-NEW ``tenants`` row + ``companies`` row — a distinct
   ``tenant_id`` per visit, so two concurrent demos are RLS-isolated exactly
   like two real customers (FORCE RLS on every tenant table keys on
   ``app.current_tenant`` = the tenant_id). Company-per-visit alone would NOT
   isolate, because RLS is keyed on tenant_id, not company_id — hence
   tenant-per-visit.
3. Seeds the company (AU CoA + tax codes + a small, realistic demo dataset)
   via the existing service layer scoped to the new company. We RESEED rather
   than CLONE — see "Seeding: reseed, not clone" below.
4. Creates the company's own demo ``users`` row (NOT a shared account) and
   mints a JWT identical in shape to ``POST /api/v1/auth/login`` so the web
   app can carry it verbatim as ``Authorization: Bearer <token>`` against
   every ``/api/v1/*`` call and ``/auth/me``.
5. Inserts the ``ephemeral_demo_tenants`` control row.

A 60s background reaper (started from the FastAPI lifespan) HARD-DELETES demo
companies whose control row is idle (> ``DEMO_IDLE_TTL``) or aged out
(> ``DEMO_MAX_AGE``). The hard delete is GATED STRICTLY on
``ephemeral_demo_tenants`` membership, so it can never touch a real company.
Ephemeral demos are explicitly EXEMPT from the engine no-hard-delete policy —
they are throwaway scratch tenants, never real books.

Why bypass the edition company cap
----------------------------------
``services.companies.create_company`` enforces the edition company cap (pro = 3
companies). That cap is meaningless for ephemeral demos, which run up to
``DEMO_MAX_TENANTS`` (50) concurrent tenants and are governed by their own cap
+ per-IP rate-limit + reaper. So provisioning inserts the company directly
(the same INSERT ``create_company`` does, minus ``check_company``). This is a
deliberate, documented bypass scoped to the demo path only; the human
company-creation flows still go through ``create_company`` and its cap.

Seeding: reseed, not clone
--------------------------
The spec preferred CLONE-from-template for latency. A correct clone would have
to copy and FK-remap rows across ~20 tenant-scoped tables (accounts,
tax_codes, contacts, invoices + lines, journal_entries + lines, payments +
allocations, ...) under FORCE RLS — a large, fragile surface that drifts every
time a table is added. Reseed reuses the already-idempotent, service-layer
seeders (``load_au_coa``-style account load + ``ensure_au_seed`` tax codes +
contacts/invoices/quotes via their services), which post through the engine
(real records, never manual JEs) and stay correct as the schema evolves. The
per-company reseed is a CoA CSV load plus a handful of records — well within
acceptable provision latency for a demo. If profiling later shows this is too
slow, a template-clone can replace ``_seed_company_dataset`` without changing
the endpoint contract.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import LoginSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import ContactType
from saebooks.models.ephemeral_demo_tenant import EphemeralDemoTenant
from saebooks.models.tenant import Tenant
from saebooks.models.user import User, UserRole
from saebooks.services.jwt_tokens import hash_password, make_access_token

logger = logging.getLogger("saebooks.ephemeral_demo")


def _demo_token_ttl() -> int:
    """JWT TTL for a demo session — capped at the demo max age.

    A token can never outlive its tenant (the company is reaped at
    DEMO_MAX_AGE), so a stale ``sb_demo`` cookie self-heals into a fresh
    provision rather than presenting a token whose tenant no longer exists.
    Read live so a config override takes effect without a restart.
    """
    return int(settings.demo_max_age)


class DemoAtCapacity(Exception):
    """All demo slots full and none reapable — provision returns 503."""


class DemoRateLimited(Exception):
    """This source IP is over the per-minute provision budget — returns 429."""


class DemoDisabled(Exception):
    """Ephemeral demo provisioning is switched off — returns 503."""


@dataclass(frozen=True)
class ProvisionResult:
    """Return shape for a successful provision.

    ``access_token`` is byte-for-byte the same JWT shape POST /auth/login
    returns; the web app stores it and sends it as Authorization: Bearer on
    every downstream call. ``token_type`` / ``expires_in`` mirror
    ``TokenResponse`` so the endpoint response is a strict superset.
    """

    company_id: uuid.UUID
    tenant_id: uuid.UUID
    demo_user_email: str
    access_token: str
    expires_in: int


# --------------------------------------------------------------------------- #
# Per-IP provision rate-limit — in-process sliding window.                     #
# --------------------------------------------------------------------------- #
# A simple per-process dict of ip -> [monotonic timestamps within the window].
# The preview runs a single api worker, so a process-local limiter is
# sufficient and avoids a Redis dependency. It is advisory abuse protection,
# not a security boundary (the real boundary is the internal-only endpoint +
# the global cap). Bounded by pruning on every check.
_ip_hits: dict[str, list[float]] = {}
_RATE_WINDOW_SECONDS = 60.0


def _check_and_record_ip(source_ip: str | None) -> None:
    """Raise ``DemoRateLimited`` if ``source_ip`` is over budget; else record a hit.

    A ``None`` / empty IP is not rate-limited (we cannot attribute it); the
    global cap still applies. Per-IP limiting only kicks in when the web
    container forwards a client IP.
    """
    limit = int(settings.demo_provision_per_ip_per_min)
    if not source_ip or limit <= 0:
        return
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS
    hits = [t for t in _ip_hits.get(source_ip, []) if t >= cutoff]
    if len(hits) >= limit:
        _ip_hits[source_ip] = hits  # keep the pruned list
        raise DemoRateLimited(
            f"source_ip {source_ip} exceeded {limit} provisions/min"
        )
    hits.append(now)
    _ip_hits[source_ip] = hits


def _reset_rate_limiter() -> None:
    """Test hook — clear the in-process per-IP window."""
    _ip_hits.clear()


# --------------------------------------------------------------------------- #
# Provisioning                                                                 #
# --------------------------------------------------------------------------- #


def _short_token() -> str:
    """Short url-safe-ish token for naming the demo company / user."""
    return uuid.uuid4().hex[:8]


async def _count_demos(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(EphemeralDemoTenant.company_id))
            )
        ).scalar_one()
    )


async def _reap_oldest_idle(session: AsyncSession) -> bool:
    """Hard-delete the oldest *reapable* demo to free a cap slot.

    "Reapable" = idle (now - last_seen_at > DEMO_IDLE_TTL) OR aged
    (now - created_at > DEMO_MAX_AGE). We do NOT reap a still-active demo to
    make room: that would kill a visitor mid-session under load. So if every
    live demo is fresh, this returns False and the cap path correctly 503s
    ("demo_at_capacity") — matching the spec's "reap oldest-idle first; if
    still at cap → 503".

    Returns True if a demo was reaped. Commits within the call. Gated on
    ``ephemeral_demo_tenants`` membership by construction (it only selects from
    that table).
    """
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(seconds=int(settings.demo_idle_ttl))
    age_cutoff = now - timedelta(seconds=int(settings.demo_max_age))
    row = (
        await session.execute(
            select(EphemeralDemoTenant)
            .where(
                (EphemeralDemoTenant.last_seen_at < idle_cutoff)
                | (EphemeralDemoTenant.created_at < age_cutoff)
            )
            .order_by(EphemeralDemoTenant.last_seen_at.asc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return False
    await _hard_delete_demo_company(session, row.company_id)
    await session.commit()
    return True


async def provision(
    *,
    source_ip: str | None = None,
) -> ProvisionResult:
    """Provision a fresh, isolated, seeded demo tenant. See module docstring.

    Raises ``DemoDisabled`` / ``DemoRateLimited`` / ``DemoAtCapacity`` which
    the endpoint maps to 503 / 429 / 503.

    Uses the BYPASSRLS owner session (``LoginSessionLocal``) throughout: we are
    creating a brand-new tenant + its first user + seed rows, all before any
    JWT exists, so there is no ``app.current_tenant`` to bind. This mirrors the
    existing seed scripts (seed_dev / seed_cashbook_demo) which also seed under
    the owner role. The rows created carry the new tenant_id explicitly, so
    once the demo JWT is in play every subsequent request is FORCE-RLS scoped
    to this tenant like any real customer.
    """
    if not settings.demo_ephemeral_enabled:
        raise DemoDisabled("ephemeral demo provisioning disabled")

    _check_and_record_ip(source_ip)

    async with LoginSessionLocal() as session:
        # Cap check — reap oldest-idle then re-check; still full => 503.
        if await _count_demos(session) >= int(settings.demo_max_tenants):
            await _reap_oldest_idle(session)
            if await _count_demos(session) >= int(settings.demo_max_tenants):
                raise DemoAtCapacity("demo_at_capacity")

        token = _short_token()
        tenant_id = uuid.uuid4()
        company_id = uuid.uuid4()
        demo_email = f"demo+{token}@saebooks.example"

        # 1. New tenant (own isolation boundary) + company.
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Demo {token}",
                slug=f"demo-{token}",
                edition="pro",
            )
        )
        await session.flush()
        company = Company(
            id=company_id,
            tenant_id=tenant_id,
            name=f"Demo Pty Ltd ({token})",
            legal_name="Demo Pty Ltd",
            base_currency="AUD",
            fin_year_start_month=7,
            gst_registered=True,
            gst_effective_date=datetime(2020, 7, 1, tzinfo=UTC).date(),
            version=1,
        )
        session.add(company)
        await session.flush()

        # 2. The company's OWN demo user (admin so the demo can drive every
        #    feature). Password set but irrelevant — the web app authenticates
        #    via the returned JWT, not the password.
        demo_user = User(
            tenant_id=tenant_id,
            username=f"demo-{token}",
            display_name="Demo User",
            email=demo_email,
            role=UserRole.ADMIN.value,
            password_hash=hash_password(uuid.uuid4().hex),
            email_verified_at=datetime.now(UTC),
            version=1,
        )
        session.add(demo_user)

        # 3. Control row — created_at = last_seen_at = now (server default).
        session.add(
            EphemeralDemoTenant(
                company_id=company_id,
                source_ip=source_ip,
            )
        )
        await session.commit()
        await session.refresh(demo_user)

        # 4. Seed the dataset (CoA, tax codes, a few records). Done after the
        #    commit above so a seed hiccup leaves a usable empty tenant rather
        #    than rolling back the whole provision; seed errors are logged, not
        #    fatal (a demo with an empty ledger is still a valid demo).
        try:
            await _seed_company_dataset(
                session, tenant_id=tenant_id, company_id=company_id
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "demo seed failed for company %s (serving empty tenant): %r",
                company_id,
                exc,
            )

        # 5. Mint the session JWT — identical claims to /auth/login.
        access_token = make_access_token(
            demo_user, expires_in_seconds=_demo_token_ttl()
        )
        logger.info(
            "provisioned demo tenant=%s company=%s user=%s ip=%s",
            tenant_id,
            company_id,
            demo_email,
            source_ip,
        )
        return ProvisionResult(
            company_id=company_id,
            tenant_id=tenant_id,
            demo_user_email=demo_email,
            access_token=access_token,
            expires_in=_demo_token_ttl(),
        )


async def _seed_company_dataset(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
) -> None:
    """Load AU CoA + tax codes + a small demo dataset into the new company.

    Reuses the engine's existing service-layer seeders scoped to this company,
    so the demo data is real records (engine-posted), never manual JEs. Kept
    deliberately small for provision latency: full AU CoA, the six AU GST tax
    codes, two customers, and two posted invoices.

    tenant_id correctness
    ---------------------
    ``Account`` and ``TaxCode`` rows carry a NOT-NULL ``tenant_id`` whose
    *Python default is the shared DEFAULT tenant* — so ``_load_accounts`` /
    ``ensure_au_seed`` (which only set ``company_id``) would stamp the wrong
    tenant and break this demo's RLS isolation. We therefore (a) stamp
    ``session.info['tenant_id']`` so the ``before_flush`` backfill listener
    fills tenant_id on any row that left it None, and (b) belt-and-braces
    re-stamp the new tenant_id onto every account/tax_code for this company
    after the CSV load, since those models supply a non-None column default
    that the backfill listener skips. Records created through the invoice /
    contact services take ``tenant_id`` explicitly and need no fix-up.
    """
    from saebooks.models.account import Account, AccountType
    from saebooks.seed.load_au_coa import _load_accounts
    from saebooks.services import contacts as contacts_svc
    from saebooks.services import invoices as invoices_svc
    from saebooks.services.tax_codes import ensure_au_seed as ensure_tax_codes

    # Backfill source for any None tenant_id on flushed children.
    session.info["tenant_id"] = str(tenant_id)

    company = await session.get(Company, company_id)
    if company is None:  # pragma: no cover — provision just committed it
        return

    # 1. AU chart of accounts + tax codes (scoped to this company).
    await _load_accounts(session, company)
    await ensure_tax_codes(session, company_id)

    # 2. Re-stamp tenant_id on accounts + tax_codes — they were inserted with
    #    the models' DEFAULT tenant (the backfill listener only fills NULLs).
    #    This is what actually gives the demo's CoA RLS isolation.
    await session.execute(
        text(
            "UPDATE accounts SET tenant_id = :tid WHERE company_id = :cid"
        ).bindparams(tid=tenant_id, cid=company_id)
    )
    await session.execute(
        text(
            "UPDATE tax_codes SET tenant_id = :tid WHERE company_id = :cid"
        ).bindparams(tid=tenant_id, cid=company_id)
    )
    await session.commit()

    # 3a. Cashbook flavour: flip the company into cashbook mode and seed the
    #     ~30 sole-trader cashbook entries (reuses the cashbook-demo seeders,
    #     scoped to this new company), then stop — no saebooks invoice fixtures.
    #     Setup must run BEFORE any journal entry exists (full→cashbook is
    #     refused once a company has ledger history); the entries below are the
    #     first JEs and are posted in cashbook mode.
    if settings.demo_seed_flavour.strip().lower() == "cashbook":
        from saebooks.cli.seed_cashbook_demo import (
            _find_default_bank_account,
            _seed_entries,
        )
        from saebooks.services.cashbook import setup_cashbook_mode

        bank = await _find_default_bank_account(session, company_id)
        if bank is None:
            logger.warning(
                "cashbook demo seed: no bank account on company %s — serving "
                "a non-cashbook tenant",
                company_id,
            )
            return
        await setup_cashbook_mode(
            db=session,
            tenant_id=tenant_id,
            company_id=company_id,
            bank_account_id=bank.id,
            actor="demo-seed",
        )
        n = await _seed_entries(
            session, tenant_id=tenant_id, company_id=company_id
        )
        logger.info("cashbook demo seeded company=%s entries=%d", company_id, n)
        return

    # 3. A couple of customers + draft invoices so the demo isn't empty.
    #    Drafts (not posted) keep the seed fast and avoid the journal-posting
    #    tenant-propagation surface; the invoice service stamps tenant_id
    #    explicitly so these rows are correctly isolated.
    income = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.account_type == AccountType.INCOME,
                Account.is_header.is_(False),
            )
            .order_by(Account.code)
        )
    ).scalars().first()
    from saebooks.models.tax_code import TaxCode

    gst = (
        await session.execute(
            select(TaxCode).where(
                TaxCode.company_id == company_id, TaxCode.code == "GST"
            )
        )
    ).scalars().first()
    if income is None or gst is None:
        return

    today = datetime.now(UTC).date()
    customers = [
        ("Harbourview Cafe", "accounts@harbourview.example"),
        ("Northside Joinery", "ap@northside.example"),
    ]
    contact_ids: list[uuid.UUID] = []
    for name, email in customers:
        c = await contacts_svc.create(
            session,
            company_id,
            actor="demo-seed",
            tenant_id=tenant_id,
            name=name,
            contact_type=ContactType.CUSTOMER,
            email=email,
        )
        contact_ids.append(c.id)
    await session.commit()

    invoice_fixtures = [
        (contact_ids[0], 12, "Monthly bookkeeping — services", Decimal("1320.00")),
        (contact_ids[1], 5, "Cabinetry consultation", Decimal("660.00")),
    ]
    for contact_id, days_ago, desc, gross in invoice_fixtures:
        issue = today - timedelta(days=days_ago)
        unit_price = (gross / Decimal("1.10")).quantize(Decimal("0.01"))
        try:
            await invoices_svc.api_create(
                session,
                company_id,
                tenant_id,
                "demo-seed",
                contact_id=contact_id,
                issue_date=issue,
                due_date=issue + timedelta(days=14),
                lines=[
                    {
                        "description": desc,
                        "account_id": income.id,
                        "tax_code_id": gst.id,
                        "quantity": Decimal("1"),
                        "unit_price": unit_price,
                    }
                ],
                notes="[demo]",
            )
            await session.commit()
        except Exception as exc:  # pragma: no cover — best-effort demo data
            await session.rollback()
            logger.debug("demo invoice seed skip (%s): %r", desc, exc)


# Cached children-first delete order for tenant-scoped tables (those carrying a
# ``tenant_id`` column). The company cascade clears every company_id table, but
# tenant_id-scoped rows (users, change_log, audit, …) FK ``tenants`` with
# RESTRICT and would block the tenant delete — so they are purged first, in
# dependency order. Computed once from the live FK graph.
_TENANT_DELETE_ORDER: list[str] | None = None


async def _tenant_scoped_delete_order(session: AsyncSession) -> list[str]:
    """Public tables with a ``tenant_id`` column, ordered children-first (a table
    that FKs another in the set is deleted before it) so RESTRICT FKs are
    satisfied. Derived from the live FK graph; cached after first call."""
    global _TENANT_DELETE_ORDER
    if _TENANT_DELETE_ORDER is not None:
        return _TENANT_DELETE_ORDER
    tables = {
        r[0]
        for r in (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                )
            )
        ).all()
    }
    edges = (
        await session.execute(
            text(
                "SELECT src.relname AS child, tgt.relname AS parent "
                "FROM pg_constraint c "
                "JOIN pg_class src ON src.oid = c.conrelid "
                "JOIN pg_class tgt ON tgt.oid = c.confrelid "
                "WHERE c.contype = 'f' AND src.relname <> tgt.relname"
            )
        )
    ).all()
    # referencing[parent] = children (within the set) that FK it.
    referencing: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in edges:
        if child in tables and parent in tables:
            referencing[parent].add(child)
    remaining = set(tables)
    order: list[str] = []
    while remaining:
        deletable = sorted(
            t for t in remaining if not (referencing[t] & remaining)
        )
        if not deletable:  # FK cycle (e.g. companies<->accounts) — break it
            deletable = [sorted(remaining)[0]]
        for t in deletable:
            order.append(t)
            remaining.discard(t)
    _TENANT_DELETE_ORDER = order
    return order


async def _hard_delete_demo_company(
    session: AsyncSession, company_id: uuid.UUID
) -> None:
    """Physically delete a demo company + everything FK-cascaded to it.

    GATING INVARIANT: callers MUST have established that ``company_id`` is a
    member of ``ephemeral_demo_tenants`` before calling this. The reaper and
    the cap path both select the company_id straight from that table, so the
    gate holds by construction. As belt-and-braces this function re-checks
    membership and refuses to delete a company that is not an ephemeral demo —
    so a real company can NEVER be hard-deleted through this path, even if a
    future caller passes the wrong id.

    Ephemeral demos are explicitly exempt from the engine no-hard-delete
    policy (they are throwaway scratch tenants). The delete cascades through
    the companies FK (ON DELETE CASCADE on tenant tables) and drops the
    control row via its own ON DELETE CASCADE. The now-orphaned tenant row is
    removed last.
    """
    is_demo = (
        await session.execute(
            select(EphemeralDemoTenant.company_id).where(
                EphemeralDemoTenant.company_id == company_id
            )
        )
    ).scalar_one_or_none()
    if is_demo is None:
        # Not an ephemeral demo — refuse. This is the hard guard against ever
        # touching a real company.
        logger.error(
            "refusing hard-delete: company %s is not in ephemeral_demo_tenants",
            company_id,
        )
        raise ValueError(
            f"company {company_id} is not an ephemeral demo tenant"
        )

    company = await session.get(Company, company_id)
    tenant_id = company.tenant_id if company is not None else None

    # Delete the control row first (explicit, even though the company delete
    # would cascade it) so partial failures still de-register the demo.
    await session.execute(
        delete(EphemeralDemoTenant).where(
            EphemeralDemoTenant.company_id == company_id
        )
    )
    # Teardown is a sanctioned rebuild-class operation — declare it so the raw
    # DELETEs below do not trip the ORM/trigger guards (e.g. je_engine_guard)
    # that protect *real* books.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(text("SET LOCAL app.db_rebuild = 'on'"))

    # FK-topological pre-delete. A plain `DELETE FROM companies` CANNOT rely on
    # the company_id ON DELETE CASCADE alone: ~25 transactional/line tables carry
    # ON DELETE *RESTRICT* FKs to `accounts` (e.g. invoice_lines.account_id,
    # journal_lines.account_id), and Postgres doesn't guarantee it removes those
    # RESTRICT-referencing rows before the accounts they point at within one
    # cascade — so the cascade aborts with a FK violation. AND a cashbook demo
    # pins `companies.cashbook_default_bank_account_id → accounts (RESTRICT)`,
    # which blocks deleting that account while the company row still exists.
    #
    # We pre-delete the demo's RESTRICT-bearing line rows up front:
    #   saebooks flavour → invoice_lines (draft invoices; no company_id, via parent)
    #   cashbook flavour → journal_lines (posted cashbook entries)
    # After that the company delete cascades every remaining company_id table
    # cleanly. If a future seed posts bills/payments, pre-delete THEIR lines too —
    # the reap test (both flavours) fails loudly if a dangling RESTRICT row blocks
    # the cascade.
    #
    # NOTE a cashbook company ALSO holds companies.cashbook_default_bank_account_id
    # → accounts RESTRICT, but that needs NO handling here: the company row is the
    # *referencing child*, so DELETE FROM companies removes it first (as the
    # cascade root), and its accounts cascade-delete only afterwards, by which
    # point no company references them. We must NOT null that column while the
    # company is still in cashbook mode — the ck_cashbook_requires_bank CHECK
    # constraint forbids a cashbook company with a null default bank account.
    await session.execute(
        text(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id = :cid)"
        ).bindparams(cid=company_id)
    )
    await session.execute(
        text("DELETE FROM journal_lines WHERE company_id = :cid").bindparams(
            cid=company_id
        )
    )
    await session.execute(
        text("DELETE FROM companies WHERE id = :cid").bindparams(cid=company_id)
    )

    # Tenant-scoped rows (users, change_log, audit, …) are NOT reached by the
    # company cascade and FK `tenants` with RESTRICT — so they block the tenant
    # delete (e.g. change_log_tenant_id_fkey). Purge every tenant_id-scoped table
    # for this tenant, children-first, then drop the tenant. Most tables in the
    # order are already empty (the company cascade cleared their company_id rows)
    # so those deletes are harmless no-ops; the ones that matter are the
    # tenant-global tables (users, change_log, …). Guard the shared default
    # tenant so a mis-tagged demo can never touch it.
    _DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
    if tenant_id is not None and tenant_id != _DEFAULT_TENANT:
        for tbl in await _tenant_scoped_delete_order(session):
            await session.execute(
                text(f"DELETE FROM {tbl} WHERE tenant_id = :tid").bindparams(
                    tid=tenant_id
                )
            )
        await session.execute(
            text("DELETE FROM tenants WHERE id = :tid").bindparams(tid=tenant_id)
        )


# --------------------------------------------------------------------------- #
# Touch — bump last_seen_at / request_count on demo-session requests.         #
# --------------------------------------------------------------------------- #


async def touch_by_tenant(tenant_id: uuid.UUID) -> None:
    """Bump ``last_seen_at`` (= now) + ``request_count`` for the demo owned by ``tenant_id``.

    Ephemeral demos are 1:1 tenant↔company, so we match the control row via the
    company that belongs to this tenant. No-op when the tenant is not an
    ephemeral demo (the subquery matches no company). Runs under the owner
    session (the control table is global / RLS-exempt). Best-effort: never
    raises into the request path.
    """
    try:
        async with LoginSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE ephemeral_demo_tenants e "
                    "SET last_seen_at = now(), request_count = e.request_count + 1 "
                    "FROM companies c "
                    "WHERE c.id = e.company_id AND c.tenant_id = :tid"
                ).bindparams(tid=tenant_id)
            )
            await session.commit()
    except Exception as exc:  # pragma: no cover — never break the request
        logger.debug("demo touch failed for tenant %s: %r", tenant_id, exc)


async def is_demo_tenant(tenant_id: uuid.UUID) -> bool:
    """True if ``tenant_id`` owns a live ephemeral demo company.

    Used by the touch middleware to decide whether to bump last_seen_at. Owner
    session; best-effort (returns False on any error).
    """
    try:
        async with LoginSessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT 1 FROM ephemeral_demo_tenants e "
                        "JOIN companies c ON c.id = e.company_id "
                        "WHERE c.tenant_id = :tid LIMIT 1"
                    ).bindparams(tid=tenant_id)
                )
            ).first()
            return row is not None
    except Exception:  # pragma: no cover — fail closed (treat as non-demo)
        return False


# --------------------------------------------------------------------------- #
# Reaper                                                                       #
# --------------------------------------------------------------------------- #


async def reap_once() -> list[uuid.UUID]:
    """Hard-delete every demo whose control row is idle or aged out.

    Selection (under the owner session, all tenants visible):
      idle:  now - last_seen_at > DEMO_IDLE_TTL
      aged:  now - created_at  > DEMO_MAX_AGE

    Returns the list of reaped company_ids. Each delete is gated on
    ``ephemeral_demo_tenants`` membership (the select source), so a real
    company is never reaped. Per-company failures are logged and skipped so one
    bad row cannot stall the sweep.
    """
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(seconds=int(settings.demo_idle_ttl))
    age_cutoff = now - timedelta(seconds=int(settings.demo_max_age))

    reaped: list[uuid.UUID] = []
    async with LoginSessionLocal() as session:
        rows = (
            await session.execute(
                select(EphemeralDemoTenant).where(
                    (EphemeralDemoTenant.last_seen_at < idle_cutoff)
                    | (EphemeralDemoTenant.created_at < age_cutoff)
                )
            )
        ).scalars().all()
        for row in rows:
            cid = row.company_id
            try:
                await _hard_delete_demo_company(session, cid)
                await session.commit()
                reaped.append(cid)
            except Exception as exc:
                await session.rollback()
                logger.warning("reap of demo %s failed: %r", cid, exc)
    if reaped:
        logger.info("reaper hard-deleted %d demo tenant(s): %s", len(reaped), reaped)
    return reaped


async def run_reaper_loop(stop) -> None:  # type: ignore[no-untyped-def]
    """Background loop: sweep every ``DEMO_REAPER_INTERVAL`` seconds until stop.

    ``stop`` is an ``asyncio.Event`` set on shutdown. Each iteration is fully
    guarded so a transient DB error logs and retries on the next tick rather
    than killing the loop. Started/stopped from the FastAPI lifespan.
    """
    import asyncio
    import contextlib

    interval = max(1, int(settings.demo_reaper_interval))
    logger.info("ephemeral demo reaper started (interval=%ss)", interval)
    while not stop.is_set():
        try:
            await reap_once()
        except Exception as exc:  # pragma: no cover — keep the loop alive
            logger.warning("reaper sweep error: %r", exc)
        # Sleep ``interval`` seconds OR wake early on shutdown.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    logger.info("ephemeral demo reaper stopped")
