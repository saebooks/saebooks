"""Live FIDO2 / WebAuthn ceremony for the cross-tenant *principal*.

This module implements the two WebAuthn ceremonies for a principal
(accountant / bank), reusing the SAME ``webauthn`` library machinery as the
ordinary user flow in ``saebooks.api.v1.webauthn`` — NOT the legacy,
signature-skipping ``saebooks.services.fido2_service``. We do real
attestation verification (register) and real assertion verification
(authenticate), exactly like the user passkey path.

THE CRITICAL INVARIANT
----------------------
At login (``complete_authentication``) the principal id is derived ONLY from
the credential the assertion was signed with:

  1. The browser sends back a credential ``id`` + an assertion.
  2. We look that credential up by id via the SECURITY DEFINER function
     ``principal_webauthn_lookup_credential`` (migration 0159) — no tenant /
     session context needed, the id is a 256-bit unguessable blob.
  3. We verify the assertion SIGNATURE against that credential's stored
     ``public_key``. A row match alone proves nothing; the signature is the
     proof.
  4. ONLY THEN do we take ``principal_id`` = the resolved row's
     ``principal_id``.

There is no code path that reads a principal id from the request body, query,
path, or header at login. A client cannot assert "I am principal X" — it can
only present a key, and the key tells us who it is. See
``docs/security/accountant-principal.md`` §10.

FIDO2-only
----------
Registration requires an already-authenticated principal session (a principal
adds a key to its own account; the first key is enrolled out-of-band / by an
operator, same as a user's first key). Authentication is FIDO2 assertion only
— there is no password and no code-2FA fallback, ever (standing rule).

Challenge store
---------------
Process-local, 5-minute TTL — identical to ``saebooks.api.v1.webauthn``.
Adequate for a single-replica saebooks instance; swap for Redis to scale
(documented seam). The principal challenge store is SEPARATE from the user
one so a user-flow challenge can never satisfy a principal ceremony.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from saebooks.db import AsyncSessionLocal, LoginSessionLocal
from saebooks.models.principal import Principal, PrincipalFido2Credential
from saebooks.services.principal import assert_fido2_satisfied

logger = logging.getLogger("saebooks.principal.webauthn")


class PrincipalWebauthnError(Exception):
    """Base error for the principal WebAuthn ceremonies."""


class PrincipalWebauthnNotConfigured(PrincipalWebauthnError):
    """RP id / origin not configured (or WebAuthn disabled)."""


class PrincipalWebauthnChallengeInvalid(PrincipalWebauthnError):
    """The presented challenge is unknown, expired, or for the wrong ceremony."""


class PrincipalCredentialNotFound(PrincipalWebauthnError):
    """No principal credential matches the presented credential id."""


class PrincipalAssertionInvalid(PrincipalWebauthnError):
    """The assertion signature failed verification."""


# --------------------------------------------------------------------------- #
# Config — reuse the same env vars as the user WebAuthn flow.
# --------------------------------------------------------------------------- #


def _enabled() -> bool:
    return os.environ.get("SAEBOOKS_WEBAUTHN_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _rp_id() -> str | None:
    v = (os.environ.get("SAEBOOKS_WEBAUTHN_RP_ID") or "").strip()
    return v or None


def _rp_name() -> str:
    return (
        os.environ.get("SAEBOOKS_WEBAUTHN_RP_NAME", "SAE Books").strip()
        or "SAE Books"
    )


def _origins() -> list[str]:
    raw = (os.environ.get("SAEBOOKS_WEBAUTHN_ORIGIN") or "").strip()
    if not raw:
        return []
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _config_or_raise() -> tuple[str, str, list[str]]:
    if not _enabled():
        raise PrincipalWebauthnNotConfigured("webauthn_disabled")
    rp_id = _rp_id()
    origins = _origins()
    if not rp_id or not origins:
        raise PrincipalWebauthnNotConfigured("webauthn_not_configured")
    return rp_id, _rp_name(), origins


# --------------------------------------------------------------------------- #
# Challenge store — process-local, separate from the user store.
# --------------------------------------------------------------------------- #

_CHALLENGE_TTL = 300


@dataclass
class _ChallengeEntry:
    purpose: str  # 'register' | 'authenticate'
    principal_id: uuid.UUID | None  # set for register, None for authenticate
    expires_at: float


class _ChallengeStore:
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
        for k in [k for k, v in self._d.items() if v.expires_at < now]:
            self._d.pop(k, None)


_STORE = _ChallengeStore()


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- #
# Registration — principal adds a key (requires an authenticated principal).
# --------------------------------------------------------------------------- #


async def begin_registration(principal_id: uuid.UUID) -> dict[str, Any]:
    """Return ``PublicKeyCredentialCreationOptions`` for ``principal_id``.

    ``principal_id`` is the *authenticated* principal's id (from its session),
    not a client value — the endpoint takes it from ``require_principal_bearer``.
    """
    rp_id, rp_name, _origins = _config_or_raise()

    try:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:  # pragma: no cover - lib is a hard dep
        raise PrincipalWebauthnError("webauthn library not installed") from exc

    # Resolve the principal + its existing credentials. principals is global
    # (no RLS), but we read via the owner role to be robust to any future
    # grant tightening — same belt as the user flow's pre-auth lookups.
    async with LoginSessionLocal() as session:
        principal = await session.get(Principal, principal_id)
        if principal is None or principal.archived_at is not None:
            raise PrincipalWebauthnError("principal not found")
        existing = (
            await session.execute(
                text(
                    "SELECT credential_id FROM principal_fido2_credentials "
                    "WHERE principal_id = :pid"
                ),
                {"pid": str(principal_id)},
            )
        ).all()

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=principal.id.bytes,
        user_name=principal.username,
        user_display_name=principal.display_name or principal.username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(row.credential_id))
            for row in existing
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
            principal_id=principal.id,
            expires_at=time.time() + _CHALLENGE_TTL,
        ),
    )
    return {"publicKey": json.loads(options_to_json(options))}


async def complete_registration(
    principal_id: uuid.UUID,
    credential: dict[str, Any],
    friendly_name: str = "Security key",
) -> dict[str, Any]:
    """Verify the attestation and persist the credential for ``principal_id``.

    ``principal_id`` is the authenticated principal (from its session). We
    additionally bind the challenge to that principal at begin-time and
    re-check it here, so a register-finish cannot land a key on another
    principal's account.
    """
    rp_id, _rp_name, origins = _config_or_raise()

    try:
        from webauthn import verify_registration_response
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.exceptions import InvalidRegistrationResponse
    except ImportError as exc:  # pragma: no cover
        raise PrincipalWebauthnError("webauthn library not installed") from exc

    try:
        client_data_b64 = credential["response"]["clientDataJSON"]
    except (KeyError, TypeError) as exc:
        raise PrincipalWebauthnError("malformed credential") from exc
    client_data = json.loads(base64url_to_bytes(client_data_b64))
    received_challenge = base64url_to_bytes(client_data.get("challenge", ""))

    entry = _STORE.pop(received_challenge)
    if (
        entry is None
        or entry.purpose != "register"
        or entry.principal_id != principal_id
    ):
        raise PrincipalWebauthnChallengeInvalid("invalid_challenge")

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=received_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins if len(origins) > 1 else origins[0],
        )
    except InvalidRegistrationResponse as exc:
        raise PrincipalWebauthnError(f"verification_failed: {exc}") from exc

    transports = [
        t
        for t in (credential.get("response", {}).get("transports") or [])
        if isinstance(t, str)
    ][:8]

    # Persist under the runtime role. principal_fido2_credentials is not
    # RLS'd, so no app.current_tenant needed.
    async with AsyncSessionLocal() as session:
        cred = PrincipalFido2Credential(
            principal_id=principal_id,
            credential_id=bytes(verified.credential_id),
            public_key=bytes(verified.credential_public_key),
            sign_count=verified.sign_count or 0,
            transports=transports,
            friendly_name=(friendly_name or "Security key")[:64],
        )
        session.add(cred)
        await session.commit()
        await session.refresh(cred)

    logger.info(
        "principal webauthn register OK: principal=%s credential=%s",
        principal_id,
        _b64url_encode(bytes(verified.credential_id))[:16],
    )
    return {
        "credential_id": _b64url_encode(bytes(verified.credential_id)),
        "friendly_name": cred.friendly_name,
    }


# --------------------------------------------------------------------------- #
# Authentication — the login ceremony. principal_id is DERIVED from the
# verified assertion, never supplied by the client.
# --------------------------------------------------------------------------- #


async def begin_authentication() -> dict[str, Any]:
    """Return ``PublicKeyCredentialRequestOptions`` for a discoverable login.

    ``allowCredentials`` is empty: the browser picks the right key by ``rpId``
    and returns its credential id, which identifies the principal back to us.
    We do NOT take any principal identifier from the client at begin-time.
    """
    rp_id, _rp_name, _origins = _config_or_raise()

    try:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement
    except ImportError as exc:  # pragma: no cover
        raise PrincipalWebauthnError("webauthn library not installed") from exc

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _STORE.put(
        bytes(options.challenge),
        _ChallengeEntry(
            purpose="authenticate",
            principal_id=None,
            expires_at=time.time() + _CHALLENGE_TTL,
        ),
    )
    return {"publicKey": json.loads(options_to_json(options))}


async def complete_authentication(credential: dict[str, Any]) -> uuid.UUID:
    """Verify a login assertion and return the authenticated principal id.

    THE security-critical function. Returns the ``principal_id`` taken from
    the credential resolved + signature-verified here — NEVER from any client
    parameter. Raises on any failure; callers mint a principal session only on
    a returned id.

    Steps (mirrors ``saebooks.api.v1.webauthn.authenticate_finish``):
      1. parse the credential id + challenge from the assertion;
      2. match the challenge to OUR begin-call (anti-CSRF / replay of options);
      3. resolve the credential by id via the SECURITY DEFINER lookup;
      4. verify the assertion signature against the stored public key;
      5. bump sign_count (anti-replay) + last_used_at;
      6. confirm the resolved principal is active and FIDO2-satisfied;
      7. return the resolved principal id.
    """
    rp_id, _rp_name, origins = _config_or_raise()

    try:
        from webauthn import verify_authentication_response
        from webauthn.helpers import base64url_to_bytes
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse
    except ImportError as exc:  # pragma: no cover
        raise PrincipalWebauthnError("webauthn library not installed") from exc

    try:
        cred_id_b64 = credential["id"]
        client_data_b64 = credential["response"]["clientDataJSON"]
    except (KeyError, TypeError) as exc:
        raise PrincipalWebauthnError("malformed credential") from exc

    received_cred_id = base64url_to_bytes(cred_id_b64)
    client_data = json.loads(base64url_to_bytes(client_data_b64))
    received_challenge = base64url_to_bytes(client_data.get("challenge", ""))

    entry = _STORE.pop(received_challenge)
    if entry is None or entry.purpose != "authenticate":
        raise PrincipalWebauthnChallengeInvalid("invalid_challenge")

    # 3. Resolve the credential by id — no session/tenant context. The
    #    SECURITY DEFINER function (0159) returns (id, principal_id,
    #    public_key, sign_count) or nothing. This is the ONLY place the
    #    principal id originates.
    async with AsyncSessionLocal() as raw_session:
        row = (
            await raw_session.execute(
                text(
                    "SELECT id, principal_id, public_key, sign_count "
                    "FROM principal_webauthn_lookup_credential(:cred_id)"
                ),
                {"cred_id": received_cred_id},
            )
        ).first()
    if row is None:
        raise PrincipalCredentialNotFound("credential_not_found")
    cred_db_id, resolved_principal_id, cred_pubkey, cred_sign_count = row

    # 4. Verify the signature against the STORED public key. A row match is
    #    not enough — this is the proof of possession.
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=received_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins if len(origins) > 1 else origins[0],
            credential_public_key=bytes(cred_pubkey),
            credential_current_sign_count=int(cred_sign_count or 0),
        )
    except InvalidAuthenticationResponse as exc:
        raise PrincipalAssertionInvalid("verification_failed") from exc

    # 5/6. Bump the anti-replay counter, confirm the principal is active +
    #      FIDO2-satisfied, all under the owner role (no RLS on these tables).
    async with LoginSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE principal_fido2_credentials "
                "SET sign_count = :sc, last_used_at = now() WHERE id = :id"
            ),
            {"sc": int(verified.new_sign_count), "id": cred_db_id},
        )
        principal = await session.get(Principal, resolved_principal_id)
        if principal is None or principal.archived_at is not None:
            raise PrincipalAssertionInvalid("principal_inactive")
        # Defence in depth — the principal authenticated WITH a credential, so
        # this is satisfied by construction, but we keep the gate so the rule
        # holds even if the credential/principal link is ever reshaped.
        await assert_fido2_satisfied(session, principal)
        await session.commit()

    logger.info(
        "principal webauthn authenticate OK: principal=%s credential=%s",
        resolved_principal_id,
        cred_db_id,
    )
    # 7. The id is server-derived, full stop.
    return resolved_principal_id
