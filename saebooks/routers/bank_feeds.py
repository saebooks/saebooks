"""Bank-feeds onboarding + sync UI.

Mounted at ``/admin/bank-feeds``. Every route is gated by
``require_feature(FLAG_BANK_FEEDS)`` — Community builds get a 404 on the
whole tree so the feature isn't even advertised.

Flow:

1. ``GET /admin/bank-feeds/``            — landing page: list connected
   accounts (if any), or a "Connect a bank" CTA
2. ``GET /admin/bank-feeds/connect``     — form: institution + variant
3. ``POST /admin/bank-feeds/connect``    — initiate SISS consent; show
   the hosted redirect URL the user must visit
4. ``GET /admin/bank-feeds/callback``    — SISS redirectURI target;
   discovers accounts, renders the CoA mapper
5. ``POST /admin/bank-feeds/link``       — persists per-account CoA
6. ``POST /admin/bank-feeds/sync``       — "Sync now" button
7. ``POST /admin/bank-feeds/<id>/revoke``— per-account revoke
8. ``POST /admin/bank-feeds/offboard``   — whole-company offboard

Designed to degrade gracefully: if SISS env vars are missing, the landing
page shows a "SISS not configured" banner rather than 500-ing. This lets
the admin explore the UI before credentials land.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.company import Company
from saebooks.services import crypto as crypto_svc
from saebooks.services.bank_feeds import onboarding
from saebooks.services.bank_feeds.errors import SissError
from saebooks.services.bank_feeds.token import TokenCache
from saebooks.services.features import (
    FLAG_BANK_FEEDS,
    FLAG_PER_COMPANY_SISS,
    is_enabled,
    require_feature,
)
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(
    prefix="/admin/bank-feeds",
    dependencies=[Depends(require_feature(FLAG_BANK_FEEDS))],
)


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _bank_accounts_for_mapping(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Account]:
    """Cash/bank accounts that a feed can map to.

    Heuristic: code prefix ``1-1`` (AU CoA bank/cash bucket). Good
    enough for default CoA; the list UI just displays them all.
    """
    rows = await session.execute(
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.is_header.is_(False),
            Account.archived_at.is_(None),
            Account.code.like("1-1%"),
        )
        .order_by(Account.code)
    )
    return list(rows.scalars().all())


async def _feed_accounts_for_company(
    session: AsyncSession, company_id: uuid.UUID
) -> list[BankFeedAccount]:
    rows = await session.execute(
        select(BankFeedAccount)
        .where(BankFeedAccount.company_id == company_id)
        .order_by(BankFeedAccount.created_at)
    )
    return list(rows.scalars().all())


async def _feed_client_for_company(
    session: AsyncSession, company_id: uuid.UUID
) -> BankFeedClient | None:
    row = await session.execute(
        select(BankFeedClient).where(BankFeedClient.company_id == company_id)
    )
    return row.scalar_one_or_none()


def _callback_redirect_uri(request: Request) -> str:
    """Absolute URL SISS should send the user back to after consent."""
    return str(request.url_for("bank_feeds_callback"))


# ---------------------------------------------------------------------- #
# Landing                                                                #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def bank_feeds_index(
    request: Request,
    message: str | None = Query(None),
    error: str | None = Query(None),
) -> HTMLResponse:
    """Landing page: list feeds + "Connect a bank" CTA."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        client = await _feed_client_for_company(session, company.id)
        feed_accounts = await _feed_accounts_for_company(session, company.id)
        ledger_accounts = await _bank_accounts_for_mapping(session, company.id)
        # Build an account-id -> Account lookup for display
        ledger_by_id = {a.id: a for a in ledger_accounts}

    return templates.TemplateResponse(
        request,
        "bank_feeds/index.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "siss_configured": onboarding.siss_configured(settings),
            "per_company_creds_enabled": is_enabled(
                FLAG_PER_COMPANY_SISS, settings=settings
            ),
            "bank_feed_client": client,
            "feed_accounts": feed_accounts,
            "ledger_by_id": ledger_by_id,
            "message": message,
            "error": error,
        },
    )


# ---------------------------------------------------------------------- #
# Connect flow                                                           #
# ---------------------------------------------------------------------- #


@router.get("/connect", response_class=HTMLResponse)
async def bank_feeds_connect_form(request: Request) -> HTMLResponse:
    company = await _first_company()
    return templates.TemplateResponse(
        request,
        "bank_feeds/connect.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "siss_configured": onboarding.siss_configured(settings),
            "error": None,
            "redirect_uri": _callback_redirect_uri(request),
        },
    )


@router.post("/connect", response_class=HTMLResponse)
async def bank_feeds_connect_submit(
    request: Request,
    institution_id: str = Form(...),
    variant: str = Form("consumer"),
) -> HTMLResponse:
    """Call SISS to begin the consent dance and display the hosted redirect URL.

    We render the URL rather than 302-ing straight to it — lets the
    admin copy it to a different device (the bank may not accept a
    tailnet-hosted callback from the admin's laptop) and makes the flow
    easier to debug in sandbox. Plan default (Q2): ``consumer``.
    """
    company = await _first_company()
    try:
        initiation = await onboarding.initiate_consent(
            settings=settings,
            institution_id=institution_id,
            redirect_uri=_callback_redirect_uri(request),
            variant=variant,
        )
    except (onboarding.SissNotConfiguredError, SissError) as exc:
        return templates.TemplateResponse(
            request,
            "bank_feeds/connect.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "siss_configured": onboarding.siss_configured(settings),
                "error": str(exc),
                "redirect_uri": _callback_redirect_uri(request),
            },
            status_code=502,
        )
    return templates.TemplateResponse(
        request,
        "bank_feeds/connect_redirect.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "initiation": initiation,
            "institution_id": institution_id,
            "variant": variant,
        },
    )


# ---------------------------------------------------------------------- #
# Callback — user came back from SISS                                    #
# ---------------------------------------------------------------------- #


@router.get("/callback", name="bank_feeds_callback")
async def bank_feeds_callback(
    request: Request,
    sdsClientId: str | None = Query(None),
    consentId: str | None = Query(None),
    error: str | None = Query(None),
) -> Response:
    """Discover accounts for the returned ``sdsClientId`` and render the mapper.

    Error query-string is forwarded from SISS verbatim; we surface it
    rather than silently succeeding with zero accounts.
    """
    company = await _first_company()
    if error:
        return RedirectResponse(
            f"/admin/bank-feeds?error={error}", status_code=303
        )
    if not sdsClientId:
        return RedirectResponse(
            "/admin/bank-feeds?error=callback missing sdsClientId",
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        default_account = (
            await _bank_accounts_for_mapping(session, company.id)
        )
        if not default_account:
            raise HTTPException(
                500, "No 1-1xxx bank accounts — seed the CoA first"
            )
        try:
            result = await onboarding.resolve_callback(
                session,
                company_id=company.id,
                sds_client_id=sdsClientId,
                default_ledger_account_id=default_account[0].id,
                settings=settings,
            )
        except (onboarding.SissNotConfiguredError, SissError) as exc:
            return RedirectResponse(
                f"/admin/bank-feeds?error={exc}", status_code=303
            )
        await session.commit()
        ledger_accounts = await _bank_accounts_for_mapping(session, company.id)

    return templates.TemplateResponse(
        request,
        "bank_feeds/map.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "consent_id": consentId or "",
            "sds_client_id": sdsClientId,
            "feed_accounts": result.discovered_accounts,
            "ledger_accounts": ledger_accounts,
        },
    )


# ---------------------------------------------------------------------- #
# Link — save the feed-account -> ledger-account mapping                 #
# ---------------------------------------------------------------------- #


@router.post("/link")
async def bank_feeds_link(request: Request) -> RedirectResponse:
    """Apply per-account ``ledger_account_id`` mappings from the form.

    Form shape (one pair per account):

        ledger_account_id__<bank_feed_account_id>=<uuid>
    """
    form = dict(await request.form())
    changes: list[tuple[uuid.UUID, uuid.UUID]] = []
    for key, value in form.items():
        if not key.startswith("ledger_account_id__"):
            continue
        feed_id_str = key.split("__", 1)[1]
        if not value:
            continue
        try:
            changes.append((uuid.UUID(feed_id_str), uuid.UUID(str(value))))
        except ValueError:
            continue
    async with AsyncSessionLocal() as session:
        for feed_id, ledger_id in changes:
            await onboarding.link_account_to_ledger(
                session,
                bank_feed_account_id=feed_id,
                ledger_account_id=ledger_id,
            )
        await session.commit()
    return RedirectResponse(
        f"/admin/bank-feeds?message=Saved+{len(changes)}+mapping(s)",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Sync                                                                   #
# ---------------------------------------------------------------------- #


@router.post("/sync")
async def bank_feeds_sync(
    bank_feed_account_id: str | None = Form(None),
) -> RedirectResponse:
    """Pull new transactions and insert them as BankStatementLine rows.

    No ``bank_feed_account_id`` → sync every active account for the
    current company (bulk "Sync now"). With one → sync that account.
    """
    company = await _first_company()
    try:
        async with AsyncSessionLocal() as session:
            if bank_feed_account_id:
                outcome = await onboarding.sync_account(
                    session,
                    bank_feed_account_id=uuid.UUID(bank_feed_account_id),
                    settings=settings,
                )
                await session.commit()
                msg = (
                    f"Synced {outcome.lines_inserted} new lines "
                    f"({outcome.transactions_seen} txns seen)"
                )
            else:
                outcomes = await onboarding.sync_all_active(
                    session,
                    company_id=company.id,
                    settings=settings,
                )
                await session.commit()
                total_new = sum(o.lines_inserted for o in outcomes)
                msg = (
                    f"Synced {len(outcomes)} account(s), "
                    f"{total_new} new line(s)"
                )
    except (onboarding.SissNotConfiguredError, SissError) as exc:
        return RedirectResponse(
            f"/admin/bank-feeds?error={exc}", status_code=303
        )
    return RedirectResponse(
        f"/admin/bank-feeds?message={msg}", status_code=303
    )


# ---------------------------------------------------------------------- #
# Revoke (per-account)                                                   #
# ---------------------------------------------------------------------- #


@router.post("/{bank_feed_account_id}/revoke")
async def bank_feeds_revoke(
    bank_feed_account_id: uuid.UUID,
    local_only: str = Form(""),
) -> RedirectResponse:
    """DELETE upstream consent + mark local row revoked.

    ``local_only=1`` skips the upstream DELETE — used when SISS is
    unreachable and the admin just wants to clear the local row.
    """
    async with AsyncSessionLocal() as session:
        await onboarding.revoke_feed_account(
            session,
            bank_feed_account_id=bank_feed_account_id,
            settings=settings,
            skip_upstream=bool(local_only),
        )
        await session.commit()
    return RedirectResponse(
        "/admin/bank-feeds?message=Account+revoked",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Offboard (whole company) — Batch K                                     #
# ---------------------------------------------------------------------- #


@router.get("/offboard", response_class=HTMLResponse)
async def bank_feeds_offboard_form(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        client = await _feed_client_for_company(session, company.id)
        feed_accounts = await _feed_accounts_for_company(session, company.id)
    return templates.TemplateResponse(
        request,
        "bank_feeds/offboard.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "bank_feed_client": client,
            "feed_accounts": feed_accounts,
            "siss_configured": onboarding.siss_configured(settings),
            "error": None,
        },
    )


@router.post("/offboard")
async def bank_feeds_offboard_submit(
    hard_delete: str = Form(""),
    export: str = Form("1"),
) -> RedirectResponse:
    """Offboard the company from SISS.

    Plan Q5 default: ``soft-revoke-only`` (``hard_delete`` empty).
    Pass ``hard_delete=1`` to also call SISS ``DELETE /sds/clients/{id}``.
    ``export=1`` writes a CDR JSON dump before upstream changes so the
    user has their data on disk.
    """
    company = await _first_company()
    export_dir: str | None = None
    if export:
        from pathlib import Path as _P

        export_dir = str(_P("/tmp/saebooks-cdr-exports"))  # overridable later

    try:
        async with AsyncSessionLocal() as session:
            result = await onboarding.offboard_company(
                session,
                company_id=company.id,
                export_dir=export_dir,
                settings=settings,
                skip_upstream=not bool(hard_delete),
            )
            await session.commit()
    except (onboarding.SissNotConfiguredError, SissError) as exc:
        return RedirectResponse(
            f"/admin/bank-feeds?error={exc}", status_code=303
        )
    parts: list[str] = []
    if result.export_path:
        parts.append(f"exported+to+{result.export_path}")
    if result.client_deleted_upstream:
        parts.append("upstream+client+deleted")
    parts.append(f"{result.accounts_revoked_locally}+accounts+revoked")
    return RedirectResponse(
        f"/admin/bank-feeds?message={'+|+'.join(parts)}",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Health (Batch J) — cache and render list_feed_issues                   #
# ---------------------------------------------------------------------- #


@router.post("/refresh-issues")
async def bank_feeds_refresh_issues() -> RedirectResponse:
    """Pull ``/sds/feedissues`` and cache; show the summary on redirect."""
    from saebooks.services.bank_feeds import health

    try:
        result = await health.refresh_feed_issues(settings=settings)
    except (onboarding.SissNotConfiguredError, SissError) as exc:
        return RedirectResponse(
            f"/admin/bank-feeds?error={exc}", status_code=303
        )
    return RedirectResponse(
        f"/admin/bank-feeds?message=Refreshed+{result.fetched}+issue(s)",
        status_code=303,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Narrow helper for mypy when pulling dict out of a typed-as-Any dict."""
    if isinstance(value, dict):
        return value
    return {}


# ---------------------------------------------------------------------- #
# Reconciliation sweep (Batch HH)                                        #
# ---------------------------------------------------------------------- #


@router.get("/health", response_class=HTMLResponse)
async def bank_feeds_health(request: Request) -> HTMLResponse:
    """Per-account staleness + variance table.

    Read-only: renders the result of :func:`reconcile.sweep` for the
    active company. No SISS network — everything comes from the
    statement-line + journal-line tables.
    """
    from saebooks.services.bank_feeds import reconcile

    company = await _first_company()
    async with AsyncSessionLocal() as session:
        report = await reconcile.sweep(session, company_id=company.id)

    return templates.TemplateResponse(
        request,
        "bank_feeds/health.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "report": report,
            "stale_cutoff": reconcile.stale_cutoff(report.through_date),
        },
    )


# ---------------------------------------------------------------------- #
# Per-company credentials (Batch II — FLAG_PER_COMPANY_SISS gated)       #
# ---------------------------------------------------------------------- #
#
# Lets an Enterprise admin paste SISS CDR creds directly into the DB
# rather than shipping them via env. Secrets are Fernet-encrypted at
# rest (see ``services/crypto.py``). The form also offers a smoke-test
# button that exchanges the creds for an OAuth bearer token — cheap,
# side-effect-free, confirms the creds actually work before the user
# walks through the consent flow with a real bank.
#
# Both the form and the smoke-test require FLAG_PER_COMPANY_SISS *and*
# SAEBOOKS_FIELD_ENCRYPTION_KEY. We surface both states distinctly on
# the form so a partial install shows exactly what's missing.


async def _redact_secret(ciphertext: str | None) -> str:
    """Return a human-readable badge for a stored secret without leaking it."""
    if not ciphertext:
        return "— not set —"
    return "set (••••••••)"


@router.get(
    "/credentials",
    response_class=HTMLResponse,
    dependencies=[Depends(require_feature(FLAG_PER_COMPANY_SISS))],
)
async def bank_feeds_credentials_form(
    request: Request,
    message: str | None = Query(None),
    error: str | None = Query(None),
    test: str | None = Query(None),
) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        fresh = await session.get(Company, company.id)
    assert fresh is not None  # _first_company guaranteed one
    return templates.TemplateResponse(
        request,
        "bank_feeds/credentials.html",
        {
            "edition": settings.edition,
            "company_name": fresh.name,
            "company": fresh,
            "encryption_configured": crypto_svc.is_configured(settings),
            "env_fallback_configured": onboarding.siss_configured(settings),
            "client_secret_status": await _redact_secret(
                fresh.siss_client_secret_encrypted
            ),
            "subscription_key_status": await _redact_secret(
                fresh.siss_subscription_key_encrypted
            ),
            "message": message,
            "error": error,
            "test_result": test,
        },
    )


@router.post(
    "/credentials",
    dependencies=[Depends(require_feature(FLAG_PER_COMPANY_SISS))],
)
async def bank_feeds_credentials_save(
    client_id: str = Form(""),
    client_secret: str = Form(""),
    subscription_key: str = Form(""),
    environment: str = Form("production"),
) -> RedirectResponse:
    """Persist per-company creds. Empty secret fields mean "leave unchanged".

    This is the one place client_secret / subscription_key are accepted
    as plaintext. ``encrypt_field`` refuses to run when the Fernet key
    is absent, so a misconfigured install bounces with an error rather
    than silently storing plaintext in the "encrypted" column.
    """
    if not crypto_svc.is_configured(settings):
        return RedirectResponse(
            "/admin/bank-feeds/credentials?error=encryption+not+configured",
            status_code=303,
        )
    company = await _first_company()
    env_norm = environment.strip().lower() or "production"
    if env_norm not in ("production", "sandbox"):
        return RedirectResponse(
            "/admin/bank-feeds/credentials?error=invalid+environment",
            status_code=303,
        )
    try:
        new_secret_ct = (
            crypto_svc.encrypt_field(client_secret, settings=settings)
            if client_secret
            else None
        )
        new_subkey_ct = (
            crypto_svc.encrypt_field(subscription_key, settings=settings)
            if subscription_key
            else None
        )
    except crypto_svc.FieldEncryptionError as exc:
        return RedirectResponse(
            f"/admin/bank-feeds/credentials?error={str(exc)[:140]}",
            status_code=303,
        )

    async with AsyncSessionLocal() as session:
        row = await session.get(Company, company.id)
        assert row is not None
        row.siss_client_id = client_id.strip() or None
        if new_secret_ct is not None:
            row.siss_client_secret_encrypted = new_secret_ct
        if new_subkey_ct is not None:
            row.siss_subscription_key_encrypted = new_subkey_ct
        row.siss_environment = env_norm
        await session.commit()

    return RedirectResponse(
        "/admin/bank-feeds/credentials?message=credentials+saved",
        status_code=303,
    )


@router.post(
    "/credentials/clear",
    dependencies=[Depends(require_feature(FLAG_PER_COMPANY_SISS))],
)
async def bank_feeds_credentials_clear() -> RedirectResponse:
    """Drop per-company creds — resolver will fall back to env vars."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        row = await session.get(Company, company.id)
        assert row is not None
        row.siss_client_id = None
        row.siss_client_secret_encrypted = None
        row.siss_subscription_key_encrypted = None
        row.siss_environment = None
        await session.commit()
    return RedirectResponse(
        "/admin/bank-feeds/credentials?message=credentials+cleared",
        status_code=303,
    )


@router.post(
    "/credentials/test",
    dependencies=[Depends(require_feature(FLAG_PER_COMPANY_SISS))],
)
async def bank_feeds_credentials_test() -> RedirectResponse:
    """Smoke-test: try to fetch an OAuth bearer token with stored creds.

    Side-effect-free — doesn't touch SISS's data endpoints, just the
    ``/oauth/token`` endpoint with client-credentials grant. Success
    confirms the creds are actually valid before the admin walks through
    a real consent flow.
    """
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        try:
            creds = await onboarding.resolve_company_siss_creds(
                session, company.id, settings=settings
            )
        except onboarding.SissNotConfiguredError as exc:
            return RedirectResponse(
                f"/admin/bank-feeds/credentials?test=fail&error={str(exc)[:140]}",
                status_code=303,
            )

    cache = TokenCache(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        token_url=creds.token_url,
    )
    try:
        token = await cache.get()
    except Exception as exc:
        return RedirectResponse(
            f"/admin/bank-feeds/credentials?test=fail&error={str(exc)[:140]}",
            status_code=303,
        )
    finally:
        await cache.aclose()

    badge = "ok" if token else "fail"
    return RedirectResponse(
        f"/admin/bank-feeds/credentials?test={badge}&message=token+fetched+%28source%3A{creds.source}%29",
        status_code=303,
    )
