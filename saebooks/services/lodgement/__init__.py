"""Lodgement service — relay envelopes through SAE Engineering's Machine Credential.

Tier-2 (Pro / Enterprise) feature. SAE Books on the customer's box
does NOT hold its own ATO Machine Credential PFX or SISS bank-feed
credentials. Instead it POSTs already-built envelopes to
``lodge.saebooks.com.au`` (the lodge-server, Build #7), which signs
them with SAE Engineering's shared MC, talks ebMS3 to the ATO SBR
endpoint, and returns the receipt.

This is the same architectural pattern as the bank-feeds relay:
SAE Engineering bears the operational cost of the regulator
relationship, and customer instances stay simple. See
``saebooks-infrastructure-plan`` (memory) and the contract document
at the commercial lodge-server API contract.

Public surface
--------------

* ``LodgementService`` — abstract base; one async method per route.
* ``LodgementResult`` / ``LodgementStatus`` — return shape.
* ``RemoteLodgementService`` — concrete implementation that talks
  HTTP to lodge.saebooks.com.au.
* ``NullLodgementService`` — community / offline edition fallback;
  every method raises ``LodgementUnsupportedEdition``.
* ``get_lodgement_service()`` — factory; returns the right impl
  based on ``LicenseService.has_feature("ato_sbr")``.

Swapping the implementation
---------------------------

Tests should call ``get_lodgement_service`` after monkey-patching
``LicenseService.has_feature``, or instantiate ``RemoteLodgementService``
directly with an injected ``httpx.AsyncClient`` (constructor accepts
a ``client`` kwarg for that). For an offline/airgapped run, point
``LODGE_SERVER_URL`` at a local stub server.

Stub-mode handling
------------------

While the lodge-server is in stub mode (Build #7 ships every route
returning 501 with a deterministic body), ``RemoteLodgementService``
maps that 501 to ``LodgementStatus.STUB`` rather than raising. The
``stub_receipt_id`` is surfaced as ``ato_receipt_id`` so end-to-end
tests can persist a placeholder. This lets the customer-side UI be
built and exercised before SBR Machine Credential onboarding lands.
"""
from __future__ import annotations

from saebooks.services.lodgement.adapters.au import UnknownRoute
from saebooks.services.lodgement.base import (
    LodgementResult,
    LodgementService,
    LodgementStatus,
)
from saebooks.services.lodgement.exceptions import (
    LodgementAuthError,
    LodgementEditionError,
    LodgementError,
    LodgementRejected,
    LodgementUnsupportedEdition,
    LodgementUpstreamUnavailable,
    LodgementValidationError,
)
from saebooks.services.lodgement.factory import get_lodgement_service
from saebooks.services.lodgement.null import NullLodgementService
from saebooks.services.lodgement.registry import UnknownJurisdiction, get_adapter
from saebooks.services.lodgement.remote import RemoteLodgementService

__all__ = [
    "LodgementAuthError",
    "LodgementEditionError",
    "LodgementError",
    "LodgementRejected",
    "LodgementResult",
    "LodgementService",
    "LodgementStatus",
    "LodgementUnsupportedEdition",
    "LodgementUpstreamUnavailable",
    "LodgementValidationError",
    "NullLodgementService",
    "RemoteLodgementService",
    "UnknownJurisdiction",
    "UnknownRoute",
    "get_adapter",
    "get_lodgement_service",
]
