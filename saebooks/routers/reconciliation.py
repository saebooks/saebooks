"""Bank reconciliation routes."""
import uuid

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.routers.deps import get_web_session
from saebooks.routers.reports import _first_company
from saebooks.services import bank_rules as rules_svc
from saebooks.services import reconciliation as svc
from saebooks.web import templates

router = APIRouter(prefix="/reconciliation")


@router.get("", response_class=HTMLResponse)
async def reconciliation_index(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Choose a bank account to reconcile."""
    company = await _first_company()
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
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Show statement lines for a bank account with matching UI."""
    company = await _first_company()
    status = None
    if status_filter == "unmatched":
        status = StatementLineStatus.UNMATCHED
    elif status_filter == "matched":
        status = StatementLineStatus.MATCHED

    accounts = await svc.bank_accounts(session, company.id)
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        return HTMLResponse("Account not found", status_code=404)

    lines = await svc.statement_lines(
        session, company.id, account_id, status=status
    )

    # Build rule suggestions for unmatched lines
    suggestions = await rules_svc.find_suggestions_for_lines(
        session, company.id, lines
    )

    # Resolve account names for the suggested rules
    suggested_acct_ids = {r.account_id for r in suggestions.values()}
    sugg_accounts = {}
    if suggested_acct_ids:
        from sqlalchemy import select

        from saebooks.models.account import Account
        res = await session.execute(
            select(Account).where(Account.id.in_(suggested_acct_ids))
        )
        sugg_accounts = {a.id: a for a in res.scalars().all()}

    return templates.TemplateResponse(
        request,
        "reconciliation/account.html",
        {
            "edition": settings.edition,
            "account": account,
            "lines": lines,
            "status_filter": status_filter or "all",
            "suggestions": suggestions,
            "sugg_accounts": sugg_accounts,
        },
    )


@router.post("/{account_id}/apply-rule/{line_id}/{rule_id}", response_model=None)
async def apply_rule(
    request: Request,
    account_id: uuid.UUID,
    line_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Apply a bank rule to a single line — creates and posts the journal."""
    try:
        await rules_svc.apply_rule_to_line(
            session, line_id, rule_id, posted_by="rule-suggested"
        )
    except Exception as exc:
        return RedirectResponse(
            f"/reconciliation/{account_id}?error={exc}",
            status_code=303,
        )
    return RedirectResponse(f"/reconciliation/{account_id}", status_code=303)


@router.post("/{account_id}/run-auto", response_model=None)
async def run_auto_for_account(
    request: Request,
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Apply all auto-create rules to unmatched lines for THIS bank account."""
    company = await _first_company()
    counts = await rules_svc.auto_apply_rules(
        session, company.id, only_account_id=account_id,
    )
    return RedirectResponse(
        f"/reconciliation/{account_id}?ran={counts['created']}",
        status_code=303,
    )


@router.post("/{account_id}/import", response_model=None)
async def import_csv(
    request: Request,
    account_id: uuid.UUID,
    file: UploadFile,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Import CSV bank statement."""
    company = await _first_company()
    content = await file.read()
    csv_text = content.decode("utf-8-sig")  # handle BOM from Excel exports

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
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Show candidate journal entries for matching."""
    company = await _first_company()
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
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Match a statement line to a journal entry."""
    form = await request.form()
    entry_id = uuid.UUID(str(form["entry_id"]))

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
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Remove match from a statement line."""
    await svc.unmatch_line(session, line_id)

    return RedirectResponse(
        f"/reconciliation/{account_id}",
        status_code=303,
    )
