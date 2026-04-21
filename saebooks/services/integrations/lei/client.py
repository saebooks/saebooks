"""HTTP client for the GLEIF LEI lookup API.

GLEIF publishes the Legal Entity Identifier (LEI) registry as a free
public JSON:API at ``https://api.gleif.org/api/v1``. We use the single
``/lei-records/{lei}`` endpoint — it returns the full entity record
(legal name, jurisdiction, registration status, status effective date,
BIC, category, addresses).

No authentication required; rate limit is undocumented but generous
(200/min/IP in practice). We don't retry on 5xx — the enrichment is
user-initiated, so the user can click "Lookup" again.

Quirks:

* ``/lei-records/{lei}`` returns ``404`` (not 200 with empty data) for
  unknown LEIs. We surface that as :class:`LeiNotFoundError`.
* The response shape is JSON:API: ``{"data": {"type", "id",
  "attributes": {...}, "relationships": {...}}}``. Parsing strips the
  envelope.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from saebooks.config import Settings

logger = logging.getLogger("saebooks.lei")

# LEI codes are 20 characters: 18 alphanumeric (base-36-ish) + 2 check digits.
_LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


class LeiError(RuntimeError):
    """Base class for all GLEIF-layer errors."""


class LeiNotFoundError(LeiError):
    """Raised when the LEI is well-formed but unknown to GLEIF."""


def _normalise_lei(lei: str) -> str:
    """Strip whitespace + uppercase — GLEIF only accepts upper-case LEIs."""
    return "".join(ch for ch in lei if not ch.isspace()).upper()


async def lookup_lei_raw(
    lei: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the raw GLEIF envelope for ``lei``.

    Returns the ``data`` sub-object (JSON:API envelope stripped).
    Raises :class:`LeiNotFoundError` on 404, :class:`LeiError` on any
    other failure.
    """
    clean = _normalise_lei(lei)
    if not _LEI_PATTERN.match(clean):
        raise LeiError(f"LEI must be 20 alphanumeric chars, got {lei!r}")

    url = f"{settings.lei_api_base.rstrip('/')}/lei-records/{clean}"
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(url)
    finally:
        if owned_client:
            await client.aclose()

    if response.status_code == 404:
        raise LeiNotFoundError(f"LEI {clean} not found in GLEIF registry")
    if response.status_code != 200:
        raise LeiError(
            f"GLEIF returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise LeiError(f"GLEIF response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "data" not in payload:
        raise LeiError(
            "GLEIF response missing 'data' envelope: "
            f"{type(payload).__name__}"
        )
    data = payload["data"]
    if not isinstance(data, dict):
        raise LeiError(
            f"GLEIF 'data' was not a JSON object: {type(data).__name__}"
        )

    logger.debug("GLEIF lookup ok: lei=%s", clean)
    return data
