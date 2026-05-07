"""HTTP client for the Australian Business Register JSON API.

The ABR exposes a handful of JSON endpoints at
``https://abr.business.gov.au/json/``. We use ``AbnDetails.aspx`` — the
"search by ABN, get full detail" endpoint — for the Contact-enrich
flow. A single GET returns one envelope covering legal name, trading
names, entity type, GST status, and the main business address.

Auth is a single query-string parameter: ``guid=<api-key>``. Keys are
issued free by abr.business.gov.au; we stash the key in
``settings.abr_api_guid``.

Quirks of the API that callers need to know:

* The response is JSON wrapped in a ``callback(...)`` JSONP wrapper.
  We strip it before decoding.
* Unknown/invalid ABNs return HTTP 200 with
  ``{"Abn":"","AbnStatus":"","Message":"No record found"}``. We surface
  that as a :class:`AbrError` so callers can react uniformly.
* Rate-limit / 5xx is surfaced as :class:`AbrError` too. There is no
  published rate-limit policy; we don't retry.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from saebooks.config import Settings

logger = logging.getLogger("saebooks.abr")


class AbrError(RuntimeError):
    """Base class for all ABR-layer errors."""


class AbrNotConfiguredError(AbrError):
    """Raised when ABR_API_GUID isn't set but a lookup was attempted."""


def _strip_jsonp(raw: str) -> str:
    """Strip the ``callback(...)`` wrapper ABR returns by default.

    The ``AbnDetails.aspx`` endpoint actually returns ``callback(...)``
    when called without ``callback=`` and a bare JSON object when called
    with ``callback=``. We always pass ``callback=callback`` to get a
    deterministic wrapper, then strip it here.
    """
    raw = raw.strip()
    if raw.startswith("callback(") and raw.endswith(")"):
        return raw[len("callback("):-1]
    return raw


def _normalise_abn(abn: str) -> str:
    """Strip whitespace so '12 345 678 901' -> '12345678901'."""
    return "".join(ch for ch in abn if ch.isdigit())


async def lookup_abn_raw(
    abn: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the raw ABR envelope for ``abn``.

    Returns the parsed JSON dict verbatim; parsing into the domain
    shape is ``enrich.parse_abr_response``'s job. Raises
    :class:`AbrNotConfiguredError` when the API guid isn't set.
    """
    if not settings.abr_api_guid:
        raise AbrNotConfiguredError(
            "ABR_API_GUID is not configured; cannot reach the ABR."
        )

    clean = _normalise_abn(abn)
    if len(clean) != 11:
        raise AbrError(f"ABN must be 11 digits, got {len(clean)}: {abn!r}")

    url = f"{settings.abr_api_base.rstrip('/')}/AbnDetails.aspx"
    params = {
        "abn": clean,
        "guid": settings.abr_api_guid,
        "callback": "callback",
    }

    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(url, params=params)
    finally:
        if owned_client:
            await client.aclose()

    if response.status_code != 200:
        raise AbrError(
            f"ABR returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = json.loads(_strip_jsonp(response.text))
    except json.JSONDecodeError as exc:
        raise AbrError(f"ABR response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise AbrError(f"ABR response was not a JSON object: {type(payload)}")

    if payload.get("Message"):
        # ABR only sets Message on error ("No record found", "Invalid ABN")
        raise AbrError(f"ABR: {payload['Message']}")

    logger.debug("ABR lookup ok: abn=%s", clean)
    return payload
