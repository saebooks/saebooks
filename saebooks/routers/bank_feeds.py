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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.company import Company
from saebooks.services.bank_feeds import onboarding
from saebooks.services.bank_feeds.errors import SissError
from saebooks.services.features import FLAG_BANK_FEEDS, require_feature

router = APIRouter(
    prefix="/admin/bank-feeds",
    dependencies=[Depends(require_feature(FLAG_BANK_FEEDS))],
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


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
