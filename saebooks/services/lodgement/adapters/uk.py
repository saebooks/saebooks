"""UK lodgement adapter — HMRC MTD + Companies House (offline,
live-creds-gated).

Reshaped from the M2 ``NotImplementedError`` stub into the UK
jurisdiction module's adapter surface: named targets, payload-agnostic
``lodge``, and a **fail-loud live gate** (the ``adapters/ee.py``
pattern) — every network-needing call raises
:class:`UKLiveCredentialsMissing` BEFORE any socket is opened.

Targets (build order per the UK market-entry strategy §4/§5)
------------------------------------------------------------
- ``vat100``          — MTD VAT return (REST/JSON, the FIRST rail:
                        smallest surface, engine-native — the 9-box
                        figures come from ``jurisdictions.uk.tax.
                        vat100_report``).
- ``itsa_quarterly``  — MTD Income Tax quarterly update (REST/JSON,
                        cumulative basis; wave 1 mandation Apr 2026).
- ``rti_fps``         — PAYE RTI Full Payment Submission (GovTalk XML
                        over the Transaction Engine — separate PAYE
                        recognition track).
- ``ct600``           — Corporation Tax return (GovTalk XML + iXBRL —
                        later phase; iXBRL is its own build).
- ``companies_house`` — CRN counterparty lookup (REST; SAE already
                        holds an API key) + accounts filing later
                        (XML Gateway, software-only mandate Apr 2028).

Deliberately NOT in this wave (parked, per the module build scope):
NO live transport, NO OAuth 2.0 client (4-hour tokens, single-use
refresh), and **NO fraud-prevention-header middleware** — the
``Gov-Client-*``/``Gov-Vendor-*`` header set is legally mandatory on
the MTD APIs and is a genuine engine-level middleware component; it is
its OWN later phase, not an adapter detail. GovTalk XML serializers
and iXBRL are likewise parked. Until those land, this adapter's only
runtime behaviour is route validation + the loud live gate.
"""
from __future__ import annotations

import os
from typing import Any

from saebooks.services.lodgement.exceptions import UKLiveCredentialsMissing

# Lodgement routes the UK module recognises, in build order. Payloads
# are built upstream (vat100_report / future RTI + CT600 serializers);
# this adapter is payload-agnostic (the EE SUBMIT_ROUTES convention).
KNOWN_ROUTES: frozenset[str] = frozenset({
    "vat100",
    "itsa_quarterly",
    "rti_fps",
    "ct600",
    "companies_house",
})

# Environment variables a LIVE configuration would provide. Read as
# presence-only (never values) — and note that even a complete set does
# not enable transport in this wave; the gate below fires regardless,
# because the OAuth client + fraud-prevention-header middleware do not
# exist yet.
_LIVE_ENV_VARS: tuple[str, ...] = (
    "UK_HMRC_CLIENT_ID",
    "UK_HMRC_CLIENT_SECRET",
    "UK_HMRC_REDIRECT_URI",
)


def _live_env_configured() -> bool:
    return all(os.environ.get(var) for var in _LIVE_ENV_VARS)


class UKLodgementAdapter:
    """Jurisdiction='UK' adapter — offline; loud live gate on every
    network-needing call."""

    jurisdiction: str = "UK"

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        """Submit a return/report payload on a UK rail.

        Validates the route, then raises
        :class:`UKLiveCredentialsMissing` before any network — the UK
        transport (OAuth + fraud-prevention headers, GovTalk for
        rti_fps/ct600) is a later phase. The env-var presence check
        only sharpens the error message; it never opens a socket.
        """
        if route not in KNOWN_ROUTES:
            raise ValueError(
                f"UK adapter does not support lodge route {route!r}. "
                f"Known routes: {sorted(KNOWN_ROUTES)}"
            )
        if _live_env_configured():
            raise UKLiveCredentialsMissing(
                f"UK HMRC credentials are present in the environment, but "
                f"the UK transport is not built in this wave (no OAuth "
                f"client, no Gov-Client-* fraud-prevention-header "
                f"middleware) — refusing to submit {route!r}."
            )
        raise UKLiveCredentialsMissing()

    async def lookup_crn(self, crn: str) -> dict[str, Any]:
        """Companies House CRN counterparty lookup — gated until the
        REST client (API key auth, 600 req/5min) is wired. Format
        pre-validation is ``jurisdictions.uk.identifiers.validate_crn``.
        """
        raise UKLiveCredentialsMissing(
            "Companies House lookup is not wired — the REST client is a "
            "later phase (SAE holds an API key, but no transport exists "
            "in this wave)."
        )
