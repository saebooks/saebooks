"""Xero adapter for the Build #9 sync feature.

Layered:

* ``client``   — async httpx wrapper. Token refresh + 429 retry + 401 re-auth.
* ``endpoints``— Xero v2 wrappers for Contacts / Invoices / Manual Journals.
* ``mappers``  — SAE Books <-> Xero shape conversions.
* ``pull``     — incremental pull via ``If-Modified-Since``.
* ``push``     — push our changes upward, conflict via LWW + audit.
* ``connector``— top-level ``sync_xero(tenant, connection)`` orchestrator.

Public surface for routers + worker is ``connector.sync_xero``. The
intermediate layers are exposed for tests but should not be called
directly from routers.
"""
from saebooks.services.sync.xero.client import XeroClient
from saebooks.services.sync.xero.token import XeroTokenCache

__all__ = ["XeroClient", "XeroTokenCache"]
