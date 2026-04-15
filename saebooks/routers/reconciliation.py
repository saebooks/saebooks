"""Bank reconciliation routes."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.routers.reports import _first_company
from saebooks.services import reconciliation as svc

router = APIRouter(prefix="/reconciliation")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def reconciliation_index(request: Request) -> HTMLResponse:
    """Choose a bank account to reconcile."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        accounts = await svc.bank_accounts(session, company.id)
    return templates.TemplateResponse(
        request,
        "reconciliation/index.html",
        {
            "edition": settings.edition,
            "accounts": accounts,
        },
    )


@router.get("/{account_id}", response_class=HTMLResponse)
async def reconciliation_account(
    request: Request,
    account_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status"),
) -> HTMLResponse:
    """Show statement lines for a bank account with matching UI."""
    company = await _first_company()
    status = None
    if status_filter == "unmatched":
        status = StatementLineStatus.UNMATCHED
    elif status_filter == "matched":
        status = StatementLineStatus.MATCHED

    async with AsyncSessionLocal() as session:
        accounts = await svc.bank_accounts(session, company.id)
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            return HTMLResponse("Account not found", status_code=404)

        lines = await svc.statement_lines(
            session, company.id, account_id, status=status
        )

    return templates.TemplateResponse(
        request,
        "reconciliation/account.html",
        {
            "edition": settings.edition,
            "account": account,
            "lines": lines,
            "status_filter": status_filter or "all",
        },
    )


@router.post("/{account_id}/import", response_model=None)
async def import_csv(
    request: Request,
    account_id: uuid.UUID,
    file: UploadFile,
) -> RedirectResponse:
    """Import CSV bank statement."""
    company = await _first_company()
    content = await file.read()
    csv_text = content.decode("utf-8-sig")  # handle BOM from Excel exports

    async with AsyncSessionLocal() as session:
        count = await svc.import_csv(session, company.id, account_id, csv_text)

    return RedirectResponse(
        f"/reconciliation/{account_id}?imported={count}",
        status_code=303,
    )


@router.get("/{account_id}/match/{line_id}", response_class=HTMLResponse)
async def match_candidates(
    request: Request,
    account_id: uuid.UUID,
    line_id: uuid.UUID,
) -> HTMLResponse:
    """Show candidate journal entries for matching."""
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        stmt_line = await session.get(BankStatementLine, line_id)
        if stmt_line is None:
            return HTMLResponse("Line not found", status_code=404)

        candidates = await svc.candidate_entries(
            session, company.id, account_id, stmt_line
        )

    return templates.TemplateResponse(
        request,
        "reconciliation/match.html",
        {
            "edition": settings.edition,
            "account_id": account_id,
            "stmt_line": stmt_line,
            "candidates": candidates,
        },
    )


@router.post("/{account_id}/match/{line_id}", response_model=None)
async def do_match(
    request: Request,
    account_id: uuid.UUID,
    line_id: uuid.UUID,
) -> RedirectResponse:
    """Match a statement line to a journal entry."""
    form = await request.form()
    entry_id = uuid.UUID(str(form["entry_id"]))

    async with AsyncSessionLocal() as session:
        await svc.match_line(session, line_id, entry_id)

    return RedirectResponse(
        f"/reconciliation/{account_id}",
        status_code=303,
    )


@router.post("/{account_id}/unmatch/{line_id}", response_model=None)
async def do_unmatch(
    request: Request,
    account_id: uuid.UUID,
    line_id: uuid.UUID,
) -> RedirectResponse:
    """Remove match from a statement line."""
    async with AsyncSessionLocal() as session:
        await svc.unmatch_line(session, line_id)

    return RedirectResponse(
        f"/reconciliation/{account_id}",
        status_code=303,
    )
