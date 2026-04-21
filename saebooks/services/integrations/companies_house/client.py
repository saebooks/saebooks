"""HTTP client for the Companies House (UK) public-information API.

Companies House exposes company records under
``https://api.company-information.service.gov.uk/company/{number}``.
Authentication is HTTP Basic with the free API key as the username and
an empty password — a CH quirk rather than a bearer token. We keep the
client thin: one ``lookup_company_raw(number)`` that returns the parsed
JSON body, and parsing / normalisation lives in ``enrich.py``.

Quirks
------
* Company numbers are 8-character alphanumeric, zero-padded on the left
  for short numbers (``00000006`` is the Crown Agents Foundation).
  ``_normalise_number`` strips whitespace + uppercases + left-pads to 8.
* CH returns ``404`` (not 200 with empty body) for unknown numbers. We
  surface that as :class:`CompaniesHouseNotFoundError` so the UI can
  show "not found" without sniffing error strings.
* Rate-limit: 600 requests / 5 minutes / key. We don't retry on 5xx —
  the enrichment is user-initiated, so the user can click "Lookup"
  again if the upstream blips.
* The API key is sensitive; never log it. The Basic-auth header is
  computed from settings at call time, never persisted.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from saebooks.config import Settings

logger = logging.getLogger("saebooks.companies_house")

# Company numbers are 8 alphanumeric characters, with an optional short
# form that CH left-pads to 8. The regex below enforces the canonical
# 8-character form after normalisation.
_NUMBER_PATTERN = re.compile(r"^[A-Z0-9]{8}$")
_MAX_PREFIX_LEN = 2  # e.g. "SC" (Scotland), "NI" (Northern Ireland)


class CompaniesHouseError(RuntimeError):
    """Base class for all Companies House-layer errors."""


class CompaniesHouseNotConfiguredError(CompaniesHouseError):
    """Raised when ``settings.ch_api_key`` is empty."""


class CompaniesHouseNotFoundError(CompaniesHouseError):
    """Raised when the company number is well-formed but unknown to CH."""


def _normalise_number(number: str) -> str:
    """Strip whitespace, uppercase, left-zero-pad short numeric forms.

    Accepts ``"6"``, ``"000006"``, ``"SC12345"``, ``"  nI987654  "`` and
    returns the 8-character canonical form.
    """
    clean = "".join(ch for ch in number if not ch.isspace()).upper()
    if not clean:
        raise CompaniesHouseError("Company number must not be empty")

    # Split optional 2-char alpha prefix (e.g. "SC"/"NI") from the
    # numeric body and zero-pad the body out to fill the remainder.
    prefix = ""
    body = clean
    if len(clean) <= 8 and clean[:1].isalpha():
        # Longest real prefix in the CH scheme is 2 chars.
        for plen in range(_MAX_PREFIX_LEN, 0, -1):
            if len(clean) > plen and clean[:plen].isalpha() and clean[plen:].isdigit():
                prefix = clean[:plen]
                body = clean[plen:]
                break
    if prefix:
        body = body.zfill(8 - len(prefix))
    elif clean.isdigit():
        body = clean.zfill(8)
    return f"{prefix}{body}"


async def lookup_company_raw(
    number: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the raw Companies House record for ``number``.

    Raises :class:`CompaniesHouseNotConfiguredError` if no API key is
    set, :class:`CompaniesHouseNotFoundError` on 404, and
    :class:`CompaniesHouseError` on any other upstream failure.
    """
    if not settings.ch_api_key:
        raise CompaniesHouseNotConfiguredError(
            "Companies House API key not configured — set CH_API_KEY"
        )

    clean = _normalise_number(number)
    if not _NUMBER_PATTERN.match(clean):
        raise CompaniesHouseError(
            f"Company number must be 8 alphanumeric chars after normalisation, "
            f"got {number!r} -> {clean!r}"
        )

    url = f"{settings.ch_api_base.rstrip('/')}/company/{clean}"
    owned_client = client is None
    # CH uses Basic auth with API key as username + empty password.
    client = client or httpx.AsyncClient(
        timeout=10.0, auth=(settings.ch_api_key, "")
    )
    try:
        response = await client.get(url)
    finally:
        if owned_client:
            await client.aclose()

    if response.status_code == 404:
        raise CompaniesHouseNotFoundError(
            f"Company {clean} not found in Companies House"
        )
    if response.status_code == 401:
        raise CompaniesHouseError(
            "Companies House rejected the API key (HTTP 401). Check CH_API_KEY."
        )
    if response.status_code == 429:
        raise CompaniesHouseError(
            "Companies House rate-limited the request (HTTP 429). "
            "Wait a few minutes and retry."
        )
    if response.status_code != 200:
        raise CompaniesHouseError(
            f"Companies House returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise CompaniesHouseError(
            f"Companies House response was not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise CompaniesHouseError(
            f"Companies House response was not a JSON object: "
            f"{type(payload).__name__}"
        )

    logger.debug("Companies House lookup ok: number=%s", clean)
    return payload
