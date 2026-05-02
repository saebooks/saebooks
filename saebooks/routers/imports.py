"""Admin → Imports router.

Three UI surfaces:

* ``/admin/imports/`` — landing page with links to each flow.
* ``/admin/imports/bank`` — upload a CSV/OFX, pick the destination bank
  account, see a preview, confirm to persist.
* ``/admin/imports/coa`` — export the current CoA as CSV, or upload a
  CSV and see a diff (new/changed/removed) before applying.
* ``/admin/imports/qbo`` — upload QBO's contact / account-list CSV
  exports, see a preview of what would be created.

Each flow is two-step: POST the file, the router parses + renders a
preview, user clicks "Confirm" which re-submits the raw CSV to a
``/apply`` endpoint. This keeps the DB out of the parsing path and
lets the user bail at the preview with zero side effects.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.services.imports import (
    bank_csv as bank_csv_svc,
)
from saebooks.services.imports import (
    bank_ofx as bank_ofx_svc,
)
from saebooks.services.imports import (
    coa as coa_svc,
)
from saebooks.services.imports import (
    persist as persist_svc,
)
from saebooks.services.imports import (
    qbo as qbo_svc,
)
from saebooks.services.authz import require_role
from saebooks.models.user import UserRole
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(
    prefix="/admin/imports",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _bank_accounts(company_id: uuid.UUID) -> list[Account]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Account)
                .where(
                    Account.company_id == company_id,
                    Account.account_type == AccountType.ASSET,
                    Account.reconcile.is_(True),
                    Account.archived_at.is_(None),
                )
                .order_by(Account.code)
            )
        ).scalars().all()
        return list(rows)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def imports_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "imports/index.html", {"edition": settings.edition}
    )


# --- Bank statements ------------------------------------------------


@router.get("/bank", response_class=HTMLResponse)
async def bank_index(request: Request) -> HTMLResponse:
    company = await _first_company()
    accounts = await _bank_accounts(company.id)
    return templates.TemplateResponse(
        request,
        "imports/bank.html",
        {"edition": settings.edition, "accounts": accounts},
    )


@router.post("/bank/preview", response_class=HTMLResponse)
async def bank_preview(
    request: Request,
    account_id: uuid.UUID = Form(...),  # noqa: B008
    file: UploadFile = Form(...),  # noqa: B008
) -> HTMLResponse:
    """Parse the uploaded file, render a preview without persisting."""
    company = await _first_company()
    accounts = await _bank_accounts(company.id)
    if not any(a.id == account_id for a in accounts):
        raise HTTPException(400, "Unknown bank account")

    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    filename = (file.filename or "").lower()
    try:
        if filename.endswith(".ofx") or raw.lstrip().startswith(("<?xml", "OFXHEADER")):
            parsed = bank_ofx_svc.parse_ofx(raw)
            fmt_label = "OFX"
        else:
            fmt = bank_csv_svc.detect_format(raw)
            parsed = bank_csv_svc.parse_bank_csv(raw, fmt=fmt)
            fmt_label = f"CSV ({fmt.value})"
    except (bank_csv_svc.BankCsvError, bank_ofx_svc.OfxError) as exc:
        return templates.TemplateResponse(
            request,
            "imports/bank.html",
            {
                "edition": settings.edition,
                "accounts": accounts,
                "error": str(exc),
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "imports/bank_preview.html",
        {
            "edition": settings.edition,
            "account_id": account_id,
            "fmt_label": fmt_label,
            "lines": parsed,
            "raw": raw,
        },
    )


@router.post("/bank/apply", response_class=HTMLResponse)
async def bank_apply(
    request: Request,
    account_id: uuid.UUID = Form(...),  # noqa: B008
    raw: str = Form(...),
) -> HTMLResponse:
    """Persist the parsed lines (idempotent on content hash)."""
    company = await _first_company()
    if raw.lstrip().startswith(("<?xml", "OFXHEADER")):
        parsed = bank_ofx_svc.parse_ofx(raw)
    else:
        parsed = bank_csv_svc.parse_bank_csv(raw)

    async with AsyncSessionLocal() as session:
        inserted = await persist_svc.persist_bank_lines(
            session,
            company_id=company.id,
            account_id=account_id,
            lines=parsed,
        )
        await session.commit()

    return templates.TemplateResponse(
        request,
        "imports/bank_done.html",
        {
            "edition": settings.edition,
            "inserted": inserted,
            "total": len(parsed),
            "account_id": account_id,
        },
    )


# --- Chart of accounts ---------------------------------------------


@router.get("/coa", response_class=HTMLResponse)
async def coa_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "imports/coa.html", {"edition": settings.edition}
    )


@router.get("/coa/export", response_class=PlainTextResponse)
async def coa_export(
    request: Request,
    download: int = Query(0),
) -> PlainTextResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        accounts = (
            await session.execute(
                select(Account)
                .where(Account.company_id == company.id)
                .order_by(Account.code)
            )
        ).scalars().all()
    csv_text = coa_svc.export_coa_csv(list(accounts))
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="coa.csv"'
    return PlainTextResponse(
        content=csv_text, media_type="text/csv", headers=headers
    )


@router.post("/coa/preview", response_class=HTMLResponse)
async def coa_preview(
    request: Request,
    file: UploadFile = Form(...),  # noqa: B008
) -> HTMLResponse:
    company = await _first_company()
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        rows = coa_svc.parse_coa_csv(raw)
    except coa_svc.CoaImportError as exc:
        return templates.TemplateResponse(
            request,
            "imports/coa.html",
            {"edition": settings.edition, "error": str(exc)},
            status_code=400,
        )

    async with AsyncSessionLocal() as session:
        accounts = (
            await session.execute(
                select(Account).where(Account.company_id == company.id)
            )
        ).scalars().all()
    diff = coa_svc.diff_coa(list(accounts), rows)

    return templates.TemplateResponse(
        request,
        "imports/coa_preview.html",
        {
            "edition": settings.edition,
            "diff": diff,
            "raw": raw,
        },
    )


@router.post("/coa/apply", response_model=None)
async def coa_apply(
    request: Request,
    raw: str = Form(...),
    archive_removed: str = Form(""),
) -> RedirectResponse:
    company = await _first_company()
    rows = coa_svc.parse_coa_csv(raw)
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(Account.company_id == company.id)
            )
        ).scalars().all()
        diff = coa_svc.diff_coa(list(existing), rows)
        applied = await coa_svc.apply_coa_diff(
            session,
            company.id,
            diff,
            archive_removed=bool(archive_removed),
        )
        await session.commit()
    query = "&".join(f"{k}={v}" for k, v in applied.items())
    return RedirectResponse(f"/admin/imports/coa?{query}", status_code=303)


# --- QBO migration -------------------------------------------------


@router.get("/qbo", response_class=HTMLResponse)
async def qbo_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "imports/qbo.html", {"edition": settings.edition}
    )


@router.post("/qbo/contacts/preview", response_class=HTMLResponse)
async def qbo_contacts_preview(
    request: Request,
    kind: str = Form("auto"),
    file: UploadFile = Form(...),  # noqa: B008
) -> HTMLResponse:
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    kind_enum = qbo_svc.QboContactKind(kind) if kind in (
        "customer", "vendor", "auto"
    ) else qbo_svc.QboContactKind.AUTO
    try:
        rows = qbo_svc.parse_qbo_contacts(raw, kind=kind_enum)
    except qbo_svc.QboImportError as exc:
        return templates.TemplateResponse(
            request,
            "imports/qbo.html",
            {"edition": settings.edition, "error": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "imports/qbo_contacts_preview.html",
        {
            "edition": settings.edition,
            "rows": rows,
            "raw": raw,
            "kind": kind_enum.value,
        },
    )


@router.post("/qbo/contacts/apply", response_model=None)
async def qbo_contacts_apply(
    request: Request,
    raw: str = Form(...),
    kind: str = Form("auto"),
) -> RedirectResponse:
    company = await _first_company()
    kind_enum = qbo_svc.QboContactKind(kind) if kind in (
        "customer", "vendor", "auto"
    ) else qbo_svc.QboContactKind.AUTO
    rows = qbo_svc.parse_qbo_contacts(raw, kind=kind_enum)

    async with AsyncSessionLocal() as session:
        existing_names = {
            n
            for n in (
                await session.execute(
                    select(Contact.name).where(
                        Contact.company_id == company.id,
                        Contact.archived_at.is_(None),
                    )
                )
            ).scalars().all()
        }
        inserted = 0
        for r in rows:
            if r.name in existing_names:
                continue
            session.add(
                Contact(
                    company_id=company.id,
                    name=r.name,
                    contact_type=r.contact_type,
                    email=r.email,
                    phone=r.phone,
                    abn=r.abn,
                    address_line1=r.address_line1,
                    city=r.city,
                    state=r.state,
                    postcode=r.postcode,
                )
            )
            existing_names.add(r.name)
            inserted += 1
        await session.commit()

    return RedirectResponse(
        f"/admin/imports/qbo?contacts_imported={inserted}", status_code=303
    )


@router.post("/qbo/accounts/preview", response_class=HTMLResponse)
async def qbo_accounts_preview(
    request: Request,
    file: UploadFile = Form(...),  # noqa: B008
) -> HTMLResponse:
    company = await _first_company()
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        qbo_rows = qbo_svc.parse_qbo_accounts(raw)
    except qbo_svc.QboImportError as exc:
        return templates.TemplateResponse(
            request,
            "imports/qbo.html",
            {"edition": settings.edition, "error": str(exc)},
            status_code=400,
        )
    coa_rows = qbo_svc.qbo_coa_to_rows(qbo_rows)
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(Account.company_id == company.id)
            )
        ).scalars().all()
    diff = coa_svc.diff_coa(list(existing), coa_rows)
    return templates.TemplateResponse(
        request,
        "imports/coa_preview.html",
        {
            "edition": settings.edition,
            "diff": diff,
            "raw": coa_svc.export_coa_csv([]).splitlines()[0]
            + "\n"
            + "\n".join(
                ",".join(
                    [
                        r.code,
                        r.name,
                        r.account_type.value,
                        r.parent_code or "",
                        r.tax_code_default or "",
                        "true" if r.reconcile else "false",
                    ]
                )
                for r in coa_rows
            ),
            "from_qbo": True,
        },
    )
