"""Journal entry routes — list, create, edit, post, reverse."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.project import Project, ProjectStatus
from saebooks.models.tax_code import TaxCode
from saebooks.routers.deps import get_web_session
from saebooks.services import journal as svc
from saebooks.services.journal import PostingError
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/journal")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _accounts_and_tax_codes(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> tuple[list[Account], list[TaxCode], list[Project]]:
    accts = await session.execute(
        select(Account)
        .where(Account.company_id == company_id, Account.archived_at.is_(None))
        .order_by(Account.code)
    )
    tcs = await session.execute(
        select(TaxCode)
        .where(TaxCode.company_id == company_id, TaxCode.archived_at.is_(None))
        .order_by(TaxCode.code)
    )
    projs = await session.execute(
        select(Project)
        .where(
            Project.company_id == company_id,
            Project.archived_at.is_(None),
            Project.status == ProjectStatus.ACTIVE,
        )
        .order_by(Project.code)
    )
    return (
        list(accts.scalars().all()),
        list(tcs.scalars().all()),
        list(projs.scalars().all()),
    )


@router.get("", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    status: str | None = Query(None),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    filter_status = EntryStatus(status) if status else None
    entries = await svc.list_entries(session, company.id, status=filter_status)
    total = await session.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.company_id == company.id)
    )
    count = total.scalar_one()
    return templates.TemplateResponse(
        request,
        "journal/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "entries": entries,
            "total": count,
            "filter_status": status or "all",
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def journal_new(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    accounts, tax_codes, projects = await _accounts_and_tax_codes(session, company.id)
    ref = await svc.next_ref(session)
    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "entry": None,
            "ref": ref,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "error": None,
        },
    )


@router.get("/{entry_id}", response_class=HTMLResponse)
async def journal_detail(
    request: Request,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    # Use the company tenant_id for the lookup rather than resolving from the
    # bearer JWT. The list route uses the same pattern. The legacy /journal
    # router has no require_bearer dependency, so request.state.jwt_claims is
    # unset, which causes resolve_tenant_id to 401 in production when the
    # static saebk_* machine token is presented. RLS at the DB layer provides
    # the real tenant isolation; the company.tenant_id check here is belt-and-
    # braces at the application layer.
    company = await _first_company()
    accounts, tax_codes, projects = await _accounts_and_tax_codes(session, company.id)
    try:
        entry = await svc.get(session, entry_id, tenant_id=company.tenant_id)
    except ValueError as exc:
        raise HTTPException(404, "Journal entry not found") from exc
    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "entry": entry,
            "ref": entry.ref,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "projects": projects,
            "error": None,
        },
    )


def _parse_lines(form: dict[str, object]) -> list[dict[str, object]]:
    """Parse line_N_field form keys into a list of line dicts."""
    lines: list[dict[str, object]] = []
    i = 0
    while f"line_{i}_account_id" in form:
        acct_raw = str(form.get(f"line_{i}_account_id", ""))
        if not acct_raw:
            i += 1
            continue

        debit_raw = str(form.get(f"line_{i}_debit", "0")) or "0"
        credit_raw = str(form.get(f"line_{i}_credit", "0")) or "0"
        gst_raw = str(form.get(f"line_{i}_gst_amount", ""))
        tc_raw = str(form.get(f"line_{i}_tax_code_id", ""))
        proj_raw = str(form.get(f"line_{i}_project_id", ""))

        try:
            debit = Decimal(debit_raw)
            credit = Decimal(credit_raw)
        except InvalidOperation as exc:
            raise HTTPException(400, f"Invalid number on line {i + 1}") from exc

        gst_amount: Decimal | None = None
        if gst_raw.strip():
            try:
                gst_amount = Decimal(gst_raw)
            except InvalidOperation as exc:
                raise HTTPException(400, f"Invalid GST amount on line {i + 1}") from exc

        line: dict[str, object] = {
            "account_id": uuid.UUID(acct_raw),
            "description": form.get(f"line_{i}_description", ""),
            "debit": debit,
            "credit": credit,
            "tax_code_id": uuid.UUID(tc_raw) if tc_raw else None,
            "gst_amount": gst_amount,
            "project_id": uuid.UUID(proj_raw) if proj_raw.strip() else None,
        }
        lines.append(line)
        i += 1
    return lines


@router.post("/save", response_model=None)
async def journal_save(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    form: dict[str, object] = dict(await request.form())
    company = await _first_company()

    entry_id = form.get("entry_id", "")
    ref = str(form.get("ref", ""))
    entry_date_str = str(form.get("entry_date", ""))
    description = str(form.get("description", ""))
    action = str(form.get("action", "draft"))

    try:
        entry_date = date.fromisoformat(entry_date_str)
    except ValueError as exc:
        raise HTTPException(400, "Invalid date") from exc

    lines = _parse_lines(form)

    error = None
    try:
        if entry_id:
            entry = await svc.update_draft(
                session,
                uuid.UUID(str(entry_id)),
                tenant_id=tenant_id,
                entry_date=entry_date,
                description=description or None,
                ref=ref or None,
                lines=lines,
                performed_by="web",
            )
        else:
            entry = await svc.create_draft(
                session,
                company_id=company.id,
                entry_date=entry_date,
                description=description or None,
                ref=ref or None,
                lines=lines,
                tenant_id=tenant_id,
            )

        if action == "post":
            override = str(form.get("override_reason", "")).strip() or None
            entry = await svc.post(
                session,
                entry.id,
                posted_by="web",
                override_reason=override,
                tenant_id=tenant_id,
            )

    except PostingError as exc:
        await session.rollback()
        error = str(exc)
        accounts, tax_codes, projects = await _accounts_and_tax_codes(session, company.id)
        return templates.TemplateResponse(
            request,
            "journal/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "entry": None,
                "ref": ref,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "projects": projects,
                "error": error,
            },
            status_code=422,
        )

    return RedirectResponse(f"/journal/{entry.id}", status_code=303)


@router.post("/{entry_id}/reverse")
async def journal_reverse(
    request: Request,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    try:
        reversal = await svc.reverse(
            session, entry_id, posted_by="web", tenant_id=tenant_id
        )
    except PostingError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, "Journal entry not found") from exc
    return RedirectResponse(f"/journal/{reversal.id}", status_code=303)


@router.post("/{entry_id}/delete")
async def journal_delete(
    request: Request,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    try:
        await svc.delete(
            session, entry_id, performed_by="web", tenant_id=tenant_id
        )
    except ValueError:
        # Cross-tenant or non-existent: silently no-op so users
        # never learn whether the entry exists in another tenant.
        pass
    return RedirectResponse("/journal", status_code=303)
