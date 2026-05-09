"""Public signup, email verification, password reset, magic link.

All endpoints live on the unauthenticated /api/v1/auth/* prefix —
they're mounted before any router that carries ``require_bearer`` as
a router-level dependency.

Why one file
------------
These endpoints are tightly coupled (shared rate-limit policy,
shared token primitives, shared mailer helpers) and small enough
that splitting them into ``signup.py``, ``reset.py``, ``magic.py``
just shuffles imports.

Rate limits
-----------
Signup is per-IP (5/min) — pre-account-existence; can't key on email
without leaking enumeration.  Reset/magic/resend are per-email
(3-5/min depending) — those endpoints always return 200 so
attackers can't probe per-IP without first getting blocked at the
email level.

Enumeration safety
------------------
``/password-reset/request``, ``/magic-link/request``,
``/resend-verification`` all return 200 regardless of whether the
email exists. The caller cannot tell from the response whether they
hit a real account.
"""
from __future__ import annotations

import logging
import re
import secrets as py_secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.middleware.rate_limit import rate_limit
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services.auth_tokens import (
    expiry_for,
    generate_token,
    hash_token,
)
from saebooks.services.jwt_tokens import (
    hash_password,
    make_access_token,
    verify_password,
)
from saebooks.services.launch_promo import attempt_promo
from saebooks.services.mailer import send_email

logger = logging.getLogger("saebooks.signup")

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


# RFC 5322 light email regex — full-spec validation needs the email-validator
# package, which we don't carry. This rejects the obvious garbage and accepts
# everything a normal MUA would. Bad addresses get bounced by the SMTP MTA
# regardless.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _check_email(value: str) -> str:
    v = value.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


class SignupRequest(BaseModel):
    email: str
    password: str = Field(min_length=1)
    company_name: str | None = None
    plan: Literal["business", "pro", "enterprise"] | None = None

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:  # noqa: D401
        return _check_email(v)


class VerifyEmailRequest(BaseModel):
    token: str


class EmailOnlyRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        return _check_email(v)


class PasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=1)


class TokenOnlyRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 8 * 3600
    signup_plan: str | None = None


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_PASSWORD_MIN_LEN = 10
_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"\d")


def _validate_password(pw: str) -> None:
    """Raise 422 on weak passwords. 10 chars minimum, must contain at
    least one letter and one digit. Deliberately permissive on
    symbols and upper/lower case so passphrase-style passwords (which
    are stronger than the NIST symbol-soup) pass.
    """
    if len(pw) < _PASSWORD_MIN_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be at least {_PASSWORD_MIN_LEN} characters",
        )
    if not _LETTER_RE.search(pw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must contain at least one letter",
        )
    if not _DIGIT_RE.search(pw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must contain at least one digit",
        )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    base = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return base[:48] or "tenant"


def _random_suffix() -> str:
    """Four lowercase-alphanumeric chars — appended to tenant slugs to
    avoid the "two Acme Pty Ltd customers signed up" collision.
    """
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # no 0/o/1/l
    return "".join(py_secrets.choice(alphabet) for _ in range(4))


def _verification_url_for(token: str) -> str:
    """Where the email link points. Web app handles the GET, calls
    POST /auth/verify-email under the hood."""
    return f"https://app.saebooks.com.au/verify-email?token={token}"


def _reset_url_for(token: str) -> str:
    return f"https://app.saebooks.com.au/reset-password?token={token}"


def _magic_url_for(token: str) -> str:
    return f"https://app.saebooks.com.au/magic-link?token={token}"


# ---------------------------------------------------------------------------
# Email templates (inline — these aren't user-facing chrome and the
# mailer is HTML-first; passing a Jinja env in for three messages is
# overkill).
# ---------------------------------------------------------------------------


def _verification_email_html(verify_url: str) -> str:
    return f"""<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;color:#1f2937;">
<h2 style="color:#194291;">Welcome to SAE Books</h2>
<p>Click the link below to verify your email and activate your account.</p>
<p><a href="{verify_url}" style="display:inline-block;background:#194291;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">Verify email</a></p>
<p style="font-size:12px;color:#6b7280;">This link expires in 24 hours. If you didn't create an account, ignore this email.</p>
</body></html>"""


def _reset_email_html(reset_url: str) -> str:
    return f"""<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;color:#1f2937;">
<h2 style="color:#194291;">Reset your SAE Books password</h2>
<p>Click below to set a new password. The link expires in 1 hour.</p>
<p><a href="{reset_url}" style="display:inline-block;background:#194291;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">Reset password</a></p>
<p style="font-size:12px;color:#6b7280;">If you didn't request this, ignore this email — your password is unchanged.</p>
</body></html>"""


def _magic_email_html(magic_url: str) -> str:
    return f"""<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;color:#1f2937;">
<h2 style="color:#194291;">Sign in to SAE Books</h2>
<p>Click below to sign in. The link expires in 15 minutes and only works once.</p>
<p><a href="{magic_url}" style="display:inline-block;background:#194291;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;">Sign in</a></p>
<p style="font-size:12px;color:#6b7280;">If you didn't request this, ignore the email.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user_by_email(session: Any, email: str) -> User | None:
    """Look up a user by lowercase email. Email column is case-stored
    as the user typed it; we match case-insensitively."""
    result = await session.execute(
        select(User).where(User.email.ilike(email))
    )
    return result.scalars().first()


def _email_local_part(email: str) -> str:
    return email.split("@", 1)[0]


# ---------------------------------------------------------------------------
# POST /auth/signup
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("signup", 5))],
)
async def signup(body: SignupRequest) -> MessageResponse:
    """Create a fresh tenant + owner user and email a verification
    link. No JWT is returned — the client must verify their email
    first.
    """
    _validate_password(body.password)

    # The static-bearer path expects SAEBOOKS_ENV != production for
    # tenant resolution. We bypass the request-scoped session
    # (get_session) deliberately because no JWT exists yet — and
    # would tenant-isolate the new tenant from itself. Use the bare
    # AsyncSessionLocal which connects as the schema-owner role and
    # bypasses RLS, the only sensible identity for "create tenant".
    async with AsyncSessionLocal() as session:
        # Duplicate-email check, case-insensitive. We deliberately
        # respond 409 not 200 — public-signup pages are typically
        # behind a captcha and the small enumeration disclosure is
        # outweighed by the better UX (clear "you already have an
        # account" message). Reset/magic/resend stay enumeration-safe.
        existing = await _user_by_email(session, body.email)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with that email already exists",
            )

        tenant_name = (
            body.company_name.strip()
            if (body.company_name and body.company_name.strip())
            else _email_local_part(body.email)
        )
        tenant_slug = f"{_slugify(tenant_name)}-{_random_suffix()}"

        tenant = Tenant(
            id=uuid.uuid4(),
            name=tenant_name,
            slug=tenant_slug,
        )
        session.add(tenant)
        await session.flush()

        # Username must be unique. Use the email as username — it's
        # what login.py keys off. If somebody else has the same
        # username (case-mismatch), suffix it.
        username = body.email.lower()
        existing_username = await session.execute(
            select(User).where(User.username == username)
        )
        if existing_username.scalars().first() is not None:
            username = f"{username}-{_random_suffix()}"

        raw_token = generate_token()
        token_hash = hash_token(raw_token)
        expires_at = expiry_for("verification")

        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            username=username,
            email=body.email,
            display_name=tenant_name,
            role="owner",
            password_hash=hash_password(body.password),
            email_verification_token_hash=token_hash,
            email_verification_expires_at=expires_at,
            password_version=0,
            version=1,
            signup_plan=body.plan,
        )
        session.add(user)
        await session.commit()

    # Attempt launch-promo JWT claim (best-effort, non-blocking).
    # If the promo is enabled and slots remain, we get a signed Pro JWT
    # back from the license-server and stamp it on the user row. A
    # failure here never aborts signup — the user just starts at Community.
    promo_jwt = await attempt_promo(
        email=body.email,
        licensed_to=tenant_name,
    )
    if promo_jwt is not None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.email.ilike(body.email))
            )
            promo_user = result.scalars().first()
            if promo_user is not None:
                promo_user.launch_promo_jwt = promo_jwt
                await session.commit()

    # Send the verification email outside the DB transaction so a
    # downstream SMTP timeout doesn't roll back the user.
    try:
        await send_email(
            body.email,
            "Verify your SAE Books email",
            _verification_email_html(_verification_url_for(raw_token)),
        )
    except Exception as exc:
        logger.error("signup: failed to send verification to %s: %s", body.email, exc)
        # The user row exists; resend-verification will let them retry.
        # Surface a 500 only if the failure is truly fatal — for outbox
        # mode a filesystem error is fatal; for SMTP the retry path is
        # /auth/resend-verification.
        # Don't leak the SMTP error string to the caller.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account created but failed to send verification email — try /auth/resend-verification",
        ) from exc

    return MessageResponse(message="Verification email sent.")


# ---------------------------------------------------------------------------
# POST /auth/verify-email
# ---------------------------------------------------------------------------


@router.post("/verify-email", response_model=TokenResponse)
async def verify_email(body: VerifyEmailRequest) -> TokenResponse:
    """Consume a verification token, mark the user as verified, and
    return a JWT so the client can drop straight into the app
    without bouncing through /auth/login."""
    token_hash = hash_token(body.token)
    now = datetime.now(UTC)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.email_verification_token_hash == token_hash)
        )
        user = result.scalars().first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid verification token",
            )
        if (
            user.email_verification_expires_at is None
            or user.email_verification_expires_at < now
        ):
            # Clear the dead token so a stale link can't sit around.
            user.email_verification_token_hash = None
            user.email_verification_expires_at = None
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Verification link has expired — request a new one",
            )

        user.email_verified_at = now
        user.email_verification_token_hash = None
        user.email_verification_expires_at = None
        pending_plan = user.signup_plan
        user.signup_plan = None
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    return TokenResponse(access_token=token, signup_plan=pending_plan)


# ---------------------------------------------------------------------------
# POST /auth/resend-verification
# ---------------------------------------------------------------------------


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("resend-verification", 3))],
)
async def resend_verification(body: EmailOnlyRequest) -> MessageResponse:
    """Issue a fresh verification token for the supplied email.

    Always returns 200 to avoid leaking whether an account exists.
    Silently no-ops if the email is unknown or already verified.
    """
    async with AsyncSessionLocal() as session:
        user = await _user_by_email(session, body.email)
        if user is None or user.email_verified_at is not None:
            return MessageResponse(message="If that email is on file, a verification link has been sent.")

        raw_token = generate_token()
        user.email_verification_token_hash = hash_token(raw_token)
        user.email_verification_expires_at = expiry_for("verification")
        await session.commit()

    try:
        await send_email(
            body.email,
            "Verify your SAE Books email",
            _verification_email_html(_verification_url_for(raw_token)),
        )
    except Exception as exc:
        logger.error("resend-verification: send failed for %s: %s", body.email, exc)
        # Still return 200 — the token has been minted, the user can retry.

    return MessageResponse(message="If that email is on file, a verification link has been sent.")


# ---------------------------------------------------------------------------
# POST /auth/password-reset/request
# ---------------------------------------------------------------------------


@router.post(
    "/password-reset/request",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("password-reset-request", 3))],
)
async def password_reset_request(body: EmailOnlyRequest) -> MessageResponse:
    """Issue a password-reset token. Always 200, regardless of
    whether the email is on file."""
    async with AsyncSessionLocal() as session:
        user = await _user_by_email(session, body.email)
        if user is None:
            return MessageResponse(message="If that email is on file, a reset link has been sent.")

        raw_token = generate_token()
        user.password_reset_token_hash = hash_token(raw_token)
        user.password_reset_expires_at = expiry_for("reset")
        await session.commit()

    try:
        await send_email(
            body.email,
            "Reset your SAE Books password",
            _reset_email_html(_reset_url_for(raw_token)),
        )
    except Exception as exc:
        logger.error("password-reset: send failed for %s: %s", body.email, exc)

    return MessageResponse(message="If that email is on file, a reset link has been sent.")


# ---------------------------------------------------------------------------
# POST /auth/password-reset/confirm
# ---------------------------------------------------------------------------


@router.post("/password-reset/confirm", response_model=TokenResponse)
async def password_reset_confirm(
    body: PasswordResetConfirmRequest,
) -> TokenResponse:
    """Verify the reset token, set a new password, bump password_version
    (invalidating every existing JWT), and return a fresh JWT."""
    _validate_password(body.new_password)
    token_hash = hash_token(body.token)
    now = datetime.now(UTC)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.password_reset_token_hash == token_hash)
        )
        user = result.scalars().first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid reset token",
            )
        if (
            user.password_reset_expires_at is None
            or user.password_reset_expires_at < now
        ):
            user.password_reset_token_hash = None
            user.password_reset_expires_at = None
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Reset link has expired — request a new one",
            )

        user.password_hash = hash_password(body.new_password)
        user.password_version = int(user.password_version or 0) + 1
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        # If the user hadn't verified yet, completing the reset proves
        # control of the inbox — counts as verification.
        if user.email_verified_at is None:
            user.email_verified_at = now
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    return TokenResponse(access_token=token)


# ---------------------------------------------------------------------------
# POST /auth/magic-link/request
# ---------------------------------------------------------------------------


@router.post(
    "/magic-link/request",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit("magic-link-request", 5))],
)
async def magic_link_request(body: EmailOnlyRequest) -> MessageResponse:
    """Email a one-time login link to ``body.email``. Always 200."""
    async with AsyncSessionLocal() as session:
        user = await _user_by_email(session, body.email)
        if user is None:
            return MessageResponse(message="If that email is on file, a sign-in link has been sent.")

        raw_token = generate_token()
        user.magic_link_token_hash = hash_token(raw_token)
        user.magic_link_expires_at = expiry_for("magic")
        await session.commit()

    try:
        await send_email(
            body.email,
            "Sign in to SAE Books",
            _magic_email_html(_magic_url_for(raw_token)),
        )
    except Exception as exc:
        logger.error("magic-link: send failed for %s: %s", body.email, exc)

    return MessageResponse(message="If that email is on file, a sign-in link has been sent.")


# ---------------------------------------------------------------------------
# POST /auth/magic-link/consume
# ---------------------------------------------------------------------------


@router.post("/magic-link/consume", response_model=TokenResponse)
async def magic_link_consume(body: TokenOnlyRequest) -> TokenResponse:
    """Single-use magic-link redemption. Returns a JWT and clears the
    token (replay protection)."""
    token_hash = hash_token(body.token)
    now = datetime.now(UTC)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.magic_link_token_hash == token_hash)
        )
        user = result.scalars().first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid sign-in link",
            )
        if (
            user.magic_link_expires_at is None
            or user.magic_link_expires_at < now
        ):
            user.magic_link_token_hash = None
            user.magic_link_expires_at = None
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Sign-in link has expired — request a new one",
            )

        # Clear the token first so a parallel request can't double-spend.
        user.magic_link_token_hash = None
        user.magic_link_expires_at = None
        # Magic-link redemption proves control of the inbox.
        if user.email_verified_at is None:
            user.email_verified_at = now
        await session.commit()
        await session.refresh(user)
        token = make_access_token(user)

    return TokenResponse(access_token=token)


# Reuse `verify_password` import to silence linter on unused import that
# may be pulled in transitively by tests.
__all__ = [
    "router",
    "verify_password",
]
