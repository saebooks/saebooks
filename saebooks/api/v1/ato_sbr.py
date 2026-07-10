"""``/api/v1/ato_sbr`` — PUBLIC SHIM (certified ATO transmission stubbed).

The ATO SBR surface (Machine-Credential keystore, onboarding wizard, lodge-server
ping, BAS prepare/lodge) is the certified, credential-bearing transmission path
and is NOT shipped in the open engine. The router is preserved and keeps its
feature gate: ``require_feature(FLAG_ATO_SBR)`` makes the whole surface 404 for
editions without the flag; where the flag is enabled, every route answers 501
("commercial feature") — the open engine computes the return but does not lodge.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from saebooks.api.v1.auth import require_bearer
from saebooks.services.features import FLAG_ATO_SBR, require_feature

router = APIRouter(
    prefix="/ato_sbr",
    tags=["ato_sbr"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_ATO_SBR)),
    ],
)

_COMMERCIAL = (
    "commercial feature: certified ATO SBR transmission (keystore, onboarding, "
    "lodgement) is not available in the open engine"
)


@router.post("/keystore", status_code=501)
async def upload_keystore() -> dict:
    raise HTTPException(501, _COMMERCIAL)


@router.get("/keystore", status_code=501)
async def list_keystore() -> dict:
    raise HTTPException(501, _COMMERCIAL)


@router.post("/onboarding/wizards", status_code=501)
async def start_wizard() -> dict:
    raise HTTPException(501, _COMMERCIAL)


@router.post("/ping", status_code=501)
async def ping_lodge_server() -> dict:
    raise HTTPException(501, _COMMERCIAL)


@router.post("/prepare-bas", status_code=501)
async def prepare_bas(body: dict[str, Any] = Body(default={})) -> dict:
    raise HTTPException(501, _COMMERCIAL)


@router.post("/lodge-bas", status_code=501)
async def lodge_bas(body: dict[str, Any] = Body(default={})) -> dict:
    raise HTTPException(501, _COMMERCIAL)


__all__ = ["router"]
