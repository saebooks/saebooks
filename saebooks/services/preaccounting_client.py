"""Engine-side HTTP client for the pre-accounting module (#32 step 4).

The engine's ``services.{quotes,purchase_orders,time_entries}`` public
functions call into here when ``settings.preaccounting_base_url`` is set.
It is the render/comms facade pattern applied to a *stateful* module: the
call goes over HTTP to a sibling ``preaccounting`` container that runs the
SAME service code against the SAME database, and the JSON response is
reconstructed back into the exact return shape the caller expects (an ``Out``
pydantic instance, which every router re-validates via ``Out.model_validate``
with ``from_attributes=True`` — so it round-trips byte-for-byte).

Contract per call
-----------------
``POST {preaccounting_base_url}/module/preaccounting/{path}``

Headers::

    Content-Type: application/json
    X-PreAccounting-Token: {preaccounting_service_token}   (when non-empty)
    X-Tenant-Id: {tenant_id}                               (RLS context)
    X-Company-Id: {company_id}                             (when known)

The tenant/company headers carry the RLS context the module needs to bind
``app.current_tenant`` — the module has no JWT of its own.

Errors
------
* Transport failure / timeout / non-2xx (other than the mapped 409) raise
  ``PreAccountingServiceError`` so a misconfigured flag fails loud rather
  than silently returning wrong data.
* A 409 with a ``{"detail","current"}`` body is a version conflict; the
  caller maps it to its own ``VersionConflict`` (see ``raise_conflict``).
* A 4xx with a ``{"code"/"message"}`` domain-error body is re-raised as the
  caller's domain error (``QuoteError`` / ``PurchaseOrderError`` /
  ``TimeEntryError``) — the caller passes its exception class.

Runtime circuit breaker (M2 wave 2a, P0a)
------------------------------------------
``post()`` reports its outcome to a module-level ``CircuitBreaker``
(``_breaker``): a transport failure (timeout / ``httpx.RequestError``)
records a breaker failure; any response actually received (including a
non-2xx one, e.g. the 409 version-conflict / 422 domain-error shapes
``_check()`` maps above) records a breaker success — only *unreachability*
should trip the breaker, not ordinary application-level errors the module
deliberately returned. ``delegating()`` consults
``_breaker.allow_request()`` in addition to the config flag: once the
breaker trips OPEN, ``delegating()`` returns False for
``_BREAKER_COOLDOWN_SECONDS`` and every one of this module's ~20 call sites
(``quotes.py`` / ``purchase_orders.py`` / ``time_entries.py``, each already
shaped ``if delegating(): ... else: <in-process>``) falls back to
in-process transparently, with no network attempt, until a single
half-open probe succeeds.
"""
from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

import httpx

from saebooks.services.circuit_breaker import CircuitBreaker, DelegatedServiceError

_TIMEOUT_SECONDS = 30.0
_BASE_PATH = "/module/preaccounting"

_BREAKER_FAILURE_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 30.0

_breaker = CircuitBreaker(
    "preaccounting",
    failure_threshold=_BREAKER_FAILURE_THRESHOLD,
    cooldown_seconds=_BREAKER_COOLDOWN_SECONDS,
)


class PreAccountingServiceError(DelegatedServiceError):
    """The pre-accounting module was unreachable or answered unexpectedly."""

    module = "preaccounting"


def _reset_breaker_for_tests() -> None:
    """Testing hook — force the runtime breaker back to CLOSED."""
    _breaker.reset()


def delegating() -> bool:
    """True when the engine should delegate pre-accounting work to the module.

    False when the flag is unset, OR when the runtime breaker is OPEN
    (module unreachable) — either way the caller's existing in-process
    fallback runs.
    """
    from saebooks.config import settings

    if not settings.preaccounting_base_url.strip():
        return False
    return _breaker.allow_request()


def jsonable(value: Any) -> Any:
    """Recursively coerce UUID / date / datetime / Decimal into JSON scalars.

    Mirrors what ``model_dump(mode="json")`` would do but works on the raw
    kwargs dicts (which may contain ``list[dict]`` line payloads built by the
    routers) without needing a schema.
    """
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


def _headers(
    tenant_id: uuid.UUID | str | None, company_id: uuid.UUID | str | None
) -> dict[str, str]:
    from saebooks.config import settings

    headers = {"Content-Type": "application/json"}
    token = settings.preaccounting_service_token.strip()
    if token:
        headers["X-PreAccounting-Token"] = token
    if tenant_id is not None:
        headers["X-Tenant-Id"] = str(tenant_id)
    if company_id is not None:
        headers["X-Company-Id"] = str(company_id)
    return headers


async def post(
    path: str,
    payload: dict[str, Any],
    *,
    tenant_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None = None,
) -> httpx.Response:
    """POST to the module and return the raw response (no status check).

    Callers inspect the status themselves so they can map 409/4xx bodies to
    their own exception types before treating anything else as a transport
    failure.
    """
    from saebooks.config import settings

    base = settings.preaccounting_base_url.rstrip("/")
    url = f"{base}{_BASE_PATH}/{path.lstrip('/')}"
    body = jsonable(payload)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body, headers=_headers(tenant_id, company_id))
    except httpx.TimeoutException as exc:
        _breaker.record_failure()
        raise PreAccountingServiceError(
            f"Timeout waiting for pre-accounting module at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        _breaker.record_failure()
        raise PreAccountingServiceError(
            f"Cannot reach pre-accounting module at {url}: {exc}"
        ) from exc
    _breaker.record_success()
    return resp


def json_body(resp: httpx.Response, url_hint: str = "") -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise PreAccountingServiceError(
            f"pre-accounting module returned non-JSON body{(' for ' + url_hint) if url_hint else ''}: "
            f"{resp.text[:200]}"
        ) from exc


def ensure_ok(resp: httpx.Response, path: str) -> Any:
    """Return the JSON body for a 2xx response, else raise transport error."""
    if resp.status_code // 100 != 2:
        raise PreAccountingServiceError(
            f"pre-accounting module {path} returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    return json_body(resp, path)
