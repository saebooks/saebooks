"""Contact management routes."""
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.config import settings
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.routers.deps import get_web_session
from saebooks.services import contacts as svc
from saebooks.services.abr import AbrError, AbrNotConfiguredError, lookup_abn
from saebooks.services.features import FLAG_ABR_LOOKUP, is_enabled, require_feature
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(prefix="/contacts")
# Beneficiary register lives at /beneficiaries (no prefix) — separate router so
# it can be mounted at the top level alongside /contacts.
beneficiaries_router = APIRouter()


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _form_dropdowns(session, company_id: uuid.UUID):
    """Fetch accounts and tax codes for form selects."""
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
    tax_codes = (
        await session.execute(
            select(TaxCode)
            .where(
                TaxCode.company_id == company_id,
                TaxCode.archived_at.is_(None),
            )
            .order_by(TaxCode.code)
        )
    ).scalars().all()
    return accounts, tax_codes


def _parse_form_fields(
    name: str,
    contact_type: str,
    email: str,
    phone: str,
    abn: str,
    address_line1: str,
    address_line2: str,
    city: str,
    state: str,
    postcode: str,
    notes: str,
    default_account_id: str,
    default_tax_code: str,
    tfn: str = "",
    share_percentage: str = "",
    default_income_classification: str = "",
    is_tpar_supplier: str = "off",
) -> dict:
    """Normalise form values into kwargs for the service layer."""
    from decimal import Decimal, InvalidOperation

    share_pct = None
    if share_percentage.strip():
        try:
            share_pct = Decimal(share_percentage.strip())
        except InvalidOperation:
            raise ValueError(f"Invalid share percentage: {share_percentage!r}")

    return {
        "name": name,
        "contact_type": ContactType(contact_type),
        "email": email.strip() or None,
        "phone": phone.strip() or None,
        "abn": abn.strip() or None,
        "address_line1": address_line1.strip() or None,
        "address_line2": address_line2.strip() or None,
        "city": city.strip() or None,
        "state": state.strip() or None,
        "postcode": postcode.strip() or None,
        "notes": notes.strip() or None,
        "default_account_id": uuid.UUID(default_account_id) if default_account_id.strip() else None,
        "default_tax_code": default_tax_code.strip() or None,
        "tfn": tfn.strip() or None,
        "share_percentage": share_pct,
        "default_income_classification": default_income_classification.strip() or None,
        "is_tpar_supplier": is_tpar_supplier.lower() in {"1", "true", "on"},
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@beneficiaries_router.get("/beneficiaries", response_class=HTMLResponse)
async def beneficiaries_list(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Beneficiary register — all BENEFICIARY contacts for the active company."""
    company = await _first_company()
    contacts = await svc.list_active(
        session, company.id, contact_type=ContactType.BENEFICIARY
    )
    return templates.TemplateResponse(
        request,
        "contacts/beneficiaries.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "beneficiaries": contacts,
        },
    )


@router.get("", response_class=HTMLResponse)
async def contacts_list(
    request: Request,
    type: str | None = Query(None),
    q: str | None = Query(None),
    one_off: str | None = Query(None),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    contact_type = None
    if type and type in ("CUSTOMER", "SUPPLIER", "BOTH", "BENEFICIARY"):
        contact_type = ContactType(type)

    one_off_filter: bool | None
    if one_off == "true":
        one_off_filter = True
    elif one_off == "false":
        one_off_filter = False
    else:
        one_off_filter = None  # "all" — show everything (back-compat default)

    contacts = await svc.list_active(
        session,
        company.id,
        contact_type=contact_type,
        search=q or None,
        is_one_off=one_off_filter,
    )

    return templates.TemplateResponse(
        request,
        "contacts/list.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "total": len(contacts),
            "contacts": contacts,
            "type_filter": type or "all",
            "search_q": q or "",
            "one_off_filter": one_off or "all",
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def contacts_new(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    accounts, tax_codes = await _form_dropdowns(session, company.id)
    return templates.TemplateResponse(
        request,
        "contacts/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name,
            "contact": None,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "error": None,
            "abr_enabled": is_enabled(FLAG_ABR_LOOKUP),
        },
    )


@router.post("", response_model=None)
async def contacts_create(
    request: Request,
    name: str = Form(...),
    contact_type: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    abn: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    postcode: str = Form(""),
    notes: str = Form(""),
    default_account_id: str = Form(""),
    default_tax_code: str = Form(""),
    tfn: str = Form(""),
    share_percentage: str = Form(""),
    default_income_classification: str = Form(""),
    is_tpar_supplier: str = Form("off"),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    company = await _first_company()
    kwargs = _parse_form_fields(
        name, contact_type, email, phone, abn,
        address_line1, address_line2, city, state, postcode,
        notes, default_account_id, default_tax_code,
        tfn, share_percentage, default_income_classification,
        is_tpar_supplier,
    )
    try:
        await svc.create(session, company.id, **kwargs)
    except ValueError as exc:
        await session.rollback()
        accounts, tax_codes = await _form_dropdowns(session, company.id)
        return templates.TemplateResponse(
            request,
            "contacts/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name,
                "contact": None,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "error": str(exc),
                "abr_enabled": is_enabled(FLAG_ABR_LOOKUP),
            },
            status_code=422,
        )
    return RedirectResponse("/contacts", status_code=303)


# ---------------------------------------------------------------------------
# HTMX search (autocomplete fragment)
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse)
async def contacts_search(
    request: Request,
    q: str = Query(""),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    company = await _first_company()
    results = await svc.search_by_name(session, company.id, q, limit=10)
    items = "".join(
        f'<li data-id="{c.id}" data-name="{c.name}">{c.name}'
        f'<span class="dim"> — {c.contact_type.value.lower()}</span></li>'
        for c in results
    )
    return HTMLResponse(f"<ul class='ac-results'>{items}</ul>" if items else "")


# ---------------------------------------------------------------------------
# Bulk-tag one-off (Jinja-side, cookie-authed). Registered ABOVE /{contact_id}
# so FastAPI matches the literal path first.
# ---------------------------------------------------------------------------


@router.post("/bulk-tag-one-off")
async def contacts_bulk_tag_one_off(
    request: Request,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    """Flip ``is_one_off`` on the selected contacts and redirect back to the list.

    Form fields: ``contact_ids`` (multiple), ``is_one_off`` (``true``/``false``),
    optional ``return_to`` so the user lands back on the same filtered view.
    """
    form = await request.form()
    raw_ids = form.getlist("contact_ids")
    flag_raw = (form.get("is_one_off") or "true").lower()
    is_one_off = flag_raw == "true"
    return_to = form.get("return_to") or "/contacts"

    tenant_id = resolve_tenant_id(request)
    parsed_ids: list[UUID] = []
    for raw in raw_ids:
        try:
            parsed_ids.append(UUID(raw))
        except (ValueError, TypeError):
            continue

    for cid in parsed_ids:
        existing = await svc.get(session, cid, tenant_id=tenant_id)
        if existing is None or existing.archived_at is not None:
            continue
        if existing.is_one_off == is_one_off:
            continue
        try:
            await svc.update(
                session,
                cid,
                actor="web",
                tenant_id=tenant_id,
                is_one_off=is_one_off,
            )
        except (ValueError, svc.VersionConflict):
            continue

    return RedirectResponse(return_to, status_code=303)


# ---------------------------------------------------------------------------
# ABR lookup (Enterprise — FLAG_ABR_LOOKUP). Registered ABOVE /{contact_id}
# so FastAPI matches the literal paths first (otherwise "abr-lookup" hits
# the UUID path matcher on /{contact_id} and 422s).
# ---------------------------------------------------------------------------


@router.post(
    "/abr-lookup",
    response_class=HTMLResponse,
    dependencies=[Depends(require_feature(FLAG_ABR_LOOKUP))],
)
async def contacts_abr_lookup(
    request: Request,
    abn: str = Form(...),
) -> HTMLResponse:
    """HTMX target: look up an ABN and render a preview fragment.

    Used from the /contacts/new form where there's no contact row yet
    to apply against. The fragment is display-only; the user edits
    the form fields by hand after reviewing.
    """
    try:
        result = await lookup_abn(abn, settings=settings)
    except AbrNotConfiguredError:
        return templates.TemplateResponse(
            request,
            "contacts/_abr_error.html",
            {"message": "ABR API is not configured. Set ABR_API_GUID."},
            status_code=502,
        )
    except AbrError as exc:
        return templates.TemplateResponse(
            request,
            "contacts/_abr_error.html",
            {"message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "contacts/_abr_result.html",
        {"result": result},
    )


@router.post(
    "/{contact_id}/abr-apply",
    response_class=HTMLResponse,
    dependencies=[Depends(require_feature(FLAG_ABR_LOOKUP))],
)
async def contacts_abr_apply(
    request: Request,
    contact_id: UUID,
    abn: str = Form(...),
    overwrite: str = Form(""),
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    """Fetch ABR and merge into the live Contact row.

    Returns an HTMX fragment summarising the fields that changed. The
    merge is conservative by default — only empty fields are filled.
    Passing ``overwrite=on`` replaces populated fields too.
    """
    try:
        lookup = await lookup_abn(abn, settings=settings)
    except AbrNotConfiguredError:
        return templates.TemplateResponse(
            request,
            "contacts/_abr_error.html",
            {"message": "ABR API is not configured. Set ABR_API_GUID."},
            status_code=502,
        )
    except AbrError as exc:
        return templates.TemplateResponse(
            request,
            "contacts/_abr_error.html",
            {"message": str(exc)},
            status_code=400,
        )

    contact = await svc.get(session, contact_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    from saebooks.services.abr import apply_to_contact

    changed = apply_to_contact(
        contact, lookup, overwrite=overwrite.lower() in {"1", "true", "on"}
    )
    await session.commit()

    return templates.TemplateResponse(
        request,
        "contacts/_abr_applied.html",
        {
            "result": lookup,
            "changed": changed,
            "contact_id": contact_id,
        },
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/{contact_id}", response_class=HTMLResponse)
async def contacts_detail(
    request: Request,
    contact_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    contact = await svc.get(session, contact_id, tenant_id=tenant_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    company = await session.get(Company, contact.company_id)

    # Resolve default account name for display
    default_account = None
    if contact.default_account_id:
        default_account = await session.get(Account, contact.default_account_id)

    return templates.TemplateResponse(
        request,
        "contacts/detail.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "contact": contact,
            "default_account": default_account,
        },
    )


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@router.get("/{contact_id}/edit", response_class=HTMLResponse)
async def contacts_edit(
    request: Request,
    contact_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    contact = await svc.get(session, contact_id, tenant_id=tenant_id)
    if contact is None:
        raise HTTPException(404, "Contact not found")
    company = await session.get(Company, contact.company_id)
    accounts, tax_codes = await _form_dropdowns(session, contact.company_id)

    return templates.TemplateResponse(
        request,
        "contacts/form.html",
        {
            "edition": settings.edition,
            "company_name": company.name if company else "",
            "contact": contact,
            "accounts": accounts,
            "tax_codes": tax_codes,
            "error": None,
            "abr_enabled": is_enabled(FLAG_ABR_LOOKUP),
        },
    )


@router.post("/{contact_id}", response_model=None)
async def contacts_update(
    request: Request,
    contact_id: UUID,
    name: str = Form(...),
    contact_type: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    abn: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    postcode: str = Form(""),
    notes: str = Form(""),
    default_account_id: str = Form(""),
    default_tax_code: str = Form(""),
    tfn: str = Form(""),
    share_percentage: str = Form(""),
    default_income_classification: str = Form(""),
    is_tpar_supplier: str = Form("off"),
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse | HTMLResponse:
    tenant_id = resolve_tenant_id(request)
    kwargs = _parse_form_fields(
        name, contact_type, email, phone, abn,
        address_line1, address_line2, city, state, postcode,
        notes, default_account_id, default_tax_code,
        tfn, share_percentage, default_income_classification,
        is_tpar_supplier,
    )
    try:
        await svc.update(
            session, contact_id, performed_by="web",
            tenant_id=tenant_id, **kwargs,
        )
    except ValueError as exc:
        await session.rollback()
        contact = await svc.get(session, contact_id, tenant_id=tenant_id)
        if contact is None:
            raise HTTPException(404, "Contact not found") from exc
        company = await session.get(Company, contact.company_id)
        accounts, tax_codes = await _form_dropdowns(session, contact.company_id)
        return templates.TemplateResponse(
            request,
            "contacts/form.html",
            {
                "edition": settings.edition,
                "company_name": company.name if company else "",
                "contact": contact,
                "accounts": accounts,
                "tax_codes": tax_codes,
                "error": str(exc),
                "abr_enabled": is_enabled(FLAG_ABR_LOOKUP),
            },
            status_code=422,
        )
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


# ---------------------------------------------------------------------------
# Archive (soft-delete)
# ---------------------------------------------------------------------------


@router.post("/{contact_id}/archive")
async def contacts_archive(
    request: Request,
    contact_id: UUID,
    session: AsyncSession = Depends(get_web_session),
) -> RedirectResponse:
    tenant_id = resolve_tenant_id(request)
    await svc.archive(
        session, contact_id, performed_by="web", tenant_id=tenant_id
    )
    return RedirectResponse("/contacts", status_code=303)
