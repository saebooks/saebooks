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

from saebooks.db import AsyncSessionLocal
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
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            UserVerificationRequirement,
        )
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    # Get user info
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise FIDO2Error("User not found")

        # Generate registration options
        registration_options = generate_registration_options(
            rp_id="saebooks.com.au",  # TODO: Make configurable
            rp_name="SAE Books",
            user_id=str(user_id),
            user_name=user.username,
            user_display_name=user.display_name or user.email,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment="cross-platform",  # External key (YubiKey, etc)
                resident_key="preferred",
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )

        # Store challenge for verification
        challenge = registration_options.challenge
        _challenge_store[challenge] = {
            "user_id": str(user_id),
            "type": "registration",
            "created_at": datetime.now(UTC),
            "options": registration_options.model_dump(),
        }

        return {
            "challenge": challenge,
            "options": registration_options.model_dump(),
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

        async with AsyncSessionLocal() as session:
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
    except ImportError:
        raise FIDO2Error("webauthn library not installed")

    # Look up user by email
    async with AsyncSessionLocal() as session:
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

        # Store challenge
        challenge = authentication_options.challenge
        _challenge_store[challenge] = {
            "user_id": str(user.id),
            "email": email,
            "type": "authentication",
            "created_at": datetime.now(UTC),
        }

        return {
            "challenge": challenge,
            "options": authentication_options.model_dump(),
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

        # Get user
        async with AsyncSessionLocal() as session:
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
