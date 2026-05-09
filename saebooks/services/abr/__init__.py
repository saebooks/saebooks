"""ABR — Australian Business Register lookup service (v1.1).

Enterprise-only feature (gated by ``FLAG_ABR_LOOKUP``). Wraps the
public ``abr.business.gov.au`` JSON API so the app can enrich a
Contact record from an ABN without the user re-typing everything.

Public surface::

    from saebooks.services.abr import AbrLookup, lookup_abn, AbrNotConfiguredError

    result = await lookup_abn("87 744 586 592", settings=settings)
    # result.business_name, result.state, result.gst_registered, ...

All network I/O lives in ``client.py``; parsing/enrichment mapping
lives in ``enrich.py``. Tests use respx to mock the ABR endpoint.
"""
from saebooks.services.abr.client import (
    AbrError,
    AbrNotConfiguredError,
    lookup_abn_raw,
)
from saebooks.services.abr.enrich import (
    AbrLookup,
    apply_to_contact,
    lookup_abn,
    parse_abr_response,
)

__all__ = [
    "AbrError",
    "AbrLookup",
    "AbrNotConfiguredError",
    "apply_to_contact",
    "lookup_abn",
    "lookup_abn_raw",
    "parse_abr_response",
]
