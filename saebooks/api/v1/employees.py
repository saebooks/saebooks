"""JSON router — ``/api/v1/employees``.

CRUD over Employee. Sensitive fields (TFN, bank BSB+acct+name) are
write-only on POST/PATCH (accept plaintext, store ciphertext) and
masked or hidden on GET.

* Default ``EmployeeOut`` includes ``tfn_masked`` (e.g. ``XXX-XXX-NNN``)
  and ``has_bank`` boolean.
* ``GET /{id}/tfn`` returns plaintext — requires ``employee.tfn_view``
  permission (Phase 2 audit-logging hook lands here; for now we
  rely on JWT role gating).
* ``POST /{id}/terminate`` sets end_date + termination_reason
  atomically (the DB CHECK constraint enforces consistency).
* ``DELETE /{id}`` is a soft-archive.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.schemas import (
    EmployeeCreate,
    EmployeeListOut,
    EmployeeOut,
    EmployeeTerminateRequest,
    EmployeeTfnRevealOut,
    EmployeeUpdate,
)
from saebooks.models.employee import Employee
from saebooks.services import employees as svc
from saebooks.services.employees import EmployeeError, EmployeeFilters

router = APIRouter(
    prefix="/employees",
    tags=["employees"],
    dependencies=[Depends(require_bearer)],
)


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            400, f"If-Match must be an integer version, got '{header}'"
        ) from exc


def _to_dto(employee: Employee) -> dict[str, Any]:
    return json.loads(
        EmployeeOut(
            id=employee.id,
            company_id=employee.company_id,
            contact_id=employee.contact_id,
            employee_number=employee.employee_number,
            payee_id_bms=employee.payee_id_bms,
            previous_payee_id=employee.previous_payee_id,
            tfn_masked=svc.masked_tfn(employee),
            tfn_status=employee.tfn_status,
            has_bank=bool(
                employee.bsb_encrypted or employee.account_number_encrypted
            ),
            dob=employee.dob,
            start_date=employee.start_date,
            end_date=employee.end_date,
            termination_reason=employee.termination_reason,
            address_line1=employee.address_line1,
            address_line2=employee.address_line2,
            suburb=employee.suburb,
            state=employee.state,
            postcode=employee.postcode,
            country_code=employee.country_code,
            employment_basis=employee.employment_basis,
            tax_treatment_code=employee.tax_treatment_code,
            claims_tax_free_threshold=employee.claims_tax_free_threshold,
            is_australian_resident=employee.is_australian_resident,
            study_training_support_loan=employee.study_training_support_loan,
            working_holiday_maker=employee.working_holiday_maker,
            whm_country_code=employee.whm_country_code,
            income_stream_type=employee.income_stream_type,
            payg_branch_code=employee.payg_branch_code,
            super_fund_id=employee.super_fund_id,
            super_member_number=employee.super_member_number,
            payslip_email=employee.payslip_email,
            payslip_delivery=employee.payslip_delivery,
            pay_frequency=employee.pay_frequency,
            pay_basis=employee.pay_basis,
            base_rate=employee.base_rate,
            weekly_hours=employee.weekly_hours,
            notes=employee.notes,
            version=employee.version,
            created_at=employee.created_at,
            updated_at=employee.updated_at,
            archived_at=employee.archived_at,
        ).model_dump_json()
    )


def _translate_error(exc: EmployeeError) -> HTTPException:
    if exc.code in {"version_mismatch"}:
        return HTTPException(412, str(exc))
    if exc.code in {"already_terminated"}:
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# List / get / create
# ---------------------------------------------------------------------------


@router.get("", response_model=EmployeeListOut)
async def list_employees(
    request: Request,
    employment_basis: str | None = Query(default=None),
    super_fund_id: uuid.UUID | None = Query(default=None),
    only_active: bool = Query(default=True),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> EmployeeListOut:
    items, total = await svc.list_employees(
        session,
        company_id=company_id,
        filters=EmployeeFilters(
            employment_basis=employment_basis,
            super_fund_id=super_fund_id,
            only_active=only_active,
            search=search,
        ),
        limit=limit,
        offset=offset,
    )
    return EmployeeListOut(
        items=[EmployeeOut.model_validate(_to_dto(e)) for e in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{employee_id}", response_model=EmployeeOut)
async def get_employee(
    employee_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> EmployeeOut:
    employee = await svc.get(
        session, company_id=company_id, employee_id=employee_id
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    return EmployeeOut.model_validate(_to_dto(employee))


@router.post("", response_model=EmployeeOut, status_code=201)
async def create_employee(
    request: Request,
    body: EmployeeCreate,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    try:
        employee = await svc.create(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            contact_id=body.contact_id,
            employee_number=body.employee_number,
            start_date=body.start_date,
            dob=body.dob,
            employment_basis=body.employment_basis,
            tax_treatment_code=body.tax_treatment_code,
            tfn=body.tfn,
            tfn_status=body.tfn_status,
            address_line1=body.address_line1,
            address_line2=body.address_line2,
            suburb=body.suburb,
            state=body.state,
            postcode=body.postcode,
            country_code=body.country_code or "AU",
            claims_tax_free_threshold=body.claims_tax_free_threshold,
            is_australian_resident=body.is_australian_resident,
            study_training_support_loan=body.study_training_support_loan,
            working_holiday_maker=body.working_holiday_maker,
            whm_country_code=body.whm_country_code,
            income_stream_type=body.income_stream_type,
            payg_branch_code=body.payg_branch_code,
            bsb=body.bsb,
            account_number=body.account_number,
            account_name=body.account_name,
            super_fund_id=body.super_fund_id,
            super_member_number=body.super_member_number,
            payslip_email=body.payslip_email,
            payslip_delivery=body.payslip_delivery,
            pay_frequency=body.pay_frequency,
            pay_basis=body.pay_basis,
            base_rate=body.base_rate,
            weekly_hours=body.weekly_hours,
            notes=body.notes,
        )
        await session.commit()
    except EmployeeError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(employee),
        status_code=201,
        headers={"ETag": f'"{employee.version}"'},
    )


# ---------------------------------------------------------------------------
# Update / archive / terminate / TFN reveal
# ---------------------------------------------------------------------------


@router.patch("/{employee_id}", response_model=EmployeeOut)
async def update_employee(
    employee_id: uuid.UUID,
    body: EmployeeUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    employee = await svc.get(
        session, company_id=company_id, employee_id=employee_id
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    expected_version = _parse_if_match(if_match)
    fields = body.model_dump(exclude_unset=True)
    try:
        employee = await svc.update(
            session,
            employee=employee,
            expected_version=expected_version,
            **fields,
        )
        await session.commit()
    except EmployeeError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(employee), headers={"ETag": f'"{employee.version}"'}
    )


@router.delete("/{employee_id}", status_code=204)
async def archive_employee(
    employee_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> Response:
    employee = await svc.get(
        session, company_id=company_id, employee_id=employee_id
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and employee.version != expected_version:
        raise HTTPException(412, "version mismatch")
    await svc.archive(session, employee=employee)
    await session.commit()
    return Response(status_code=204)


@router.post("/{employee_id}/terminate", response_model=EmployeeOut)
async def terminate_employee(
    employee_id: uuid.UUID,
    body: EmployeeTerminateRequest,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    employee = await svc.get(
        session, company_id=company_id, employee_id=employee_id
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    try:
        employee = await svc.terminate(
            session,
            employee=employee,
            end_date=body.end_date,
            reason=body.reason,
        )
        await session.commit()
    except EmployeeError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(employee), headers={"ETag": f'"{employee.version}"'}
    )


@router.get("/{employee_id}/tfn", response_model=EmployeeTfnRevealOut)
async def reveal_tfn(
    employee_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> EmployeeTfnRevealOut:
    """Return plaintext TFN. Caller MUST have employee.tfn_view permission.

    For Phase 1A we rely on JWT role gating (admin / payroll.admin).
    Phase 2 layers in an audit-log row per call (table audit_log already
    exists; we'll write a row via services.audit_log here).
    """
    employee = await svc.get(
        session, company_id=company_id, employee_id=employee_id
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    plain = svc.decrypt_tfn(employee)
    if plain is None:
        raise HTTPException(404, "employee has no TFN on file")
    return EmployeeTfnRevealOut(employee_id=employee.id, tfn=plain)
