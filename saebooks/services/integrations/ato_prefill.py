"""ATO Business Portal — BAS prefill stub.

Placeholder for the future "pull ATO-held data to pre-fill the BAS
worksheet" flow. Lodging a BAS via ATO's SBR ebMS3 endpoint requires
an AUSkey / machine-to-machine certificate, an SBR-approved software
ID, and a live registration — none of which we have.

We ship the scaffold now so:

1. The service surface is discoverable (``services/integrations/ato_prefill.py``
   rather than a forgotten git-stash).
2. Config keys (``ATO_SBR_CERT_PATH``, ``ATO_SBR_CERT_PASSWORD``,
   ``ATO_SBR_SOFTWARE_ID``) can land in a later migration without
   reshuffling the imports tree.
3. The router returns a clean 501 "Not Implemented" so operators see
   the endpoint exists but is deliberately unreachable today.

See Batch KK in the master plan for the live implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class AtoPrefillError(RuntimeError):
    """Base class for ATO-prefill errors."""


class AtoPrefillNotImplementedError(AtoPrefillError):
    """Raised by stub; lifted when Batch KK lands."""


@dataclass(frozen=True)
class BasPrefillResult:
    """Shape the live implementation will return."""

    period_start: date
    period_end: date
    w1_gross_wages: int = 0
    w2_paygw: int = 0
    g1_total_sales: int = 0
    g11_non_capital_purchases: int = 0
    source: str = "stub"


async def prefill_bas(
    *,
    period_start: date,
    period_end: date,
) -> BasPrefillResult:
    """Stub — always raises :class:`AtoPrefillNotImplementedError`.

    Live implementation will call the SBR ebMS3 ``lodgement/report``
    endpoint with a signed request and parse the returned pre-fill
    envelope. Requires:

    * AUSkey / machine-to-machine cert on disk (``ATO_SBR_CERT_PATH``)
    * SBR-registered software ID (``ATO_SBR_SOFTWARE_ID``)
    * ``ATO_SBR_ENABLED=1`` toggle (guard against accidental live fires)
    """
    raise AtoPrefillNotImplementedError(
        "ATO BAS prefill is not implemented yet — see Batch KK in the "
        "master plan. Tracked under FLAG_BAS_ELODGE (proposed, not "
        "wired yet). Until then, populate BAS worksheets manually "
        "from /reports/bas."
    )


__all__ = [
    "AtoPrefillError",
    "AtoPrefillNotImplementedError",
    "BasPrefillResult",
    "prefill_bas",
]
