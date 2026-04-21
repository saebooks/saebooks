"""Journal entry routes — list, create, edit, post, reverse."""
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry
from saebooks.models.project import Project, ProjectStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as svc
from saebooks.services.journal import PostingError

router = APIRouter(prefix="/journal")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise HTTPException(500, "No active company")
        return company


async def _accounts_and_tax_codes(
    company_id: uuid.UUID,
) -> tuple[list[Account], list[TaxCode], list[Project]]:
    async with AsyncSessionLocal() as session:
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
) -> HTMLResponse:
    company = await _first_company()
    filter_status = EntryStatus(status) if status else None
    async with AsyncSessionLocal() as session:
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
async def journal_new(request: Request) -> HTMLResponse:
    company = await _first_company()
    accounts, tax_codes, projects = await _accounts_and_tax_codes(company.id)
    async with AsyncSessionLocal() as session:
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
async def journal_detail(request: Request, entry_id: uuid.UUID) -> HTMLResponse:
    company = await _first_company()
    accounts, tax_codes, projects = await _accounts_and_tax_codes(company.id)
    async with AsyncSessionLocal() as session:
        entry = await svc.get(session, entry_id)
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
async def journal_save(request: Request) -> RedirectResponse | HTMLResponse:
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
    async with AsyncSessionLocal() as session:
        try:
            if entry_id:
                entry = await svc.update_draft(
                    session,
                    uuid.UUID(str(entry_id)),
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
                )

            if action == "post":
                override = str(form.get("override_reason", "")).strip() or None
                entry = await svc.post(
                    session, entry.id, posted_by="web", override_reason=override
                )

        except PostingError as exc:
            error = str(exc)
            accounts, tax_codes, projects = await _accounts_and_tax_codes(company.id)
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
async def journal_reverse(entry_id: uuid.UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        try:
            reversal = await svc.reverse(session, entry_id, posted_by="web")
        except PostingError as exc:
            raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/journal/{reversal.id}", status_code=303)


@router.post("/{entry_id}/delete")
async def journal_delete(entry_id: uuid.UUID) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        await svc.delete(session, entry_id, performed_by="web")
    return RedirectResponse("/journal", status_code=303)
