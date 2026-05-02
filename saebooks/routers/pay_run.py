"""Pay-run routes — select unpaid bills, export ABA.

Public under ``/pay-run``. Community-tier. No flag gate.

Flow:

    GET  /pay-run            → pick-list of POSTED bills with
                               balance_due > 0, bank-account picker,
                               process-date picker
    POST /pay-run/export     → form-encoded list of selections,
                               returns ``text/plain`` ABA file
                               with ``Content-Disposition: attachment;
                               filename="aba-<yymmdd>-<n>.txt"``

No Payment rows are created on export — the user uploads the file
to their bank first, then comes back and posts the matching
Payments via ``/payments/new?direction=OUTGOING&...``. This matches
the Xero / QBO "mark as paid" pattern.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.services import pay_run as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/pay-run")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def pay_run_index(request: Request) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        candidates = await svc.candidates_for_payrun(session, company.id)
        # Bank accounts with ABA fields already populated. Accounts
        # without BSB/APCA-ID are hidden — they'd fail on export
        # anyway and showing them here confuses the UI.
        bank_accounts = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company.id,
                    Account.archived_at.is_(None),
                    Account.bsb.is_not(None),
                    Account.apca_user_id.is_not(None),
                )
                .order_by(Account.code)
            )
        ).scalars().all()

    return templates.TemplateResponse(
        request,
        "pay_run/index.html",
        {
            "candidates": candidates,
            "bank_accounts": bank_accounts,
            "today": date.today(),
        },
    )


@router.post("/export")
async def pay_run_export(request: Request) -> PlainTextResponse:
    """Build the ABA file from the submitted form and return it.

    Form shape:

      * ``bank_account_id``  — uuid of the remitter bank account
      * ``process_date``     — ISO date to request processing on
      * ``description``      — optional, default 'CREDITORS'
      * for each selected row:
          * ``select_<bill_id>``   = "on"
          * ``amount_<bill_id>``   = "123.45"
    """
    form = await request.form()
    company = await _first_company()

    try:
        bank_account_id = UUID(str(form["bank_account_id"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Missing or invalid bank_account_id") from exc

    try:
        process_date = date.fromisoformat(str(form["process_date"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Missing or invalid process_date") from exc

    description = str(form.get("description") or "CREDITORS")

    selections: list[svc.PayRunSelection] = []
    for key in form:
        if not key.startswith("select_"):
            continue
        bill_id_str = key.removeprefix("select_")
        try:
            bill_id = UUID(bill_id_str)
        except ValueError:
            continue
        amount_str = str(form.get(f"amount_{bill_id_str}") or "").strip()
        if not amount_str:
            continue
        try:
            amount = Decimal(amount_str)
        except InvalidOperation as exc:
            raise HTTPException(
                400, f"Invalid amount for bill {bill_id}: {amount_str!r}"
            ) from exc
        selections.append(svc.PayRunSelection(bill_id=bill_id, amount=amount))

    if not selections:
        raise HTTPException(400, "Select at least one bill to export")

    async with AsyncSessionLocal() as session:
        try:
            aba_text = await svc.build_aba_from_selection(
                session,
                company.id,
                bank_account_id=bank_account_id,
                selections=selections,
                process_date=process_date,
                description=description,
            )
        except svc.PayRunError as exc:
            raise HTTPException(400, str(exc)) from exc

    filename = f"aba-{process_date.strftime('%y%m%d')}-{len(selections)}.txt"
    # Plain text (ABA files are ASCII). The correct MIME would be
    # application/octet-stream but text/plain renders + downloads
    # equally well and is easier to peek at in a browser.
    return PlainTextResponse(
        aba_text,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
