"""LV lodgement adapter — VID EDS (shaped, live-gated).

LV jurisdiction module. The adapter validates targets and reads
credential config, but there is deliberately NO live transport: the
Latvian rail is VID's Electronic Declaration System (EDS —
eds.vid.gov.lv), which exposes both the interactive portal and a
machine interface; building that client is a later phase (credentials
PARKED — no EDS API credential set is provisioned). Every
network-needing call raises :class:`LVLiveCredentialsMissing` **before
any socket opens** — the exact ``ee.py``/``nz.py``/``uk.py`` fail-loud
pattern.

Targets
-------

- ``pvn`` — the PVN declaration + its PVN1 (domestic/EU transaction
  listing) and PVN2 (EU supplies listing) appendices, filed together
  via EDS by the 20th of the month following the taxation period
  (payment by the 23rd).
- ``employer_report`` — darba devēja ziņojums (the monthly employer
  report: per-employee VSAOI object, contributions and withheld IIN),
  due the 17th of the following month.
- ``uin`` — the UIN (corporate income tax) declaration, filed only for
  months with a taxable object except the financial year's final month
  (always filed), by the 20th of the following month.
- ``annual_income_return`` — deliberately NOT accepted: the employee's
  gada ienākumu deklarācija (where the 33%/3% IIN bands settle) is the
  individual's own filing, not an employer/company lodgement.

Route validation is real (an unknown target is a caller bug and raises
``ValueError`` immediately, matching the AU adapter's ``UnknownRoute``
posture) — only the transport is gated. ``NotImplementedError`` fires
if a complete credential set is ever supplied before the EDS client
exists: a configured-but-unbuilt transport must not look like a
credential problem (the NZ adapter's distinction).
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from saebooks.services.lodgement.exceptions import LVLiveCredentialsMissing

#: Lodgeable targets (see module docstring).
KNOWN_TARGETS: frozenset[str] = frozenset({
    "pvn",
    "employer_report",
    "uin",
})


@dataclasses.dataclass(frozen=True)
class EdsConfig:
    """VID EDS credential set.

    Identifiers/paths only — reading credential *contents* is the
    (unbuilt) client's job, so importing this module never touches the
    filesystem (the ``ee.py`` convention).
    """

    base_url: str | None = None
    client_id: str | None = None
    client_secret_path: str | None = None

    def is_complete(self) -> bool:
        return all((self.base_url, self.client_id, self.client_secret_path))


def _eds_from_env() -> EdsConfig:
    return EdsConfig(
        base_url=os.environ.get("LV_EDS_BASE_URL"),
        client_id=os.environ.get("LV_EDS_CLIENT_ID"),
        client_secret_path=os.environ.get("LV_EDS_CLIENT_SECRET_PATH"),
    )


class LVLodgementAdapter:
    """Jurisdiction='LV' adapter — shaped targets, loud live gate."""

    jurisdiction: str = "LV"

    def __init__(self, config: EdsConfig | None = None) -> None:
        self._config = config if config is not None else _eds_from_env()

    @property
    def config(self) -> EdsConfig:
        return self._config

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        """Validate the target, then refuse loudly before any socket.

        ``ValueError`` for an unknown target (caller bug);
        :class:`LVLiveCredentialsMissing` when credentials are absent
        (always, today); ``NotImplementedError`` if a complete
        credential set is ever supplied before the EDS client exists.
        """
        if route == "annual_income_return":
            raise ValueError(
                "LV target 'annual_income_return' is the individual's own "
                "gada ienākumu deklarācija (where the 33%/3% IIN bands "
                "settle) — not a company lodgement this adapter carries."
            )
        if route not in KNOWN_TARGETS:
            raise ValueError(
                f"LV adapter does not support lodge target {route!r}. "
                f"Known targets: {sorted(KNOWN_TARGETS)}"
            )
        if not self._config.is_complete():
            raise LVLiveCredentialsMissing()
        raise NotImplementedError(
            "LV VID EDS credentials are configured but the EDS client is "
            "a later phase — no transport exists to carry this lodgement. "
            "Refusing rather than fabricating a request."
        )


__all__ = ["KNOWN_TARGETS", "EdsConfig", "LVLodgementAdapter"]
