"""JSON router — ``/api/v1/super-funds``.

CRUD over SuperFund. Sensitive SMSF bank fields are write-only:
they accept plaintext on POST/PATCH and store ciphertext, but the
default response carries only a ``has_smsf_bank`` boolean. A
dedicated ``/super-funds/{id}/reveal-bank`` endpoint (Phase 2,
audit-logged) returns plaintext.

* Bearer-token auth via ``require_bearer``.
* Optimistic locking via ``If-Match: <version>`` on update/delete/set-default.
* ``DELETE`` is a soft-archive (only when not the company default).
* ``POST /{id}/set-default`` flips the default flag atomically.
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
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    SuperFundCreate,
    SuperFundListOut,
    SuperFundOut,
    SuperFundUpdate,
)
from saebooks.models.super_fund import SuperFund
from saebooks.services import super_funds as svc
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.super_funds import SuperFundError

router = APIRouter(
    prefix="/super-funds",
    tags=["super-funds"],
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


def _to_dto(fund: SuperFund) -> dict[str, Any]:
    return json.loads(
        SuperFundOut(
            id=fund.id,
            company_id=fund.company_id,
            name=fund.name,
            usi=fund.usi,
            spin=fund.spin,
            is_smsf=fund.is_smsf,
            employer_abn=fund.employer_abn,
            esa=fund.esa,
            has_smsf_bank=bool(
                fund.smsf_bsb_encrypted or fund.smsf_account_number_encrypted
            ),
            is_default=fund.is_default,
            version=fund.version,
            created_at=fund.created_at,
            updated_at=fund.updated_at,
            archived_at=fund.archived_at,
        ).model_dump_json()
    )


def _translate_error(exc: SuperFundError) -> HTTPException:
    if exc.code in {"version_mismatch"}:
        return HTTPException(412, str(exc))
    if exc.code in {"not_found"}:
        return HTTPException(404, str(exc))
    if exc.code in {"cannot_archive_default"}:
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# List / get / create
# ---------------------------------------------------------------------------


@router.get("", response_model=SuperFundListOut)
async def list_super_funds(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> SuperFundListOut:
    items, total = await svc.list_funds(
        session, company_id=company_id, limit=limit, offset=offset
    )
    return SuperFundListOut(
        items=[SuperFundOut.model_validate(_to_dto(f)) for f in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{fund_id}", response_model=SuperFundOut)
async def get_super_fund(
    fund_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> SuperFundOut:
    fund = await svc.get(session, company_id=company_id, fund_id=fund_id)
    if fund is None:
        raise HTTPException(404, "super fund not found")
    return SuperFundOut.model_validate(_to_dto(fund))


@router.post("", response_model=SuperFundOut, status_code=201)
async def create_super_fund(
    request: Request,
    body: SuperFundCreate,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    tenant_id = resolve_tenant_id(request)
    try:
        fund = await svc.create(
            session,
            company_id=company_id,
            tenant_id=tenant_id,
            name=body.name,
            is_smsf=body.is_smsf,
            usi=body.usi,
            employer_abn=body.employer_abn,
            esa=body.esa,
            smsf_bsb=body.smsf_bsb,
            smsf_account_number=body.smsf_account_number,
            smsf_account_name=body.smsf_account_name,
            is_default=body.is_default,
        )
        await session.commit()
    except SuperFundError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(fund),
        status_code=201,
        headers={"ETag": f'"{fund.version}"'},
    )


# ---------------------------------------------------------------------------
# Update / archive / set-default
# ---------------------------------------------------------------------------


@router.patch("/{fund_id}", response_model=SuperFundOut)
async def update_super_fund(
    fund_id: uuid.UUID,
    body: SuperFundUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    fund = await svc.get(session, company_id=company_id, fund_id=fund_id)
    if fund is None:
        raise HTTPException(404, "super fund not found")
    expected_version = _parse_if_match(if_match)
    fields = body.model_dump(exclude_unset=True)
    try:
        fund = await svc.update(
            session,
            fund=fund,
            expected_version=expected_version,
            **fields,
        )
        await session.commit()
    except SuperFundError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(fund), headers={"ETag": f'"{fund.version}"'}
    )


@router.delete("/{fund_id}", status_code=204)
async def archive_super_fund(
    fund_id: uuid.UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Response:
    fund = await svc.get(session, company_id=company_id, fund_id=fund_id)
    if fund is None:
        raise HTTPException(404, "super fund not found")
    if hard:
        await hard_delete_with_audit(
            session, fund, "super_funds", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)
    expected_version = _parse_if_match(if_match)
    if expected_version is not None and fund.version != expected_version:
        raise HTTPException(412, "version mismatch")
    try:
        await svc.archive(session, fund=fund)
        await session.commit()
    except SuperFundError as exc:
        raise _translate_error(exc) from exc
    return Response(status_code=204)


@router.post("/{fund_id}/set-default", response_model=SuperFundOut)
async def set_default_super_fund(
    fund_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    company_id: uuid.UUID = Depends(get_active_company_id),
) -> JSONResponse:
    try:
        fund = await svc.set_default(
            session, company_id=company_id, fund_id=fund_id
        )
        await session.commit()
    except SuperFundError as exc:
        raise _translate_error(exc) from exc
    return JSONResponse(
        _to_dto(fund), headers={"ETag": f'"{fund.version}"'}
    )
