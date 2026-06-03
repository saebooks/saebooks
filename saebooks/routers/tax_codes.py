from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.routers.deps import get_web_session
from saebooks.services import tax_codes as svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/admin/tax-codes")


REPORTING_TYPES = [
    ("taxable", "Taxable"),
    ("gst_free", "GST Free / Zero-rated"),
    ("input_taxed", "Input Taxed"),
    ("out_of_scope", "Out of scope"),
    ("exempt", "Exempt"),
]
TAX_SYSTEMS = ["GST", "VAT", "other"]


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


def _parse_rate(raw: str) -> Decimal:
    try:
        return Decimal(raw.strip() or "0")
    except InvalidOperation as exc:
        raise HTTPException(400, f"Invalid rate {raw!r}") from exc


@router.get("", response_class=HTMLResponse)
async def tax_codes_list(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    rows = await svc.list_active(session, company.id)
    return templates.TemplateResponse(
        request,
        "tax_codes/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "tax_codes": rows,
            "reporting_types": REPORTING_TYPES,
            "tax_systems": TAX_SYSTEMS,
        },
    )


@router.post("")
async def tax_codes_create(
    code: str = Form(...),
    name: str = Form(...),
    rate: str = Form("0"),
    tax_system: str = Form("GST"),
    reporting_type: str = Form("taxable"),
    description: str = Form(""),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    company = await _first_company()
    await svc.create(
        session,
        company.id,
        code=code,
        name=name,
        rate=_parse_rate(rate),
        tax_system=tax_system,
        reporting_type=reporting_type,
        description=description.strip() or None,
    )
    return RedirectResponse("/admin/tax-codes", status_code=303)


@router.get("/{tax_code_id}/edit", response_class=HTMLResponse)
async def tax_codes_edit(
    request: Request,
    tax_code_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    tax_code = await svc.get(session, tax_code_id)
    if tax_code is None:
        raise HTTPException(404, "Tax code not found")
    company = await session.get(Company, tax_code.company_id)
    return templates.TemplateResponse(
        request,
        "tax_codes/edit.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "tax_code": tax_code,
            "reporting_types": REPORTING_TYPES,
            "tax_systems": TAX_SYSTEMS,
        },
    )


@router.post("/{tax_code_id}")
async def tax_codes_update(
    tax_code_id: UUID,
    code: str = Form(...),
    name: str = Form(...),
    rate: str = Form("0"),
    tax_system: str = Form("GST"),
    reporting_type: str = Form("taxable"),
    description: str = Form(""),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    await svc.update(
        session,
        tax_code_id,
        code=code,
        name=name,
        rate=_parse_rate(rate),
        tax_system=tax_system,
        reporting_type=reporting_type,
        description=description,
        performed_by="web",
    )
    return RedirectResponse("/admin/tax-codes", status_code=303)


@router.post("/{tax_code_id}/archive")
async def tax_codes_archive(
    tax_code_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    await svc.archive(session, tax_code_id, performed_by="web")
    return RedirectResponse("/admin/tax-codes", status_code=303)
