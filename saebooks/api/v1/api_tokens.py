"""REST endpoints for managing API tokens.

* ``POST /api/v1/api-tokens`` — issue a new token, returns cleartext ONCE
* ``GET /api/v1/api-tokens`` — list active tokens for the current user
* ``DELETE /api/v1/api-tokens/{id}`` — revoke

Requires JWT auth (not API token auth — you can't bootstrap a new
token from a token, that's a chicken-and-egg). The handler checks
``request.state.user`` was stamped by ``require_bearer``'s JWT branch.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import BearerDep
from saebooks.api.v1.deps import get_session
from saebooks.services import active_company as active_company_svc
from saebooks.services import api_tokens as token_svc

router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])


class CreateApiTokenRequest(BaseModel):
    name: str = Field(..., max_length=200, description="Human-friendly label")
    scopes: list[str] = Field(default_factory=list)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


def _require_authenticated_user(request: Request) -> uuid.UUID:
    """Return the user_id from the JWT-stamped ``request.state.user`` or
    401. API-token-auth requests don't have a User row stamped here —
    they cannot mint new tokens (chicken-and-egg)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token issuance requires JWT authentication",
        )
    return user.id


async def _active_company_id(
    session: AsyncSession, request: Request
) -> uuid.UUID:
    company = await active_company_svc.resolve_active_company(session, request)
    return company.id


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_token(
    body: CreateApiTokenRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: str = BearerDep,
) -> dict[str, Any]:
    user_id = _require_authenticated_user(request)
    company_id = await _active_company_id(session, request)
    token, cleartext = await token_svc.issue(
        session,
        user_id=user_id,
        company_id=company_id,
        name=body.name,
        scopes=body.scopes,
        ttl_days=body.ttl_days,
    )
    await session.commit()
    return token_svc.to_public_dict(token, cleartext=cleartext)


@router.get("")
async def list_api_tokens(
    request: Request,
    session: AsyncSession = Depends(get_session),
    include_revoked: bool = False,
    _: str = BearerDep,
) -> list[dict[str, Any]]:
    user_id = _require_authenticated_user(request)
    company_id = await _active_company_id(session, request)
    rows = await token_svc.list_for_user(
        session,
        user_id=user_id,
        company_id=company_id,
        include_revoked=include_revoked,
    )
    return [token_svc.to_public_dict(t) for t in rows]


@router.delete("/{token_id}")
async def revoke_api_token(
    token_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: str = BearerDep,
) -> dict[str, Any]:
    user_id = _require_authenticated_user(request)
    ok = await token_svc.revoke(session, token_id=token_id, user_id=user_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found or already revoked",
        )
    await session.commit()
    return {"revoked": True, "id": str(token_id)}
