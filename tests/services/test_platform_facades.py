"""Facade tests for the platform env-flag delegation (#32 wave 1).

Flag OFF (``PLATFORM_BASE_URL`` empty) → ``delegating()`` is False and the
engine runs its in-process code unchanged (the existing ``test_billing`` /
``test_auth_signup`` / ``test_auth_promo`` suites prove that behaviour; here we
just assert the flag gate).

Flag ON → the delegation surface POSTs to the module. These tests respx-mock
the module and assert:
  1. the request reaches the right ``/module/platform`` path,
  2. it carries the service token (``X-Platform-Token``) — and, for the
     webhook, the forwarded ``Stripe-Signature`` header and RAW body,
  3. the module's status + JSON body are mirrored back verbatim.

Per the 2026-07-04 test-hygiene rule, settings are patched by STRING path only.
"""
from __future__ import annotations

import json as _json

import httpx
import pytest
import respx

from saebooks.services import platform_client as pc
from saebooks.services import platform_facades as pf

_BASE = "http://platform-module:8080"
_SVC_TOKEN = "platform-svc-token"


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.platform_base_url", _BASE)
    monkeypatch.setattr(
        "saebooks.config.settings.platform_service_token", _SVC_TOKEN
    )


# --------------------------------------------------------------------------- #
# Flag gate                                                                     #
# --------------------------------------------------------------------------- #
def test_delegating_reflects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saebooks.config.settings.platform_base_url", "")
    assert pc.delegating() is False
    monkeypatch.setattr("saebooks.config.settings.platform_base_url", _BASE)
    assert pc.delegating() is True


# --------------------------------------------------------------------------- #
# mirror_post_json — signup / magic-link routes                                #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_mirror_post_json_forwards_and_mirrors_status(flag_on: None) -> None:
    route = respx.post(f"{_BASE}/module/platform/auth/signup").mock(
        return_value=httpx.Response(201, json={"message": "Verification email sent."})
    )
    resp = await pf.mirror_post_json(
        "auth/signup",
        {"email": "a@b.com", "password": "TestPass1234", "company_name": "Co"},
    )
    assert resp.status_code == 201
    assert _json.loads(bytes(resp.body))["message"] == "Verification email sent."

    req = route.calls.last.request
    assert req.headers["X-Platform-Token"] == _SVC_TOKEN
    assert _json.loads(req.content)["email"] == "a@b.com"


@respx.mock
async def test_mirror_post_json_passes_through_error_status(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/auth/signup").mock(
        return_value=httpx.Response(409, json={"detail": "already exists"})
    )
    resp = await pf.mirror_post_json("auth/signup", {"email": "dup@b.com"})
    assert resp.status_code == 409
    assert _json.loads(bytes(resp.body))["detail"] == "already exists"


@respx.mock
async def test_mirror_post_json_mirrors_token_response(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/auth/magic-link/consume").mock(
        return_value=httpx.Response(
            200, json={"access_token": "jwt.abc.def", "token_type": "bearer"}
        )
    )
    resp = await pf.mirror_post_json(
        "auth/magic-link/consume", {"token": "magic-tok"}
    )
    assert resp.status_code == 200
    assert _json.loads(bytes(resp.body))["access_token"] == "jwt.abc.def"


@respx.mock
async def test_mirror_post_json_forwards_retry_after(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/auth/magic-link/request").mock(
        return_value=httpx.Response(
            429, json={"detail": "rate limited"}, headers={"Retry-After": "30"}
        )
    )
    resp = await pf.mirror_post_json("auth/magic-link/request", {"email": "a@b.com"})
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "30"


# --------------------------------------------------------------------------- #
# mirror_post_raw — Stripe webhook                                             #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_mirror_post_raw_forwards_body_and_signature(flag_on: None) -> None:
    route = respx.post(f"{_BASE}/module/platform/billing/webhook").mock(
        return_value=httpx.Response(200, json={"received": True, "type": "x"})
    )
    raw = b'{"type":"checkout.session.completed"}'
    resp = await pf.mirror_post_raw(
        "billing/webhook",
        raw,
        forward_headers={"Stripe-Signature": "t=1,v1=abc"},
    )
    assert resp.status_code == 200
    assert _json.loads(bytes(resp.body))["received"] is True

    req = route.calls.last.request
    assert req.headers["X-Platform-Token"] == _SVC_TOKEN
    assert req.headers["Stripe-Signature"] == "t=1,v1=abc"
    assert req.content == raw


@respx.mock
async def test_mirror_post_raw_passes_through_400(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/billing/webhook").mock(
        return_value=httpx.Response(400, json={"detail": "Invalid Stripe signature"})
    )
    resp = await pf.mirror_post_raw("billing/webhook", b"{}", forward_headers=None)
    assert resp.status_code == 400


@respx.mock
async def test_transport_failure_raises_service_error(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/auth/signup").mock(
        side_effect=httpx.ConnectError("no route")
    )
    with pytest.raises(pc.PlatformServiceError):
        await pf.mirror_post_json("auth/signup", {"email": "a@b.com"})


# --------------------------------------------------------------------------- #
# Wave-2 moved ceremonies — proxy path (login / webauthn-assert / principal)   #
# --------------------------------------------------------------------------- #
@respx.mock
async def test_mirror_login_forwards_and_mirrors_token(flag_on: None) -> None:
    route = respx.post(f"{_BASE}/module/platform/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "jwt.log.in", "token_type": "bearer", "expires_in": 28800},
        )
    )
    resp = await pf.mirror_post_json(
        "auth/login", {"email": "a@b.com", "password": "hunter2hunter2"}
    )
    assert resp.status_code == 200
    assert _json.loads(bytes(resp.body))["access_token"] == "jwt.log.in"
    req = route.calls.last.request
    assert req.headers["X-Platform-Token"] == _SVC_TOKEN
    assert _json.loads(req.content)["email"] == "a@b.com"


@respx.mock
async def test_mirror_login_passes_through_401(flag_on: None) -> None:
    respx.post(f"{_BASE}/module/platform/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid credentials"})
    )
    resp = await pf.mirror_post_json("auth/login", {"email": "a@b.com", "password": "x"})
    assert resp.status_code == 401
    assert _json.loads(bytes(resp.body))["detail"] == "Invalid credentials"


@respx.mock
async def test_mirror_oauth_handoff_forwards_secret_header(flag_on: None) -> None:
    route = respx.post(f"{_BASE}/module/platform/auth/oauth-handoff").mock(
        return_value=httpx.Response(
            200, json={"access_token": "jwt.oauth", "token_type": "bearer", "expires_in": 28800}
        )
    )
    resp = await pf.mirror_post_json(
        "auth/oauth-handoff",
        {"provider": "discourse", "provider_user_id": "42", "email": "a@b.com"},
        forward_headers={"X-OAuth-Handoff-Secret": "shh"},
    )
    assert resp.status_code == 200
    req = route.calls.last.request
    assert req.headers["X-Platform-Token"] == _SVC_TOKEN
    # The caller's handoff secret rides along for the module to re-verify.
    assert req.headers["X-OAuth-Handoff-Secret"] == "shh"


@respx.mock
async def test_mirror_webauthn_assert_begin_empty_body(flag_on: None) -> None:
    route = respx.post(
        f"{_BASE}/module/platform/auth/webauthn/authenticate/begin"
    ).mock(return_value=httpx.Response(200, json={"publicKey": {"challenge": "abc"}}))
    resp = await pf.mirror_post_json("auth/webauthn/authenticate/begin", {})
    assert resp.status_code == 200
    assert _json.loads(bytes(resp.body))["publicKey"]["challenge"] == "abc"
    # begin carries no body — an empty JSON object is forwarded.
    assert _json.loads(route.calls.last.request.content) == {}


@respx.mock
async def test_mirror_webauthn_assert_finish_mirrors_token(flag_on: None) -> None:
    respx.post(
        f"{_BASE}/module/platform/auth/webauthn/authenticate/finish"
    ).mock(
        return_value=httpx.Response(
            200, json={"access_token": "jwt.passkey", "token_type": "bearer", "expires_in": 28800}
        )
    )
    resp = await pf.mirror_post_json(
        "auth/webauthn/authenticate/finish", {"credential": {"id": "x"}}
    )
    assert resp.status_code == 200
    assert _json.loads(bytes(resp.body))["access_token"] == "jwt.passkey"


@respx.mock
async def test_mirror_principal_login_finish_mirrors_token(flag_on: None) -> None:
    respx.post(
        f"{_BASE}/module/platform/principal/auth/webauthn/authenticate/finish"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "jwt.principal",
                "token_type": "bearer",
                "expires_in": 3600,
                "principal_id": "11111111-1111-1111-1111-111111111111",
            },
        )
    )
    resp = await pf.mirror_post_json(
        "principal/auth/webauthn/authenticate/finish", {"credential": {"id": "x"}}
    )
    assert resp.status_code == 200
    body = _json.loads(bytes(resp.body))
    assert body["access_token"] == "jwt.principal"
    assert body["principal_id"] == "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# SAEBOOKS_SECRET_KEY parity preflight (verify_key_parity_or_disable)          #
# --------------------------------------------------------------------------- #
def _mint_with_key(key: str, payload: dict) -> str:
    """Mint a JWT signed with ``key`` (isolated from the process cache).

    ``jwt_tokens._secret_key()`` resolves the signing key from
    ``settings.secret_key`` (a constructed singleton) — NOT os.environ — so we
    temporarily set that attribute and reset the module cache around the mint,
    restoring both afterwards.
    """
    from saebooks.config import settings
    from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

    prev = settings.secret_key
    settings.secret_key = key
    _reset_secret_cache()
    try:
        return create_access_token(payload, expires_in_seconds=60)
    finally:
        settings.secret_key = prev
        _reset_secret_cache()


@pytest.fixture(autouse=True)
def _reset_delegation_switch() -> None:
    pc._reset_delegation_for_tests()
    yield
    pc._reset_delegation_for_tests()


@pytest.fixture
def engine_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin the ENGINE's SAEBOOKS_SECRET_KEY (used to verify the probe).

    Patched by STRING path on the settings singleton, since that is what the
    JWT layer reads; a stable value avoids the empty-key ephemeral-regeneration
    path that would make every reset produce a different key.
    """
    from saebooks.services.jwt_tokens import _reset_secret_cache

    monkeypatch.setattr("saebooks.config.settings.secret_key", "engine-parity-key")
    _reset_secret_cache()
    yield "engine-parity-key"
    _reset_secret_cache()


@respx.mock
async def test_keycheck_parity_match_keeps_delegation(
    flag_on: None, engine_key: str
) -> None:
    # Module mints with the SAME key the engine verifies with → parity holds.
    good = _mint_with_key(engine_key, {"sub": "platform-keycheck", "kind": "keycheck"})
    respx.get(f"{_BASE}/module/platform/keycheck").mock(
        return_value=httpx.Response(200, json={"token": good})
    )
    ok = await pc.verify_key_parity_or_disable()
    assert ok is True
    assert pc.delegating() is True


@respx.mock
async def test_keycheck_parity_mismatch_disables_delegation(
    flag_on: None, engine_key: str
) -> None:
    # Module mints with a DIFFERENT key → the engine cannot verify it.
    bad = _mint_with_key("module-different-key", {"sub": "platform-keycheck"})
    route = respx.get(f"{_BASE}/module/platform/keycheck").mock(
        return_value=httpx.Response(200, json={"token": bad})
    )
    ok = await pc.verify_key_parity_or_disable()
    assert ok is False
    # Fail-open to in-process: delegation is disabled for the run.
    assert pc.delegating() is False
    # The probe was token-gated with the service token.
    assert route.calls.last.request.headers["X-Platform-Token"] == _SVC_TOKEN


@respx.mock
async def test_keycheck_unreachable_disables_delegation(
    flag_on: None, engine_key: str
) -> None:
    respx.get(f"{_BASE}/module/platform/keycheck").mock(
        side_effect=httpx.ConnectError("no route")
    )
    ok = await pc.verify_key_parity_or_disable()
    assert ok is False
    assert pc.delegating() is False


async def test_keycheck_noop_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, engine_key: str
) -> None:
    monkeypatch.setattr("saebooks.config.settings.platform_base_url", "")
    # No delegation configured → preflight is a no-op and never disables.
    assert await pc.verify_key_parity_or_disable() is False
