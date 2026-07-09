"""Security tests for the principal LOGIN ceremony (feat/accountant-login).

THE most security-critical surface in SAE Books: a successful login mints an
identity that can cross tenant boundaries. These tests pin the one invariant
that everything else rests on:

    The authenticated principal id is derived ONLY from the verified FIDO2
    assertion (the resolved credential's owner) — NEVER from a client-supplied
    parameter.

To exercise the ceremony without a physical authenticator we patch the
``webauthn`` library's ``verify_authentication_response`` (the exact same
verification the user passkey flow uses). We DO NOT patch our own resolution
logic — the credential lookup, the principal-id derivation, and the token mint
all run for real. So a test that passes proves OUR plumbing takes the id from
the credential, not from the request.

All run against the saebooks_app (NOBYPASSRLS) DB the test stack provides.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

os.environ.setdefault("SAEBOOKS_SECRET_KEY", "test-secret-key-principal-login")
# The principal WebAuthn ceremony needs RP config or it 503s.
os.environ.setdefault("SAEBOOKS_WEBAUTHN_RP_ID", "books.test")
os.environ.setdefault("SAEBOOKS_WEBAUTHN_ORIGIN", "https://books.test")

from saebooks.db import engine as _owner_engine
from saebooks.main import app
from saebooks.models.principal import Principal, PrincipalFido2Credential
from saebooks.services import principal_webauthn as pw
from saebooks.services.principal_session import (
    PRINCIPAL_TOKEN_TYPE,
    decode_principal_token,
)


@pytest_asyncio.fixture
async def owner_sessionmaker() -> AsyncIterator[Any]:
    yield async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest_asyncio.fixture
async def two_principals(owner_sessionmaker: Any) -> AsyncIterator[dict[str, Any]]:
    """Two principals, each with one credential. Returns ids + cred ids."""
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {}
    async with owner_sessionmaker() as s:
        p_victim = Principal(
            id=uuid.uuid4(),
            display_name="Victim Acct",
            username=f"victim-{suffix}",
        )
        p_attacker = Principal(
            id=uuid.uuid4(),
            display_name="Attacker Acct",
            username=f"attacker-{suffix}",
        )
        s.add_all([p_victim, p_attacker])
        await s.flush()
        # Each principal owns one credential.
        victim_cred = b"victim-cred-" + suffix.encode()
        attacker_cred = b"attacker-cred-" + suffix.encode()
        s.add_all(
            [
                PrincipalFido2Credential(
                    principal_id=p_victim.id,
                    credential_id=victim_cred,
                    public_key=b"victim-pubkey",
                ),
                PrincipalFido2Credential(
                    principal_id=p_attacker.id,
                    credential_id=attacker_cred,
                    public_key=b"attacker-pubkey",
                ),
            ]
        )
        await s.commit()
        out = {
            "victim_id": p_victim.id,
            "attacker_id": p_attacker.id,
            "victim_cred": victim_cred,
            "attacker_cred": attacker_cred,
        }
    yield out
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "DELETE FROM principal_fido2_credentials "
                "WHERE principal_id IN (:a, :b)"
            ),
            {"a": str(out["victim_id"]), "b": str(out["attacker_id"])},
        )
        await s.execute(
            text("DELETE FROM principals WHERE id IN (:a, :b)"),
            {"a": str(out["victim_id"]), "b": str(out["attacker_id"])},
        )
        await s.commit()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _assertion_for(cred_id: bytes) -> dict[str, Any]:
    """Build a minimal assertion JSON whose credential id is ``cred_id``.

    clientDataJSON carries a placeholder challenge; the test patches
    ``_STORE.pop`` to accept it and patches ``verify_authentication_response``
    so the signature step passes. The credential ``id`` is the only field that
    matters for the invariant under test — it is what our lookup keys on.
    """
    import base64
    import json

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    client_data = json.dumps(
        {"type": "webauthn.get", "challenge": b64u(b"test-challenge"), "origin": "https://books.test"}
    ).encode()
    return {
        "id": b64u(cred_id),
        "rawId": b64u(cred_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64u(client_data),
            "authenticatorData": b64u(b"authdata"),
            "signature": b64u(b"sig"),
        },
    }


class _FakeVerified:
    def __init__(self, new_sign_count: int = 1) -> None:
        self.new_sign_count = new_sign_count


@pytest.fixture
def patched_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the challenge check + signature verification pass deterministically.

    Crucially we do NOT touch the credential lookup or the principal-id
    derivation — those run for real against the seeded rows.
    """
    import base64

    # Accept our placeholder challenge regardless of store contents.
    def _fake_pop(self: Any, challenge: bytes) -> Any:
        return pw._ChallengeEntry(
            purpose="authenticate", principal_id=None, expires_at=1e18
        )

    monkeypatch.setattr(pw._ChallengeStore, "pop", _fake_pop)

    # Patch the library verify in the module's import namespace. The service
    # imports it locally inside the function, so patch the library symbol.
    import webauthn

    def _fake_verify(**kwargs: Any) -> _FakeVerified:
        # Confirm the verification is fed the STORED public key for the
        # resolved credential — proves we looked it up, not trusted input.
        assert kwargs.get("credential_public_key") is not None
        return _FakeVerified(new_sign_count=99)

    monkeypatch.setattr(
        webauthn, "verify_authentication_response", _fake_verify
    )
    # base64url_to_bytes is real; nothing to patch there.
    _ = base64


# --------------------------------------------------------------------------- #
# THE invariant: principal_id comes from the resolved credential, not input.
# --------------------------------------------------------------------------- #


async def test_login_derives_principal_id_from_credential_not_client(
    client: AsyncClient,
    two_principals: dict[str, Any],
    patched_assertion: None,
) -> None:
    """An assertion signed with the VICTIM credential mints a session for the
    VICTIM — even though the attacker controls the whole request body and
    might wish to be someone else. There is no field to claim an identity; the
    credential id decides.
    """
    victim_cred = two_principals["victim_cred"]
    victim_id = two_principals["victim_id"]

    assertion = _assertion_for(victim_cred)
    # An attacker cannot add a principal_id field — the schema forbids extras
    # and the server ignores anything but ``credential``. We still try, to
    # prove it is inert.
    body = {"credential": assertion, "principal_id": str(two_principals["attacker_id"])}

    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/finish", json=body
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # The minted session is the VICTIM's — derived from the credential.
    assert data["principal_id"] == str(victim_id)
    assert data["tenant_id"] is None  # unbound login token

    claims = decode_principal_token(data["access_token"])
    assert claims["psub"] == str(victim_id)
    assert claims["typ"] == PRINCIPAL_TOKEN_TYPE
    assert "tenant_id" not in claims


async def test_login_with_attacker_credential_is_attacker_only(
    client: AsyncClient,
    two_principals: dict[str, Any],
    patched_assertion: None,
) -> None:
    """Positive control: an assertion signed with the attacker credential
    authenticates the attacker — proving the derivation tracks the credential,
    not a fixed value.
    """
    assertion = _assertion_for(two_principals["attacker_cred"])
    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/finish",
        json={"credential": assertion},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_id"] == str(two_principals["attacker_id"])


async def test_login_unknown_credential_rejected(
    client: AsyncClient,
    two_principals: dict[str, Any],
    patched_assertion: None,
) -> None:
    """A credential id that matches no principal -> 401, no session."""
    assertion = _assertion_for(b"no-such-credential-anywhere")
    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/finish",
        json={"credential": assertion},
    )
    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.json()


async def test_login_bad_signature_rejected(
    client: AsyncClient,
    two_principals: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed signature verification -> 401, no session — even with a real,
    resolvable credential id. A row match is not enough.
    """
    # Challenge passes, but the library verify RAISES.
    def _fake_pop(self: Any, challenge: bytes) -> Any:
        return pw._ChallengeEntry(
            purpose="authenticate", principal_id=None, expires_at=1e18
        )

    monkeypatch.setattr(pw._ChallengeStore, "pop", _fake_pop)
    import webauthn
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    def _raise(**kwargs: Any) -> Any:
        raise InvalidAuthenticationResponse("bad sig")

    monkeypatch.setattr(webauthn, "verify_authentication_response", _raise)

    assertion = _assertion_for(two_principals["victim_cred"])
    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/finish",
        json={"credential": assertion},
    )
    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.json()


async def test_login_stale_challenge_rejected(
    client: AsyncClient, two_principals: dict[str, Any]
) -> None:
    """Without a matching begin-challenge in the store -> 400. (No monkeypatch
    of the store here, so the real empty store is consulted.)"""
    assertion = _assertion_for(two_principals["victim_cred"])
    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/finish",
        json={"credential": assertion},
    )
    assert resp.status_code == 400, resp.text


async def test_begin_authentication_returns_options(
    client: AsyncClient,
) -> None:
    """The begin endpoint returns PublicKeyCredentialRequestOptions with an
    empty allowCredentials (discoverable login) and a challenge."""
    resp = await client.post(
        "/api/v1/principal/auth/webauthn/authenticate/begin"
    )
    assert resp.status_code == 200, resp.text
    pk = resp.json()["publicKey"]
    assert "challenge" in pk
    assert pk.get("allowCredentials", []) == []
