"""FIDO2 / WebAuthn — hardware key authentication (YubiKey, Windows Hello, etc).

Supports:
- Registration: User enrolls a security key
- Authentication: User taps key to login
- Optional 2FA: Can require FIDO2 after initial OAuth/Magic Link login
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from saebooks.db import AsyncSessionLocal, LoginSessionLocal
from saebooks.models.user import User

logger = logging.getLogger("saebooks.fido2")

# In-memory store for registration/authentication challenges
# In production, use Redis with TTL
_challenge_store: dict[str, dict[str, Any]] = {}


class FIDO2Error(Exception):
    """Base exception for FIDO2 errors."""


class FIDO2ChallengeInvalid(FIDO2Error):
    """Challenge is invalid or expired."""


async def begin_registration(user_id: uuid.UUID) -> dict[str, Any]:
    """Begin FIDO2 registration for a user.

    Returns registration options to send to the client's browser.
    The browser will prompt the user to tap their security key.
    """
    try:
        from webauthn import generate_registration_options
        from webauthn import options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            UserVerificationRequirement,
        )
        from webauthn.helpers.structs import AuthenticatorAttachment, ResidentKeyRequirement
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    # Get user info. Auth-flow lookup by primary key where the tenant is
    # not yet known — use the BYPASSRLS owner role (LoginSessionLocal), or
    # FORCE RLS on ``users`` (0055) drops the row under the saebooks_app
    # runtime role. FIDO2 is the only permitted 2FA in this org so this
    # MUST resolve the real user. See db.py + api/v1/login.py::_user_by_email.
    async with LoginSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise FIDO2Error("User not found")

        # Generate registration options
        registration_options = generate_registration_options(
            rp_id="saebooks.com.au",  # TODO: Make configurable
            rp_name="SAE Books",
            user_id=str(user_id).encode("utf-8"),
            user_name=user.username,
            user_display_name=user.display_name or user.email,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.CROSS_PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )

        # Store challenge for verification
        # Store challenge for verification (base64url-encoded for JSON safety)
        import base64 as _b64
        challenge_b64 = _b64.urlsafe_b64encode(registration_options.challenge).rstrip(b"=").decode("ascii")
        _challenge_store[challenge_b64] = {
            "user_id": str(user_id),
            "type": "registration",
            "created_at": datetime.now(UTC),
        }
        opts_dict = __import__("json").loads(options_to_json(registration_options))

        return {
            "challenge": challenge_b64,
            "options": opts_dict,
        }

        return {
            "challenge": challenge,
            "options": __import__("json").loads(options_to_json(registration_options)),
        }


async def complete_registration(
    user_id: uuid.UUID,
    challenge: str,
    credential_data: dict[str, Any],
) -> dict[str, Any]:
    """Complete FIDO2 registration.

    credential_data is the JSON response from navigator.credentials.create()
    on the client side.
    """
    try:
        from webauthn import verify_registration_response
        from webauthn.helpers import base64url_to_bytes
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    # Verify challenge
    if challenge not in _challenge_store:
        raise FIDO2ChallengeInvalid("Challenge not found")

    challenge_data = _challenge_store[challenge]
    if challenge_data["type"] != "registration":
        raise FIDO2ChallengeInvalid("Challenge is not for registration")

    try:
        # Verify the registration response
        verified_registration = verify_registration_response(
            credential=credential_data,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id="saebooks.com.au",
            expected_origin="https://app.saebooks.com.au",  # TODO: Make configurable
        )

        # Store credential data
        credential_id = verified_registration.credential_id_hex
        public_key = verified_registration.credential_public_key_hex
        sign_count = verified_registration.credential_sign_count

        # Resolve the user first via the BYPASSRLS owner role to learn the
        # tenant (tenant is not threaded into this legacy entrypoint), then
        # bind that tenant on a runtime session for the UPDATE so FORCE RLS
        # on ``users`` permits the write.
        async with LoginSessionLocal() as lookup_session:
            owner_user = await lookup_session.get(User, user_id)
        if not owner_user:
            raise FIDO2Error("User not found")
        tenant_id = owner_user.tenant_id

        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = str(tenant_id)
            user = await session.get(User, user_id)
            if not user:
                raise FIDO2Error("User not found")

            # Update user's FIDO2 registration timestamp
            if not user.fido2_registered_at:
                user.fido2_registered_at = datetime.now(UTC)
            user.fido2_credential_count = (user.fido2_credential_count or 0) + 1

            session.add(user)
            await session.commit()

        # Clean up challenge
        _challenge_store.pop(challenge, None)

        logger.info(f"User {user_id} registered FIDO2 credential {credential_id}")

        return {
            "credential_id": credential_id,
            "public_key": public_key,
            "sign_count": sign_count,
        }

    except Exception as e:
        logger.error(f"FIDO2 registration verification failed: {e}")
        raise FIDO2Error(f"Registration verification failed: {e}")


async def begin_authentication(email: str) -> dict[str, Any]:
    """Begin FIDO2 authentication for a user.

    User provides email, system checks if they have FIDO2 keys registered.
    """
    try:
        from webauthn import generate_authentication_options
        from webauthn import options_to_json
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    # Pre-auth lookup BY EMAIL — tenant unknown at this point, so use the
    # BYPASSRLS owner role (LoginSessionLocal). Under the saebooks_app
    # runtime role FORCE RLS on ``users`` would return zero rows and every
    # FIDO2 sign-in would falsely report "no credentials registered".
    async with LoginSessionLocal() as session:
        user_result = await session.execute(
            select(User).where(User.email == email)
        )
        user = user_result.scalars().first()

        if not user or not user.fido2_registered_at:
            raise FIDO2Error("No FIDO2 credentials registered for this email")

        # Generate authentication options
        authentication_options = generate_authentication_options(
            rp_id="saebooks.com.au",
        )

        # Store challenge. (Was a NameError here: referenced an undefined
        # ``user_id`` instead of ``user.id`` — fixed so the function can
        # actually reach the DB and store the challenge.)
        import base64 as _b64
        challenge_b64 = _b64.urlsafe_b64encode(authentication_options.challenge).rstrip(b"=").decode("ascii")
        _challenge_store[challenge_b64] = {
            "user_id": str(user.id),
            "type": "authentication",
            "created_at": datetime.now(UTC),
        }
        opts_dict = __import__("json").loads(options_to_json(authentication_options))

        return {
            "challenge": challenge_b64,
            "options": opts_dict,
        }

        return {
            "challenge": challenge,
            "options": __import__("json").loads(options_to_json(authentication_options)),
        }


async def complete_authentication(
    challenge: str,
    credential_data: dict[str, Any],
) -> User:
    """Complete FIDO2 authentication.

    Returns the authenticated user.
    """
    try:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    if challenge not in _challenge_store:
        raise FIDO2ChallengeInvalid("Challenge not found")

    challenge_data = _challenge_store[challenge]
    if challenge_data["type"] != "authentication":
        raise FIDO2ChallengeInvalid("Challenge is not for authentication")

    user_id_str = challenge_data["user_id"]

    try:
        # In production, you would:
        # 1. Look up the credential from the database
        # 2. Verify the authentication response against the stored credential
        # 3. Update sign_count to prevent replay attacks
        #
        # This is a simplified version - full implementation requires
        # storing credential details in the database.

        # Get user — auth-flow lookup by primary key, tenant unknown, so
        # use the BYPASSRLS owner role (LoginSessionLocal). FORCE RLS on
        # ``users`` would otherwise drop the row under saebooks_app and the
        # authenticated user would be reported as "not found".
        async with LoginSessionLocal() as session:
            user = await session.get(User, uuid.UUID(user_id_str))
            if not user:
                raise FIDO2Error("User not found")

            logger.info(f"User {user_id_str} authenticated via FIDO2")
            return user

    except Exception as e:
        logger.error(f"FIDO2 authentication verification failed: {e}")
        raise FIDO2Error(f"Authentication verification failed: {e}")
    finally:
        # Clean up challenge
        _challenge_store.pop(challenge, None)
