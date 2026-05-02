"""ATO SBR Machine Credential onboarding wizard (Batch II.5).

Mounted at ``/admin/ato-sbr``. Every route is gated by
``require_feature(FLAG_ATO_SBR)`` — Community builds 404 the whole tree.

AUSkey is retired. To lodge STP Phase 2 (Batch JJ) and BAS via SBR
(Batch KK) the admin must:

1. set up myGovID with Strong identity strength,
2. link Principal Authority for the active company's ABN in RAM,
3. install the Machine Credential Downloader Chrome extension,
4. upload the resulting ``keystore.xml`` + password to this form,
5. paste in the Software Service ID (SSID) from ATO Software Developer,
6. hit "Test against EVTE" to confirm reachability.

Steps 1-3 are tracked as off-system checkboxes (we can't verify them
programmatically). Steps 4-6 are real form submissions.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.ato_sbr import AtoSbrConfig
from saebooks.models.company import Company
from saebooks.models.user import UserRole
from saebooks.services import crypto as crypto_svc
from saebooks.services.ato_sbr import onboarding as sbr
from saebooks.services.ato_sbr.keystore import KeystoreError
from saebooks.services.authz import require_role
from saebooks.services.features import FLAG_ATO_SBR, require_feature
from saebooks.web import templates
from saebooks.services import active_company as active_svc

router = APIRouter(
    prefix="/admin/ato-sbr",
    dependencies=[
        Depends(require_feature(FLAG_ATO_SBR)),
        Depends(require_role(UserRole.ADMIN)),
    ],
)


async def _first_company() -> Company:
    return await active_svc.first_company_compat()


async def _with_config(
    session: AsyncSession, company_id: uuid.UUID
) -> AtoSbrConfig:
    return await sbr.get_or_create_config(session, company_id)


def _ctx(
    *,
    request: Request,
    company: Company,
    config: AtoSbrConfig,
    message: str | None = None,
    error: str | None = None,
    test_badge: str | None = None,
    test_env: str | None = None,
) -> dict[str, Any]:
    return {
        "edition": settings.edition,
        "request": request,
        "company": company,
        "company_name": company.name,
        "config": config,
        "status": sbr.status_for(config),
        "encryption_configured": crypto_svc.is_configured(settings),
        "message": message,
        "error": error,
        "test_badge": test_badge,
        "test_env": test_env,
        "ram_url": (
            "https://info.authorisationmanager.gov.au/"
            f"?abn={company.abn or ''}"
        ),
    }


# ---------------------------------------------------------------------- #
# Wizard page                                                            #
# ---------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
async def onboard_form(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    test: str | None = None,
    test_env: str | None = None,
) -> HTMLResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        await session.commit()
    return templates.TemplateResponse(
        request,
        "ato_sbr/onboard.html",
        _ctx(
            request=request,
            company=company,
            config=config,
            message=message,
            error=error,
            test_badge=test,
            test_env=test_env,
        ),
    )


# ---------------------------------------------------------------------- #
# Off-system checkbox confirmations                                      #
# ---------------------------------------------------------------------- #


@router.post("/confirm")
async def confirm_offsystem_step(
    step: str = Form(...),
) -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        try:
            await sbr.confirm_step(session, config, step)
        except sbr.OnboardingError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:140]}", status_code=303
            )
        await session.commit()
    return RedirectResponse(
        f"/admin/ato-sbr?message=step+{step}+confirmed", status_code=303
    )


# ---------------------------------------------------------------------- #
# Keystore upload                                                        #
# ---------------------------------------------------------------------- #


@router.post("/keystore")
async def upload_keystore(
    file: UploadFile = Form(...),  # noqa: B008
    password: str = Form(...),
) -> RedirectResponse:
    data = await file.read()
    if not data:
        return RedirectResponse(
            "/admin/ato-sbr?error=no+file+uploaded", status_code=303
        )
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        try:
            loaded = await sbr.save_keystore(
                session,
                config,
                data=data,
                password=password,
                filename=file.filename or "keystore.xml",
                settings=settings,
            )
        except sbr.OnboardingError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:200]}", status_code=303
            )
        except KeystoreError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:200]}", status_code=303
            )
        await session.commit()
    cn = loaded.subject_cn or "?"
    return RedirectResponse(
        f"/admin/ato-sbr?message=keystore+loaded+({cn})", status_code=303
    )


# ---------------------------------------------------------------------- #
# SSID                                                                   #
# ---------------------------------------------------------------------- #


@router.post("/ssid")
async def save_ssid_route(
    ssid: str = Form(...),
) -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        try:
            await sbr.save_ssid(session, config, ssid)
        except sbr.OnboardingError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:140]}", status_code=303
            )
        await session.commit()
    return RedirectResponse("/admin/ato-sbr?message=ssid+saved", status_code=303)


# ---------------------------------------------------------------------- #
# Environment toggle                                                     #
# ---------------------------------------------------------------------- #


@router.post("/environment")
async def set_environment_route(
    environment: str = Form(...),
) -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        try:
            await sbr.set_environment(session, config, environment)
        except sbr.OnboardingError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:140]}", status_code=303
            )
        await session.commit()
    return RedirectResponse(
        f"/admin/ato-sbr?message=environment+set+to+{environment}",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Smoke-test (real HTTPS round-trip to ATO)                              #
# ---------------------------------------------------------------------- #


@router.post("/test")
async def test_environment_route(
    environment: str = Form(...),
) -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        try:
            result = await sbr.test_environment(
                session,
                config,
                environment=environment,
                settings=settings,
            )
        except sbr.OnboardingError as exc:
            await session.rollback()
            return RedirectResponse(
                f"/admin/ato-sbr?error={str(exc)[:140]}", status_code=303
            )
        await session.commit()
    badge = "ok" if result.ok else "fail"
    detail = result.detail.replace(" ", "+")[:140]
    return RedirectResponse(
        f"/admin/ato-sbr?test={badge}&test_env={environment}&message={detail}",
        status_code=303,
    )


# ---------------------------------------------------------------------- #
# Clear                                                                  #
# ---------------------------------------------------------------------- #


@router.post("/clear")
async def clear_route() -> RedirectResponse:
    company = await _first_company()
    async with AsyncSessionLocal() as session:
        config = await _with_config(session, company.id)
        await sbr.clear_config(session, config)
        await session.commit()
    return RedirectResponse(
        "/admin/ato-sbr?message=config+cleared", status_code=303
    )
