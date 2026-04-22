"""HTTPS reachability check against the ATO SBR authentication gateway.

This is the smoke-test button behind the onboarding wizard. We
deliberately don't implement the full SBR SOAP + XML-DSig stack here
— that's Batch JJ work. At II.5 onboarding time we only need to
confirm:

* the admin's network can reach ATO over TLS,
* the keystore file loads + password decrypts locally (covered by
  ``services.ato_sbr.keystore`` — the caller runs that before this),
* the base URL for the selected environment resolves + returns a
  live HTTPS response.

We issue a ``GET /`` — ATO's softwareauthorisations gateway returns
a real response on the bare path (typically a 200 HTML login page or
a 403 depending on env). Anything in the 200/3xx/4xx band counts as
"reachable"; only connection errors and 5xx count as fail.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from saebooks.config import Settings

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass(frozen=True)
class PingResult:
    ok: bool
    status_code: int | None
    url: str
    detail: str


def _base_url_for(environment: str, *, settings: Settings) -> str:
    if environment == "production":
        return settings.ato_sbr_prod_base
    return settings.ato_sbr_evte_base


async def ping_environment(
    environment: str, *, settings: Settings
) -> PingResult:
    """Return a ``PingResult`` describing one GET round-trip.

    Anything that completes the TLS handshake + receives an HTTP
    response < 500 is considered ``ok``. A 5xx is reported verbatim
    but with ``ok=False`` — the admin can see it's the ATO side, not
    ours.
    """
    url = _base_url_for(environment, settings=settings)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return PingResult(
                ok=False,
                status_code=None,
                url=url,
                detail=f"{type(exc).__name__}: {exc}",
            )
    ok = 200 <= response.status_code < 500
    return PingResult(
        ok=ok,
        status_code=response.status_code,
        url=url,
        detail=f"HTTP {response.status_code}",
    )
