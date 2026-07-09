"""WebAuthn / FIDO2 endpoints under ``/api/v1/auth/webauthn/``.

Browser-native FIDO2 login for saebooks — replaces dependency on an
external IdP (Authentik / CF Access) for the FIDO2 enforcement step.
Ships with every saebooks instance.

Endpoints
---------

Registration (a user adds a key to their account — requires existing session):

* POST ``/register/begin``    — returns ``PublicKeyCredentialCreationOptions``
* POST ``/register/finish``   — verifies attestation, stores credential

Authentication (user logs in with their key — no prior session needed):

* POST ``/authenticate/begin``  — returns ``PublicKeyCredentialRequestOptions``
* POST ``/authenticate/finish`` — verifies assertion, returns JWT

Management:

* GET    ``/credentials``      — list current user's credentials
* DELETE ``/credentials/{id}`` — delete a credential

Configuration (per-instance env)
--------------------------------

* ``SAEBOOKS_WEBAUTHN_ENABLED``  (default ``"1"``)
* ``SAEBOOKS_WEBAUTHN_RP_ID``    (e.g. ``books.example.com.au``)
* ``SAEBOOKS_WEBAUTHN_RP_NAME``  (display name, default ``SAE Books``)
* ``SAEBOOKS_WEBAUTHN_ORIGIN``   (comma-separated; must include https://RP_ID)

Without these set, the endpoints return ``503 webauthn_not_configured``.

Challenge storage
-----------------
In-memory dict keyed by challenge bytes → ``(purpose, user_id, expires_at)``.
Single-process, 5-minute TTL, cleaned lazily. Adequate for a typical
single-container saebooks deployment. For multi-replica deployments swap
to Redis (one env var change — see ``_ChallengeStore``).

Discoverable credential login (no prior user context)
-----------------------------------------------------
The authenticate-begin call returns an empty ``allowCredentials`` list so
the browser picks the right key by ``rpId``. The credential's user
handle on the authenticator identifies the user back to us — we look it
up via the SECURITY DEFINER function ``webauthn_lookup_credential``
because we don't have a tenant context until we know the user.
"""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer
from saebooks.api.v1.deps import get_session
from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User
from saebooks.models.user_webauthn_credential import UserWebauthnCredential
from saebooks.services import platform_client as _platform
from saebooks.services import platform_facades as _pf
from saebooks.services.jwt_tokens import make_access_token

logger = logging.getLogger("saebooks.api.v1.webauthn")

router = APIRouter(prefix="/auth/webauthn", tags=["webauthn"])

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _enabled() -> bool:
    return os.environ.get("SAEBOOKS_WEBAUTHN_ENABLED", "1").strip().lower() in (
        "1", "true", "yes",
    )


def _rp_id() -> str | None:
    v = (os.environ.get("SAEBOOKS_WEBAUTHN_RP_ID") or "").strip()
    return v or None


def _rp_name() -> str:
    return os.environ.get("SAEBOOKS_WEBAUTHN_RP_NAME", "SAE Books").strip() or "SAE Books"


def _origins() -> list[str]:
    raw = (os.environ.get("SAEBOOKS_WEBAUTHN_ORIGIN") or "").strip()
    if not raw:
        return []
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _config_or_503() -> tuple[str, str, list[str]]:
    if not _enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_disabled")
    rp_id = _rp_id()
    origins = _origins()
    if not rp_id or not origins:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_not_configured")
    return rp_id, _rp_name(), origins


# --------------------------------------------------------------------------
# Challenge store
# --------------------------------------------------------------------------

_CHALLENGE_TTL = 300  # seconds


@dataclass
class _ChallengeEntry:
    purpose: str  # 'register' | 'authenticate'
    user_id: uuid.UUID | None  # set for register, None for authenticate
    tenant_id: uuid.UUID | None
    expires_at: float


class _ChallengeStore:
    """Process-local. Single-replica deployments only.

    To scale: replace with Redis (SETEX with TTL, key = challenge bytes
    hex, value = json-serialised entry). One env var.
    """

    def __init__(self) -> None:
        self._d: dict[bytes, _ChallengeEntry] = {}

    def put(self, challenge: bytes, entry: _ChallengeEntry) -> None:
        self._gc()
        self._d[challenge] = entry

    def pop(self, challenge: bytes) -> _ChallengeEntry | None:
        self._gc()
        return self._d.pop(challenge, None)

    def _gc(self) -> None:
        now = time.time()
        dead = [k for k, v in self._d.items() if v.expires_at < now]
        for k in dead:
            self._d.pop(k, None)


_STORE = _ChallengeStore()


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class RegisterBeginResponse(BaseModel):
    publicKey: dict  # PublicKeyCredentialCreationOptions, ready for navigator.credentials.create()


class RegisterFinishRequest(BaseModel):
    credential: dict
    friendly_name: str = Field(default="Security key", max_length=64)


class RegisterFinishResponse(BaseModel):
    credential_id: str  # base64url
    friendly_name: str


class AuthenticateBeginResponse(BaseModel):
    publicKey: dict


class AuthenticateFinishRequest(BaseModel):
    credential: dict


class AuthenticateFinishResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class CredentialOut(BaseModel):
    id: uuid.UUID
    friendly_name: str
    transports: list[str]
    last_used_at: str | None
    created_at: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


async def _current_user(request: Request, db: AsyncSession) -> User:
    """Resolve the authenticated user from request.state set by require_bearer."""
    u = getattr(request.state, "user", None)
    if isinstance(u, User):
        return u
    sub = (getattr(request.state, "jwt_claims", None) or {}).get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
    try:
        uid = uuid.UUID(sub)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required") from exc
    user = await db.get(User, uid)
    if user is None or user.archived_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication_required")
    return user


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@router.post("/register/begin", response_model=RegisterBeginResponse)
async def register_begin(
    request: Request,
    _: str = Depends(require_bearer),
    db: AsyncSession = Depends(get_session),
) -> RegisterBeginResponse:
    rp_id, rp_name, _origins = _config_or_503()
    user = await _current_user(request, db)

    # Don't let the user re-enroll the same physical key twice (the
    # browser sends excludeCredentials so the authenticator refuses).
    existing = (
        await db.execute(
            select(UserWebauthnCredential.credential_id)
            .where(UserWebauthnCredential.user_id == user.id)
        )
    ).scalars().all()

    try:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:
        logger.error("webauthn library missing: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_library_missing") from exc

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user.id.bytes,
        user_name=user.email or user.username,
        user_display_name=user.display_name or user.email or user.username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(cid)) for cid in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    _STORE.put(
        bytes(options.challenge),
        _ChallengeEntry(
            purpose="register",
            user_id=user.id,
            tenant_id=user.tenant_id,
            expires_at=time.time() + _CHALLENGE_TTL,
        ),
    )

    import json as _json
    return RegisterBeginResponse(publicKey=_json.loads(options_to_json(options)))


@router.post("/register/finish", response_model=RegisterFinishResponse)
async def register_finish(
    body: RegisterFinishRequest,
    request: Request,
    _: str = Depends(require_bearer),
    db: AsyncSession = Depends(get_session),
) -> RegisterFinishResponse:
    rp_id, _rp_name, origins = _config_or_503()
    user = await _current_user(request, db)

    try:
        from webauthn import verify_registration_response
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.exceptions import InvalidRegistrationResponse
    except ImportError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_library_missing") from exc

    # The browser's response contains the challenge inside the clientDataJSON;
    # we extract and match it against our store to be sure this is the
    # response to OUR begin-call.
    try:
        client_data_b64 = body.credential["response"]["clientDataJSON"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed_credential") from exc
    import json as _json
    client_data = _json.loads(base64url_to_bytes(client_data_b64))
    received_challenge = base64url_to_bytes(client_data.get("challenge", ""))
    entry = _STORE.pop(received_challenge)
    if entry is None or entry.purpose != "register" or entry.user_id != user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_challenge")

    try:
        verified = verify_registration_response(
            credential=body.credential,
            expected_challenge=received_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins if len(origins) > 1 else origins[0],
        )
    except InvalidRegistrationResponse as exc:
        logger.warning("webauthn register verify failed for user %s: %s", user.id, exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "verification_failed") from exc

    cred = UserWebauthnCredential(
        tenant_id=user.tenant_id,
        user_id=user.id,
        credential_id=bytes(verified.credential_id),
        public_key=bytes(verified.credential_public_key),
        sign_count=verified.sign_count or 0,
        transports=[t for t in (body.credential.get("response", {}).get("transports") or []) if isinstance(t, str)][:8],
        aaguid=bytes(verified.aaguid.bytes) if hasattr(verified.aaguid, "bytes") else (verified.aaguid if isinstance(verified.aaguid, (bytes, bytearray)) else b"\0" * 16),
        friendly_name=body.friendly_name[:64] or "Security key",
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    logger.info(
        "webauthn register OK: user=%s credential=%s aaguid=%s",
        user.id, _b64url_encode(bytes(verified.credential_id))[:16], cred.aaguid.hex(),
    )

    return RegisterFinishResponse(
        credential_id=_b64url_encode(bytes(verified.credential_id)),
        friendly_name=cred.friendly_name,
    )


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@router.post("/authenticate/begin", response_model=AuthenticateBeginResponse)
async def authenticate_begin() -> AuthenticateBeginResponse:
    # Platform-module delegation (#32 wave 2): the passkey ASSERT (login) half
    # is a public pre-auth ceremony that ends in a JWT mint — it moves. The
    # begin/finish pair share a process-local challenge store, so BOTH halves
    # run in the same process (module when delegating, engine otherwise). The
    # REGISTER half stays engine-side (bearer-authed, tenant-scoped write —
    # see wave-2 verdict). Flag off → in-process below.
    if _platform.delegating():
        return await _pf.mirror_post_json("auth/webauthn/authenticate/begin", {})

    rp_id, _rp_name, _origins = _config_or_503()

    try:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement
    except ImportError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_library_missing") from exc

    # Empty allowCredentials → browser uses discoverable credentials,
    # any registered passkey/key bound to this RP works.
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    _STORE.put(
        bytes(options.challenge),
        _ChallengeEntry(
            purpose="authenticate",
            user_id=None,
            tenant_id=None,
            expires_at=time.time() + _CHALLENGE_TTL,
        ),
    )

    import json as _json
    return AuthenticateBeginResponse(publicKey=_json.loads(options_to_json(options)))


@router.post("/authenticate/finish", response_model=AuthenticateFinishResponse)
async def authenticate_finish(body: AuthenticateFinishRequest) -> AuthenticateFinishResponse:
    if _platform.delegating():
        return await _pf.mirror_post_json(
            "auth/webauthn/authenticate/finish", body.model_dump(mode="json")
        )

    rp_id, _rp_name, origins = _config_or_503()

    try:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse
    except ImportError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webauthn_library_missing") from exc

    try:
        cred_id_b64 = body.credential["id"]
        client_data_b64 = body.credential["response"]["clientDataJSON"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "malformed_credential") from exc

    received_cred_id = base64url_to_bytes(cred_id_b64)
    import json as _json
    client_data = _json.loads(base64url_to_bytes(client_data_b64))
    received_challenge = base64url_to_bytes(client_data.get("challenge", ""))

    entry = _STORE.pop(received_challenge)
    if entry is None or entry.purpose != "authenticate":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_challenge")

    # Lookup the credential WITHOUT a tenant context — uses the
    # SECURITY DEFINER function from migration 0135.
    async with AsyncSessionLocal() as raw_session:
        row = (
            await raw_session.execute(
                text(
                    "SELECT id, user_id, tenant_id, public_key, sign_count "
                    "FROM webauthn_lookup_credential(:cred_id)"
                ),
                {"cred_id": received_cred_id},
            )
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "credential_not_found")
        cred_db_id, cred_user_id, cred_tenant_id, cred_pubkey, cred_sign_count = row

    try:
        verified = verify_authentication_response(
            credential=body.credential,
            expected_challenge=received_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins if len(origins) > 1 else origins[0],
            credential_public_key=bytes(cred_pubkey),
            credential_current_sign_count=int(cred_sign_count or 0),
        )
    except InvalidAuthenticationResponse as exc:
        logger.warning("webauthn authenticate verify failed: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "verification_failed") from exc

    # Update sign_count + last_used_at. Set tenant context first so the
    # tenant_isolation policy allows the UPDATE.
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(cred_tenant_id)
        await session.execute(
            text(
                "UPDATE user_webauthn_credentials "
                "SET sign_count = :sc, last_used_at = now() "
                "WHERE id = :id"
            ),
            {"sc": int(verified.new_sign_count), "id": cred_db_id},
        )
        user = await session.get(User, cred_user_id)
        if user is None or user.archived_at is not None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user_inactive")
        await session.commit()

    token = make_access_token(user)
    logger.info("webauthn authenticate OK: user=%s credential=%s", user.id, cred_db_id)
    return AuthenticateFinishResponse(
        access_token=token, token_type="bearer", expires_in=8 * 3600,
    )


# --------------------------------------------------------------------------
# Management
# --------------------------------------------------------------------------


@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(
    request: Request,
    _: str = Depends(require_bearer),
    db: AsyncSession = Depends(get_session),
) -> list[CredentialOut]:
    _config_or_503()
    user = await _current_user(request, db)
    rows = (
        await db.execute(
            select(UserWebauthnCredential)
            .where(UserWebauthnCredential.user_id == user.id)
            .order_by(UserWebauthnCredential.created_at.desc())
        )
    ).scalars().all()
    return [
        CredentialOut(
            id=c.id,
            friendly_name=c.friendly_name,
            transports=list(c.transports or []),
            last_used_at=c.last_used_at.isoformat() if c.last_used_at else None,
            created_at=c.created_at.isoformat() if c.created_at else "",
        )
        for c in rows
    ]


@router.delete("/credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    cred_id: uuid.UUID,
    request: Request,
    _: str = Depends(require_bearer),
    db: AsyncSession = Depends(get_session),
) -> None:
    _config_or_503()
    user = await _current_user(request, db)
    row = await db.get(UserWebauthnCredential, cred_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential_not_found")
    await db.delete(row)
    await db.commit()
