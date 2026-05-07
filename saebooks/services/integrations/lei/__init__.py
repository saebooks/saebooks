"""LEI / GLEIF enrichment — Enterprise-gated, same shape as ABR.

Public surface re-exports the two-step flow: ``lookup_lei(lei)`` returns
a parsed :class:`LeiLookup`; ``apply_to_contact`` merges it into a
Contact record.

Gate: ``FLAG_LEI_LOOKUP`` in ``saebooks.services.features``. Router
uses ``Depends(require_feature(FLAG_LEI_LOOKUP))`` so Community builds
404 on the routes even when the module is importable.
"""
from saebooks.services.integrations.lei.client import (
    LeiError,
    LeiNotFoundError,
    lookup_lei_raw,
)
from saebooks.services.integrations.lei.enrich import (
    LeiLookup,
    apply_to_contact,
    lookup_lei,
    parse_lei_response,
)

__all__ = [
    "LeiError",
    "LeiLookup",
    "LeiNotFoundError",
    "apply_to_contact",
    "lookup_lei",
    "lookup_lei_raw",
    "parse_lei_response",
]
