"""NZ lodgement adapter — Inland Revenue gateway services (shaped, live-gated).

NZ jurisdiction module (reshapes the former M1 ``NotImplementedError``
stub per ~/records/saebooks/nz-market-entry-strategy.md §5.11). The
adapter now validates targets and reads credential config, but there is
deliberately NO live transport: the IR rail is gateway-services **SOAP
+ mTLS** (IR-issued X.509 client cert + OAuth2 + JWT) behind the DSP
due-diligence gate, and building that client is a later phase
(credentials PARKED — DSP onboarding + the SPS 21/02 offshore-storage
authorisation have not been started). Every network-needing call raises
:class:`NZLiveCredentialsMissing` **before any socket opens** — the
exact ``ee.py`` fail-loud pattern.

Targets (corrected from the old stub)
-------------------------------------

- ``gst101`` — GST return via the gateway-services SOAP return service
  (Prepop / File / RetrieveStatus / RetrieveReturn /
  RetrieveFilingObligation operations, when built).
- ``employment_information`` — payday-filing EI. **Replaces the stale
  ``ir348``** (the Employer Monthly Schedule was retired when payday
  filing became mandatory; EI is due within 2 working days of each
  payday for electronic filers, electronic mandatory at >= $50k
  PAYE+ESCT). Dual path when built: CSV/XML Express File Transfer file
  generation first, the gateway EI service later.
- ``ir3`` / ``ir4`` / ``ir6`` / ``ir7`` — income-tax returns
  (individual / company / trust / partnership+LTC), staged via the
  income-tax return service.
- ``nzbn`` — counterparty lookup against the **MBIE** NZBN API (a REST
  rail with its own API key — MBIE, not IR).
- ``aim`` (Statement of Activity) — a later target once AIM provider
  approval exists; deliberately NOT accepted yet (AIM is out of scope).

Route validation is real (an unknown target is a caller bug and raises
``ValueError`` immediately, matching the AU adapter's ``UnknownRoute``
posture) — only the transport is gated.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from saebooks.services.lodgement.exceptions import NZLiveCredentialsMissing

#: Lodgeable targets (see module docstring). ``nzbn`` is a lookup, not a
#: lodgement, and goes through :meth:`NZLodgementAdapter.lookup_nzbn`.
KNOWN_TARGETS: frozenset[str] = frozenset({
    "gst101",
    "employment_information",
    "ir3",
    "ir4",
    "ir6",
    "ir7",
})


@dataclasses.dataclass(frozen=True)
class IrGatewayConfig:
    """IR gateway-services credential set (SOAP + mTLS + OAuth2).

    Paths/identifiers only — reading cert *contents* is the (unbuilt)
    client's job, so importing this module never touches the filesystem
    (the ``ee.py`` convention).
    """

    client_cert_path: str | None = None
    client_key_path: str | None = None
    gateway_base_url: str | None = None
    oauth_client_id: str | None = None

    def is_complete(self) -> bool:
        return all(
            (
                self.client_cert_path,
                self.client_key_path,
                self.gateway_base_url,
                self.oauth_client_id,
            )
        )


def _gateway_from_env() -> IrGatewayConfig:
    return IrGatewayConfig(
        client_cert_path=os.environ.get("NZ_IRD_GATEWAY_CLIENT_CERT"),
        client_key_path=os.environ.get("NZ_IRD_GATEWAY_CLIENT_KEY"),
        gateway_base_url=os.environ.get("NZ_IRD_GATEWAY_BASE_URL"),
        oauth_client_id=os.environ.get("NZ_IRD_GATEWAY_OAUTH_CLIENT_ID"),
    )


class NZLodgementAdapter:
    """Jurisdiction='NZ' adapter — shaped targets, loud live gate."""

    jurisdiction: str = "NZ"

    def __init__(self, config: IrGatewayConfig | None = None) -> None:
        self._config = config if config is not None else _gateway_from_env()

    @property
    def config(self) -> IrGatewayConfig:
        return self._config

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        """Validate the target, then refuse loudly before any socket.

        ``ValueError`` for an unknown target (caller bug — includes the
        stale ``ir348``, whose message points at the payday-filing
        replacement); :class:`NZLiveCredentialsMissing` when credentials
        are absent (always, today); ``NotImplementedError`` if a
        complete credential set is ever supplied before the SOAP
        gateway client exists — a configured-but-unbuilt transport must
        not look like a credential problem.
        """
        if route == "ir348":
            raise ValueError(
                "NZ target 'ir348' is stale — the Employer Monthly "
                "Schedule was replaced by payday filing. Use "
                "'employment_information' (EI within 2 working days of "
                "payday for electronic filers)."
            )
        if route not in KNOWN_TARGETS:
            raise ValueError(
                f"NZ adapter does not support lodge target {route!r}. "
                f"Known targets: {sorted(KNOWN_TARGETS)}"
            )
        if not self._config.is_complete():
            raise NZLiveCredentialsMissing()
        raise NotImplementedError(
            "NZ IR gateway credentials are configured but the SOAP + "
            "mTLS gateway client is a later phase — no transport exists "
            "to carry this lodgement. Refusing rather than fabricating "
            "a request."
        )

    async def lookup_nzbn(self, nzbn: str) -> dict[str, Any]:
        """NZBN counterparty lookup — MBIE API (separate rail from IR).

        Format is checked locally first (a malformed NZBN is a caller
        bug, not a credential problem); the network call is gated on the
        MBIE API key, which is not provisioned.
        """
        from saebooks.jurisdictions.nz.identifiers import validate_nzbn

        if not validate_nzbn(nzbn):
            raise ValueError(
                f"{nzbn!r} is not a structurally valid NZBN (13 digits, "
                "GS1 GLN check digit)."
            )
        if not os.environ.get("MBIE_NZBN_API_KEY"):
            raise NZLiveCredentialsMissing(
                "NZBN lookup is not configured — no MBIE_NZBN_API_KEY "
                "present (free key via the NZBN portal). Refusing to "
                "open a connection."
            )
        raise NotImplementedError(
            "MBIE NZBN API key is configured but the REST client is a "
            "later phase — no transport exists for this lookup."
        )
