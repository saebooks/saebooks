"""API v1 — pure JSON routers for the self-host API-first rebuild.

Phase 0 scope: ``/api/v1/contacts``, ``/api/v1/changes``,
``/api/v1/snapshot``.
Phase 1 tier-1: ``/api/v1/accounts``, ``/api/v1/companies``,
``/api/v1/tax_codes``.
Phase 1 tier-2: ``/api/v1/users``, ``/api/v1/permissions``.
Phase 1 will extend to the full entity set (invoices, bills,
journal, bank feeds, …).
"""
from fastapi import APIRouter

from saebooks.api.v1.accounts import router as accounts_router
from saebooks.api.v1.changes import router as changes_router
from saebooks.api.v1.companies import router as companies_router
from saebooks.api.v1.contacts import router as contacts_router
from saebooks.api.v1.snapshot import router as snapshot_router
from saebooks.api.v1.tax_codes import router as tax_codes_router
from saebooks.api.v1.users import permissions_router, router as users_router

# One umbrella router — main.py mounts this at /api/v1.
router = APIRouter(prefix="/api/v1")
router.include_router(contacts_router)
router.include_router(accounts_router)
router.include_router(companies_router)
router.include_router(tax_codes_router)
router.include_router(users_router)
router.include_router(permissions_router)
router.include_router(changes_router)
router.include_router(snapshot_router)

__all__ = ["router"]
