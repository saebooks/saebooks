"""Authentication router for OAuth2, Magic Links, and FIDO2."""
from typing import Optional
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException, status, responses, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import get_session
from saebooks.models.user import User
from saebooks.models.oauth import OAuthProviderLink
from saebooks.services.oauth_service import (
    get_authorize_url,
    exchange_code as oauth_exchange_code,
    find_or_create_user as oauth_find_or_create_user,
)
from saebooks.services.magic_link_service import (
    generate_magic_link,
    verify_magic_link,
    MagicLinkTokenExpired,
    MagicLinkTokenInvalid,
)
from saebooks.services.fido2_service import (
    begin_registration,
    complete_registration,
    begin_authentication,
    complete_authentication,
)
from saebooks.services.jwt_tokens import create_access_token
from saebooks.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# Jinja2 setup for templates
template_dir = "saebooks/templates"
jinja_env = Environment(loader=FileSystemLoader(template_dir))

# JWT token TTL: 7 days in seconds
JWT_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60


def _set_auth_cookie(response: responses.Response, token: str) -> None:
    """Set HttpOnly, Secure authentication cookie."""
    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=JWT_TOKEN_TTL_SECONDS,
        expires=JWT_TOKEN_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=not settings.debug,  # Only secure in production
        samesite="lax",
    )


def _auth_response(user: User, redirect_to: str = "/") -> RedirectResponse:
    """Create authenticated response with JWT cookie."""
    # Generate JWT token with user claims
    token = create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "username": user.username,
            "tenant_id": str(user.tenant_id),
        },
        expires_in_seconds=JWT_TOKEN_TTL_SECONDS,
    )
    
    # Create redirect response
    response = RedirectResponse(url=redirect_to, status_code=302)
    
    # Set authentication cookie
    _set_auth_cookie(response, token)
    
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(db: AsyncSession = Depends(get_session)):
    """Return HTML login page with OAuth, Magic Link, and FIDO2 options."""
    template = jinja_env.get_template("login.html")
    
    # Check which OAuth providers are enabled
    oauth_enabled = getattr(settings, 'oauth_enabled', False)
    github_enabled = getattr(settings, 'oauth_provider_github', False) and oauth_enabled
    microsoft_enabled = getattr(settings, 'oauth_provider_microsoft', False) and oauth_enabled
    google_enabled = getattr(settings, 'oauth_provider_google', False) and oauth_enabled
    
    return template.render(
        github_enabled=github_enabled,
        microsoft_enabled=microsoft_enabled,
        google_enabled=google_enabled,
    )


@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(provider: str, db: AsyncSession = Depends(get_session)):
    """Initiate OAuth authorization code flow.
    
    Redirects to the OAuth provider's authorization endpoint.
    """
    oauth_enabled = getattr(settings, 'oauth_enabled', False)
    if not oauth_enabled:
        raise HTTPException(status_code=403, detail="OAuth not enabled")
    
    provider = provider.lower()
    if provider not in ['github', 'microsoft', 'google']:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    
    # Check if provider is enabled
    provider_setting = f'oauth_provider_{provider}'
    if not getattr(settings, provider_setting, False):
        raise HTTPException(status_code=403, detail=f"{provider} provider not enabled")
    
    # Generate CSRF state token
    state = secrets.token_urlsafe(32)
    
    # Store state in Redis (for validation in callback)
    # TODO: Store state in Redis with expiry
    
    # Get authorization URL
    authorize_url = get_authorize_url(provider, state)
    
    return RedirectResponse(url=authorize_url)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_session)
):
    """Handle OAuth provider callback.
    
    Exchanges authorization code for token, fetches user info,
    and creates/updates user account.
    """
    oauth_enabled = getattr(settings, 'oauth_enabled', False)
    if not oauth_enabled:
        raise HTTPException(status_code=403, detail="OAuth not enabled")
    
    provider = provider.lower()
    if provider not in ['github', 'microsoft', 'google']:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    
    try:
        # Exchange code for token
        token_data = await oauth_exchange_code(provider, code)
        
        # Get user info from provider
        user_info = token_data.get('user_info', {})
        provider_user_id = user_info.get('id') or user_info.get('sub')
        provider_email = user_info.get('email')
        
        if not provider_user_id or not provider_email:
            raise HTTPException(status_code=400, detail="Invalid token response from provider")
        
        # Find or create user
        user = await oauth_find_or_create_user(
            db,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=provider_email,
        )
        
        # Return authenticated response
        return _auth_response(user, redirect_to="/")
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth callback error: {str(e)}")


@router.post("/magic-link/send")
async def send_magic_link(request: Request, db: AsyncSession = Depends(get_session)):
    """Send magic link email to user.
    
    Returns: {"status": "sent", "message": "Check your email for login link"}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    email = body.get('email', '').strip().lower()
    if not email or '@' not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    
    try:
        # Generate and send magic link
        token = await generate_magic_link(email)
        return {"status": "sent", "message": f"Login link sent to {email}"}
    except Exception as e:
        # Don't reveal whether email exists
        return {"status": "sent", "message": "If an account exists, a login link has been sent"}


@router.get("/magic-link/verify/{token}")
async def verify_magic_link_endpoint(token: str, db: AsyncSession = Depends(get_session)):
    """Verify magic link and auto-login user."""
    try:
        user = await verify_magic_link(token)
        
        # Return authenticated response
        return _auth_response(user, redirect_to="/")
        
    except MagicLinkTokenExpired:
        raise HTTPException(status_code=401, detail="Magic link has expired (15 minutes)")
    except MagicLinkTokenInvalid:
        raise HTTPException(status_code=401, detail="Invalid or already-used magic link")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Login error: {str(e)}")


@router.post("/fido2/register/begin")
async def fido2_register_begin(request: Request, db: AsyncSession = Depends(get_session)):
    """Begin FIDO2 security key registration.
    
    Returns attestation challenge and credential creation options.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    user_id = body.get('user_id')
    email = body.get('email')
    
    if not user_id or not email:
        raise HTTPException(status_code=400, detail="user_id and email required")
    
    try:
        import uuid as _uuid
        challenge_data = await begin_registration(_uuid.UUID(user_id))
        return challenge_data
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration error: {str(e)}")


@router.post("/fido2/register/complete")
async def fido2_register_complete(request: Request, db: AsyncSession = Depends(get_session)):
    """Complete FIDO2 security key registration.
    
    Validates attestation and stores credential.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    user_id = body.get('user_id')
    credential_id = body.get('credential_id')
    attestation_object = body.get('attestation_object')
    client_data_json = body.get('client_data_json')
    
    try:
        result = await complete_registration(
            user_id,
            credential_id,
            attestation_object,
            client_data_json,
        )
        return {"status": "success", "message": "Security key registered"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration error: {str(e)}")


@router.post("/fido2/authenticate/begin")
async def fido2_authenticate_begin(request: Request, db: AsyncSession = Depends(get_session)):
    """Begin FIDO2 authentication challenge.
    
    Returns assertion challenge for security key.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    email = body.get('email')
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    
    try:
        challenge_data = await begin_authentication(email)
        return challenge_data
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Auth error: {str(e)}")


@router.post("/fido2/authenticate/complete")
async def fido2_authenticate_complete(request: Request, db: AsyncSession = Depends(get_session)):
    """Complete FIDO2 authentication.
    
    Validates assertion and authenticates user.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    user_id = body.get('user_id')
    credential_id = body.get('credential_id')
    authenticator_data = body.get('authenticator_data')
    client_data_json = body.get('client_data_json')
    signature = body.get('signature')
    
    try:
        user = await complete_authentication(
            user_id,
            credential_id,
            authenticator_data,
            client_data_json,
            signature,
        )
        
        # Return authenticated response
        return _auth_response(user, redirect_to="/")
        
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")
