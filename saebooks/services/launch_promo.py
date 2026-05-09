"""Launch-promo service — first-1000-customers free Pro for 12 months.

Called from the signup flow when ``settings.launch_promo_enabled`` is True.
Calls the license-server ``POST /api/v1/license/issue-launch-promo`` endpoint,
then stamps the signed JWT on the user row so the app starts at Pro tier on
first login.

Design decisions
----------------
* The promo call is best-effort. If the license-server is down or the counter
  is exhausted, signup still completes — the user just gets Community. We log
  the failure clearly so Richard can manually issue a token later if needed.
* Idempotent: if the license-server already has a token for this email
  (e.g. the user signed up, deleted, re-signed-up) the same token is returned.
* ``LAUNCH_PROMO_ENABLED=false`` (default) short-circuits the entire call path
  so there is zero runtime cost when the promo is not active.
"""
from __future__ import annotations

import logging

import httpx

from saebooks.config import settings as _settings

logger = logging.getLogger("saebooks.launch_promo")

# Response shape we care about from the license-server.
_PROMO_FIELDS = frozenset({"token", "edition", "license_id", "expires_at", "promo"})


async def attempt_promo(
    *,
    email: str,
    licensed_to: str,
) -> str | None:
    """Attempt to claim a launch-promo slot and return the signed JWT.

    Returns the JWT string on success, or None when:
    - the promo flag is off,
    - the counter is exhausted (410 from license-server),
    - or the license-server is unreachable / returns an error.

    Never raises — caller logs whatever we return.
    """
    if not _settings.launch_promo_enabled:
        return None

    url = f"{_settings.license_server_url.rstrip('/')}/api/v1/license/issue-launch-promo"
    payload = {"email": email, "licensed_to": licensed_to}

    try:
        async with httpx.AsyncClient(timeout=_settings.license_server_timeout) as client:
            resp = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        logger.warning(
            "launch_promo: license-server unreachable for %s: %s", email, exc
        )
        return None

    if resp.status_code == 410:
        # Counter exhausted — legitimate end of promo, not an error.
        logger.info("launch_promo: counter exhausted for %s (410)", email)
        return None

    if resp.status_code == 503:
        # Flag was off server-side (race between env flip), or server cold.
        logger.info(
            "launch_promo: license-server returned 503 for %s — promo inactive server-side",
            email,
        )
        return None

    if resp.status_code not in (200, 201):
        logger.warning(
            "launch_promo: unexpected status %d for %s: %s",
            resp.status_code,
            email,
            resp.text[:200],
        )
        return None

    try:
        body = resp.json()
    except Exception:
        logger.warning("launch_promo: could not parse JSON for %s", email)
        return None

    token: str | None = body.get("token")
    if not token:
        logger.warning("launch_promo: no token in response for %s: %s", email, body)
        return None

    logger.info(
        "launch_promo: issued Pro token for %s (slot %s, idempotent=%s)",
        email,
        body.get("promo_slot", "?"),
        body.get("idempotent", "?"),
    )
    return token


async def get_promo_stats() -> dict:
    """Return live stats from the license-server promo-stats endpoint.

    Used by the API stats endpoint which the web layer polls (with 60s
    caching) to populate the signup banner counter. Returns a safe
    fallback dict when the license-server is unreachable.
    """
    if not _settings.launch_promo_enabled:
        return {
            "enabled": False,
            "issued": 0,
            "limit": _settings.launch_promo_limit,
            "remaining": _settings.launch_promo_limit,
        }

    url = f"{_settings.license_server_url.rstrip('/')}/api/v1/license/promo-stats"
    try:
        async with httpx.AsyncClient(timeout=_settings.license_server_timeout) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("launch_promo: could not fetch stats: %s", exc)
        return {
            "enabled": _settings.launch_promo_enabled,
            "issued": 0,
            "limit": _settings.launch_promo_limit,
            "remaining": _settings.launch_promo_limit,
            "error": "stats_unavailable",
        }
