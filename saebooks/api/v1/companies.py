"""Pure JSON companies router — ``/api/v1/companies``.

Phase 1 tier-1 entity. FLAG_MULTI_COMPANY exists in the codebase.

Endpoints:
  GET  /api/v1/companies          — list all active companies
  GET  /api/v1/companies/{id}     — get one company
  PATCH /api/v1/companies/{id}    — update metadata with If-Match
  GET  /api/v1/companies/{id}/gst-backdate-preview — preview affected invoices

Create and archive are intentionally omitted from the JSON API at
Phase 1: creating companies requires licence-cap enforcement via the
portal JWT (Phase 2+), and archiving a company is a destructive
multi-step operation that needs explicit UX flow. The Jinja UI still
handles those paths via the service layer.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.api.v1.schemas import (
    CompanyConflictBody,
    CompanyListOut,
    CompanyOut,
    CompanyUpdate,
)
from saebooks.models.company import Company
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.services import companies as svc
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response

router = APIRouter(
    prefix="/companies",
    tags=["companies"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _first_company_id(session: AsyncSession, tenant_id: UUID) -> UUID:
    """Return the first active company for the request tenant."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
    return company.id


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _parse_idempotency_key(header: str | None) -> str | None:
    """Return the raw idempotency key string, or None if absent."""
    if header is None or not header.strip():
        return None
    return header.strip()


def _dump(company: Company) -> dict[str, Any]:
    return json.loads(CompanyOut.model_validate(company).model_dump_json())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CompanyListOut)
async def list_companies(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CompanyListOut:
    tenant_id = resolve_tenant_id(request)
    total_stmt = (
        select(func.count())
        .select_from(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
    )
    total = (await session.execute(total_stmt)).scalar_one()
    stmt = (
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.name)
        .offset(offset)
        .limit(limit)
    )
    companies = list((await session.execute(stmt)).scalars().all())
    return CompanyListOut(
        items=[CompanyOut.model_validate(c) for c in companies],
        total=total,
    )


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(
    request: Request,
    company_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> CompanyOut:
    tenant_id = resolve_tenant_id(request)
    company = await svc.get(session, company_id)
    if company is None or company.archived_at is not None:
        raise HTTPException(404, "Company not found")
    if company.tenant_id != tenant_id:
        raise HTTPException(404, "Company not found")
    return CompanyOut.model_validate(company)


# ---------------------------------------------------------------------------
# Update (PATCH with If-Match)
# ---------------------------------------------------------------------------


@router.patch(
    "/{company_id}",
    responses={
        200: {"model": CompanyOut},
        409: {"model": CompanyConflictBody, "description": "Version mismatch"},
    },
)
async def update_company(
    request: Request,
    company_id: UUID,
    payload: CompanyUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with company version is required")
    key = _parse_idempotency_key(idempotency_key)

    # Belt-and-braces tenant check before write
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, company_id)
    if existing is None or existing.archived_at is not None or existing.tenant_id != tenant_id:
        raise HTTPException(404, "Company not found")

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "A request with this idempotency key is currently being processed. Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    try:
        company = await svc.update(
            session,
            company_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **payload.model_dump(exclude_unset=True),
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = CompanyConflictBody(
            detail="version mismatch",
            current=CompanyOut.model_validate(exc.current),
        ).model_dump(mode="json")
        if key is not None:
            await store_response(session, key, 409, json.dumps(body).encode())
            await session.commit()
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(company)
    body = _dump(company)
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Hard-delete (admin only — gap ADMIN-DELETE-1)
# ---------------------------------------------------------------------------

# Tables that hold a company_id FK — checked at hard-delete pre-flight.
# The DELETE blocks if ANY of these have any row referencing the target
# company (archived OR live), since archived rows still pin the FK.
# Sourced from `SELECT table_name FROM information_schema.columns WHERE
# column_name = 'company_id'` against a freshly-migrated DB.
_COMPANY_REF_TABLES: tuple[str, ...] = (
    "invoices",
    "bills",
    "payments",
    "journal_entries",
    "allocation_rules",
    "bank_statement_lines",
    "fixed_assets",
    "recurring_invoices",
    "contacts",
    "credit_notes",
    "accounts",
    "account_ranges",
    "bank_rules",
    "budgets",
    "items",
    "journal_templates",
    "tax_codes",
    "projects",
    "departments",
    "cost_centres",
    "trust_distributions",
    "ato_sbr_configs",
    "document_counters",
    "bank_feed_clients",
    "bank_feed_accounts",
    "period_locks",
)


async def _company_ref_counts(
    session: AsyncSession, company_id: UUID
) -> dict[str, int]:
    """Return non-zero ref counts per table for a candidate company hard-delete."""
    counts: dict[str, int] = {}
    for table in _COMPANY_REF_TABLES:
        assert table in _COMPANY_REF_TABLES, f"table {table!r} not in allowed list"  # noqa: S101
        result = await session.execute(
            text("SELECT count(*) FROM " + table + " WHERE company_id = :cid"),  # noqa: S608
            {"cid": str(company_id)},
        )
        n = int(result.scalar() or 0)
        if n > 0:
            counts[table] = n
    return counts


@router.delete(
    "/{company_id}",
    responses={
        204: {"description": "Deleted"},
        409: {"description": "Company has linked rows; hard-delete the rows first."},
    },
)
async def hard_delete_company(
    request: Request,
    company_id: UUID,
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    if not hard:
        raise HTTPException(
            400,
            "Company DELETE is admin-only and requires ?hard=true",
        )
    tenant_id = resolve_tenant_id(request)
    company = await svc.get(session, company_id)
    if company is None or company.tenant_id != tenant_id:
        raise HTTPException(404, "Company not found")

    blocking = await _company_ref_counts(session, company_id)
    if blocking:
        return JSONResponse(
            {
                "detail": "Company has linked rows; hard-delete the rows first.",
                "blocking_refs": blocking,
            },
            status_code=409,
        )

    await hard_delete_with_audit(
        session, company, "companies", getattr(request.state, "user", None)
    )
    await session.commit()
    return Response(status_code=204)
# GST backdate preview (HOBB-5)
# ---------------------------------------------------------------------------


@router.get("/{company_id}/gst-backdate-preview")
async def gst_backdate_preview(
    request: Request,
    company_id: UUID,
    effective_date: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return the count of invoices issued on or after effective_date that carry no GST.

    These are the invoices an operator would need to retroactively amend when
    backdating their GST registration (ATO: up to 4 years).  The response is
    informational only — no data is mutated.
    """
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, company_id)
    if existing is None or existing.archived_at is not None or existing.tenant_id != tenant_id:
        raise HTTPException(404, "Company not found")

    today = date.today()
    if effective_date > today:
        raise HTTPException(422, "effective_date cannot be in the future")
    earliest = today.replace(year=today.year - 4)
    if effective_date < earliest:
        raise HTTPException(422, "effective_date cannot be more than 4 years in the past (ATO limit)")

    # Count non-draft invoices on or after the backdated date with no GST charged.
    count_stmt = (
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.archived_at.is_(None),
            Invoice.status != InvoiceStatus.DRAFT,
            Invoice.issue_date >= effective_date,
            Invoice.tax_total == 0,
        )
    )
    invoice_count = int((await session.execute(count_stmt)).scalar_one())

    return JSONResponse(
        {
            "company_id": str(company_id),
            "effective_date": effective_date.isoformat(),
            "invoice_count": invoice_count,
        }
    )
