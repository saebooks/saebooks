"""Authentication routes — login, OAuth2 callbacks, Magic Links, FIDO2."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from saebooks.config import settings
from saebooks.models.user import User
from saebooks.routers.deps import get_current_user
from saebooks.services import auth_tokens
from saebooks.services import fido2_service, magic_link_service, oauth_service

logger = logging.getLogger("saebooks.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Jinja2 template environment
template_env = Environment(
    loader=FileSystemLoader("saebooks/templates"),
    autoescape=True,
)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    """Display login page with OAuth, Magic Link, and FIDO2 options."""
    template = template_env.get_template("login.html")
    return template.render(
        error=error,
        github_enabled=settings.oauth_enabled and settings.github_client_id,
        microsoft_enabled=settings.oauth_enabled and settings.microsoft_client_id,
        google_enabled=settings.oauth_enabled and settings.google_client_id,
    )


# ========================================
# OAuth2 Routes
# ========================================


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(provider: str, request: Request):
    """Redirect to OAuth provider's authorize endpoint."""
    if not settings.oauth_enabled:
        raise HTTPException(status_code=400, detail="OAuth is not enabled")

    try:
        redirect_uri = f"{settings.oauth_redirect_uri_base}/auth/oauth/{provider}/callback"
        authorize_url, state_token = oauth_service.get_authorize_url(
            provider=provider,
            redirect_uri=redirect_uri,
        )

        # Store state for verification in callback
        await oauth_service._store_state(state_token, {"provider": provider})

        return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
    except oauth_service.OAuthProviderNotConfigured as e:
        logger.warning(f"OAuth provider not configured: {e}")
        return RedirectResponse(
            url=f"/auth/login?error={e}",
            status_code=status.HTTP_302_FOUND,
        )


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    request: Request = None,
):
    """Handle OAuth provider callback."""
    try:
        # Verify state token
        state_data = await oauth_service._retrieve_state(state)
        if not state_data or state_data.get("provider") != provider:
            raise oauth_service.OAuthStateMismatch("State token mismatch")

        # Exchange code for token
        redirect_uri = f"{settings.oauth_redirect_uri_base}/auth/oauth/{provider}/callback"
        provider_user_id, email, display_name = await oauth_service.exchange_code(
            provider=provider,
            code=code,
            state=state,
            redirect_uri=redirect_uri,
            stored_state=state_data,
        )

        # Find or create user
        user = await oauth_service.find_or_create_user(
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
            display_name=display_name,
        )

        # Generate JWT token
        token = auth_tokens.create_access_token(user)

        # Redirect to app with token in cookie
        response = RedirectResponse(
            url="/dashboard",
            status_code=status.HTTP_302_FOUND,
        )
        response.set_cookie(
            key="access_token",
            value=token,
            max_age=86400 * 7,  # 7 days
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    except oauth_service.OAuthStateMismatch as e:
        logger.warning(f"OAuth state mismatch: {e}")
        return RedirectResponse(
            url=f"/auth/login?error=State+token+mismatch",
            status_code=status.HTTP_302_FOUND,
        )
    except oauth_service.OAuthError as e:
        logger.error(f"OAuth error: {e}")
        return RedirectResponse(
            url=f"/auth/login?error={e}",
            status_code=status.HTTP_302_FOUND,
        )


# ========================================
# Magic Link Routes
# ========================================


@router.post("/magic-link/send")
async def magic_link_send(request: Request):
    """Send a magic link to the provided email."""
    data = await request.json()
    email = data.get("email", "").strip().lower()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    try:
        # Generate and send magic link
        token = await magic_link_service.generate_magic_link(email)
        # In production, email is sent via send_email(), return success
        return {"success": True, "message": "Check your email for a login link"}

    except magic_link_service.MagicLinkError as e:
        logger.error(f"Magic link error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/magic-link/verify/{token}")
async def magic_link_verify(token: str):
    """Verify magic link token and create session."""
    try:
        # Verify token and get/create user
        user = await magic_link_service.verify_magic_link(token)

        # Generate JWT token
        jwt_token = auth_tokens.create_access_token(user)

        # Redirect to app with token
        response = RedirectResponse(
            url="/dashboard",
            status_code=status.HTTP_302_FOUND,
        )
        response.set_cookie(
            key="access_token",
            value=jwt_token,
            max_age=86400 * 7,  # 7 days
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    except magic_link_service.MagicLinkTokenExpired:
        return RedirectResponse(
            url="/auth/login?error=Magic+link+expired",
            status_code=status.HTTP_302_FOUND,
        )
    except magic_link_service.MagicLinkError as e:
        logger.error(f"Magic link verification error: {e}")
        return RedirectResponse(
            url=f"/auth/login?error=Invalid+magic+link",
            status_code=status.HTTP_302_FOUND,
        )


# ========================================
# FIDO2 Routes
# ========================================


@router.post("/fido2/register/begin")
async def fido2_register_begin(request: Request, user: User = Depends(get_current_user)):
    """Begin FIDO2 registration for authenticated user."""
    try:
        options = await fido2_service.begin_registration(user.id)
        return options
    except fido2_service.FIDO2Error as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/fido2/register/complete")
async def fido2_register_complete(
    request: Request,
    user: User = Depends(get_current_user),
):
    """Complete FIDO2 registration."""
    data = await request.json()
    challenge = data.get("challenge")
    credential = data.get("credential")

    if not challenge or not credential:
        raise HTTPException(status_code=400, detail="Missing challenge or credential")

    try:
        result = await fido2_service.complete_registration(
            user_id=user.id,
            challenge=challenge,
            credential_data=credential,
        )
        return {
            "success": True,
            "credential_id": result["credential_id"],
            "message": "Security key registered successfully",
        }
    except fido2_service.FIDO2ChallengeInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except fido2_service.FIDO2Error as e:
        logger.error(f"FIDO2 registration error: {e}")
        raise HTTPException(status_code=400, detail="Registration failed")


@router.post("/fido2/authenticate/begin")
async def fido2_auth_begin(request: Request):
    """Begin FIDO2 authentication."""
    data = await request.json()
    email = data.get("email", "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Email required")

    try:
        options = await fido2_service.begin_authentication(email)
        return options
    except fido2_service.FIDO2Error as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/fido2/authenticate/complete")
async def fido2_auth_complete(request: Request):
    """Complete FIDO2 authentication."""
    data = await request.json()
    challenge = data.get("challenge")
    credential = data.get("credential")

    if not challenge or not credential:
        raise HTTPException(status_code=400, detail="Missing challenge or credential")

    try:
        user = await fido2_service.complete_authentication(
            challenge=challenge,
            credential_data=credential,
        )

        # Generate JWT token
        token = auth_tokens.create_access_token(user)

        # Return token (client sets cookie)
        return {
            "success": True,
            "access_token": token,
            "token_type": "bearer",
        }
    except fido2_service.FIDO2ChallengeInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except fido2_service.FIDO2Error as e:
        logger.error(f"FIDO2 auth error: {e}")
        raise HTTPException(status_code=400, detail="Authentication failed")


@router.post("/logout")
async def logout(user: User = Depends(get_current_user)):
    """Logout — invalidate session."""
    response = RedirectResponse(url="/auth/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response
