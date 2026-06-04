"""JSON router — ``/api/v1/statement-templates``.

Supplier-statement extraction-hint templates (P4, Gitea #28).

Endpoints
---------
POST /api/v1/statement-templates
    Create a template for a supplier. Write-scoped. Returns 201 with the
    template object.

GET /api/v1/statement-templates
    List templates for the active company. Optional filters: contact_id,
    supplier_abn, active. Returns ``{"items": [TemplateOut]}``.

DELETE /api/v1/statement-templates/{id}
    Hard-delete a template. Write-scoped. 204 on success, 404 if not found
    for tenant/company.

Auth / RLS
----------
All routes: Bearer auth via ``require_bearer`` (router-level dep).
Write-scope enforcement via ``require_bearer``'s API-token scope check.
Tenant binding: ``get_session`` stamps ``app.current_tenant`` so
FORCE-RLS on ``supplier_statement_templates`` applies to every query.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1.auth import resolve_tenant_id
from saebooks.models.supplier_statement_template import SupplierStatementTemplate

logger = logging.getLogger("saebooks.api.v1.statement_templates")

router = APIRouter(
    prefix="/statement-templates",
    tags=["statement-templates"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Request / response schemas (local — simple enough to not need schemas.py)
# ---------------------------------------------------------------------------


class TemplateCreateRequest(BaseModel):
    contact_id: UUID | None = None
    supplier_abn: str | None = None
    supplier_name: str | None = None
    prompt_hint: str = Field(min_length=1)
    page_scope: str | None = None

    @model_validator(mode="after")
    def at_least_one_match_key(self) -> "TemplateCreateRequest":
        if not any([self.contact_id, self.supplier_abn, self.supplier_name]):
            raise ValueError(
                "At least one match key (contact_id, supplier_abn, or supplier_name) "
                "must be provided."
            )
        return self


def _serialize_template(t: SupplierStatementTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "contact_id": str(t.contact_id) if t.contact_id is not None else None,
        "supplier_abn": t.supplier_abn,
        "supplier_name": t.supplier_name,
        "prompt_hint": t.prompt_hint,
        "page_scope": t.page_scope,
        "active": t.active,
        "created_at": t.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# POST /statement-templates
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="Create a supplier-statement extraction-hint template",
    status_code=status.HTTP_201_CREATED,
)
async def create_template(
    payload: TemplateCreateRequest,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Create a per-supplier extraction-hint template.

    At least one match key (``contact_id``, ``supplier_abn``, or
    ``supplier_name``) is required alongside a non-empty ``prompt_hint``.

    Write-scope: read-only API tokens receive 403.

    Returns:
        201 with the created template object.
    """
    tenant_id = resolve_tenant_id(request)

    tmpl = SupplierStatementTemplate(
        tenant_id=tenant_id,
        company_id=company_id,
        contact_id=payload.contact_id,
        supplier_abn=payload.supplier_abn,
        supplier_name=payload.supplier_name,
        prompt_hint=payload.prompt_hint,
        page_scope=payload.page_scope,
        active=True,
    )
    session.add(tmpl)
    await session.commit()
    await session.refresh(tmpl)

    logger.info(
        "statement_templates: created id=%s tenant=%s company=%s",
        tmpl.id,
        tenant_id,
        company_id,
    )
    return JSONResponse(_serialize_template(tmpl), status_code=201)


# ---------------------------------------------------------------------------
# GET /statement-templates
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List supplier-statement extraction-hint templates",
)
async def list_templates(
    contact_id: UUID | None = Query(default=None),
    supplier_abn: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Return templates for the active company.

    All filters are optional; multiple filters combine with AND.

    Returns:
        ``{"items": [TemplateOut]}``
    """
    q = select(SupplierStatementTemplate).where(
        SupplierStatementTemplate.company_id == company_id
    )
    if contact_id is not None:
        q = q.where(SupplierStatementTemplate.contact_id == contact_id)
    if supplier_abn is not None:
        q = q.where(SupplierStatementTemplate.supplier_abn == supplier_abn)
    if active is not None:
        q = q.where(SupplierStatementTemplate.active == active)

    q = q.order_by(SupplierStatementTemplate.created_at.desc())
    rows = (await session.execute(q)).scalars().all()
    return JSONResponse({"items": [_serialize_template(t) for t in rows]})


# ---------------------------------------------------------------------------
# DELETE /statement-templates/{id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{template_id}",
    summary="Delete a supplier-statement extraction-hint template",
)
async def delete_template(
    template_id: UUID,
    request: Request,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Hard-delete a template.

    Write-scope: read-only API tokens receive 403.

    Returns:
        204 on success.

    Raises:
        404: Template not found for this tenant/company.
    """
    tmpl = await session.get(SupplierStatementTemplate, template_id)
    if tmpl is None or tmpl.company_id != company_id:
        raise HTTPException(status_code=404, detail="Template not found")

    await session.delete(tmpl)
    await session.commit()

    logger.info(
        "statement_templates: deleted id=%s tenant=%s company=%s",
        template_id,
        resolve_tenant_id(request),
        company_id,
    )
    return Response(status_code=204)


__all__ = ["router"]
