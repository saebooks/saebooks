"""LT lodgement adapter — VMI EDS / i.MAS / Sodra (offline,
live-creds-gated).

LT jurisdiction module (bolt-on architecture; the ``adapters/uk.py`` /
``adapters/ee.py`` fail-loud shape): named targets, payload-agnostic
``lodge``, and a **loud live gate** — every network-needing call raises
:class:`LTLiveCredentialsMissing` BEFORE any socket is opened.

Targets
-------
- ``fr0600``  — the monthly PVM (VAT) declaration, form FR0600, filed
                through VMI's EDS (deklaravimas.vmi.lt) by the 25th of
                the following month. Figures come from
                ``jurisdictions.lt.tax.fr0600_report`` (the data-driven
                return generator off the LT box seed).
- ``gpm313``  — the monthly employer income-tax declaration (form
                GPM313, the TSD analogue), due the 15th of the
                following month. Payroll figures come from the LT
                payroll engine's role-tagged results.
- ``isaf``    — the monthly i.SAF registers of issued/received VAT
                invoices submitted through i.MAS by the 20th of the
                following month. This is a DATA obligation (a SAF-T
                subset XML register), distinct from the FR0600 return
                itself. NAMED-BUT-LATER: the i.SAF register EXPORTER is
                deliberately not built in this wave — the architectural
                precedent for a register-level data export is the EE
                2027 data-based-KMD exporter
                (``services/lodgement/kmd_2027/``: invoice-level
                generator + serializer + reconciliation against the
                box-level return); an LT i.SAF exporter is that same
                shape pointed at the i.SAF XML schema, a later phase.
- ``cit_annual`` — the annual corporate income tax return (form
                PLN204), due by the 15th day of the 6th month after
                the tax period ends. Named target only; no PLN204
                serializer exists in this wave.

Deliberately NOT in this wave (parked): NO live transport of any kind —
no i.MAS web-service client, no EDS integration, no Sodra EDAS (SAM
report) channel — and no i.SAF/PLN204 serializers. Until those land,
this adapter's only runtime behaviour is route validation + the loud
live gate.
"""
from __future__ import annotations

import os
from typing import Any

from saebooks.services.lodgement.exceptions import LTLiveCredentialsMissing

# Lodgement routes the LT module recognises. Payloads are built
# upstream (fr0600_report / future GPM313, i.SAF and PLN204
# serializers); this adapter is payload-agnostic (the EE SUBMIT_ROUTES
# convention).
KNOWN_ROUTES: frozenset[str] = frozenset({
    "fr0600",
    "gpm313",
    "isaf",
    "cit_annual",
})

# Environment variables a LIVE configuration would provide. Read as
# presence-only (never values) — and note that even a complete set does
# not enable transport in this wave; the gate below fires regardless,
# because no i.MAS/EDS client exists yet.
_LIVE_ENV_VARS: tuple[str, ...] = (
    "LT_IMAS_CLIENT_ID",
    "LT_IMAS_CLIENT_SECRET",
)


def _live_env_configured() -> bool:
    return all(os.environ.get(var) for var in _LIVE_ENV_VARS)


class LTLodgementAdapter:
    """Jurisdiction='LT' adapter — offline; loud live gate on every
    network-needing call."""

    jurisdiction: str = "LT"

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        """Submit a return/report payload on an LT rail.

        Validates the route, then raises
        :class:`LTLiveCredentialsMissing` before any network — the LT
        transport (i.MAS web services, EDS, Sodra EDAS) is a later
        phase. The env-var presence check only sharpens the error
        message; it never opens a socket.
        """
        if route not in KNOWN_ROUTES:
            raise ValueError(
                f"LT adapter does not support lodge route {route!r}. "
                f"Known routes: {sorted(KNOWN_ROUTES)}"
            )
        if _live_env_configured():
            raise LTLiveCredentialsMissing(
                f"LT i.MAS credentials are present in the environment, "
                f"but the LT transport is not built in this wave (no "
                f"i.MAS web-service client, no EDS integration) — "
                f"refusing to submit {route!r}."
            )
        raise LTLiveCredentialsMissing()
