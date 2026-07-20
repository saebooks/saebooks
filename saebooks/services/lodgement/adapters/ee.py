"""EE lodgement adapter — X-Road / e-MTA KMD3 (M3, offline, live-creds-gated).

Turns the former ``NotImplementedError`` stub into the real adapter. It delegates
the async ``submit → poll → confirm`` lifecycle to :class:`EELodgementClient`
(``ee_client.py``) and parses feedback via ``ee_messages.py``.

Lifecycle, not fire-and-forget
------------------------------

Unlike the AU relay (one POST, one ``LodgementResult``), the EE rail is a genuine
state machine over X-Road. So ``EELodgementAdapter`` does NOT implement the
``LodgementService`` ABC and does NOT return a ``LodgementResult`` — ``lodge()``
returns a :class:`SubmitReceipt` (the ``feedbackReportId`` UUID to poll on), and
the adapter exposes ``poll`` / ``confirm`` for the rest of the lifecycle. Callers
persist the UUID + state (Option A columns on ``tax_returns``) between calls.

The live gate
-------------

The default-constructed adapter reads mTLS/security-server config from the
environment (``_mtls_from_env``). None is provisioned (X-Road membership branch
17151236 unwired), so the config is incomplete and every network-needing call
raises ``EELiveCredentialsMissing`` before any socket opens. Tests inject an
``EELodgementClient`` backed by an ``httpx.MockTransport`` to exercise the whole
state machine offline with zero network egress.

Routes
------

- ``vat_kmd`` / ``kmd3`` / ``tsd`` → ``submit`` (file a declaration payload).
- ``regcode`` lookup via ``lookup_regcode`` (e-Business Register) — gated stub
  until that X-Road service is wired.
"""
from __future__ import annotations

import os
from typing import Any

from saebooks.services.lodgement.adapters.ee_client import (
    EELodgementClient,
    MtlsConfig,
    PollResult,
    SubmitReceipt,
)
from saebooks.services.lodgement.exceptions import EELiveCredentialsMissing

# Routes that map to a KMD3 submit. Kept permissive — the payload is built
# upstream (Module 4 / TSD / KMD serializers); this adapter is payload-agnostic.
SUBMIT_ROUTES: frozenset[str] = frozenset({"vat_kmd", "kmd3", "kmd", "tsd"})


def _mtls_from_env() -> MtlsConfig | None:
    """Assemble an :class:`MtlsConfig` from the environment, or None.

    Returns None (→ loud live gate) unless a COMPLETE set of X-Road creds is
    present. Deliberately does not read cert *contents* — only paths — so
    importing this module never touches the filesystem. No creds are set today.
    """
    cfg = MtlsConfig(
        client_cert_path=os.environ.get("EE_XROAD_CLIENT_CERT"),
        client_key_path=os.environ.get("EE_XROAD_CLIENT_KEY"),
        security_server_url=os.environ.get("EE_XROAD_SECURITY_SERVER"),
        xroad_client_header=os.environ.get("EE_XROAD_CLIENT_HEADER"),
        ca_bundle_path=os.environ.get("EE_XROAD_CA_BUNDLE"),
    )
    return cfg if cfg.is_complete() else None


class EELodgementAdapter:
    """Jurisdiction='EE' adapter over the X-Road KMD3 client."""

    jurisdiction: str = "EE"

    def __init__(
        self,
        client: EELodgementClient | None = None,
        *,
        represented_party: str | None = None,
        environment: str | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            # Default (production) path: build from env. No creds provisioned →
            # mtls=None → EELiveCredentialsMissing on first network-needing call.
            self._client = EELodgementClient(
                mtls=_mtls_from_env(),
                represented_party=represented_party
                or os.environ.get("EE_XROAD_REPRESENTED_PARTY"),
                environment=environment
                or os.environ.get("EE_XROAD_ENVIRONMENT", "ee-test"),
            )

    @property
    def client(self) -> EELodgementClient:
        return self._client

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> SubmitReceipt:
        """Submit a declaration payload on the KMD3 rail.

        Returns a :class:`SubmitReceipt` (the poll handle), NOT a
        ``LodgementResult`` — the EE lifecycle is submit→poll→confirm. Raises
        ``EELiveCredentialsMissing`` (before any network) when creds are absent.
        """
        if route not in SUBMIT_ROUTES:
            raise ValueError(
                f"EE adapter does not support lodge route {route!r}. "
                f"Known submit routes: {sorted(SUBMIT_ROUTES)}"
            )
        section = str(metadata.get("section", "EE0203001")) if metadata else "EE0203001"
        return await self._client.submit(
            envelope, section=section, idempotency_id=idempotency_id
        )

    async def poll(self, request_id: str) -> PollResult:
        """Fetch the feedback report for a previously-submitted UUID."""
        return await self._client.poll(request_id)

    async def confirm(self, request_id: str) -> Any:
        """Confirm an accepted return — gated stub (shape UNVERIFIED)."""
        return await self._client.confirm(request_id)

    async def lookup_regcode(self, regcode: str) -> dict[str, Any]:
        """e-Business Register counterparty lookup — gated on its X-Road service.

        The registry-lookup X-Road service is a separate rail from KMD3 and is
        not wired. Raise the same loud live gate rather than fabricate a lookup.
        """
        raise EELiveCredentialsMissing(
            "EE e-Business Register lookup is not wired (separate X-Road "
            "service, no creds provisioned)."
        )
