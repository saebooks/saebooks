"""Per-jurisdiction lodgement adapters.

Each module owns one jurisdiction's relay-route surface. Adapters are
registered with the factory via ``factory.get_adapter(jurisdiction,
route)`` so the API layer doesn't import jurisdiction-specific modules.

AU is the only jurisdiction wired end-to-end at M0 — routes ``stp``,
``bas``, ``tpar``, ``superstream``, ``abr``, ``audit`` map onto the
existing ``RemoteLodgementService`` / ``NullLodgementService`` chain
(licence-gated, see ``adapters.au``).

NZ/UK/EE adapters are stubs that raise ``NotImplementedError`` keyed
to M1/M2/M3.
"""
from __future__ import annotations

from saebooks.services.lodgement.adapters.au import AULodgementAdapter

__all__ = ["AULodgementAdapter"]
