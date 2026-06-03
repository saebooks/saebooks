"""Fixed-asset register routes.

Public under ``/assets`` — Community-tier feature, no flag gate.

Shape mirrors ``saebooks.routers.contacts``:

- ``GET /assets`` — list, filtered by status (active/disposed/archived)
- ``GET /assets/new`` + ``POST /assets`` — create
- ``GET /assets/{id}`` — detail view (NBV, last-depreciated, disposal info)
- ``GET /assets/{id}/edit`` + ``POST /assets/{id}`` — update
- ``POST /assets/{id}/depreciate`` — post depreciation through a date
- ``GET /assets/{id}/dispose`` + ``POST /assets/{id}/dispose`` — dispose
- ``POST /assets/{id}/archive`` — soft-delete

Money fields are locked for edit once depreciation has posted; the
service layer raises ValueError and we re-render the form with the
error message (422).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.depreciation_model import DepreciationModel
from saebooks.routers.deps import get_web_session
from saebooks.services import assets as svc
from saebooks.services import assets_import as imp_svc
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/assets")


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _form_dropdowns(
    session: AsyncSession, company_id: uuid.UUID
) -> dict[str, Any]:
    """Fetch all the selects the asset form needs."""
    accounts = (
        await session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.is_header.is_(False),
                Account.archived_at.is_(None),
            )
            .order_by(Account.code)
        )
    ).scalars().all()
    models = (
        await session.execute(
            select(DepreciationModel).order_by(DepreciationModel.id)
        )
    ).scalars().all()
    contacts = (
        await session.execute(
            select(Contact)
            .where(
                Contact.company_id == company_id,
                Contact.archived_at.is_(None),
            )
            .order_by(Contact.name)
        )
    ).scalars().all()

    # Split accounts into useful buckets for the form.
    asset_accounts = [a for a in accounts if a.account_type == AccountType.ASSET]
    expense_accounts = [
        a for a in accounts
        if a.account_type in (AccountType.EXPENSE, AccountType.COST_OF_SALES)
    ]
    cash_accounts = [
        a for a in accounts
        if a.account_type == AccountType.ASSET and a.code.startswith("1-1")
    ]
    # Inventory accounts: current asset range 1-13xx (excludes cash/bank 1-11xx,
    # receivables 1-12xx, and fixed-asset 1-3xxx ranges).
    inventory_accounts = [
        a for a in accounts
        if a.account_type == AccountType.ASSET and a.code.startswith("1-13")
    ]
    # If no 1-13xx accounts exist in this CoA, fall back to all asset accounts
    # so the form is never empty.
    if not inventory_accounts:
        inventory_accounts = asset_accounts
    return {
        "asset_accounts": asset_accounts,
        "expense_accounts": expense_accounts,
        "cash_accounts": cash_accounts,
        "inventory_accounts": inventory_accounts,
        "models": models,
        "contacts": contacts,
    }


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid number for {field}: {raw!r}") from exc


def _parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid date for {field}: {raw!r}") from exc


def _optional_uuid(raw: str) -> uuid.UUID | None:
    raw = raw.strip()
    return uuid.UUID(raw) if raw else None


def _optional_date(raw: str, field: str) -> date | None:
    raw = raw.strip()
    return _parse_date(raw, field) if raw else None


# ---------------------------------------------------------------------- #
# List                                                                   #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def assets_list(
    request: Request,
    status: str = Query("active"),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    filter_status: str | None = status if status in {"active", "disposed", "archived"} else None
    assets = await svc.list_assets(
        session,
        company.id,
        status=filter_status,
        include_archived=(status == "archived"),
    )
    return templates.TemplateResponse(
        request,
        "assets/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "assets": assets,
            "status_filter": status,
            "total": len(assets),
        },
    )


# ---------------------------------------------------------------------- #
# Create                                                                 #
# ---------------------------------------------------------------------- #


@router.get("/new", response_class=HTMLResponse)
async def assets_new(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    dropdowns = await _form_dropdowns(session, company.id)
    return templates.TemplateResponse(
        request,
        "assets/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "asset": None,
            "error": None,
            **dropdowns,
        },
    )


def _parse_asset_form(
    *,
    name: str,
    cost_account_id: str,
    accum_dep_account_id: str,
    dep_expense_account_id: str,
    depreciation_model_id: str,
    purchase_date: str,
    in_service_date: str,
    cost: str,
    residual_value: str,
    code: str,
    description: str,
    serial_number: str,
    manufacturer: str,
    model_number: str,
    location: str,
    custody_person: str,
    warranty_end: str,
    purchase_contact_id: str,
    tax_model_id: str = "",
) -> dict[str, Any]:
    return {
        "name": name.strip(),
        "code": code.strip() or None,
        "description": description.strip() or None,
        "cost_account_id": uuid.UUID(cost_account_id),
        "accum_dep_account_id": uuid.UUID(accum_dep_account_id),
        "dep_expense_account_id": uuid.UUID(dep_expense_account_id),
        "depreciation_model_id": depreciation_model_id.strip(),
        "tax_model_id": tax_model_id.strip() or None,
        "purchase_date": _parse_date(purchase_date, "purchase_date"),
        "in_service_date": _optional_date(in_service_date, "in_service_date"),
        "cost": _parse_decimal(cost, "cost"),
        "residual_value": _parse_decimal(residual_value or "0", "residual_value"),
        "serial_number": serial_number.strip() or None,
        "manufacturer": manufacturer.strip() or None,
        "model_number": model_number.strip() or None,
        "location": location.strip() or None,
        "custody_person": custody_person.strip() or None,
        "warranty_end": _optional_date(warranty_end, "warranty_end"),
        "purchase_contact_id": _optional_uuid(purchase_contact_id),
    }


@router.post("", response_model=None)
async def assets_create(
    request: Request,
    name: str = Form(...),
    cost_account_id: str = Form(...),
    accum_dep_account_id: str = Form(...),
    dep_expense_account_id: str = Form(...),
    depreciation_model_id: str = Form(...),
    purchase_date: str = Form(...),
    cost: str = Form(...),
    in_service_date: str = Form(""),
    residual_value: str = Form("0"),
    code: str = Form(""),
    description: str = Form(""),
    serial_number: str = Form(""),
    manufacturer: str = Form(""),
    model_number: str = Form(""),
    location: str = Form(""),
    custody_person: str = Form(""),
    warranty_end: str = Form(""),
    purchase_contact_id: str = Form(""),
    tax_model_id: str = Form(""),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    try:
        kwargs = _parse_asset_form(
            name=name,
            code=code,
            description=description,
            cost_account_id=cost_account_id,
            accum_dep_account_id=accum_dep_account_id,
            dep_expense_account_id=dep_expense_account_id,
            depreciation_model_id=depreciation_model_id,
            tax_model_id=tax_model_id,
            purchase_date=purchase_date,
            in_service_date=in_service_date,
            cost=cost,
            residual_value=residual_value,
            serial_number=serial_number,
            manufacturer=manufacturer,
            model_number=model_number,
            location=location,
            custody_person=custody_person,
            warranty_end=warranty_end,
            purchase_contact_id=purchase_contact_id,
        )
        asset = await svc.create(session, company.id, **kwargs)
    except ValueError as exc:
        await session.rollback()
        dropdowns = await _form_dropdowns(session, company.id)
        return templates.TemplateResponse(
            request,
            "assets/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "asset": None,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/assets/{asset.id}", status_code=303)


# ---------------------------------------------------------------------- #
# CSV bulk import (preview → apply)                                      #
# ---------------------------------------------------------------------- #
#
# Routes MUST be registered before ``/{asset_id}`` — otherwise
# FastAPI's UUID coercion on the catch-all matcher 422s on the
# literal "import" path segment.


@router.get("/import", response_class=HTMLResponse)
async def assets_import_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "assets/import.html",
        {"edition": settings.edition, "error": None},
    )


@router.post("/import/preview", response_class=HTMLResponse)
async def assets_import_preview(
    request: Request,
    file: UploadFile = Form(...),  # noqa: B008
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        rows = imp_svc.parse_assets_csv(raw)
    except imp_svc.AssetImportError as exc:
        return templates.TemplateResponse(
            request,
            "assets/import.html",
            {"edition": settings.edition, "error": str(exc)},
            status_code=400,
        )
    plan = await imp_svc.classify_rows(session, company.id, rows)
    return templates.TemplateResponse(
        request,
        "assets/import_preview.html",
        {
            "edition": settings.edition,
            "plan": plan,
            "raw": raw,
        },
    )


@router.post("/import/apply", response_model=None)
async def assets_import_apply(
    raw: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    company = await _first_company()
    rows = imp_svc.parse_assets_csv(raw)
    plan = await imp_svc.classify_rows(session, company.id, rows)
    written = await imp_svc.apply_import(session, company.id, plan)
    await session.commit()
    skipped = len(plan.skip)
    invalid = len(plan.invalid)
    return RedirectResponse(
        f"/assets?imported={written}&skipped={skipped}&invalid={invalid}",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Detail                                                                 #
# ---------------------------------------------------------------------- #


@router.get("/{asset_id}", response_class=HTMLResponse)
async def assets_detail(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    asset = await svc.get(session, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    company = await session.get(Company, asset.company_id)

    # Compute current NBV as of today for the detail page.
    today = date.today()
    cumulative = await svc.cumulative_depreciation_through(
        session, asset, today
    )
    nbv = (asset.cost - cumulative).quantize(Decimal("0.01"))

    cost_acct = await session.get(Account, asset.cost_account_id)
    accum_acct = await session.get(Account, asset.accum_dep_account_id)
    dep_acct = await session.get(Account, asset.dep_expense_account_id)
    model = await session.get(DepreciationModel, asset.depreciation_model_id)

    return templates.TemplateResponse(
        request,
        "assets/show.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "asset": asset,
            "cost_acct": cost_acct,
            "accum_acct": accum_acct,
            "dep_acct": dep_acct,
            "model": model,
            "nbv": nbv,
            "today": today,
            "cumulative_today": cumulative,
        },
    )


# ---------------------------------------------------------------------- #
# Edit                                                                   #
# ---------------------------------------------------------------------- #


@router.get("/{asset_id}/edit", response_class=HTMLResponse)
async def assets_edit(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    asset = await svc.get(session, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    company = await session.get(Company, asset.company_id)
    dropdowns = await _form_dropdowns(session, asset.company_id)
    return templates.TemplateResponse(
        request,
        "assets/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "asset": asset,
            "error": None,
            **dropdowns,
        },
    )


@router.post("/{asset_id}", response_model=None)
async def assets_update(
    request: Request,
    asset_id: UUID,
    name: str = Form(...),
    cost_account_id: str = Form(...),
    accum_dep_account_id: str = Form(...),
    dep_expense_account_id: str = Form(...),
    depreciation_model_id: str = Form(...),
    purchase_date: str = Form(...),
    cost: str = Form(...),
    in_service_date: str = Form(""),
    residual_value: str = Form("0"),
    code: str = Form(""),
    description: str = Form(""),
    serial_number: str = Form(""),
    manufacturer: str = Form(""),
    model_number: str = Form(""),
    location: str = Form(""),
    custody_person: str = Form(""),
    warranty_end: str = Form(""),
    purchase_contact_id: str = Form(""),
    tax_model_id: str = Form(""),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    try:
        kwargs = _parse_asset_form(
            name=name,
            code=code,
            description=description,
            cost_account_id=cost_account_id,
            accum_dep_account_id=accum_dep_account_id,
            dep_expense_account_id=dep_expense_account_id,
            depreciation_model_id=depreciation_model_id,
            tax_model_id=tax_model_id,
            purchase_date=purchase_date,
            in_service_date=in_service_date,
            cost=cost,
            residual_value=residual_value,
            serial_number=serial_number,
            manufacturer=manufacturer,
            model_number=model_number,
            location=location,
            custody_person=custody_person,
            warranty_end=warranty_end,
            purchase_contact_id=purchase_contact_id,
        )
        # create()-style kwargs include ``code``; svc.update uses raw model
        # column names. Strip the code None (service won't know the field).
        if kwargs.get("code") is None:
            kwargs.pop("code")
        # in_service_date was optional in create — require it for update.
        if kwargs.get("in_service_date") is None:
            kwargs["in_service_date"] = kwargs["purchase_date"]
        await svc.update(session, asset_id, **kwargs)
    except ValueError as exc:
        await session.rollback()
        asset = await svc.get(session, asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found") from exc
        company = await session.get(Company, asset.company_id)
        dropdowns = await _form_dropdowns(session, asset.company_id)
        return templates.TemplateResponse(
            request,
            "assets/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "asset": asset,
                "error": str(exc),
                **dropdowns,
            },
            status_code=422,
        )
    return RedirectResponse(f"/assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Depreciate                                                             #
# ---------------------------------------------------------------------- #


@router.post("/{asset_id}/depreciate", response_model=None)
async def assets_depreciate(
    asset_id: UUID,
    through_date: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    through = _parse_date(through_date, "through_date")
    await svc.post_depreciation(
        session, asset_id, through, posted_by="web"
    )
    return RedirectResponse(f"/assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Dispose                                                                #
# ---------------------------------------------------------------------- #


@router.get("/{asset_id}/dispose", response_class=HTMLResponse)
async def assets_dispose_form(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    asset = await svc.get(session, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    if asset.status != "active":
        raise HTTPException(
            400, f"Cannot dispose — asset status is {asset.status!r}"
        )
    company = await session.get(Company, asset.company_id)
    dropdowns = await _form_dropdowns(session, asset.company_id)
    return templates.TemplateResponse(
        request,
        "assets/dispose.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "asset": asset,
            "cash_accounts": dropdowns["cash_accounts"],
            "today": date.today(),
            "error": None,
        },
    )


@router.post("/{asset_id}/dispose", response_model=None)
async def assets_dispose(
    request: Request,
    asset_id: UUID,
    disposal_date: str = Form(...),
    proceeds: str = Form(...),
    cash_account_id: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    try:
        await svc.dispose_asset(
            session,
            asset_id,
            disposal_date=_parse_date(disposal_date, "disposal_date"),
            proceeds=_parse_decimal(proceeds, "proceeds"),
            cash_account_id=uuid.UUID(cash_account_id),
            posted_by="web",
        )
    except ValueError as exc:
        await session.rollback()
        asset = await svc.get(session, asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found") from exc
        company = await session.get(Company, asset.company_id)
        dropdowns = await _form_dropdowns(session, asset.company_id)
        return templates.TemplateResponse(
            request,
            "assets/dispose.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "asset": asset,
                "cash_accounts": dropdowns["cash_accounts"],
                "today": date.today(),
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse(f"/assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Partial disposal (MM/3)                                                #
# ---------------------------------------------------------------------- #


@router.get("/{asset_id}/dispose-partial", response_class=HTMLResponse)
async def assets_dispose_partial_form(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    asset = await svc.get(session, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    if asset.status != "active":
        raise HTTPException(
            400,
            f"Cannot partially dispose — asset status is {asset.status!r}",
        )
    company = await session.get(Company, asset.company_id)
    dropdowns = await _form_dropdowns(session, asset.company_id)
    return templates.TemplateResponse(
        request,
        "assets/dispose_partial.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "asset": asset,
            "cash_accounts": dropdowns["cash_accounts"],
            "today": date.today(),
            "error": None,
        },
    )


@router.post("/{asset_id}/dispose-partial", response_model=None)
async def assets_dispose_partial(
    request: Request,
    asset_id: UUID,
    fraction: str = Form(...),
    disposal_date: str = Form(...),
    proceeds: str = Form(...),
    cash_account_id: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    try:
        parent, _child, _gl = await svc.dispose_partial(
            session,
            asset_id,
            fraction=_parse_decimal(fraction, "fraction"),
            disposal_date=_parse_date(disposal_date, "disposal_date"),
            proceeds=_parse_decimal(proceeds, "proceeds"),
            cash_account_id=uuid.UUID(cash_account_id),
            posted_by="web",
        )
    except ValueError as exc:
        await session.rollback()
        asset = await svc.get(session, asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found") from exc
        company = await session.get(Company, asset.company_id)
        dropdowns = await _form_dropdowns(session, asset.company_id)
        return templates.TemplateResponse(
            request,
            "assets/dispose_partial.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "asset": asset,
                "cash_accounts": dropdowns["cash_accounts"],
                "today": date.today(),
                "error": str(exc),
            },
            status_code=422,
        )
    # Redirect back to the parent — the child row is visible on the list.
    return RedirectResponse(f"/assets/{parent.id}", status_code=303)


# ---------------------------------------------------------------------- #
# Convert to inventory (MOTR-3)                                          #
# ---------------------------------------------------------------------- #


@router.get("/{asset_id}/convert", response_class=HTMLResponse)
async def assets_convert_form(
    request: Request,
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    asset = await svc.get(session, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    if asset.status != "active":
        raise HTTPException(
            400, f"Cannot convert — asset status is {asset.status!r}"
        )
    company = await session.get(Company, asset.company_id)
    dropdowns = await _form_dropdowns(session, asset.company_id)

    today = date.today()
    cumulative = await svc.cumulative_depreciation_through(
        session, asset, today
    )
    nbv = (asset.cost - cumulative).quantize(Decimal("0.01"))

    return templates.TemplateResponse(
        request,
        "assets/convert.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "asset": asset,
            "inventory_accounts": dropdowns["inventory_accounts"],
            "today": today,
            "nbv": nbv,
            "error": None,
        },
    )


@router.post("/{asset_id}/convert", response_model=None)
async def assets_convert(
    request: Request,
    asset_id: UUID,
    conversion_date: str = Form(...),
    inventory_account_id: str = Form(...),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    try:
        await svc.convert_to_inventory(
            session,
            asset_id,
            conversion_date=_parse_date(conversion_date, "conversion_date"),
            inventory_account_id=uuid.UUID(inventory_account_id),
            posted_by="web",
        )
    except ValueError as exc:
        await session.rollback()
        asset = await svc.get(session, asset_id)
        if asset is None:
            raise HTTPException(404, "Asset not found") from exc
        company = await session.get(Company, asset.company_id)
        dropdowns = await _form_dropdowns(session, asset.company_id)
        today = date.today()
        from saebooks.services import assets as _svc
        cumulative = await _svc.cumulative_depreciation_through(
            session, asset, today
        )
        nbv = (asset.cost - cumulative).quantize(Decimal("0.01"))
        return templates.TemplateResponse(
            request,
            "assets/convert.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "asset": asset,
                "inventory_accounts": dropdowns["inventory_accounts"],
                "today": today,
                "nbv": nbv,
                "error": str(exc),
            },
            status_code=422,
        )
    return RedirectResponse(f"/assets/{asset_id}", status_code=303)


# ---------------------------------------------------------------------- #
# Archive                                                                #
# ---------------------------------------------------------------------- #


@router.post("/{asset_id}/archive")
async def assets_archive(
    asset_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    await svc.archive(session, asset_id)
    return RedirectResponse("/assets", status_code=303)
