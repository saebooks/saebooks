"""Factory — pick Remote vs Null based on the active licence.

Single chokepoint so the gating logic lives in one place and tests
can ``monkeypatch.setattr(LicenseService, "has_feature", ...)``
to flip between impls without touching the resolver cache.

The factory is sync because it doesn't issue I/O — the licence
snapshot is already cached. The FastAPI dep wrapping it is async
to satisfy the framework's expectations.
"""
from __future__ import annotations

from saebooks.services.licence import LicenseService
from saebooks.services.lodgement.base import LodgementService
from saebooks.services.lodgement.null import NullLodgementService
from saebooks.services.lodgement.remote import RemoteLodgementService

_FEATURE_FLAG = "ato_sbr"


def get_lodgement_service() -> LodgementService:
    """Return the right ``LodgementService`` for the current licence.

    Pro / Enterprise → ``RemoteLodgementService``.
    Anything else → ``NullLodgementService``.
    """
    if LicenseService.has_feature(_FEATURE_FLAG):
        return RemoteLodgementService()
    return NullLodgementService()
