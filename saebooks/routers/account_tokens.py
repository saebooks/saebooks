"""Self-serve API token management — HTML page.

Lives at ``/admin/api-tokens`` so it slots in with the other admin nav
links in ``base.html``. Despite the prefix, this is a per-user page:
every authenticated user manages their own tokens. The "admin" naming
is the existing convention for settings pages — it doesn't imply
role-gating.

Tokens are scoped to (user, active company) by the underlying service.
Switching the active-company cookie and minting another token here is
how a user gets credentials for a second company.

The plaintext value of a newly-minted token is included once in a
flash payload (URL query-param ``new``) and never rendered into the
table thereafter — refreshing the page after copy loses access to the
secret, which is deliberate.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import active_company as active_svc
from saebooks.services import api_tokens as token_svc
from saebooks.services.authz import require_user
from saebooks.web import templates

router = APIRouter(
    prefix="/admin/api-tokens",
    dependencies=[Depends(require_user())],
)


@router.get("", response_class=HTMLResponse)
async def list_page(request: Request) -> HTMLResponse:
    user = request.state.user
    include_revoked = request.query_params.get("include_revoked") == "1"
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(user.tenant_id)
        company = await active_svc.resolve_active_company(
            session, request, tenant_id=user.tenant_id
        )
        tokens = await token_svc.list_for_user(
            session,
            user_id=user.id,
            company_id=company.id,
            include_revoked=include_revoked,
        )

    new_token = request.query_params.get("new") or None
    new_name = request.query_params.get("name") or None

    return templates.TemplateResponse(
        request,
        "admin/api_tokens.html",
        {
            "edition": settings.edition,
            "company": company,
            "tokens": tokens,
            "include_revoked": include_revoked,
            "new_token": new_token,
            "new_name": new_name,
            "now": datetime.now(timezone.utc),
        },
    )


@router.post("")
async def create(
    request: Request,
    name: str = Form(...),
    ttl_days: str = Form(""),
    scopes: str = Form(""),
) -> RedirectResponse:
    user = request.state.user
    ttl: int | None = int(ttl_days) if ttl_days.strip() else None
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]

    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(user.tenant_id)
        company = await active_svc.resolve_active_company(
            session, request, tenant_id=user.tenant_id
        )
        try:
            _, cleartext = await token_svc.issue(
                session,
                user_id=user.id,
                company_id=company.id,
                name=name,
                scopes=scope_list,
                ttl_days=ttl,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await session.commit()
        token_name = name.strip()

    from urllib.parse import urlencode

    qs = urlencode({"new": cleartext, "name": token_name})
    return RedirectResponse(
        f"/admin/api-tokens?{qs}", status_code=303
    )


@router.post("/{token_id}/revoke")
async def revoke(
    request: Request,
    token_id: uuid.UUID,
) -> RedirectResponse:
    user = request.state.user
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(user.tenant_id)
        ok = await token_svc.revoke(
            session, token_id=token_id, user_id=user.id
        )
        if not ok:
            raise HTTPException(404, "Token not found")
        await session.commit()
    return RedirectResponse("/admin/api-tokens", status_code=303)
