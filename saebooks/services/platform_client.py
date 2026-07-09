"""Engine-side HTTP client for the platform / identity module (#32 wave 1).

The engine's billing-webhook and signup / magic-link route handlers call into
here when ``settings.platform_base_url`` is set. It is the render / comms /
capture facade pattern applied to the platform surface: the call goes over HTTP
to a sibling ``platform-web`` container that runs the SAME code against the SAME
database, and the response is mirrored back to the caller.

Unlike the capture / pre-accounting clients, the platform surface is NOT
tenant-scoped — the moved flows (signup, magic-link, the Stripe webhook) run
under the OWNER-role session factory (``LoginSessionLocal``) and resolve /
mint the tenant themselves from the payload. So there is no ``X-Tenant-Id`` /
``X-Company-Id`` context to forward: the module gate is the shared
``X-Platform-Token`` secret only, plus whatever request-authenticity header the
moved flow carries in its own body / headers (``Stripe-Signature`` for the
webhook — the module re-verifies it in-process).

Contract per call
-----------------
``{platform_base_url}/module/platform/{path}``

Headers::

    X-Platform-Token: {platform_service_token}   (when non-empty)
    <forwarded request-authenticity headers, e.g. Stripe-Signature>

Errors
------
* Transport failure / timeout raise ``PlatformServiceError`` so a misconfigured
  flag fails loud rather than silently returning wrong data.
* HTTP status codes and JSON bodies from the module are mirrored back to the
  original caller (the module already speaks the engine's error contract), so
  a 400 / 409 / 410 / 422 / 503 the module returns is the status the API client
  sees — identical to the in-process path.
"""
from __future__ import annotations

import datetime
import decimal
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger("saebooks.platform")

_TIMEOUT_SECONDS = 30.0  # signup / webhook round-trips are DB + outbound email
_BASE_PATH = "/module/platform"

# Runtime kill-switch flipped by the engine-startup key-parity preflight
# (``verify_key_parity_or_disable``). Wave-2 moved the login / webauthn /
# principal-login CEREMONIES into the module, and those routes MINT the JWT
# that the engine's require_bearer / jwt-decode path then VERIFIES. If the
# module and engine hold different ``SAEBOOKS_SECRET_KEY`` values every minted
# token would 401 at the engine — a silent auth outage. The preflight catches
# that at boot and DISABLES delegation (falls back to the in-process path,
# where the same process both mints and verifies, so parity is automatic).
# Fail-open to in-process, never fail-broken.
_delegation_disabled = False


class PlatformServiceError(RuntimeError):
    """The platform module was unreachable or answered unexpectedly."""


def disable_delegation() -> None:
    """Force ``delegating()`` False for the process lifetime.

    Called by the key-parity preflight when the module cannot be proven to
    mint engine-verifiable JWTs. Deliberately one-way: a failed preflight means
    the safe posture is in-process identity for the whole run, not a flapping
    half-delegated state.
    """
    global _delegation_disabled
    _delegation_disabled = True


def _reset_delegation_for_tests() -> None:
    """Testing hook — clear the preflight kill-switch."""
    global _delegation_disabled
    _delegation_disabled = False


def delegating() -> bool:
    """True when the engine should delegate platform work to the module."""
    if _delegation_disabled:
        return False
    from saebooks.config import settings

    return bool(settings.platform_base_url.strip())


def jsonable(value: Any) -> Any:
    """Recursively coerce UUID / date / datetime / Decimal into JSON scalars."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    from saebooks.config import settings

    headers: dict[str, str] = {}
    token = settings.platform_service_token.strip()
    if token:
        headers["X-Platform-Token"] = token
    if extra:
        # Skip None-valued forwarded headers (a missing Stripe-Signature must
        # arrive at the module as ABSENT, so the module's own presence check
        # returns the same 400 the in-process path would).
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers


def _url(path: str) -> str:
    from saebooks.config import settings

    base = settings.platform_base_url.rstrip("/")
    return f"{base}{_BASE_PATH}/{path.lstrip('/')}"


async def post_json(
    path: str,
    payload: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a JSON body to the module and return the raw response."""
    url = _url(path)
    headers = _headers(extra_headers)
    headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(url, json=jsonable(payload), headers=headers)
    except httpx.TimeoutException as exc:
        raise PlatformServiceError(
            f"Timeout waiting for platform module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise PlatformServiceError(
            f"Cannot reach platform module at {url}: {exc}"
        ) from exc


async def post_raw(
    path: str,
    content: bytes,
    *,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a raw (already-serialised) body to the module — used to forward a
    request body verbatim (the Stripe webhook, whose signature is computed over
    the exact bytes)."""
    url = _url(path)
    headers = _headers(extra_headers)
    headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(url, content=content, headers=headers)
    except httpx.TimeoutException as exc:
        raise PlatformServiceError(
            f"Timeout waiting for platform module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise PlatformServiceError(
            f"Cannot reach platform module at {url}: {exc}"
        ) from exc


def json_body(resp: httpx.Response, url_hint: str = "") -> Any:
    """Parse a response body as JSON, tolerating an empty body (→ None)."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise PlatformServiceError(
            f"platform module returned non-JSON body"
            f"{(' for ' + url_hint) if url_hint else ''}: {resp.text[:200]}"
        ) from exc


def ensure_ok(resp: httpx.Response, path: str) -> Any:
    """Return the JSON body for a 2xx response, else raise transport error."""
    if resp.status_code // 100 != 2:
        raise PlatformServiceError(
            f"platform module {path} returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    return json_body(resp, path)


# --------------------------------------------------------------------------- #
# SAEBOOKS_SECRET_KEY parity preflight (#32 wave 2)                            #
# --------------------------------------------------------------------------- #
# The module mints JWTs (login / webauthn-assert / principal-login); the engine
# verifies them (require_bearer / jwt decode stay engine-side). Both sign with
# HS256 over ``SAEBOOKS_SECRET_KEY``. The preflight below asks the module to
# mint a probe JWT and verifies it with the ENGINE's own key: a signature match
# proves the two containers share the secret. On any doubt we disable
# delegation and run identity in-process (where mint + verify share one key).


async def _fetch_keycheck_token() -> str | None:
    """GET the module's token-gated keycheck probe; return its minted JWT."""
    from saebooks.config import settings

    base = settings.platform_base_url.rstrip("/")
    url = f"{base}{_BASE_PATH}/keycheck"
    headers = _headers()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise PlatformServiceError(
            f"Timeout on platform keycheck at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise PlatformServiceError(
            f"Cannot reach platform module for keycheck at {url}: {exc}"
        ) from exc
    if resp.status_code // 100 != 2:
        raise PlatformServiceError(
            f"platform keycheck returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = json_body(resp, "keycheck") or {}
    token = body.get("token") if isinstance(body, dict) else None
    return token if isinstance(token, str) and token else None


async def _keycheck_matches() -> bool:
    """True iff the module-minted probe JWT verifies under the engine key."""
    from saebooks.services.jwt_tokens import JWTError, decode_access_token

    token = await _fetch_keycheck_token()
    if not token:
        return False
    try:
        decode_access_token(token)
    except JWTError:
        return False
    return True


async def verify_key_parity_or_disable() -> bool:
    """Engine-startup preflight — returns True if delegation stays enabled.

    Only runs when delegation is configured. On a signature mismatch OR an
    unreachable module it DISABLES delegation (fall back in-process) and logs a
    loud ERROR — it never crashes the engine and never leaves the engine in a
    state where module-minted tokens would silently 401.
    """
    if not delegating():
        return False
    try:
        ok = await _keycheck_matches()
    except PlatformServiceError as exc:
        disable_delegation()
        logger.error(
            "platform keycheck: module unreachable at startup (%s); delegation "
            "DISABLED — identity ceremonies run in-process this run.",
            exc,
        )
        return False
    if not ok:
        disable_delegation()
        logger.error(
            "platform keycheck MISMATCH: JWTs minted by the platform module do "
            "NOT verify under the engine SAEBOOKS_SECRET_KEY. Delegation "
            "DISABLED — identity ceremonies run in-process. Fix: set the SAME "
            "SAEBOOKS_SECRET_KEY in the engine and platform-web containers.",
        )
        return False
    logger.info(
        "platform keycheck OK: engine/module SAEBOOKS_SECRET_KEY parity "
        "confirmed; identity delegation ENABLED."
    )
    return True
